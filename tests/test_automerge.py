import json

import pytest
import yaml

import automerge
import reviewer
from conftest import ROOT

TOOLS = {"vm_list", "host_metrics", "vm_snapshot", "vm_start", "vm_stop"}


def pr(**over):
    base = {
        "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefName": "agent/runbook-guestdown", "labels": [],
        "files": [{"path": "agents/runbooks/guestdown.yaml"}], "additions": 20, "deletions": 0,
        "statusCheckRollup": [{"name": "lint", "conclusion": "SUCCESS"}, {"name": "review", "conclusion": "SUCCESS"}],
    }
    return {**base, **over}


def test_low_risk_agent_pr_with_green_checks_is_eligible():
    assert automerge.eligible(pr())[0]
    assert automerge.eligible(pr(files=[{"path": "proposals/capacity-2026-10-04.md"}]))[0]


@pytest.mark.parametrize("over, why", [
    ({"files": [{"path": "agents/runbooks/x.yaml"}, {"path": "agents/cost.py"}]}, "code outside the low-risk tier"),
    ({"files": [{"path": "agents/runbooks/../runtime.py"}]}, "path trick"),
    ({"files": [{"path": "agents/runbooks/run.sh"}]}, "wrong extension"),
    ({"headRefName": "feature/x"}, "not an agent branch"),
    ({"baseRefName": "release"}, "not main"),
    ({"isDraft": True}, "draft"),
    ({"labels": [{"name": "hold"}]}, "held"),
    ({"statusCheckRollup": [{"name": "lint", "conclusion": "SUCCESS"}]}, "review missing"),
    ({"statusCheckRollup": [{"name": "lint", "conclusion": "SUCCESS"}, {"name": "review", "conclusion": "FAILURE"}]}, "review failed"),
    ({"additions": 500}, "too large"),
    ({"files": []}, "empty"),
])
def test_everything_else_waits_for_a_human(over, why):
    assert not automerge.eligible(pr(**over))[0], why


def test_tier0_needs_no_label_but_other_agent_files_still_do():
    assert not reviewer.policy_problems(["agents/runbooks/a.yaml"], "", set(), "agent/x")
    assert reviewer.policy_problems(["agents/runbooks/a.yaml", "agents/cost.py"], "", set(), "agent/x")
    assert reviewer.policy_problems(["agents/policy.py"], "", {"human-approved"}, "agent/x")  # immutable even with the label
    assert reviewer.policy_problems(["agents/autonomy.yaml"], "", {"human-approved"}, "agent/x")
    assert not reviewer.policy_problems(["agents/autonomy.yaml"], "", {"human-approved"}, "feature/x")  # a human PR may change it


def test_every_runbook_is_well_formed_and_uses_known_tools():
    for path in (ROOT / "agents/runbooks").glob("*.yaml"):
        doc = yaml.safe_load(path.read_text())
        assert {"alert", "description", "steps", "verify", "escalate_if"} <= set(doc), path.name
        assert 0 < len(doc["steps"]) <= 8, path.name
        assert all(s["tool"] in TOOLS for s in doc["steps"]), path.name
