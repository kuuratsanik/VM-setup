"""Models agent: finds newer cloud models than the configured ones, gates them with agents/evalgate.py, and proposes a routing PR."""
import json
import os
import re
import urllib.request
from pathlib import Path

import config  # noqa: F401
import evalgate
import pr
import yaml
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
OPENAI_SMALL = re.compile(r"^gpt-\d+(\.\d+)?-mini$")
OPENAI_FRONTIER = re.compile(r"^gpt-\d+(\.\d+)?$")


def fetch(url, headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return json.load(r)["data"]


def newest(models, key, pattern):
    ids = [m for m in models if pattern.search(m["id"])]
    return max(ids, key=lambda m: m[key])["id"] if ids else None


def discover():
    """Newest small/frontier model per provider with an API key set. Naming heuristics: review the PR before merging."""
    found = {}
    if os.environ.get("OPENAI_API_KEY"):
        models = fetch("https://api.openai.com/v1/models", {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
        found["cloud_small"] = ("openai", newest(models, "created", OPENAI_SMALL))
    if os.environ.get("ANTHROPIC_API_KEY"):
        models = fetch("https://api.anthropic.com/v1/models?limit=100", {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
        found["cloud_frontier"] = ("anthropic", newest(models, "created_at", re.compile(r"sonnet")))
    return {k: f"{p}/{m}" for k, (p, m) in found.items() if m}


def main():
    current = json.loads((ROOT / "profile.generated.json").read_text()).get("llm_routing", {})
    defaults = {"cloud_small": "openai/gpt-4o-mini", "cloud_frontier": "anthropic/claude-sonnet-4-5"}
    client = OpenAI(base_url=os.environ.get("LITELLM_URL", "http://127.0.0.1:4000"), api_key=os.environ.get("LITELLM_KEY", "none"))
    cases = evalgate.load_cases()
    accepted, notes = {}, []
    for slot, candidate in discover().items():
        in_use = current.get(slot, defaults[slot])
        if candidate == in_use:
            continue
        # Wildcard routes (openai/*, anthropic/*) let the gateway serve any candidate id for evaluation.
        result = evalgate.compare(client, "cloud-small" if slot == "cloud_small" else "cloud-frontier", candidate, cases)
        notes.append(f"- `{slot}`: `{in_use}` -> `{candidate}`: {'passed' if result['pass'] else 'FAILED'} gate, {json.dumps(result)}")
        if result["pass"]:
            accepted[slot] = candidate
    if not accepted:
        print("models: no newer model passed the gate" if notes else "models: routing is current")
        print("\n".join(notes))
        return

    def edit(repo):
        path = repo / "profile.override.yaml"
        doc = yaml.safe_load(path.read_text()) or {}
        doc.setdefault("llm_routing", {}).update(accepted)
        return {"profile.override.yaml": yaml.safe_dump(doc, sort_keys=False)}

    url = pr.open_pr("agent/models-update", edit, "Update cloud model routing", "Models that passed the Operator eval gate:\n" + "\n".join(notes))
    print(f"models: {url or 'proposal already up to date'}")


if __name__ == "__main__":
    main()
