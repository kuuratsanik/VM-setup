"""Root applier: merges staged provider keys into /etc/vmsetup/*.env and restarts consumers.

Runs as root from a systemd path unit when jarvis stages a file, so it only accepts allowlisted variable names and strict
value patterns; nothing else from the staged file ever reaches an env file.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

STAGED = Path(os.environ.get("JARVIS_STAGED", "/var/lib/vmsetup/jarvis/staged-secrets.json"))
SECRETS = Path(os.environ.get("VMSETUP_SECRETS", "/etc/vmsetup/secrets.env"))
JARVIS_ENV = Path(os.environ.get("JARVIS_ENV", "/etc/vmsetup/jarvis.env"))
MARKER = Path(os.environ.get("JARVIS_CONFIGURED_FILE", "/etc/vmsetup/jarvis-configured.json"))
VALUE_RE = re.compile(r"^[A-Za-z0-9_\-.:/+=]{1,512}$")
# name -> files that receive it. Jarvis itself only gets what it needs to call RunPod/Kaggle.
ALLOWED = {
    "OPENAI_API_KEY": [SECRETS], "ANTHROPIC_API_KEY": [SECRETS], "OPENROUTER_API_KEY": [SECRETS], "HF_TOKEN": [SECRETS],
    "GH_TOKEN": [SECRETS], "RUNPOD_API_KEY": [SECRETS, JARVIS_ENV], "KAGGLE_API_TOKEN": [SECRETS, JARVIS_ENV], "KAGGLE_USERNAME": [SECRETS, JARVIS_ENV],
}
RESTARTS = os.environ.get("JARVIS_RESTART_CMDS", "podman restart litellm;systemctl restart vmsetup-agent;systemctl restart jarvis").split(";")


def merge(path, updates):
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0]
        if name in updates:
            lines[i] = f"{name}={updates[name]}"
            seen.add(name)
    lines += [f"{n}={v}" for n, v in updates.items() if n not in seen]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.chmod(0o640 if path == JARVIS_ENV else 0o600)
    if path == JARVIS_ENV:
        try:
            import grp
            os.chown(tmp, 0, grp.getgrnam("jarvis").gr_gid)
        except (KeyError, PermissionError, ImportError):
            pass
    tmp.replace(path)


def write_marker(files):
    """Names (never values) of the allowlisted variables now set, so Jarvis can show which providers are configured.

    Runs as root, so the marker lives in a root-owned directory Jarvis cannot write to (never the Jarvis state dir, where a
    planted symlink would make root overwrite an arbitrary file).
    """
    names = set()
    for path in files:
        if path.exists():
            for line in path.read_text().splitlines():
                name, _, value = line.partition("=")
                if name in ALLOWED and value:
                    names.add(name)
    parent = MARKER.parent
    st = parent.stat()
    if st.st_uid != 0 or st.st_mode & 0o022:
        raise RuntimeError(f"apply_secrets: refusing to write the marker in {parent}: not root-owned or writable by others")
    tmp = parent / (MARKER.name + ".tmp")
    try:
        tmp.unlink()  # a stale temp from a crashed run; unlink never follows a symlink
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(sorted(names)))
        os.chmod(tmp, 0o644)
        os.replace(tmp, MARKER)
    finally:
        if tmp.exists():
            tmp.unlink()


def main():
    if not STAGED.exists():
        return 0
    try:
        staged = json.loads(STAGED.read_text())
    except ValueError:
        STAGED.unlink()
        sys.exit("apply_secrets: unreadable staged file removed")
    per_file = {}
    for name, value in staged.items():
        if name not in ALLOWED or not isinstance(value, str) or not VALUE_RE.match(value):
            print(f"apply_secrets: ignored {name!r}", file=sys.stderr)
            continue
        for target in ALLOWED[name]:
            per_file.setdefault(target, {})[name] = value
    STAGED.unlink()
    for target, updates in per_file.items():
        merge(target, updates)
    if per_file:
        try:
            write_marker([SECRETS, JARVIS_ENV])
        except (OSError, RuntimeError) as exc:  # the marker is cosmetic; never block the key apply or the restarts
            print(f"apply_secrets: marker not written: {exc}", file=sys.stderr)
        for cmd in RESTARTS:
            subprocess.run(cmd.split(), check=False)
    print(f"apply_secrets: applied {sorted({n for u in per_file.values() for n in u})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
