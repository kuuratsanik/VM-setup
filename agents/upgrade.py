"""Upgrade agent: proposes a PR when a newer Ubuntu LTS than the configured cloud image exists."""
import re
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = ROOT / "terraform" / "main.tf"
META = "https://changelogs.ubuntu.com/meta-release-lts"
URL_RE = re.compile(r"minimal/releases/(\w+)/release/ubuntu-([\d.]+)-minimal-cloudimg-amd64\.img")


def latest_lts():
    text = urllib.request.urlopen(META, timeout=20).read().decode()
    latest = None
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if fields.get("Supported") == "1" and "Version" in fields:
            latest = (fields["Dist"], ".".join(fields["Version"].split()[0].split(".")[:2]))
    return latest


def main():
    match = URL_RE.search(TF.read_text())
    if not match:
        print("upgrade: image URL not in the expected format")
        return
    current_codename = match.group(1)
    dist, version = latest_lts()
    if dist == current_codename:
        print(f"upgrade: already on newest LTS ({dist})")
        return

    new_url = f"minimal/releases/{dist}/release/ubuntu-{version}-minimal-cloudimg-amd64.img"
    TF.write_text(URL_RE.sub(new_url, TF.read_text(), count=1))
    branch = f"agent/ubuntu-{dist}"
    body = (
        f"Ubuntu LTS `{dist}` ({version}) is available; the node image still points at `{current_codename}`.\n\n"
        "WARNING: changing the base image replaces every node VM. Roll one cluster at a time (hub last) and take snapshots first. "
        "Requires human approval (L3)."
    )
    for cmd in (
        ["git", "checkout", "-b", branch],
        ["git", "add", str(TF)],
        ["git", "commit", "-m", f"upgrade: Ubuntu {version} ({dist}) node image"],
        ["git", "push", "-u", "origin", branch],
        ["gh", "pr", "create", "--title", f"Upgrade node image to Ubuntu {version}", "--body", body, "--label", "agent"],
    ):
        if subprocess.run(cmd, cwd=ROOT).returncode != 0:
            print(f"upgrade: stopped at {' '.join(cmd[:2])}")
            break


if __name__ == "__main__":
    main()
