"""Jarvis web dashboard: login, status, chat with tools, voice and image, provider setup, and GPU compute."""
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))

from jarvis import auth, providers, status  # noqa: E402
import approvals  # noqa: E402
from policy import Policy  # noqa: E402
from jarvis.chat import Chat, Hub, contains_secret  # noqa: E402
from jarvis.compute.runpod import GPUS  # noqa: E402
from jarvis.compute.service import Compute, NotConfigured  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
MODELS = ["default", "cloud-small", "cloud-frontier", "local"]
GATEWAY = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000")
CSP = "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    code: str = Field(default="", max_length=8)


class ChatIn(BaseModel):
    messages: list[dict] = Field(max_length=40)
    model: str = "default"
    tools: bool = True


class KeyIn(BaseModel):
    values: dict[str, str]


class PodIn(BaseModel):
    name: str
    gpu_type: str
    hours: float = 2.0


class KaggleIn(BaseModel):
    slug: str
    code: str = Field(max_length=200_000)
    gpu: bool = True


def create_app(compute=None, hub=None, start_tools=True):
    compute = compute or Compute()
    hub = hub or Hub(compute)

    @asynccontextmanager
    async def lifespan(app):
        if start_tools:
            try:
                await hub.start()
            except Exception as exc:  # the dashboard still works without MCP tools
                print(f"jarvis: tools unavailable: {exc}", file=sys.stderr)
        yield
        await hub.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    secret = os.environ.get("JARVIS_SESSION_SECRET")
    if not secret:
        raise RuntimeError("JARVIS_SESSION_SECRET is not set")
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="jarvis", max_age=8 * 3600, same_site="strict", https_only=os.environ.get("JARVIS_HTTPS") == "1")
    chat = Chat(hub)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if request.headers.get("x-requested-with") != "jarvis" or (origin and origin.split("://", 1)[-1] != request.headers.get("host")):
                return JSONResponse({"detail": "blocked"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def owner(request: Request):
        if not request.session.get("user"):
            raise HTTPException(401, "login required")
        return request.session["user"]

    @app.get("/api/auth/state")
    async def auth_state(request: Request):
        return {"configured": auth.configured(), "logged_in": bool(request.session.get("user")), "totp": auth.totp_enabled()}

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        if not auth.configured():
            raise HTTPException(503, "no account yet: run `python -m jarvis.manage set-password` on the host")
        ip = request.client.host if request.client else "?"
        if auth.locked(ip):
            raise HTTPException(429, "too many attempts; try again later")
        if not auth.verify(ip, body.username, body.password, body.code):
            raise HTTPException(401, "invalid credentials")
        request.session.clear()
        request.session["user"] = body.username
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.post("/api/totp/enroll")
    async def totp_enroll(user: str = Depends(owner)):
        secret_, uri = auth.enroll_totp()
        return {"secret": secret_, "uri": uri}

    @app.post("/api/totp/confirm")
    async def totp_confirm(body: dict, user: str = Depends(owner)):
        if not auth.confirm_totp(str(body.get("code", ""))):
            raise HTTPException(400, "wrong code")
        return {"ok": True}

    @app.get("/api/status")
    async def get_status(user: str = Depends(owner)):
        return await status.collect()

    @app.get("/api/incidents")
    async def incidents(user: str = Depends(owner)):
        return status.recent_incidents()

    @app.get("/api/autonomy")
    async def autonomy(user: str = Depends(owner)):
        policy = Policy()
        return {**policy.snapshot(), "approvals": approvals.pending(), "audit_tail": list(reversed(policy.audit_tail(25)))}

    @app.post("/api/autonomy/breaker/reset")
    async def breaker_reset(user: str = Depends(owner)):
        Policy().reset_breaker(user)
        return {"ok": True}

    @app.get("/api/models")
    async def models(user: str = Depends(owner)):
        return MODELS

    @app.post("/api/chat")
    async def chat_stream(body: ChatIn, user: str = Depends(owner)):
        last = next((m for m in reversed(body.messages) if m.get("role") == "user"), None)
        if last is None or body.model not in MODELS:
            raise HTTPException(400, "bad request")
        if any(contains_secret(str(m.get("content", ""))) for m in body.messages):
            raise HTTPException(400, "that looks like a key or token; enter it in the Setup tab, not in chat")

        async def events():
            import json
            try:
                async for event in chat.run(body.messages, body.model, body.tools):
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'text': str(exc)[:200]})}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/api/actions/{pid}/confirm")
    async def confirm(pid: str, user: str = Depends(owner)):
        try:
            return {"result": await hub.confirm(pid)}
        except KeyError:
            raise HTTPException(404, "unknown or expired action")
        except Exception as exc:  # NotConfigured, SpendGuard, provider errors: tell the owner why
            raise HTTPException(400, str(exc)[:300])

    @app.post("/api/actions/{pid}/discard")
    async def discard(pid: str, user: str = Depends(owner)):
        return {"discarded": hub.discard(pid)}

    async def gateway(path, **kwargs):
        async with httpx.AsyncClient(base_url=GATEWAY, headers={"Authorization": f"Bearer {os.environ.get('LITELLM_KEY', 'none')}"}, timeout=120) as client:
            resp = await client.post(path, **kwargs)
        if resp.status_code >= 400:
            raise HTTPException(502, "model gateway error")
        return resp

    @app.post("/api/stt")
    async def stt(file: UploadFile = File(...), user: str = Depends(owner)):
        data = await file.read()
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(413, "audio too large")
        resp = await gateway("/v1/audio/transcriptions", files={"file": (file.filename or "audio.webm", data)}, data={"model": "stt"})
        return {"text": resp.json().get("text", "")}

    @app.post("/api/tts")
    async def tts(body: dict, user: str = Depends(owner)):
        resp = await gateway("/v1/audio/speech", json={"model": "tts", "voice": "alloy", "input": str(body.get("text", ""))[:4000]})
        return Response(resp.content, media_type="audio/mpeg")

    @app.post("/api/image")
    async def image(body: dict, user: str = Depends(owner)):
        resp = await gateway("/v1/images/generations", json={"model": "image", "prompt": str(body.get("prompt", ""))[:2000], "size": "1024x1024"})
        item = resp.json()["data"][0]
        return {"b64": item.get("b64_json")}

    @app.get("/api/providers")
    async def provider_list(user: str = Depends(owner)):
        present = providers.configured_providers()
        return [{"id": pid, "configured": present[pid], **{k: v for k, v in spec.items()}} for pid, spec in providers.PROVIDERS.items()]

    @app.post("/api/providers/{pid}/key")
    async def provider_key(pid: str, body: KeyIn, user: str = Depends(owner)):
        if pid not in providers.PROVIDERS:
            raise HTTPException(404, "unknown provider")
        try:
            values = providers.clean(pid, body.values)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not await providers.validate(pid, values):
            raise HTTPException(400, "the provider rejected that key")
        providers.stage(values)
        return {"staged": True, "note": "Applied by the root helper within a minute; services restart."}

    @app.get("/api/compute/gpus")
    async def gpus(user: str = Depends(owner)):
        return GPUS

    @app.get("/api/compute/runpod/pods")
    async def pods(user: str = Depends(owner)):
        try:
            return await compute.runpod().list_pods()
        except NotConfigured as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/compute/runpod/pods")
    async def pod_create(body: PodIn, user: str = Depends(owner)):
        pid = hub.queue("runpod_create_pod", body.model_dump())
        return {"pending": pid}

    @app.post("/api/compute/runpod/pods/{pod_id}/terminate")
    async def pod_terminate(pod_id: str, user: str = Depends(owner)):
        try:
            await compute.runpod().terminate(pod_id)
        except NotConfigured as exc:
            raise HTTPException(400, str(exc))
        return {"terminated": pod_id}

    @app.post("/api/compute/kaggle/run")
    async def kaggle_run(body: KaggleIn, user: str = Depends(owner)):
        return {"pending": hub.queue("kaggle_run_script", body.model_dump())}

    @app.get("/api/compute/kaggle/status/{user_}/{slug}")
    async def kaggle_status(user_: str, slug: str, user: str = Depends(owner)):
        try:
            return await compute.kaggle().status(f"{user_}/{slug}")
        except (NotConfigured, ValueError) as exc:
            raise HTTPException(400, str(exc))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    return app


def main():
    import uvicorn

    uvicorn.run(create_app(), host=os.environ.get("JARVIS_HOST", "127.0.0.1"), port=int(os.environ.get("JARVIS_PORT", "8088")), log_level="info")


if __name__ == "__main__":
    main()
