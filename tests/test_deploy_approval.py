import os
import subprocess
from types import SimpleNamespace

import pytest

import deploy


@pytest.fixture
def env(monkeypatch):
    state = {"files": [], "origin": "https://github.com/o/r.git", "gh": "true", "gh_calls": 0, "modes": {}}

    def fake_git(repo, *args):
        return state["origin"]

    def fake_raw(repo, *args):
        if "diff-tree" in args:
            assert "-m" in args and "-z" in args and "core.quotePath=false" in args
            return "".join(f + "\0" for f in state["files"])
        path = args[-1]
        mode = state["modes"].get(path, "100644")
        return "" if mode is None else f"{mode} blob abc\t{path}\0"

    def fake_run(cmd, **kw):
        assert cmd[0] == "gh"
        state["gh_calls"] += 1
        return SimpleNamespace(returncode=0, stdout=state["gh"] + "\n")

    monkeypatch.setattr(deploy, "git", fake_git)
    monkeypatch.setattr(deploy, "git_raw", fake_raw)
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


def test_non_ascii_protected_path_needs_label(env):
    env["gh"] = "false"
    check(env, ["agents/\u00fc.py"], False, gh_calls=1)


def test_merge_commit_files_are_seen(env):
    # fake_raw asserts -m is passed; merge-commit files (listed per parent) are then checked
    env["gh"] = "false"
    check(env, ["docs/a.md", "agents/policy.py", "agents/policy.py"], False, gh_calls=1)


def test_symlinked_runbook_needs_label(env):
    env["modes"]["agents/runbooks/x.yaml"] = "120000"
    env["gh"] = "false"
    check(env, ["agents/runbooks/x.yaml"], False, gh_calls=1)


def test_executable_runbook_exempt(env):
    env["modes"]["agents/runbooks/x.yaml"] = "100755"
    check(env, ["agents/runbooks/x.yaml"], True)


def test_deleted_runbook_exempt(env):
    env["modes"]["agents/runbooks/x.yaml"] = None
    check(env, ["agents/runbooks/x.yaml"], True)


def test_real_git_symlink_mode_and_nul_listing(tmp_path):
    import os
    r = str(tmp_path)

    def g(*a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=r, capture_output=True, text=True, check=True).stdout.strip()

    g("init", "-q", "-b", "main")
    (tmp_path / "agents/runbooks").mkdir(parents=True)
    (tmp_path / "README").write_text("x")
    g("add", "."); g("commit", "-qm", "base")
    os.symlink("../policy.py", tmp_path / "agents/runbooks/l.yaml")
    (tmp_path / "agents/\u00fc.py").write_text("x")
    g("add", "."); g("commit", "-qm", "link")
    sha = g("rev-parse", "HEAD")
    assert deploy.regular_file(r, sha, "agents/runbooks/l.yaml") is False
    assert deploy.regular_file(r, sha, "README") is True
    assert deploy.git_raw(r, "-c", "core.quotePath=false", "diff-tree", "-z", "--no-commit-id", "--name-only", "-r", sha).count("\0") == 2


def test_undecodable_filename_is_refused_not_crash(tmp_path):
    import subprocess as sp
    repo = tmp_path / "r"
    repo.mkdir()
    sp.run(["git", "init", "-q", str(repo)], check=True)
    sp.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    sp.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    sp.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/x.git"], check=True)
    (repo / "README").write_text("base\n")
    sp.run(["git", "-C", str(repo), "add", "-A"], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)  # diff-tree needs a parent
    agents = repo / "agents"
    agents.mkdir()
    (agents / os.fsdecode(b"\xffbad.py")).write_text("x = 1\n")
    sp.run(["git", "-C", str(repo), "add", "-A"], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True)
    sha = sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    # Protected path with invalid UTF-8 and a non-GitHub origin: refused (False), no UnicodeDecodeError.
    assert deploy.approved(str(repo), sha) is False
