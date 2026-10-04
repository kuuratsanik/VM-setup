"""Export human-approved Operator incidents as redacted, deduplicated chat-format JSONL for fine-tuning."""
import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

import evalgate  # noqa: E402
import memory  # noqa: E402
import yaml  # noqa: E402
from redact import redact  # noqa: E402


def to_messages(incident):
    """Transcript -> OpenAI chat messages with tool calls; returns (messages, tool names used)."""
    messages = [
        {"role": "system", "content": evalgate.SYSTEM},
        {"role": "user", "content": json.dumps({"alert": redact(incident["alert"])})},
    ]
    pending, used, counter = [], set(), 0
    for step in incident["transcript"]:
        if step["role"] == "assistant":
            calls = []
            for c in step.get("tool_calls") or []:
                counter += 1
                calls.append({"id": f"call_{counter}", "type": "function", "function": {"name": c["name"], "arguments": redact(c.get("arguments") or "{}")}})
                used.add(c["name"])
                pending.append(f"call_{counter}")
            msg = {"role": "assistant", "content": redact(step.get("content") or "") or None}
            if calls:
                msg["tool_calls"] = calls
            messages.append(msg)
        elif step["role"] == "tool" and pending:
            messages.append({"role": "tool", "tool_call_id": pending.pop(0), "content": redact(step.get("content") or "")})
    return messages, used


def build(incidents):
    seen, examples = set(), []
    for inc in incidents:
        messages, used = to_messages(inc)
        if messages[-1]["role"] != "assistant" or not messages[-1].get("content"):
            continue
        digest = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        tools = [{"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {}}}} for n in sorted(used)]
        examples.append({"messages": messages, **({"tools": tools} if tools else {})})
    return examples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", type=Path, default=Path("/var/lib/vmsetup/training/data"))
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "training/config.yaml").read_text())
    if not memory.enabled():
        sys.exit("export: vector memory is disabled (VMSETUP_DATABASE_URL unset)")

    examples = build(memory.reviewed("good"))
    if len(examples) < cfg["min_examples"]:
        sys.exit(f"export: {len(examples)} approved examples, need {cfg['min_examples']}. Mark more incidents with agents/feedback.py.")
    random.Random(args.seed).shuffle(examples)
    cut = max(1, int(len(examples) * cfg["validation_fraction"]))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train.jsonl", examples[cut:]), ("validation.jsonl", examples[:cut])):
        (args.out_dir / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"export: {len(examples) - cut} train / {cut} validation examples in {args.out_dir}")


if __name__ == "__main__":
    main()
