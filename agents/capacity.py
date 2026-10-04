"""Capacity agent: compares detected hardware and live pressure with the active profile and opens a PR proposal."""
import datetime
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROM = "http://127.0.0.1:9090"
QUERIES = {
    "cpu_steal": 'avg(rate(node_cpu_seconds_total{mode="steal"}[1h]))',
    "mem_available_ratio": "avg_over_time((node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)[24h:5m])",
    "disk_free_ratio": 'node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}',
}


def prom(query):
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            result = json.load(r)["data"]["result"]
        return float(result[0]["value"][1]) if result else None
    except (OSError, ValueError, KeyError):
        return None


def detected():
    out = subprocess.run([sys.executable, str(ROOT / "detect.py"), "--detect-only"], capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 else None


def main():
    current = json.loads((ROOT / "profile.generated.json").read_text())
    new = detected()
    metrics = {k: prom(q) for k, q in QUERIES.items()}
    findings = []
    if new and new["profile"] != current["profile"]:
        findings.append(f"Hardware now selects profile `{new['profile']}` (current `{current['profile']}`).")
    if new and new["nodes"] != current["nodes"] and new["profile"] == current["profile"]:
        findings.append("Detected node sizing differs from the generated profile; regenerate it.")
    if (metrics["cpu_steal"] or 0) > 0.2:
        findings.append(f"Sustained CPU steal {metrics['cpu_steal']:.2f}: reduce vCPU overcommit or worker count.")
    if metrics["mem_available_ratio"] is not None and metrics["mem_available_ratio"] < 0.10:
        findings.append("Host memory is under 10% available on average: reduce node RAM or worker count.")
    if metrics["disk_free_ratio"] is not None and metrics["disk_free_ratio"] < 0.20:
        findings.append("Host disk is under 20% free: shrink worker disks or add storage.")
    if not findings:
        print("capacity: no change proposed")
        return

    body = "# Capacity proposal\n\n" + "\n".join(f"- {f}" for f in findings) + f"\n\nMetrics: `{json.dumps(metrics)}`\n"
    stamp = datetime.date.today().isoformat()
    path = ROOT / "proposals" / f"capacity-{stamp}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(body)
    print(body)
    # Proposal only: opening a PR needs gh auth; resizing is L3 and always reviewed by a human.
    branch = f"agent/capacity-{stamp}"
    for cmd in (
        ["git", "checkout", "-b", branch],
        ["git", "add", str(path)],
        ["git", "commit", "-m", f"capacity: proposal {stamp}"],
        ["git", "push", "-u", "origin", branch],
        ["gh", "pr", "create", "--title", f"Capacity proposal {stamp}", "--body", body, "--label", "agent"],
    ):
        if subprocess.run(cmd, cwd=ROOT).returncode != 0:
            print(f"capacity: stopped at {' '.join(cmd[:2])}; proposal left in {path}")
            break


if __name__ == "__main__":
    main()
