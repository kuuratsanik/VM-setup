import json
import subprocess

import pytest
import yaml

import automerge
import reviewer
from conftest import ROOT

TOOLS = {"vm_list", "host_metrics", "vm_snapshot", "vm_start", "vm_stop"}


AGENT = "vmsetup-agent-bot"


@pytest.fixture(autouse=True)
def agent_identity(monkeypatch):
    monkeypatch.setenv("AGENT_PR_AUTHOR", AGENT)


def added(path, mode="100644"):
    return {"status": "A", "old_mode": "000000", "new_mode": mode, "path": path}


def pr(**over):
    base = {
        "state": "OPEN", "isDraft": False, "isCrossRepository": False, "author": {"login": AGENT}, "baseRefName": "main", "headRefName": "agent/runbook-guestdown",
        "labels": [], "files": [{"path": "agents/runbooks/guestdown.yaml"}], "additions": 20, "deletions": 0,
        "statusCheckRollup": [{"name": "lint", "conclusion": "SUCCESS"}, {"name": "review", "conclusion": "SUCCESS"}],
    }
    out = {**base, **over}
    if "changes" not in over:  # by default git agrees with GitHub: plain additions of the listed files
        out["changes"] = [added(f["path"]) for f in out["files"]]
    return out


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
    # Fork PRs: anyone can name a fork branch agent/x.
    ({"isCrossRepository": True}, "fork"),
    ({"isCrossRepository": None}, "fork status unknown"),
    # git-level changes: gh's file list shows only the new side of a rename and no modes.
    ({"changes": [{"status": "D", "old_mode": "100644", "new_mode": "000000", "path": "tests/test_guardrails.py"},
                  added("agents/runbooks/guestdown.yaml")]}, "rename out of tests/ into TIER0"),
    ({"changes": [{"status": "D", "old_mode": "100644", "new_mode": "000000", "path": "agents/runbooks/old.yaml"},
                  added("agents/runbooks/guestdown.yaml")]}, "rename inside TIER0 deletes a runbook"),
    ({"changes": [{"status": "R100", "old_mode": "100644", "new_mode": "100644", "path": "agents/runbooks/guestdown.yaml"}]}, "rename status"),
    ({"changes": [added("agents/runbooks/guestdown.yaml", "120000")]}, "symlink"),
    ({"changes": [added("agents/runbooks/guestdown.yaml", "160000")]}, "gitlink"),
    ({"changes": [{"status": "T", "old_mode": "100644", "new_mode": "120000", "path": "agents/runbooks/guestdown.yaml"}]}, "type change"),
    ({"changes": [{"status": "M", "old_mode": "120000", "new_mode": "100644", "path": "agents/runbooks/guestdown.yaml"}]}, "was a symlink"),
    ({"changes": []}, "no git change list"),
    ({"changes": [added("agents/runbooks/guestdown.yaml"), added("agents/runbooks/other.yaml")]}, "git and gh disagree"),
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


def test_changes_are_required_not_optional():
    p = pr()
    del p["changes"]
    assert not automerge.eligible(p)[0]


def _git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "diff.renames=true", *a], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def test_rename_of_guardrail_test_into_runbooks_is_not_auto_merged(tmp_path, monkeypatch):
    """The audit repro: `git mv tests/test_guardrails.py agents/runbooks/guardrails.md` on an agent branch. GitHub's file
    list shows only the new (TIER0) path; the git-level list must show the deletion, so neither gate lets it through."""
    origin, clone = tmp_path / "origin", tmp_path / "clone"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "tests").mkdir()
    (origin / "agents/runbooks").mkdir(parents=True)
    (origin / "tests/test_guardrails.py").write_text("def test_x():\n    assert True\n")
    _git(origin, "add", "."); _git(origin, "commit", "-qm", "base")
    _git(origin, "checkout", "-q", "-b", "agent/x")
    _git(origin, "mv", "tests/test_guardrails.py", "agents/runbooks/guardrails.md")
    _git(origin, "commit", "-qm", "rename")
    sha = _git(origin, "rev-parse", "HEAD")
    _git(origin, "update-ref", "refs/pull/7/head", sha)
    _git(origin, "checkout", "-q", "main")
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    monkeypatch.chdir(clone)

    changes = automerge.head_changes("7", sha)
    assert {(c["status"], c["path"]) for c in changes} == {("D", "tests/test_guardrails.py"), ("A", "agents/runbooks/guardrails.md")}
    gh_view = pr(headRefName="agent/x", files=[{"path": "agents/runbooks/guardrails.md"}], changes=changes)
    assert not automerge.eligible(gh_view)[0]


def test_head_changes_refuses_a_moved_head(tmp_path, monkeypatch):
    origin, clone = tmp_path / "origin", tmp_path / "clone"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "README").write_text("x")
    _git(origin, "add", "."); _git(origin, "commit", "-qm", "base")
    _git(origin, "update-ref", "refs/pull/1/head", "HEAD")
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    monkeypatch.chdir(clone)
    with pytest.raises(RuntimeError):
        automerge.head_changes("1", "0" * 40)


def test_author_must_be_the_configured_agent_identity(monkeypatch):
    assert automerge.eligible(pr())[0]
    assert automerge.eligible(pr(author={"login": AGENT.upper()}))[0]  # GitHub logins are case-insensitive
    for author in ({"login": "someone-else"}, {"login": ""}, {}, None):
        assert not automerge.eligible(pr(author=author))[0], author
    no_author = pr()
    del no_author["author"]
    assert not automerge.eligible(no_author)[0]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_agent_identity_refuses_everything(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("AGENT_PR_AUTHOR", raising=False)
    else:
        monkeypatch.setenv("AGENT_PR_AUTHOR", value)
    ok, reason = automerge.eligible(pr())
    assert not ok and "AGENT_PR_AUTHOR" in reason
