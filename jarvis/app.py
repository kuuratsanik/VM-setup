"""Jarvis web dashboard: login, status, chat with tools, voice and image, provider setup, and GPU compute."""
import asyncio
import os
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))

from jarvis import auth, providers, status  # noqa: E402
import approvals  # noqa: E402
from policy import Policy  # noqa: E402
from jarvis.chat import ActionInProgress, Chat, Hub, history_has_secret  # noqa: E402
from jarvis.compute.models import KaggleIn, PodIn  # noqa: E402
from jarvis.compute.runpod import GPUS, POD_ID_RE  # noqa: E402
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


STT_LIMIT = 25 * 1024 * 1024


def normalize_origin(value):
    """scheme://host[:port] with lowercase scheme and host, default ports and trailing slash dropped; '' if empty."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return value.lower()
    if not parts.scheme or not parts.hostname:
        return value.lower()
    if port == (443 if parts.scheme.lower() == "https" else 80):
        port = None
    host = parts.hostname.lower()
    try:  # a Unicode host matches the browser's punycode Origin
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme.lower()}://{host}" + (f":{port}" if port else "")


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
    public_origin = normalize_origin(os.environ.get("JARVIS_PUBLIC_ORIGIN", ""))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if public_origin:  # behind a TLS proxy the Host header is the upstream's, so pin the exact public origin instead
                bad_origin = bool(origin) and normalize_origin(origin) != public_origin
            else:
                bad_origin = bool(origin) and origin.split("://", 1)[-1] != request.headers.get("host")
            if request.headers.get("x-requested-with") != "jarvis" or bad_origin:
                return JSONResponse({"detail": "blocked"}, status_code=403)
        if request.method == "POST" and request.url.path == "/api/stt":
            # Refuse before routing: the multipart body is spooled to disk before any handler or auth dependency runs.
            declared = request.headers.get("content-length", "")
            if "transfer-encoding" in request.headers or not declared.isdigit():
                return JSONResponse({"detail": "Content-Length required"}, status_code=411)
            if int(declared) > STT_LIMIT + 64 * 1024:  # multipart framing adds a little to the file itself
                return JSONResponse({"detail": "audio too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def session_user(request: Request):
        """The logged-in user, only if the cookie carries the current server-side session generation."""
        user, gen = request.session.get("user"), request.session.get("gen")
        if not user or gen != auth.session_gen():
            return None
        return user

    def owner(request: Request):
        user = session_user(request)
        if not user:
            raise HTTPException(401, "login required")
        return user

    @app.get("/api/auth/state")
    async def auth_state(request: Request):
        return {"configured": auth.configured(), "logged_in": bool(session_user(request)), "totp": auth.totp_enabled()}

    # One per app; state is guarded by auth._lock (single-process service). Saturation answers 429 for everyone,
    # including the owner: a token bucket or per-client slot would turn the owner away just the same, and this is
    # already strictly better than hashing inline on the event loop.
    login_slots = asyncio.Semaphore(4)

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        if not auth.configured():
            raise HTTPException(503, "no account yet: run `python -m jarvis.manage set-password` on the host")
        ip = auth.client_ip(request)
        wait = auth.retry_after(ip)
        if wait and not auth.totp_enabled():
            raise HTTPException(429, "too many attempts; try again later", headers={"Retry-After": str(wait)})
        if login_slots.locked():  # all hashing slots busy: refuse instantly instead of queueing work for an attacker
            raise HTTPException(429, "too many attempts; try again later", headers={"Retry-After": "1"})
        async with login_slots:  # argon2 is ~100 ms of CPU: keep it off the event loop
            ok = await run_in_threadpool(auth.verify, ip, body.username, body.password, body.code)
        if not ok:
            if wait:  # locked + TOTP: a failed bypass is always 429, whether password or code was wrong
                raise HTTPException(429, "too many attempts; try again later", headers={"Retry-After": str(wait)})
            await asyncio.sleep(auth.tarpit_delay())  # global failure tarpit; correct logins are never delayed
            raise HTTPException(401, "invalid credentials")
        request.session.clear()
        request.session["user"] = body.username
        request.session["gen"] = auth.session_gen()
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request):
        if session_user(request):  # only a valid session may revoke; this also kills any stolen copy of the cookie
            auth.bump_session_gen()
        request.session.clear()
        return {"ok": True}

    # TOTP enrollment is host-only (`python -m jarvis.manage enroll-totp`); there is deliberately no HTTP endpoint for it.

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
        if history_has_secret(body.messages):
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
        except ActionInProgress:
            raise HTTPException(409, "that action is already being confirmed")
        except httpx.HTTPStatusError as exc:  # the provider refused: relay only its status, never its body or headers
            raise HTTPException(502, f"provider returned HTTP {exc.response.status_code}")
        except httpx.HTTPError:
            raise HTTPException(502, "could not reach the provider")
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
        limit = STT_LIMIT  # the middleware already bounded Content-Length; this still bounds the in-memory copy
        data = b""
        while chunk := await file.read(1024 * 1024):
            data += chunk
            if len(data) > limit:
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
        try:
            valid = await providers.validate(pid, values)
        except (httpx.HTTPError, subprocess.TimeoutExpired, FileNotFoundError):
            raise HTTPException(502, "could not reach the provider to check that key; try again later")
        if not valid:
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
        if not POD_ID_RE.match(pod_id):
            raise HTTPException(422, "invalid pod id")
        args = {"pod_id": pod_id}
        try:
            name = next((p["name"] for p in await compute.runpod().list_pods() if p["id"] == pod_id), None)
        except Exception:  # the name is only a courtesy for the confirm card
            name = None
        if name:
            args["name"] = re.sub(r"[^\x20-\x7e]", "", str(name))[:80]  # printable ASCII only: no bidi/control chars on the confirm card
        return {"pending": hub.queue("runpod_terminate_pod", args)}

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
