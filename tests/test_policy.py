import json
import time

import pytest
import yaml

import policy as policy_mod
from policy import Policy

CFG = {
    "mode": "autonomous",
    "actions": {
        "vm_snapshot": {"level": "auto", "per_target_per_hour": 2},
        "vm_start": {"level": "auto", "per_target_per_hour": 3},
        "vm_stop": {"level": "approve", "snapshot_first": True},
        "vm_wipe": {"level": "deny"},
    },
    "budgets": {"actions_per_day": 4},
    "breaker": {"failures": 2, "window_minutes": 60},
    "approvals": {"ttl_hours": 1},
    "earned_autonomy": {"promote_after": 3},
}


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr(policy_mod, "KILL_SWITCH", tmp_path / "PAUSED")
    clock = {"t": 1_000_000.0}

    def build(**over):
        cfg = {**CFG, **over}
        return Policy(config=cfg, state_dir=tmp_path / "state", clock=lambda: clock["t"]), clock

    return build


def test_autonomous_mode_follows_action_levels(make):
    p, _ = make()
    assert p.decide("vm_start", "hub-a1").level == "auto"
    assert p.decide("vm_stop", "hub-a1").level == "approve"
    assert p.decide("vm_wipe", "hub-a1").level == "deny"
    assert p.decide("rm_rf", "hub-a1").level == "deny"  # unknown actions are refused


def test_supervised_queues_everything_and_dry_run_never_executes(make):
    p, _ = make(mode="supervised")
    assert p.decide("vm_start", "x").level == "approve"
    p, _ = make(mode="dry_run")
    assert p.decide("vm_start", "x").level == "dry_run"


def test_human_confirmation_overrides_level_but_not_deny_or_kill_switch(make, tmp_path):
    p, _ = make()
    assert p.decide("vm_stop", "x", actor="owner", confirmed=True).level == "auto"
    assert p.decide("vm_wipe", "x", actor="owner", confirmed=True).level == "deny"
    (tmp_path / "PAUSED").write_text("")
    assert p.decide("vm_start", "x", actor="owner", confirmed=True).level == "deny"


def test_rate_limits_per_target_and_per_day(make):
    p, clock = make()
    for _ in range(3):
        assert p.decide("vm_start", "hub-a1").level == "auto"
        p.record("vm_start", "hub-a1", True)
    assert p.decide("vm_start", "hub-a1").reason == "per-target hourly limit reached"
    assert p.decide("vm_start", "dev-a1").level == "auto"  # other targets are unaffected
    p.record("vm_start", "dev-a1", True)
    assert p.decide("vm_snapshot", "prod-a1").reason == "daily action budget used up"
    clock["t"] += 90_000
    assert p.decide("vm_start", "hub-a1").level == "auto"


def test_breaker_trips_degrades_to_approval_and_resets_only_explicitly(make):
    p, clock = make()
    assert not p.record("vm_start", "a", False)
    assert p.record("vm_start", "b", False)  # second failure trips it
    assert p.breaker_tripped() and p.decide("vm_start", "c").reason == "circuit breaker is tripped"
    assert p.decide("vm_start", "c", actor="owner", confirmed=True).level == "auto"  # humans can still act
    clock["t"] += 7200
    assert p.breaker_tripped()  # time does not heal it
    p.reset_breaker("owner")
    assert not p.breaker_tripped() and p.decide("vm_start", "c").level == "auto"


def test_failures_outside_the_window_do_not_count(make):
    p, clock = make()
    p.record("vm_start", "a", False)
    clock["t"] += 7200
    assert not p.record("vm_start", "b", False)


def test_trust_ledger_suggests_promotion_after_a_clean_streak(make):
    p, _ = make()
    for _ in range(3):
        p.record("vm_stop", "x", True, actor="owner", confirmed=True)
    assert [c["action"] for c in p.promotion_candidates()] == ["vm_stop"]
    p.record("vm_stop", "x", False, actor="owner", confirmed=True)
    assert p.promotion_candidates() == []  # one failure resets the streak


def test_audit_chain_detects_edits_and_deletions(make, tmp_path):
    p, _ = make()
    for i in range(4):
        p.decide("vm_start", f"n{i}")
    assert p.verify_audit() == (True, 4, None)
    path = tmp_path / "state" / "audit.jsonl"
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["target"] = "evil"
    path.write_text("\n".join([lines[0], json.dumps(tampered), *lines[2:]]) + "\n")
    assert p.verify_audit()[:1] == (False,) and p.verify_audit()[2] == 1
    path.write_text("\n".join([lines[0], *lines[2:]]) + "\n")  # a deleted entry breaks the chain too
    assert p.verify_audit()[0] is False


def test_unusable_policy_fails_closed(tmp_path, monkeypatch):
    bad = tmp_path / "p.yaml"
    bad.write_text("mode: free-for-all\nactions: {}\n")
    monkeypatch.setattr(policy_mod, "POLICY_FILE", bad)
    p = Policy(state_dir=tmp_path / "s")
    assert p.error and p.mode == "dry_run" and p.decide("vm_start", "x").level == "dry_run"


def test_shipped_policy_is_valid_and_conservative():
    cfg = policy_mod.load_config()
    assert cfg["mode"] in ("dry_run", "supervised")  # autonomy is switched on by a reviewed change, not by default
    assert cfg["actions"]["vm_stop"]["level"] != "auto"
