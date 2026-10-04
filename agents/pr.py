"""Open pull requests from agents using a dedicated clone, never the deployed copy (which has no .git)."""
import os
import subprocess
from pathlib import Path

import config  # noqa: F401

REPO_DIR = Path(os.environ.get("VMSETUP_REPO_DIR", "/var/lib/vmsetup/repo"))
GIT_ID = ["-c", "user.name=vmsetup-agent", "-c", "user.email=vmsetup-agent@users.noreply.github.com"]
CRED = ["-c", "credential.helper=!gh auth git-credential"]


def _run(*cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd or REPO_DIR, capture_output=True, text=True, check=check)


def ensure_clone():
    """Fresh checkout of origin/main in the agent's own clone; returns its path."""
    url = os.environ.get("VMSETUP_REPO_URL")
    if not REPO_DIR.exists():
        if not url:
            raise RuntimeError("VMSETUP_REPO_URL is not set")
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        _run("git", *CRED, "clone", url, str(REPO_DIR), cwd=REPO_DIR.parent)
    _run("git", *CRED, "fetch", "origin")
    _run("git", "checkout", "-q", "main")
    _run("git", "reset", "-q", "--hard", "origin/main")
    return REPO_DIR


def open_pr(branch, files, title, body):
    """files: {relative path: text} or a callable(repo_dir) returning that dict. Returns the PR URL, or None if nothing changed."""
    repo = ensure_clone()
    try:
        _run("git", "checkout", "-q", "-B", branch)
        for rel, text in (files(repo) if callable(files) else files).items():
            target = (repo / rel).resolve()
            if repo.resolve() not in target.parents:
                raise ValueError(f"path escapes the repo: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        _run("git", "add", "-A")
        if not _run("git", "status", "--porcelain").stdout.strip():
            return None
        _run("git", *GIT_ID, "commit", "-q", "-m", title)
        _run("git", *CRED, "push", "-q", "-u", "origin", branch)
        _run("gh", "label", "create", "agent", "--color", "5319E7", "--description", "Opened by an agent", check=False)
        out = _run("gh", "pr", "create", "--title", title, "--body", body, "--label", "agent", "--base", "main", "--head", branch)
        return out.stdout.strip().splitlines()[-1]
    finally:
        _run("git", "checkout", "-q", "main", check=False)
