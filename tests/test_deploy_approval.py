import subprocess
from types import SimpleNamespace

import pytest

import deploy


@pytest.fixture
def env(monkeypatch):
    state = {"files": [], "origin": "https://github.com/o/r.git", "gh": "true", "gh_calls": 0}

    def fake_git(repo, *args):
        return "\n".join(state["files"]) if args[0] == "diff-tree" else state["origin"]

    def fake_run(cmd, **kw):
        assert cmd[0] == "gh"
        state["gh_calls"] += 1
        return SimpleNamespace(returncode=0, stdout=state["gh"] + "\n")

    monkeypatch.setattr(deploy, "git", fake_git)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return state


def check(env, files, expect, gh_calls=0):
    env["files"] = files
    assert deploy.approved("repo", "abc") is expect
    assert env["gh_calls"] == gh_calls


def test_runbooks_only_approved_without_gh(env):
    check(env, ["agents/runbooks/x.yaml"], True)


@pytest.mark.parametrize("label,expect", [("true", True), ("false", False)])
def test_runbooks_plus_protected_needs_label(env, label, expect):
    env["gh"] = label
    check(env, ["agents/runbooks/x.yaml", "agents/policy.py"], expect, gh_calls=1)


def test_dotdot_not_exempt(env):
    env["gh"] = "false"
    check(env, ["agents/runbooks/../policy.yaml"], False, gh_calls=1)


def test_yml_not_exempt(env):
    env["gh"] = "false"
    check(env, ["agents/runbooks/x.yml"], False, gh_calls=1)


def test_proposals_md_approved(env):
    check(env, ["proposals/idea.md"], True)


def test_unprotected_approved_without_gh(env):
    check(env, ["docs/x.md"], True)


def test_protected_without_github_origin_refused(env):
    env["origin"] = "/srv/local/repo"
    check(env, ["agents/policy.py"], False)
