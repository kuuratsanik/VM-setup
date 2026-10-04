"""Upgrade agent: proposes a PR when a newer Ubuntu LTS than the configured cloud image exists."""
import re
import urllib.request

import pr

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
    repo = pr.ensure_clone()
    tf = (repo / "terraform" / "main.tf").read_text()
    match = URL_RE.search(tf)
    if not match:
        print("upgrade: image URL not in the expected format")
        return
    dist, version = latest_lts()
    if dist == match.group(1):
        print(f"upgrade: already on newest LTS ({dist})")
        return

    new_url = f"minimal/releases/{dist}/release/ubuntu-{version}-minimal-cloudimg-amd64.img"
    body = (
        f"Ubuntu LTS `{dist}` ({version}) is available; the node image still points at `{match.group(1)}`.\n\n"
        "WARNING: changing the base image replaces every node VM. Roll one cluster at a time (hub last) and take snapshots first. "
        "Requires human approval (L3)."
    )
    url = pr.open_pr(f"agent/ubuntu-{dist}", {"terraform/main.tf": URL_RE.sub(new_url, tf, count=1)}, f"Upgrade node image to Ubuntu {version}", body)
    print(f"upgrade: {url or 'proposal already up to date'}")


if __name__ == "__main__":
    main()
