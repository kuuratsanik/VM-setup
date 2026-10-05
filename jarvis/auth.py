"""Single-owner login: Argon2id password, optional TOTP, per-IP lockout, global failure tarpit. No default credentials exist."""
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

STATE_DIR = Path(os.environ.get("JARVIS_STATE_DIR", "/var/lib/vmsetup/jarvis"))
MAX_ATTEMPTS, WINDOW_S, LOCKOUT_S = 5, 300, 300
MAX_KEYS = 1000  # cap on tracked clients so lockout.json cannot grow without bound
GLOBAL_MAX, GLOBAL_DELAY_MAX_S = 30, 10.0
_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("jarvis-dummy-password")
log = logging.getLogger(__name__)


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


# Every auth.json read-modify-write runs under _lock (below), so a logout racing a login cannot lose a session_gen
# bump. Async callers (logout) accept a brief block on the local-disk write. The lock is per process, which is
# enough for the single-process service; `jarvis.manage` runs rarely and offline from the request path.
def bump_session_gen():
    """Invalidate every session cookie issued so far (logout, password change)."""
    with _lock:
        doc = _load()
        if doc is None:
            return 0
        doc["session_gen"] = int(doc.get("session_gen", 0)) + 1
        _save(doc)
        return doc["session_gen"]


def set_password(username, password):
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    pw_hash = _hasher.hash(password)  # slow: outside the lock
    with _lock:
        doc = _load() or {}
        doc.update({"username": username, "password_hash": pw_hash, "session_gen": int(doc.get("session_gen", 0)) + 1})
        _save(doc)


def enroll_totp(username="owner"):
    """Create a pending TOTP secret; it only counts once confirm_totp succeeds."""
    with _lock:
        doc = _load()
        if doc is None:
            raise RuntimeError("no account configured")
        secret = pyotp.random_base32()
        doc.update({"totp_secret": secret, "totp_confirmed": False})
        doc.pop("totp_last_step", None)  # a new secret starts a new step history
        _save(doc)
    return secret, pyotp.TOTP(secret).provisioning_uri(name=doc["username"], issuer_name="Jarvis")


def confirm_totp(code):
    with _lock:
        doc = _load()
        if not doc or not doc.get("totp_secret"):
            return False
        step = _totp_step(doc, str(code), time.time())
        if step is None:
            return False
        doc["totp_confirmed"] = True
        doc["totp_last_step"] = max(step, int(doc.get("totp_last_step", -1)))  # the enrollment code cannot log in
        _save(doc)
        return True


def _parse_nets(raw):
    nets = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                continue  # a typo must never widen trust
    return nets


def _addr(text):
    """Parse an address; an IPv4-mapped IPv6 address is unwrapped to its IPv4 form. Raises ValueError."""
    addr = ipaddress.ip_address(text)
    return addr.ipv4_mapped if addr.version == 6 and addr.ipv4_mapped else addr


def _is_trusted(addr, nets):
    return any(addr in net for net in nets if net.version == addr.version)


def _warn_unconfigured_proxy(request, peer_addr):
    """Behind an unconfigured reverse proxy every client shares the proxy's address and one attacker locks out
    everyone; say so (rate-limited) when a private/loopback peer sends X-Forwarded-For with no trusted proxies set."""
    global _last_warn
    if (peer_addr.is_loopback or peer_addr.is_private) and request.headers.getlist("x-forwarded-for"):
        now = time.time()
        if now - _last_warn >= 300:
            _last_warn = now
            log.warning("X-Forwarded-For seen from %s but JARVIS_TRUSTED_PROXIES is unset: all clients share one lockout key; set it to your reverse proxy address", peer_addr)


def client_ip(request):
    """Peer address, or (only when the peer is a JARVIS_TRUSTED_PROXIES member) the right-most valid X-Forwarded-For
    hop that is not itself a trusted proxy. Left-most entries are client-controlled and never used; malformed hops
    (including "ip:port") are skipped. A 0.0.0.0/0 entry means every IPv4 hop is trusted, so XFF yields nothing
    and the peer is used."""
    peer = request.client.host if request.client else "?"
    nets = _parse_nets(os.environ.get("JARVIS_TRUSTED_PROXIES", ""))
    try:
        peer_addr = _addr(peer)
    except ValueError:
        return peer
    if not nets:
        _warn_unconfigured_proxy(request, peer_addr)
    if not nets or not _is_trusted(peer_addr, nets):
        return str(peer_addr)
    for hop in reversed(",".join(request.headers.getlist("x-forwarded-for")).split(",")):
        try:
            addr = _addr(hop.strip())
        except ValueError:
            continue
        if not _is_trusted(addr, nets):
            return str(addr)
    return str(peer_addr)


def _key(ip):
    """Lockout key: IPv4 exact, IPv6 by /64 (a client owns a whole /64, so rotating within it must not reset)."""
    try:
        addr = _addr(ip)
    except ValueError:
        return str(ip)
    if addr.version == 6:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


# State: {"ips": {key: [count, first_ts, locked_until]}, "global": [count, first_ts, tarpit_until, trips],
# "totp": [bypass_failures, first_ts]} (the per-account budget of correct-password/wrong-TOTP attempts while locked).
# The in-memory copy is authoritative (enforcement holds even if every write fails); STATE_DIR/lockout.json only
# carries it across restarts. The threading.Lock is sufficient because jarvis.service runs a single process.
_lock = threading.Lock()
_mem = None
_mem_path = None
_last_log = 0.0
_last_warn = 0.0
TOTP_MAX, TOTP_WINDOW_S = 10, 900  # per ACCOUNT: correct-password + wrong-TOTP attempts tolerated while a client is locked


def _lock_file():
    return STATE_DIR / "lockout.json"


def _empty(now):
    return {"ips": {}, "global": [0, now, 0.0, 0], "totp": [0, now]}


def _read_file(now):
    try:
        doc = json.loads(_lock_file().read_text())
        ips = {str(k): [int(v[0]), float(v[1]), float(v[2])] for k, v in doc["ips"].items()}
        g = doc["global"]
        t = doc.get("totp", [0, now])
        return {"ips": ips, "global": [int(g[0]), float(g[1]), float(g[2]), int(g[3])], "totp": [int(t[0]), float(t[1])]}
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return _empty(now)  # missing or corrupt -> empty state


def _state(now):
    """Authoritative in-memory state (caller holds _lock), loaded from disk once per process / state dir, pruned."""
    global _mem, _mem_path
    if _mem is None or _mem_path != _lock_file():
        _mem, _mem_path = _read_file(now), _lock_file()
    ips = {k: v for k, v in _mem["ips"].items() if v[2] > now or now - v[1] <= WINDOW_S}  # prune expired
    if len(ips) > MAX_KEYS:  # cap: keep locked, then newest, entries
        ips = dict(sorted(ips.items(), key=lambda kv: (kv[1][2] > now, kv[1][1]), reverse=True)[:MAX_KEYS])
    _mem["ips"] = ips
    return _mem


def reset_lockout_state():
    """Forget the in-memory state (a 'restart'); the next access reloads from disk. For tests and tooling."""
    global _mem, _mem_path
    with _lock:
        _mem, _mem_path = None, None


class _Attempts(dict):
    """Legacy `auth._attempts.clear()` still works: it resets the lockout state."""

    def clear(self):
        reset_lockout_state()


_attempts = _Attempts()


def _save_lockout(state, now):
    # Called while holding _lock, so event-loop callers can block briefly on this disk write; acceptable on local disk.
    global _last_log
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _lock_file().with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state))
        tmp.chmod(0o600)
        os.replace(tmp, _lock_file())
    except OSError as exc:
        if now - _last_log >= 60:  # rate-limited; never log credentials
            _last_log = now
            log.error("cannot persist login lockout state to %s (%s); enforcing in memory only", _lock_file(), exc.__class__.__name__)


def retry_after(ip, now=None):
    """Seconds until this client may try again (0 when not locked). Per-IP only: see tarpit_delay for the global part."""
    now = now or time.time()
    with _lock:
        until = _state(now)["ips"].get(_key(ip), [0, 0, 0])[2]
    return max(0, int(until - now + 0.999)) if until > now else 0


def locked(ip, now=None):
    return retry_after(ip, now) > 0


def tarpit_delay(now=None):
    """Seconds to delay a FAILED login response while failures across all clients are elevated (0 otherwise).

    Credential stuffing over many IPs defeats per-IP limits, so after GLOBAL_MAX failures inside WINDOW_S every
    failed response is slowed, doubling per consecutive trip up to GLOBAL_DELAY_MAX_S. Tradeoff: this throttles
    guessing but NEVER rejects or delays a correct login, so the owner cannot be locked out by an attacker (a hard
    global lock would let ~2 requests a minute from throwaway IPs keep the owner out indefinitely)."""
    now = now or time.time()
    with _lock:
        _, _, until, trips = _state(now)["global"]
    return min(GLOBAL_DELAY_MAX_S, 0.5 * 2 ** max(0, trips - 1)) if until > now and trips else 0.0


def _record_failure(ip, now, was_locked=False):
    key = _key(ip)
    with _lock:
        state = _state(now)
        count, first, until = state["ips"].get(key, [0, now, 0])
        if now - first > WINDOW_S and until <= now:
            count, first = 0, now
        count += 1
        if not was_locked:  # a failed attempt during a lock keeps counting but never extends the lock
            until = now + LOCKOUT_S if count >= MAX_ATTEMPTS else 0
        state["ips"][key] = [count, first, until]
        gcount, gfirst, guntil, trips = state["global"]
        if now - gfirst > WINDOW_S:
            gcount, gfirst = 0, now
            if guntil <= now:
                trips = 0  # quiet for a full window: de-escalate
        gcount += 1
        if gcount >= GLOBAL_MAX:
            trips, guntil, gcount, gfirst = trips + 1, now + WINDOW_S, 0, now
        state["global"] = [gcount, gfirst, guntil, trips]
        _save_lockout(state, now)


def _totp_budget_exhausted(now):
    with _lock:
        count, first = _state(now)["totp"]
        return now - first <= TOTP_WINDOW_S and count >= TOTP_MAX


def _record_totp_failure(now):
    """Count a correct-password/wrong-TOTP attempt made while locked, against the whole account."""
    with _lock:
        state = _state(now)
        count, first = state["totp"]
        if now - first > TOTP_WINDOW_S:
            count, first = 0, now
        count += 1
        state["totp"] = [count, first]
        if count == TOTP_MAX:
            log.warning("TOTP bypass budget exhausted: %d correct-password/wrong-code attempts while locked; the password is likely compromised, rotate it (python -m jarvis.manage set-password)", count)
        _save_lockout(state, now)


def _record_success(ip, now):
    with _lock:
        state = _state(now)
        state["ips"].pop(_key(ip), None)
        state["global"] = [0, now, 0.0, 0]
        state["totp"] = [0, now]
        _save_lockout(state, now)


def _totp_step(doc, code, now):
    """Matched RFC 6238 time step of `code` (window +-1), or None. Compared in constant time."""
    totp = pyotp.TOTP(doc["totp_secret"])
    # Anything but exactly `digits` ASCII digits is a plain wrong code. (compare_digest raises TypeError on
    # non-ASCII str; that crash must never differ from a wrong code, or it would be a password oracle.)
    if not isinstance(code, str) or not (code.isascii() and code.isdigit() and len(code) == totp.digits):
        return None
    base = int(now // totp.interval)
    for step in (base - 1, base, base + 1):
        if secrets.compare_digest(totp.at(step * totp.interval), code):
            return step
    return None


_last_skew_warn = 0.0


def _accept_totp(code, now):
    """Atomically accept `code` once: valid, and its step newer than the last accepted one (RFC 6238 5.2, no
    replay). The check and the store happen together under _lock, reading the stored step fresh from disk."""
    global _last_skew_warn
    with _lock:
        doc = _load()
        if doc is None or not doc.get("totp_secret"):
            return False
        step = _totp_step(doc, code, now)
        if step is None:
            return False
        last = int(doc.get("totp_last_step", -1))
        if step <= last:
            if step < last - 1 and now - _last_skew_warn >= 300:  # beyond the +-1 window: not an ordinary replay
                _last_skew_warn = now
                log.warning("valid TOTP code is %d steps older than the last accepted one: the system clock probably stepped backwards (clock skew)", last - step)
            return False
        doc["totp_last_step"] = step
        _save(doc)  # temp file + os.replace
        return True


def verify(ip, username, password, code="", now=None):
    """True only for the right username, password and (when enabled) TOTP code; failures count toward lockout.
    The password hash is always verified, whatever the lock state (against a dummy hash for an unknown username),
    so timing is flat. A TOTP code is accepted once: a step <= the last accepted one is refused (replay).

    Lockout bypass: with TOTP enabled, a locked client may still log in with the right password AND a valid TOTP
    code. Behind a proxy that is not in JARVIS_TRUSTED_PROXIES all clients share one key, so otherwise 5 wrong
    passwords from anyone would lock the owner out for good. The attempt is fully verified like an unlocked one,
    success resets the counters, and any failed attempt returns plain False (the caller answers a uniform 429, so a
    lock never reveals which factor was wrong).
    Brute force of the TOTP by someone who already knows the password is bounded by a per-ACCOUNT budget (TOTP_MAX
    per TOTP_WINDOW_S) of correct-password/wrong-code attempts made while locked; once spent, bypasses are refused
    and a WARNING says to rotate the password. Wrong-password attempts never touch that budget (they feed the
    per-IP counter and the global tarpit), so an attacker without the password can never block the owner's bypass.
    Without TOTP a lock is never bypassed."""
    now = now or time.time()
    doc = _load()
    if doc is None:
        return False
    was_locked = locked(ip, now)
    totp_on = bool(doc.get("totp_confirmed"))
    user_ok = doc.get("username") == username
    try:
        pw_ok = _hasher.verify(doc["password_hash"] if user_ok and "password_hash" in doc else _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError, UnicodeEncodeError):  # e.g. a lone surrogate: just a wrong password
        pw_ok = False
    pw_ok = user_ok and pw_ok
    if was_locked:
        if not totp_on:
            return False  # no second factor: the lock holds
        if not pw_ok:  # counts toward the per-IP counter and the tarpit, never toward the TOTP budget
            _record_failure(ip, now, True)
            return False
        if _totp_budget_exhausted(now):
            return False
    ok = pw_ok
    if ok and totp_on:
        ok = _accept_totp(code, now)  # consumes the code atomically
    if ok:
        _record_success(ip, now)
    elif was_locked:
        _record_totp_failure(now)
    else:
        _record_failure(ip, now)
    return ok
