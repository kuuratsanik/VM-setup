"""Single-owner login: Argon2id password, optional TOTP, per-IP lockout. No default credentials exist."""
import json
import os
import time
from pathlib import Path

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

STATE_DIR = Path(os.environ.get("JARVIS_STATE_DIR", "/var/lib/vmsetup/jarvis"))
MAX_ATTEMPTS, WINDOW_S, LOCKOUT_S = 5, 300, 300
_hasher = PasswordHasher()
_attempts = {}  # ip -> [count, first_ts, locked_until]


def _file():
    return STATE_DIR / "auth.json"


def _load():
    try:
        return json.loads(_file().read_text())
    except (OSError, ValueError):
        return None


def _save(doc):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    tmp.chmod(0o600)
    tmp.replace(_file())


def configured():
    return _load() is not None


def totp_enabled():
    doc = _load()
    return bool(doc and doc.get("totp_secret") and doc.get("totp_confirmed"))


def session_gen():
    """Current session generation; a cookie is valid only while it carries this number."""
    doc = _load()
    return int(doc.get("session_gen", 0)) if doc else 0


def bump_session_gen():
    """Invalidate every session cookie issued so far (logout, password change)."""
    doc = _load()
    if doc is None:
        return 0
    doc["session_gen"] = int(doc.get("session_gen", 0)) + 1
    _save(doc)
    return doc["session_gen"]


def set_password(username, password):
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    doc = _load() or {}
    doc.update({"username": username, "password_hash": _hasher.hash(password), "session_gen": int(doc.get("session_gen", 0)) + 1})
    _save(doc)


def enroll_totp(username="owner"):
    """Create a pending TOTP secret; it only counts once confirm_totp succeeds."""
    doc = _load()
    if doc is None:
        raise RuntimeError("no account configured")
    secret = pyotp.random_base32()
    doc.update({"totp_secret": secret, "totp_confirmed": False})
    _save(doc)
    return secret, pyotp.TOTP(secret).provisioning_uri(name=doc["username"], issuer_name="Jarvis")


def confirm_totp(code):
    doc = _load()
    if not doc or not doc.get("totp_secret") or not pyotp.TOTP(doc["totp_secret"]).verify(code, valid_window=1):
        return False
    doc["totp_confirmed"] = True
    _save(doc)
    return True


def locked(ip, now=None):
    now = now or time.time()
    entry = _attempts.get(ip)
    return bool(entry and entry[2] > now)


def _record_failure(ip, now):
    count, first, _ = _attempts.get(ip, [0, now, 0])
    if now - first > WINDOW_S:
        count, first = 0, now
    count += 1
    _attempts[ip] = [count, first, now + LOCKOUT_S if count >= MAX_ATTEMPTS else 0]


def verify(ip, username, password, code="", now=None):
    """True only for the right username, password and (when enabled) TOTP code; failures count toward lockout."""
    now = now or time.time()
    doc = _load()
    if doc is None or locked(ip, now):
        return False
    ok = False
    try:
        ok = doc["username"] == username and _hasher.verify(doc["password_hash"], password)
    except (VerifyMismatchError, KeyError):
        ok = False
    if ok and doc.get("totp_confirmed"):
        ok = bool(code) and pyotp.TOTP(doc["totp_secret"]).verify(code, valid_window=1)
    if ok:
        _attempts.pop(ip, None)
    else:
        _record_failure(ip, now)
    return ok
