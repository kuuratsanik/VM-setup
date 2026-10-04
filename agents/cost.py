"""Cost agent: sums LLM token usage from incident records and reports against the profile budget."""
import json
import os
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS = Path(os.environ.get("VMSETUP_INCIDENTS", "/var/lib/vmsetup/incidents.jsonl"))
# Rough USD per 1M tokens (prompt, completion); adjust to your provider's pricing.
PRICES = {"local": (0, 0), "cloud-small": (0.15, 0.6), "cloud-frontier": (3, 15)}


def main():
    profile = json.loads((ROOT / "profile.generated.json").read_text())
    budget = profile["llm_routing"]["cloud_budget_usd_month"]
    since = time.time() - 30 * 86400
    spend, tokens = defaultdict(float), defaultdict(int)
    if INCIDENTS.exists():
        for line in INCIDENTS.read_text().splitlines():
            row = json.loads(line)
            if row["ts"] < since or not row.get("usage"):
                continue
            model = row.get("model") or "local"
            p_in, p_out = PRICES.get(model, PRICES["cloud-frontier"])
            u = row["usage"]
            spend[model] += (u["prompt_tokens"] * p_in + u["completion_tokens"] * p_out) / 1e6
            tokens[model] += u["prompt_tokens"] + u["completion_tokens"]
    total = sum(spend.values())
    print(json.dumps({"window_days": 30, "budget_usd": budget, "estimated_usd": round(total, 4), "tokens": tokens}, indent=2))
    if total > 0.8 * budget:
        print("cost: over 80% of the budget; route more tasks to local/cloud-small")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
