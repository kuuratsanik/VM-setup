"""Daily digest: what the agents did, what is waiting for a human, and any trust or safety signals. One short message."""
import argparse
import json
import os
import time
from pathlib import Path

import approvals
import notify
from policy import Policy

INCIDENTS = Path(os.environ.get("VMSETUP_INCIDENTS", "/var/lib/vmsetup/incidents.jsonl"))


def build(policy, now=None, incidents=INCIDENTS):
    now = now or time.time()
    since = now - 86400
    rows = [json.loads(line) for line in incidents.read_text().splitlines() if line] if incidents.exists() else []
    rows = [r for r in rows if r["ts"] >= since]
    by_outcome = {}
    for r in rows:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
    audit = [e for e in policy.audit_tail(10_000) if e["ts"] >= since]
    decisions = {}
    for e in audit:
        if e["kind"] == "decision":
            decisions[e["level"]] = decisions.get(e["level"], 0) + 1
    failed = sum(1 for e in audit if e["kind"] == "outcome" and not e["ok"])
    snap = policy.snapshot()
    waiting = approvals.pending()
    lines = [f"Mode: {snap['mode']}. Incidents (24 h): {len(rows)} {by_outcome or ''}".strip()]
    lines.append(f"Actions decided: {decisions or 'none'}; failed: {failed}")
    if waiting:
        lines.append(f"WAITING FOR YOU: {len(waiting)} approval(s): " + ", ".join(f"{w['action']} {w['target']}" for w in waiting[:5]))
    if snap["breaker"].get("tripped"):
        lines.append(f"BREAKER TRIPPED: {snap['breaker'].get('reason')}")
    if not snap["audit"]["chain_ok"]:
        lines.append(f"AUDIT CHAIN BROKEN at entry {snap['audit']['first_bad']}")
    for c in snap["promotion_candidates"]:
        lines.append(f"Earned trust: {c['action']} has {c['streak']} approved successes in a row; consider level: auto.")
    actionable = bool(waiting or snap["breaker"].get("tripped") or not snap["audit"]["chain_ok"] or snap["promotion_candidates"])
    return "\n".join(lines), actionable or bool(rows or decisions)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet-if-idle", action="store_true")
    args = ap.parse_args()
    text, noteworthy = build(Policy())
    print(text)
    if noteworthy or not args.quiet_if_idle:
        notify.send("digest", "Daily digest", text)


if __name__ == "__main__":
    main()
