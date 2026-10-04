"""Media MCP server: image, speech, transcription, vision and video through the LiteLLM gateway, local or cloud.

Files are only read from and written to MEDIA_DIR. provider is 'auto' (local if the gateway has a local model, else cloud),
'local' or 'cloud'. Video needs a gateway/provider that implements the /v1/videos job API; that path is untested
against a real provider.
"""
import base64
import os
import re
import time
import urllib.request
import uuid
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from openai import OpenAI

BASE = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000")
KEY = os.environ.get("LITELLM_KEY", "none")
MEDIA_DIR = Path(os.environ.get("VMSETUP_MEDIA_DIR", "/var/lib/vmsetup/media")).resolve()
MAX_BYTES = 50 * 1024 * 1024
PROVIDERS = {"auto", "local", "cloud"}
SIZE_RE = re.compile(r"^\d{3,4}x\d{3,4}$")
VOICE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

mcp = FastMCP("media")


def _client():
    return OpenAI(base_url=BASE, api_key=KEY)


def _model(kind, provider):
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {sorted(PROVIDERS)}")
    return os.environ.get(f"MEDIA_{kind.upper()}_MODEL", kind) + ("" if provider == "auto" else f"-{provider}")


def _inside(path):
    resolved = (MEDIA_DIR / path).resolve()
    if MEDIA_DIR not in resolved.parents:
        raise ValueError("path must be inside the media directory")
    if not resolved.is_file() or resolved.stat().st_size > MAX_BYTES:
        raise ValueError("file missing or too large")
    return resolved


def _save(data, suffix):
    if len(data) > MAX_BYTES:
        raise ValueError("generated file too large")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    path = MEDIA_DIR / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(data)
    return f"{path.name} ({len(data)} bytes) in {MEDIA_DIR}"


@mcp.tool()
def generate_image(prompt: str, size: str = "1024x1024", provider: str = "auto") -> str:
    """Generate an image and save it to the media directory."""
    if not SIZE_RE.match(size):
        raise ValueError("size must look like 1024x1024")
    result = _client().images.generate(model=_model("image", provider), prompt=prompt, size=size).data[0]
    if result.b64_json:
        return _save(base64.b64decode(result.b64_json), ".png")
    # Local backends may return a URL; only fetch it from the gateway's own host.
    if result.url and result.url.startswith(("http://127.0.0.1", "http://localhost", BASE)):
        with urllib.request.urlopen(result.url, timeout=60) as r:
            return _save(r.read(MAX_BYTES + 1), ".png")
    raise RuntimeError("image backend returned no usable data")


@mcp.tool()
def synthesize_speech(text: str, voice: str = "alloy", provider: str = "auto") -> str:
    """Convert text to speech (MP3) and save it to the media directory."""
    if not VOICE_RE.match(voice):
        raise ValueError("invalid voice name")
    audio = _client().audio.speech.create(model=_model("tts", provider), voice=voice, input=text)
    return _save(audio.content, ".mp3")


@mcp.tool()
def transcribe_audio(path: str, provider: str = "auto") -> str:
    """Transcribe an audio file from the media directory to text."""
    with _inside(path).open("rb") as fh:
        return _client().audio.transcriptions.create(model=_model("stt", provider), file=fh).text


@mcp.tool()
def describe_image(path: str, question: str = "Describe this image.") -> str:
    """Answer a question about an image from the media directory using the gateway's vision-capable default model."""
    file = _inside(path)
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(file.suffix.lower())
    if not mime:
        raise ValueError("unsupported image type")
    data_url = f"data:{mime};base64,{base64.b64encode(file.read_bytes()).decode()}"
    reply = _client().chat.completions.create(
        model=os.environ.get("MEDIA_VISION_MODEL", "default"),
        messages=[{"role": "user", "content": [{"type": "text", "text": question}, {"type": "image_url", "image_url": {"url": data_url}}]}],
    )
    return reply.choices[0].message.content


@mcp.tool()
def generate_video(prompt: str, seconds: int = 4, provider: str = "auto", timeout_s: int = 600) -> str:
    """Generate a video through the gateway's /v1/videos job API and save the MP4."""
    headers = {"Authorization": f"Bearer {KEY}"}
    with httpx.Client(base_url=f"{BASE}/v1", headers=headers, timeout=60) as http:
        job = http.post("/videos", json={"model": _model("video", provider), "prompt": prompt, "seconds": str(int(seconds))})
        job.raise_for_status()
        job_id = job.json()["id"]
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            state = http.get(f"/videos/{job_id}").json()
            if state.get("status") == "completed":
                content = http.get(f"/videos/{job_id}/content")
                content.raise_for_status()
                return _save(content.content, ".mp4")
            if state.get("status") in ("failed", "cancelled"):
                raise RuntimeError(f"video job {state.get('status')}")
            time.sleep(5)
    raise TimeoutError("video job did not finish in time")


if __name__ == "__main__":
    mcp.run()
