"""Persistent approval queue shared by the Operator (root) and Jarvis (jarvis user); one JSON file per request."""
import json
import os
import re
import secrets
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("VMSETUP_AUTONOMY_DIR", "/var/lib/vmsetup/autonomy"))
ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")


def _dir(name, base=None):
    path = Path(base or STATE_DIR) / name
    old = os.umask(0o007)
    try:
        path.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old)
    return path


def queue(action, target, args, context, ttl_hours=24, base=None):
    """Park an action for a human; returns its id."""
    rid = secrets.token_urlsafe(8)
    doc = {"id": rid, "action": action, "target": target, "args": args, "context": context, "created": time.time(), "expires": time.time() + ttl_hours * 3600}
    old = os.umask(0o007)
    try:
        (_dir("approvals", base) / f"{rid}.json").write_text(json.dumps(doc))
    finally:
        os.umask(old)
    return rid


def pending(base=None, now=None):
    now = now or time.time()
    items = []
    try:
        files = sorted(_dir("approvals", base).glob("*.json"))
    except OSError:
        return []
    for path in files:
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if doc["expires"] < now:
            finish(doc["id"], "expired", base=base)
        else:
            items.append(doc)
    return items


def finish(rid, status, result="", base=None):
    """Move a request out of the pending set and keep a record of how it ended."""
    if not ID_RE.match(rid):
        raise ValueError("invalid id")
    src = _dir("approvals", base) / f"{rid}.json"
    if not src.exists():
        return None
    doc = json.loads(src.read_text())
    doc.update({"status": status, "result": str(result)[:500], "finished": time.time()})
    old = os.umask(0o007)
    try:
        (_dir("approvals-done", base) / f"{rid}.json").write_text(json.dumps(doc))
    finally:
        os.umask(old)
    src.unlink()
    return doc


def get(rid, base=None):
    if not ID_RE.match(rid):
        return None
    try:
        doc = json.loads((_dir("approvals", base) / f"{rid}.json").read_text())
    except (OSError, ValueError):
        return None
    return doc if doc["expires"] >= time.time() else None
