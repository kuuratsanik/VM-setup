#!/usr/bin/env python3
"""Detect host resources, choose a profile (min/std/max) and compute guest sizes."""
import argparse
import json
import os
import shutil
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
GIB = 1024**3


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def physical_cores():
    pairs = {
        line for line in run(["lscpu", "-p=CORE,SOCKET"]).splitlines() if line and not line.startswith("#")
    }
    return len(pairs) or os.cpu_count() or 1


def ram_gb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0


def gpu_vram_gb():
    out = run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    values = [int(v) for v in out.split() if v.isdigit()]
    return max(values) / 1024 if values else 0


def has_virt():
    flags = open("/proc/cpuinfo").read()
    return " vmx" in flags or " svm" in flags


def collect(storage_path):
    path = storage_path if os.path.exists(storage_path) else "/"
    return {
        "cores": physical_cores(),
        "ram_gb": round(ram_gb(), 1),
        "disk_free_gb": round(shutil.disk_usage(path).free / GIB, 1),
        "gpu_vram_gb": round(gpu_vram_gb(), 1),
        "virtualization": has_virt(),
    }


def select_profile(f):
    if not f["virtualization"]:
        sys.exit("error: CPU virtualization (vmx/svm) not available")
    if f["cores"] < 4 or f["ram_gb"] < 24 or f["disk_free_gb"] < 300:
        sys.exit("error: below supported minimum (4 cores, 24 GB RAM, 300 GB free disk)")
    if f["gpu_vram_gb"] >= 24 and f["cores"] >= 24 and f["ram_gb"] >= 96:
        return "max"
    if f["cores"] >= 12 and f["ram_gb"] >= 48:
        return "std"
    return "min"


def deep_merge(base, extra):
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


CLUSTER_NAMES = ["hub", "dev", "prod"]
NETWORK_PREFIX = "10.10.10"


def size(f, p):
    cores, ram, disk = f["cores"], f["ram_gb"], f["disk_free_gb"]
    reserve = max(3, 0.08 * ram)
    avail = ram - reserve - p["mgmt_vm"]["ram_gb"]
    c, s, w = p["clusters"], p["servers_per_cluster"], p["workers_per_cluster"]
    server_ram = p["server_ram_gb"]
    worker_ram = int(min(32, max(4, (avail / c - s * server_ram) / w)))
    server_disk = 30
    usable = 0.8 * disk - p["mgmt_vm"]["disk_gb"]
    worker_disk = int(min(200, (usable - c * s * server_disk) / (c * w)))
    if c > len(CLUSTER_NAMES) or c * (s * server_ram + w * worker_ram) > avail or worker_disk < 40:
        sys.exit("error: profile does not fit; lower clusters/workers in profile.override.yaml")

    nodes, host_octet = {}, 10
    for cluster in CLUSTER_NAMES[:c]:
        for role, count, vcpu, ram_gb, disk_gb in (
            ("server", s, p["server_vcpu"], server_ram, server_disk),
            ("agent", w, min(8, max(2, cores // w)), worker_ram, worker_disk),
        ):
            for i in range(count):
                nodes[f"{cluster}-{role[0]}{i + 1}"] = {
                    "cluster": cluster,
                    "role": role,
                    "primary": role == "server" and i == 0,
                    "ha": s > 1,
                    "vcpu": vcpu,
                    "ram_gb": ram_gb,
                    "disk_gb": disk_gb,
                    "ip": f"{NETWORK_PREFIX}.{host_octet}",
                }
                host_octet += 1
    return {"host_reserve_ram_gb": round(reserve), "nodes": nodes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect-only", action="store_true", help="print result, write nothing")
    ap.add_argument("--storage-path", default="/var/lib/libvirt")
    ap.add_argument("--out", default=os.path.join(ROOT, "profile.generated.json"))
    args = ap.parse_args()

    facts = collect(args.storage_path)
    name = select_profile(facts)
    with open(os.path.join(ROOT, "profiles", f"{name}.yaml")) as fh:
        profile = yaml.safe_load(fh)

    override_path = os.path.join(ROOT, "profile.override.yaml")
    override = {}
    if os.path.exists(override_path):
        with open(override_path) as fh:
            override = yaml.safe_load(fh) or {}
    deep_merge(profile, override)  # before size(), so clusters/workers/mgmt_vm overrides change the sizing
    result = {"profile": name, "facts": facts, **profile, **size(facts, profile)}
    deep_merge(result, override)  # again, so direct pins of computed keys (nodes, host_reserve_ram_gb) still win

    text = json.dumps(result, indent=2)
    if args.detect_only:
        print(text)
    else:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        print(f"profile={result['profile']} -> {args.out}")


if __name__ == "__main__":
    main()
