import asyncio
import time

import pytest

import actions
import approvals
import policy as policy_mod
from policy import Policy
from test_policy import CFG


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(policy_mod, "KILL_SWITCH", tmp_path / "PAUSED")
    monkeypatch.setattr(approvals, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(actions.notify, "send", lambda *a, **k: True)
    calls = []

    async def call(tool, args):
        calls.append((tool, args))
        return (False, "boom") if args.get("fail") else (True, "ok")

    def make(**over):
        return Policy(config={**CFG, **over}, state_dir=tmp_path / "state"), call, calls

    return make


def run(coro):
    return asyncio.run(coro)


def test_auto_action_executes_live_and_is_recorded(env):
    p, call, calls = env()
    out = run(actions.gate(p, call, "vm_start", "hub-a1", {"domain": "hub-a1"}, actor="operator"))
    assert out.status == "executed" and calls == [("vm_start", {"domain": "hub-a1", "dry_run": False})]
    assert p.snapshot()["actions_24h"] == 1


def test_approval_level_queues_instead_of_executing(env):
    p, call, calls = env()
    out = run(actions.gate(p, call, "vm_stop", "hub-a1", {"domain": "hub-a1"}, actor="operator", context={"alert": "GuestDown"}))
    assert out.status == "queued" and calls == [] and out.approval_id
    item = approvals.pending()[0]
    assert item["action"] == "vm_stop" and item["target"] == "hub-a1" and item["context"]["alert"] == "GuestDown"


def test_confirmed_stop_snapshots_first_and_aborts_if_the_snapshot_fails(env):
    p, call, calls = env()
    out = run(actions.gate(p, call, "vm_stop", "hub-a1", {"domain": "hub-a1"}, actor="owner", confirmed=True))
    assert out.status == "executed" and [c[0] for c in calls] == ["vm_snapshot", "vm_stop"]

    async def failing(tool, args):
        return (False, "disk full") if tool == "vm_snapshot" else (True, "ok")

    out = run(actions.gate(p, failing, "vm_stop", "hub-a1", {"domain": "hub-a1"}, actor="owner", confirmed=True))
    assert out.status == "failed" and "snapshot" in out.text


def test_dry_run_mode_only_describes(env):
    p, call, calls = env(mode="dry_run")
    out = run(actions.gate(p, call, "vm_start", "x", {"domain": "x"}, actor="operator"))
    assert out.status == "dry_run" and calls == [("vm_start", {"domain": "x", "dry_run": True})]


def test_denied_actions_never_reach_the_tool(env):
    p, call, calls = env()
    out = run(actions.gate(p, call, "vm_wipe", "x", {}, actor="operator"))
    assert out.status == "denied" and calls == []


def test_repeated_failures_trip_the_breaker_and_later_actions_queue(env):
    p, call, calls = env()
    for target in ("a", "b"):
        run(actions.gate(p, call, "vm_start", target, {"domain": target, "fail": True}, actor="operator"))
    assert p.breaker_tripped()
    out = run(actions.gate(p, call, "vm_start", "c", {"domain": "c"}, actor="operator"))
    assert out.status == "queued"


def test_approval_queue_lifecycle_expiry_and_id_validation(tmp_path):
    rid = approvals.queue("vm_start", "x", {}, {}, ttl_hours=1, base=tmp_path)
    assert [d["id"] for d in approvals.pending(base=tmp_path)] == [rid]
    assert approvals.pending(base=tmp_path, now=time.time() + 7200) == []  # expired items drop out
    assert (tmp_path / "approvals-done" / f"{rid}.json").exists()
    with pytest.raises(ValueError):
        approvals.finish("../../etc/passwd", "x", base=tmp_path)
    assert approvals.get("../x", base=tmp_path) is None
