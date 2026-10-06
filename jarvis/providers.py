"""Provider registry: sign-up and key pages for the human, key validation, and staging for the root secrets applier.

Accounts are created by the human. Jarvis never automates registration, CAPTCHA, email or phone verification; it opens the
right pages, explains each step, checks the pasted token with a harmless read-only call, and stages it.
"""
import asyncio
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import httpx

STATE_DIR = Path(os.environ.get("JARVIS_STATE_DIR", "/var/lib/vmsetup/jarvis"))
VALUE_RE = re.compile(r"^[A-Za-z0-9_\-.:/+=]{8,512}$")
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

PROVIDERS = {
    "openai": {
        "label": "OpenAI", "kind": "cloud LLM, images, speech", "cost": "paid",
        "signup": "https://platform.openai.com/signup", "keys": "https://platform.openai.com/api-keys",
        "fields": {"OPENAI_API_KEY": "secret"},
        "steps": ["Create an account and add a small prepaid balance", "Create an API key", "Paste it in the Setup tab"],
    },
    "anthropic": {
        "label": "Anthropic", "kind": "cloud LLM", "cost": "paid",
        "signup": "https://console.anthropic.com/", "keys": "https://console.anthropic.com/settings/keys",
        "fields": {"ANTHROPIC_API_KEY": "secret"},
        "steps": ["Create a Console account and add credit", "Create an API key", "Paste it in the Setup tab"],
    },
    "openrouter": {
        "label": "OpenRouter", "kind": "many cloud models incl. Hermes, some free", "cost": "free tier and paid",
        "signup": "https://openrouter.ai/", "keys": "https://openrouter.ai/keys",
        "fields": {"OPENROUTER_API_KEY": "secret"},
        "steps": ["Sign in", "Create a key (set a credit limit)", "Paste it in the Setup tab"],
    },
    "huggingface": {
        "label": "Hugging Face", "kind": "model downloads (vLLM, LocalAI)", "cost": "free",
        "signup": "https://huggingface.co/join", "keys": "https://huggingface.co/settings/tokens",
        "fields": {"HF_TOKEN": "secret"},
        "steps": ["Create an account and verify your email", "Create a read token", "Paste it in the Setup tab"],
    },
    "github": {
        "label": "GitHub", "kind": "PRs from agents", "cost": "free",
        "signup": "https://github.com/signup", "keys": "https://github.com/settings/personal-access-tokens/new",
        "fields": {"GH_TOKEN": "secret"},
        "steps": ["Create a fine-grained token for this repository only", "Grant contents and pull requests read/write", "Paste it in the Setup tab"],
    },
    "runpod": {
        "label": "RunPod", "kind": "rented GPUs for training and inference", "cost": "paid (per hour)",
        "signup": "https://www.runpod.io/", "keys": "https://www.console.runpod.io/user/settings",
        "fields": {"RUNPOD_API_KEY": "secret"},
        "steps": ["Create an account and add credit", "Create an API key with the narrowest scope that lists and creates pods", "Paste it in the Setup tab"],
    },
    "kaggle": {
        "label": "Kaggle", "kind": "free notebook GPUs (weekly quota)", "cost": "free",
        "signup": "https://www.kaggle.com/account/login?phase=startRegisterTab", "keys": "https://www.kaggle.com/settings/api",
        "fields": {"KAGGLE_API_TOKEN": "secret", "KAGGLE_USERNAME": "text"},
        "steps": ["Register and verify your phone number on Kaggle (required for GPUs)", "Click Generate New Token under API", "Paste the token and your username in the Setup tab"],
    },
}


async def _get(url, headers, transport=None):
    async with httpx.AsyncClient(timeout=15, transport=transport) as client:
        return await client.get(url, headers=headers)


async def _check(provider, values, transport=None):
    key = values[next(iter(PROVIDERS[provider]["fields"]))]
    bearer = {"Authorization": f"Bearer {key}"}
    urls = {
        "openai": ("https://api.openai.com/v1/models", bearer),
        "anthropic": ("https://api.anthropic.com/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"}),
        "openrouter": ("https://openrouter.ai/api/v1/auth/key", bearer),
        "huggingface": ("https://huggingface.co/api/whoami-v2", bearer),
        "github": ("https://api.github.com/user", {**bearer, "Accept": "application/vnd.github+json"}),
        "runpod": ("https://rest.runpod.io/v1/pods", bearer),
    }
    url, headers = urls[provider]
    return (await _get(url, headers, transport)).status_code == 200


def _check_kaggle(values, runner=subprocess.run):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": tempfile.gettempdir(), "KAGGLE_API_TOKEN": values["KAGGLE_API_TOKEN"], "KAGGLE_USERNAME": values["KAGGLE_USERNAME"]}
    out = runner(["kaggle", "datasets", "list", "--page-size", "1"], env=env, capture_output=True, text=True, timeout=60)
    return out.returncode == 0


def clean(provider, values):
    """Only the provider's own fields, each matching a strict pattern; raises ValueError otherwise."""
    spec = PROVIDERS[provider]["fields"]
    cleaned = {}
    for name, kind in spec.items():
        value = (values.get(name) or "").strip()
        if not (USER_RE if kind == "text" else VALUE_RE).match(value):
            raise ValueError(f"{name} has an invalid format")
        cleaned[name] = value
    return cleaned


async def validate(provider, values, transport=None, runner=subprocess.run):
    values = clean(provider, values)
    if provider == "kaggle":
        return await asyncio.to_thread(_check_kaggle, values, runner)
    return await _check(provider, values, transport)


CONFIGURED_FILE = Path(os.environ.get("JARVIS_CONFIGURED_FILE", "/etc/vmsetup/jarvis-configured.json"))
PROVIDERS_FIELDS = {name for spec in PROVIDERS.values() for name in spec["fields"]}


def stage(values):
    """Hand validated values to the root applier (jarvis/apply_secrets.py) through a 0600 file the path unit watches."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / "staged-secrets.json"
    existing = json.loads(path.read_text()) if path.exists() else {}
    existing.update(values)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing))
    tmp.chmod(0o600)
    tmp.replace(path)


def configured_providers():
    """Which providers already have a value in the environment (names only, never values)."""
    names = {name for name in PROVIDERS_FIELDS if os.environ.get(name)}
    try:  # the root applier lists (in a root-owned file) the variable names it wrote (never values); Jarvis itself only gets RunPod/Kaggle in its env
        marked = json.loads(CONFIGURED_FILE.read_text())
        names |= {n for n in marked if isinstance(n, str)}
    except (OSError, ValueError, TypeError):
        pass
    return {pid: all(name in names for name in spec["fields"]) for pid, spec in PROVIDERS.items()}
