"""Run state-changing infra actions under the autonomy policy. Shared by the Operator and Jarvis."""
from dataclasses import dataclass

import approvals
import notify


@dataclass
class Outcome:
    status: str  # executed | failed | queued | denied | dry_run
    text: str
    approval_id: str = ""


async def _execute(policy, call, action, target, args, actor, confirmed):
    if policy.needs_snapshot(action):
        ok, text = await call("vm_snapshot", {"domain": target, "dry_run": False})
        policy.record("vm_snapshot", target, ok, actor, confirmed, f"snapshot before {action}")
        if not ok:
            return Outcome("failed", f"aborted: the pre-change snapshot failed: {text}")
    ok, text = await call(action, {**args, "dry_run": False})
    policy.record(action, target, ok, actor, confirmed, text)
    return Outcome("executed" if ok else "failed", text)


async def gate(policy, call, action, target, args, *, actor, context=None, confirmed=False):
    """Decide, then execute, queue for a human, or refuse. call(tool, args) -> (ok, text)."""
    decision = policy.decide(action, target, actor, confirmed)
    if decision.level == "deny":
        return Outcome("denied", f"denied by policy: {decision.reason}")
    if decision.level == "dry_run":
        _, text = await call(action, {**args, "dry_run": True})
        return Outcome("dry_run", text)
    if decision.level == "approve":
        rid = approvals.queue(action, target, args, {**(context or {}), "reason": decision.reason, "actor": actor}, policy.cfg["approvals"]["ttl_hours"])
        notify.send("approval_needed", f"{action} {target}", f"{decision.reason}. Approve or discard it in Jarvis > Autonomy (id {rid}).")
        return Outcome("queued", f"queued for human approval (id {rid}): {decision.reason}. Do not retry.", rid)
    return await _execute(policy, call, action, target, args, actor, confirmed)
