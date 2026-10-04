"""Hybrid fine-tuning: a cloud job via the OpenAI API, or a local LoRA run on this host's GPU with Axolotl."""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

import config  # noqa: E402,F401
import yaml  # noqa: E402

AXOLOTL = """base_model: {base_model}
load_in_4bit: true
adapter: qlora
lora_r: 16
lora_alpha: 32
lora_dropout: 0.05
lora_target_linear: true
sequence_len: 4096
datasets:
  - path: /work/data/train.jsonl
    type: chat_template
    field_messages: messages
val_set_size: 0
num_epochs: {epochs}
micro_batch_size: 1
gradient_accumulation_steps: 8
learning_rate: 0.0002
optimizer: adamw_bnb_8bit
bf16: auto
gradient_checkpointing: true
output_dir: /work/adapter
"""


def estimate(dataset, epochs, price):
    tokens = sum(len(line) // 4 for line in dataset.read_text().splitlines())  # rough: 4 characters per token
    return tokens, tokens * epochs * price / 1e6


def train_openai(cfg, train, val, args):
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])  # direct to the provider, not through the gateway
    up = client.files.create(file=train.open("rb"), purpose="fine-tune")
    kwargs = {"validation_file": client.files.create(file=val.open("rb"), purpose="fine-tune").id} if val.exists() else {}
    job = client.fine_tuning.jobs.create(training_file=up.id, model=cfg["base_model"], suffix="vmsetup", hyperparameters={"n_epochs": cfg["epochs"]}, **kwargs)
    print(f"train: job {job.id} submitted")
    while job.status not in ("succeeded", "failed", "cancelled"):
        time.sleep(30)
        job = client.fine_tuning.jobs.retrieve(job.id)
        print(f"train: {job.status}")
    if job.status != "succeeded":
        sys.exit(f"train: job ended as {job.status}")
    return {"job": job.id, "model": job.fine_tuned_model}


def train_local(cfg, data_dir, args):
    work = Path(cfg["local"]["work_dir"])
    (work / "data").mkdir(parents=True, exist_ok=True)
    for name in ("train.jsonl", "validation.jsonl"):
        if (data_dir / name).exists():
            shutil.copy(data_dir / name, work / "data" / name)
    (work / "axolotl.yaml").write_text(AXOLOTL.format(base_model=cfg["local"]["base_model"], epochs=cfg["epochs"]))
    cmd = ["podman", "run", "--rm", "--device", "nvidia.com/gpu=all", "-v", f"{work}:/work", "-w", "/work", cfg["local"]["image"], "axolotl", "train", "axolotl.yaml"]
    print("train: " + " ".join(cmd))
    if not args.run:
        print("train: dry run; add --run on a host with an NVIDIA GPU and CDI configured")
        return {"adapter": str(work / "adapter"), "ran": False}
    subprocess.run(cmd, check=True)
    return {"adapter": str(work / "adapter"), "ran": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["openai", "local"], required=True)
    ap.add_argument("--data-dir", type=Path, default=Path("/var/lib/vmsetup/training/data"))
    ap.add_argument("--yes", action="store_true", help="confirm that redacted incident data may be uploaded (openai)")
    ap.add_argument("--run", action="store_true", help="actually start local training")
    args = ap.parse_args()

    profile = json.loads((ROOT / "profile.generated.json").read_text())
    tr = (profile.get("ai") or {}).get("training") or {}
    if not tr.get("enabled"):
        sys.exit("train: disabled; set ai.training.enabled: true in profile.override.yaml")
    cfg = yaml.safe_load((ROOT / "training/config.yaml").read_text())
    train, val = args.data_dir / "train.jsonl", args.data_dir / "validation.jsonl"
    if not train.exists():
        sys.exit("train: no dataset; run training/export_dataset.py first")

    tokens, cost = estimate(train, cfg["epochs"], cfg["price_per_million_tokens_usd"])
    print(f"train: ~{tokens} tokens x {cfg['epochs']} epochs, estimated ${cost:.2f} (cap ${tr.get('max_usd', 20)})")
    record = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "provider": args.provider, "dataset_sha256": hashlib.sha256(train.read_bytes()).hexdigest(), "estimated_usd": cost}
    if args.provider == "openai":
        if cost > tr.get("max_usd", 20):
            sys.exit("train: estimate exceeds ai.training.max_usd")
        if not args.yes:
            sys.exit("train: this uploads redacted incident data to OpenAI; re-run with --yes to confirm")
        record.update(train_openai(cfg, train, val, args))
    else:
        record.update(train_local(cfg, args.data_dir, args))
    runs = Path(cfg["local"]["work_dir"]) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{record['ts']}.json").write_text(json.dumps(record, indent=2))
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
