"""Read-only status for the dashboard: service health, nodes, recent incidents, token spend."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

INCIDENTS = Path(os.environ.get("VMSETUP_INCIDENTS", "/var/lib/vmsetup/incidents.jsonl"))
PROFILE = Path(os.environ.get("VMSETUP_PROFILE", ROOT / "profile.generated.json"))
PRICES = {"local": (0, 0), "default": (0, 0), "cloud-small": (0.15, 0.6), "cloud-frontier": (3, 15)}  # USD per 1M tokens, rough
PROM = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")


def profile():
    try:
        return json.loads(PROFILE.read_text())
    except (OSError, ValueError):
        return {}


def service_list(p):
    ai = p.get("ai") or {}
    services = {
        "LiteLLM gateway": "http://127.0.0.1:4000/health/liveliness",
        "Prometheus": f"{PROM}/-/healthy",
        "Alertmanager": "http://127.0.0.1:9093/-/healthy",
        "Netdata": "http://127.0.0.1:19999/api/v1/info",
    }
    if (p.get("local_llm") or {}).get("enabled"):
        engine = (p["local_llm"].get("engine") or "ollama")
        services["vLLM" if engine == "vllm" else "Ollama"] = "http://127.0.0.1:8000/health" if engine == "vllm" else "http://127.0.0.1:11434/api/tags"
    if ai.get("phoenix"):
        services["Phoenix"] = "http://127.0.0.1:6006/healthz"
    if (ai.get("media") or {}).get("local"):
        services["LocalAI"] = "http://127.0.0.1:8080/readyz"
    return services


async def probe(client, name, url):
    try:
        resp = await client.get(url)
        return {"name": name, "ok": resp.status_code < 400}
    except httpx.HTTPError:
        return {"name": name, "ok": False}


async def prom_value(client, query):
    try:
        data = (await client.get(f"{PROM}/api/v1/query", params={"query": query})).json()["data"]["result"]
        return float(data[0]["value"][1]) if data else None
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None


def _rows():
    """Parsed incident rows; truncated, invalid or non-object lines are skipped."""
    try:
        lines = INCIDENTS.read_text(errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def recent_incidents(limit=25):
    rows = _rows()[-limit:]
    return [{"ts": r.get("ts"), "alert": r.get("alert") or "unknown", "outcome": r.get("outcome") or "unknown", "detail": str(r.get("detail") or "")[:300], "model": r.get("model")} for r in reversed(rows)]


def spend(days=30):
    total, tokens = 0.0, 0
    since = time.time() - days * 86400
    for row in _rows():
        usage = row.get("usage")
        ts = row.get("ts")
        if not isinstance(usage, dict) or not isinstance(ts, (int, float)) or ts < since:
            continue
        p_in, p_out = PRICES.get(row.get("model") or "default", PRICES["cloud-frontier"])
        prompt, completion = usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0
        if not isinstance(prompt, (int, float)) or not isinstance(completion, (int, float)):
            continue
        total += (prompt * p_in + completion * p_out) / 1e6
        tokens += prompt + completion
    budget = (profile().get("llm_routing") or {}).get("cloud_budget_usd_month")
    return {"estimated_usd_30d": round(total, 4), "tokens_30d": tokens, "budget_usd_month": budget}


async def collect():
    p = profile()
    async with httpx.AsyncClient(timeout=2.5) as client:
        checks = await asyncio.gather(*(probe(client, n, u) for n, u in service_list(p).items()))
        steal, mem = await asyncio.gather(
            prom_value(client, 'avg(rate(node_cpu_seconds_total{mode="steal"}[5m]))'),
            prom_value(client, "node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes"),
        )
    nodes = [{"name": k, **{f: v[f] for f in ("cluster", "role", "vcpu", "ram_gb", "ip")}} for k, v in (p.get("nodes") or {}).items()]
    return {
        "profile": p.get("profile"), "services": checks, "nodes": nodes, "spend": spend(),
        "host": {"cpu_steal": steal, "mem_available_ratio": mem},
        "paused": Path("/etc/vmsetup/AGENTS_PAUSED").exists(),
    }
