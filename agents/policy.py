"""Autonomy policy engine: decides whether a state-changing action runs by itself, needs a human, or is refused.

Everything an agent does to infrastructure passes through Policy.decide(). It enforces the mode (dry_run, supervised,
autonomous), per-action levels, rate limits, a daily budget, the circuit breaker and the kill switch, and records every
decision and outcome in a hash-chained audit log. A broken or missing policy file fails closed (dry-run).
"""
import fcntl
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
POLICY_FILE = Path(os.environ.get("VMSETUP_AUTONOMY_POLICY", ROOT / "agents/autonomy.yaml"))
STATE_DIR = Path(os.environ.get("VMSETUP_AUTONOMY_DIR", "/var/lib/vmsetup/autonomy"))
KILL_SWITCH = Path(os.environ.get("VMSETUP_KILL_SWITCH", "/etc/vmsetup/AGENTS_PAUSED"))
MODES = ("dry_run", "supervised", "autonomous")
LEVELS = ("auto", "approve", "deny")


@dataclass
class Decision:
    level: str  # auto | approve | dry_run | deny
    reason: str


def load_config(path=None):
    cfg = yaml.safe_load(Path(path or POLICY_FILE).read_text())
    if not isinstance(cfg, dict) or cfg.get("mode") not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    actions = cfg.get("actions")
    if not isinstance(actions, dict) or any(not isinstance(a, dict) or a.get("level") not in LEVELS for a in actions.values()):
        raise ValueError(f"every action needs a level in {LEVELS}")
    for key in ("budgets", "breaker", "approvals", "earned_autonomy"):
        if not isinstance(cfg.get(key), dict):
            raise ValueError(f"missing section {key}")
    return cfg


class Policy:
    def __init__(self, config=None, state_dir=None, clock=time.time):
        self.clock = clock
        self.dir = Path(state_dir or STATE_DIR)
        self.error = None
        try:
            self.cfg = config if config is not None else load_config()
        except (OSError, ValueError, yaml.YAMLError) as exc:
            self.cfg, self.error = None, f"policy unusable ({exc}); failing closed"

    # -- files -------------------------------------------------------------------------------------------------
    def _ensure(self):
        old = os.umask(0o007)  # group-writable so the jarvis user can share state with the root operator
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        finally:
            os.umask(old)

    @contextmanager
    def _locked(self):
        self._ensure()
        old = os.umask(0o007)
        try:
            with open(self.dir / ".lock", "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            os.umask(old)

    def _read(self):
        try:
            state = json.loads((self.dir / "state.json").read_text())
        except (OSError, ValueError):
            state = {}
        state.setdefault("actions", [])
        state.setdefault("failures", [])
        state.setdefault("breaker", {"tripped": False})
        state.setdefault("ledger", {})
        return state

    def _write(self, state):
        tmp = self.dir / "state.json.tmp"
        old = os.umask(0o007)
        try:
            tmp.write_text(json.dumps(state))
        finally:
            os.umask(old)
        tmp.replace(self.dir / "state.json")

    # -- audit -------------------------------------------------------------------------------------------------
    def audit(self, **event):
        """Append a hash-chained entry: each hash covers the previous one, so edits or deletions are detectable."""
        with self._locked():
            path = self.dir / "audit.jsonl"
            prev = "0" * 64
            if path.exists():
                lines = path.read_text().splitlines()
                if lines:
                    prev = json.loads(lines[-1])["hash"]
            entry = {"ts": self.clock(), **event, "prev": prev}
            entry["hash"] = hashlib.sha256((prev + json.dumps(entry, sort_keys=True)).encode()).hexdigest()
            with path.open("a") as fh:
                fh.write(json.dumps(entry) + "\n")
        return entry

    def audit_tail(self, n=20):
        path = self.dir / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()[-n:]] if path.exists() else []

    def verify_audit(self):
        """(ok, entries, first_bad_index)."""
        path = self.dir / "audit.jsonl"
        prev, count = "0" * 64, 0
        for i, line in enumerate(path.read_text().splitlines() if path.exists() else []):
            entry = json.loads(line)
            claimed = entry.pop("hash", None)
            expected = hashlib.sha256((prev + json.dumps(entry, sort_keys=True)).encode()).hexdigest()
            if entry.get("prev") != prev or claimed != expected:
                return False, count, i
            prev, count = claimed, count + 1
        return True, count, None

    # -- decisions ---------------------------------------------------------------------------------------------
    @property
    def mode(self):
        return self.cfg["mode"] if self.cfg else "dry_run"

    def breaker_tripped(self):
        return self._read()["breaker"].get("tripped", False)

    def decide(self, action, target, actor="operator", confirmed=False):
        """confirmed=True means a logged-in human approved this exact action."""
        decision = self._decide(action, target, confirmed)
        self.audit(kind="decision", actor=actor, action=action, target=target, level=decision.level, reason=decision.reason, confirmed=confirmed)
        return decision

    def _decide(self, action, target, confirmed):
        if self.error:
            return Decision("dry_run", self.error)
        if KILL_SWITCH.exists():
            return Decision("deny", "kill switch is active")
        spec = self.cfg["actions"].get(action)
        if spec is None or spec["level"] == "deny":
            return Decision("deny", "action is not permitted by policy")
        if self.mode == "dry_run":
            return Decision("dry_run", "mode is dry_run")
        if confirmed:
            return Decision("auto", "approved by a human")
        if self.mode == "supervised":
            return Decision("approve", "mode is supervised")
        if self.breaker_tripped():
            return Decision("approve", "circuit breaker is tripped")
        if spec["level"] == "approve":
            return Decision("approve", "policy requires approval for this action")
        now = self.clock()
        state = self._read()
        recent = [a for a in state["actions"] if a["ts"] > now - 3600 and a["action"] == action and a["target"] == target]
        if len(recent) >= spec.get("per_target_per_hour", 10**9):
            return Decision("approve", "per-target hourly limit reached")
        if len([a for a in state["actions"] if a["ts"] > now - 86400]) >= self.cfg["budgets"]["actions_per_day"]:
            return Decision("approve", "daily action budget used up")
        return Decision("auto", "allowed by policy")

    def needs_snapshot(self, action):
        return bool(self.cfg and self.cfg["actions"].get(action, {}).get("snapshot_first"))

    # -- outcomes ----------------------------------------------------------------------------------------------
    def record(self, action, target, ok, actor="operator", confirmed=False, note=""):
        """Count the attempt, feed the breaker and the trust ledger, and audit the outcome."""
        now = self.clock()
        tripped_now = False
        with self._locked():
            state = self._read()
            state["actions"] = [a for a in state["actions"] if a["ts"] > now - 86400] + [{"ts": now, "action": action, "target": target}]
            window = self.cfg["breaker"]["window_minutes"] * 60 if self.cfg else 3600
            state["failures"] = [t for t in state["failures"] if t > now - window]
            if not ok:
                state["failures"].append(now)
                limit = self.cfg["breaker"]["failures"] if self.cfg else 1
                if len(state["failures"]) >= limit and not state["breaker"].get("tripped"):
                    state["breaker"] = {"tripped": True, "since": now, "reason": f"{len(state['failures'])} failures in {window // 60} min (last: {action} {target})"}
                    tripped_now = True
            if confirmed:
                led = state["ledger"].setdefault(action, {"ok": 0, "bad": 0, "streak": 0})
                led["ok" if ok else "bad"] += 1
                led["streak"] = led["streak"] + 1 if ok else 0
            self._write(state)
        self.audit(kind="outcome", actor=actor, action=action, target=target, ok=ok, confirmed=confirmed, note=note[:300])
        if tripped_now:
            self.audit(kind="breaker", actor="policy", action="trip", target="*", reason=state["breaker"]["reason"])
        return tripped_now

    def reset_breaker(self, actor):
        with self._locked():
            state = self._read()
            state["breaker"], state["failures"] = {"tripped": False}, []
            self._write(state)
        self.audit(kind="breaker", actor=actor, action="reset", target="*")

    def promotion_candidates(self):
        """Approval-level actions with a long human-approved success streak: evidence for a (human) policy change."""
        need = self.cfg["earned_autonomy"]["promote_after"] if self.cfg else 10**9
        ledger = self._read()["ledger"]
        return [{"action": a, "streak": v["streak"], "ok": v["ok"], "bad": v["bad"]} for a, v in ledger.items()
                if v["streak"] >= need and self.cfg["actions"].get(a, {}).get("level") != "auto"]

    def snapshot(self):
        state = self._read()
        ok, count, bad = self.verify_audit()
        return {"mode": self.mode, "error": self.error, "breaker": state["breaker"], "actions_24h": len(state["actions"]),
                "ledger": state["ledger"], "promotion_candidates": self.promotion_candidates() if self.cfg else [],
                "audit": {"entries": count, "chain_ok": ok, "first_bad": bad}}


def main():
    policy = Policy()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "reset-breaker":
        policy.reset_breaker(os.environ.get("USER", "cli"))
        print("breaker reset")
    elif cmd == "verify-audit":
        ok, count, bad = policy.verify_audit()
        print(f"audit chain {'ok' if ok else 'BROKEN at entry ' + str(bad)} ({count} entries)")
        sys.exit(0 if ok else 1)
    else:
        print(json.dumps(policy.snapshot(), indent=2))


if __name__ == "__main__":
    main()
