#!/usr/bin/env python3
"""Build /etc/vmsetup/ai-kubeconfig.yaml (read-only ServiceAccount tokens for every cluster) for the Kubernetes MCP server."""
import base64
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = Path("/etc/vmsetup/ai-kubeconfig.yaml")


def ssh(ip, *cmd):
    out = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=accept-new", f"ops@{ip}", *cmd], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"ssh to {ip} failed")
    return out.stdout.strip()


def main():
    nodes = json.loads((ROOT / "profile.generated.json").read_text())["nodes"].values()
    cfg = {"apiVersion": "v1", "kind": "Config", "clusters": [], "users": [], "contexts": [], "current-context": ""}
    for node in nodes:
        if not node["primary"]:
            continue
        name, ip = node["cluster"], node["ip"]
        try:
            token = base64.b64decode(ssh(ip, "sudo", "k3s", "kubectl", "-n", "kube-system", "get", "secret", "ai-readonly-token", "-o", "jsonpath={.data.token}")).decode()
            ca = ssh(ip, "sudo", "k3s", "kubectl", "config", "view", "--raw", "--minify", "-o", "jsonpath={.clusters[0].cluster.certificate-authority-data}")
        except Exception as exc:
            print(f"skipped {name}: {exc} (is the ai-readonly app synced yet?)", file=sys.stderr)
            continue
        cfg["clusters"].append({"name": name, "cluster": {"server": f"https://{ip}:6443", "certificate-authority-data": ca}})
        cfg["users"].append({"name": f"ai-{name}", "user": {"token": token}})
        cfg["contexts"].append({"name": name, "context": {"cluster": name, "user": f"ai-{name}"}})
        cfg["current-context"] = cfg["current-context"] or name
    if not cfg["clusters"]:
        sys.exit("no cluster reachable")
    OUT.write_text(yaml.safe_dump(cfg))
    OUT.chmod(0o640)  # read-only tokens; the jarvis group needs to read it for the Kubernetes MCP server
    try:
        import grp
        import os
        os.chown(OUT, 0, grp.getgrnam("jarvis").gr_gid)
    except (KeyError, PermissionError):
        OUT.chmod(0o600)
    print(f"wrote {OUT} with contexts: {[c['name'] for c in cfg['contexts']]}")


if __name__ == "__main__":
    main()
