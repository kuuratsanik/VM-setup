"""Deploy agent (self-upgrade): rolls the running copy forward to origin/main after tests pass, with automatic rollback.

A commit that changes a protected path (other than regular TIER0 runbook/proposal files) must have come from a merged
PR labelled 'human-approved'. Other commits are deployed without a PR lookup and rely on GitHub branch protection for
review. Infrastructure changes (Terraform, Ansible) are never applied here.
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pr
import reviewer

LIVE = Path(os.environ.get("VMSETUP_LIVE_DIR", "/opt/vm-setup"))
PREV = LIVE.with_name(LIVE.name + ".prev")
STAGED = LIVE.with_name(LIVE.name + ".staged")
SYSTEMCTL = os.environ.get("VMSETUP_SYSTEMCTL", "systemctl")
SERVICE = "vmsetup-agent"
COMPANIONS = ("jarvis",)  # restarted with the operator so the dashboard runs the same code
KEEP_FILES = ("profile.generated.json",)  # not in git; .venv is moved separately


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def git_raw(repo, *args):
    """Like git() but without stripping, so NUL-separated output stays intact; undecodable bytes in file names are kept (surrogateescape) instead of crashing."""
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, errors="surrogateescape", check=True).stdout


def regular_file(repo, sha, path):
    """True if path is absent at sha (deleted) or a regular file (100644/100755), i.e. not a symlink or submodule."""
    entry = git_raw(repo, "--literal-pathspecs", "-c", "core.quotePath=false", "ls-tree", "-z", sha, "--", path)
    return not entry or entry.split(" ", 1)[0] in ("100644", "100755")


def approved(repo, sha):
    """True unless the commit changes a protected path that is not an exempt TIER0 file (same rule as
    reviewer.policy_problems, but TIER0 is exempt only as a regular file, never a symlink); then it must have come
    from a merged PR labelled human-approved. Lists files of every parent for merge commits, NUL-separated and unquoted."""
    out = git_raw(repo, "-c", "core.quotePath=false", "diff-tree", "-m", "-z", "--no-commit-id", "--name-only", "-r", sha)
    files = [f for f in out.split("\0") if f]
    if not any(f.startswith(reviewer.PROTECTED) and not (reviewer.is_tier0(f) and regular_file(repo, sha, f)) for f in files):
        return True
    slug = re.search(r"github\.com[:/](.+?)(?:\.git)?$", git(repo, "remote", "get-url", "origin"))
    if not slug:
        return False
    out = subprocess.run(["gh", "api", f"repos/{slug.group(1)}/commits/{sha}/pulls", "--jq", '[.[] | select(.merged_at != null) | .labels[].name] | any(. == "human-approved")'], cwd=repo, capture_output=True, text=True)
    return out.returncode == 0 and out.stdout.strip() == "true"


def run(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def healthy():
    smoke = run(str(LIVE / ".venv/bin/python"), "-c", "import sys; sys.path.insert(0, 'agents'); import runtime", cwd=LIVE)
    if smoke.returncode != 0:
        return False
    time.sleep(float(os.environ.get("VMSETUP_HEALTH_WAIT", "5")))
    return run(SYSTEMCTL, "is-active", "--quiet", SERVICE).returncode == 0


def swap_in(repo):
    shutil.rmtree(STAGED, ignore_errors=True)
    shutil.copytree(repo, STAGED, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    for name in KEEP_FILES:
        if (LIVE / name).exists():
            shutil.copy(LIVE / name, STAGED / name)
    shutil.rmtree(PREV, ignore_errors=True)
    LIVE.rename(PREV)
    STAGED.rename(LIVE)
    if (PREV / ".venv").exists():
        (PREV / ".venv").rename(LIVE / ".venv")


def restart_all():
    result = run(SYSTEMCTL, "restart", SERVICE)
    for unit in COMPANIONS:
        run(SYSTEMCTL, "restart", unit)
    return result


def roll_back():
    if (LIVE / ".venv").exists() and PREV.exists():
        (LIVE / ".venv").rename(PREV / ".venv")
    shutil.rmtree(LIVE, ignore_errors=True)
    PREV.rename(LIVE)
    restart_all()


def main():
    repo = pr.ensure_clone()
    head = git(repo, "rev-parse", "HEAD")
    marker = LIVE / ".deployed_sha"
    deployed = marker.read_text().strip() if marker.exists() else None
    if deployed == head:
        print("deploy: up to date")
        return
    new = git(repo, "rev-list", f"{deployed}..HEAD").split() if deployed else [head]
    unapproved = [s[:8] for s in new if not approved(repo, s)]
    if unapproved:
        sys.exit(f"deploy: refusing, commits touch protected paths without an approved PR: {unapproved}")

    python = LIVE / ".venv/bin/python"
    tests = run(str(python), "-m", "pytest", "-q", "-x", "tests", cwd=repo)
    if tests.returncode != 0:
        sys.exit(f"deploy: tests failed, keeping the current version\n{tests.stdout[-1500:]}")

    swap_in(repo)
    pip = run(str(python), "-m", "pip", "install", "-q", "-r", str(LIVE / "agents/requirements.txt"))
    restart = restart_all()
    if pip.returncode != 0 or restart.returncode != 0 or not healthy():
        roll_back()
        sys.exit("deploy: new version unhealthy, rolled back")
    marker.write_text(head + "\n")
    print(f"deploy: now running {head[:8]}")


if __name__ == "__main__":
    main()
