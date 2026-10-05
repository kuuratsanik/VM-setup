"""Auto-merge agent PRs that only change low-risk files once CI and the review gate pass; everything else waits for a human."""
import argparse
import json
import os
import subprocess
import sys

import reviewer

REQUIRED_CHECKS = ("lint", "review")
MAX_FILES, MAX_LINES = 5, 300


REGULAR_MODES = reviewer.REGULAR_MODES


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


git = reviewer.git  # no system config or attributes


def head_changes(number, sha, base="main"):
    """The PR's changes as git sees them: [{"status", "old_mode", "new_mode", "path"}], from `git diff --raw --no-renames`
    of origin/<base>...<sha>. Unlike `gh pr view --json files`, this lists the old side of a rename (as a deletion) and the
    file modes, so a rename out of a protected path or a symlink/submodule cannot hide. Raises if the head moved."""
    git("fetch", "--no-tags", "--quiet", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}", f"+refs/pull/{number}/head:refs/automerge/pr-{number}")
    fetched = git("rev-parse", "--verify", f"refs/automerge/pr-{number}^{{commit}}").strip()
    if fetched != sha:
        raise RuntimeError(f"PR #{number} head is {fetched}, expected {sha}")
    return reviewer.raw_changes(f"origin/{base}", sha)


def change_problems(changes, files):
    """Reasons the git-level change list is not a plain add/edit of regular TIER0 files; [] if it is."""
    if not changes:
        return ["no git-level change list"]
    problems = []
    for c in changes:
        if c["status"] not in ("A", "M"):
            problems.append(f"{c['path']}: status {c['status']} (deletes, renames and type changes need a human)")
        if c["new_mode"] not in REGULAR_MODES or (c["status"] == "M" and c["old_mode"] not in REGULAR_MODES):
            problems.append(f"{c['path']}: mode {c['old_mode']}->{c['new_mode']} is not a regular file")
        if not reviewer.is_tier0(c["path"]):
            problems.append(f"{c['path']}: outside the low-risk tier")
    if {c["path"] for c in changes} != set(files):
        problems.append("git and GitHub disagree on the changed files")
    return problems


def eligible(pr):
    """(ok, reason) for a PR dict from `gh pr view --json ...`."""
    if pr["state"] != "OPEN" or pr.get("isDraft"):
        return False, "not an open, ready PR"
    if pr["baseRefName"] != "main" or not pr["headRefName"].startswith("agent/"):
        return False, "not an agent PR into main"
    # Agents push agent/* branches to this repository (agents/pr.py); anyone can name a fork branch agent/x.
    if pr.get("isCrossRepository") is not False:
        return False, "PR comes from a fork (or its origin is unknown)"
    # ...and anyone with push access can name a branch agent/x: bind to the agents' own GitHub identity.
    expected = os.environ.get("AGENT_PR_AUTHOR", "").strip()
    if not expected:
        return False, "AGENT_PR_AUTHOR is not set, so the agent identity cannot be checked; refusing to auto-merge"
    login = ((pr.get("author") or {}).get("login") or "").strip()
    if login.lower() != expected.lower():
        return False, f"author {login or '(unknown)'} is not the agent identity {expected}"
    if any(label["name"] in ("hold", "needs-human") for label in pr.get("labels", [])):
        return False, "held by a label"
    files = [f["path"] for f in pr["files"]]
    if not files or len(files) > MAX_FILES or pr["additions"] + pr["deletions"] > MAX_LINES:
        return False, "too many files or lines for auto-merge"
    outside = [f for f in files if not reviewer.is_tier0(f)]
    if outside:
        return False, f"touches files that need a human: {outside}"
    problems = change_problems(pr.get("changes") or [], files)
    if problems:
        return False, f"changes need a human: {problems}"
    if len(pr["changes"]) > MAX_FILES:
        return False, "too many files or lines for auto-merge"
    checks = {c.get("name"): c.get("conclusion") for c in pr.get("statusCheckRollup", []) if c.get("name")}
    missing = [n for n in REQUIRED_CHECKS if checks.get(n) != "SUCCESS"]
    if missing:
        return False, f"checks not green yet: {missing}"
    return True, "low-risk files and all checks green"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sha", required=True, help="head commit whose checks just finished")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    repo = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], capture_output=True, text=True, check=True).stdout.strip()
    numbers = gh("api", f"repos/{repo}/commits/{args.sha}/pulls", "--jq", ".[].number").split()
    for number in numbers:
        pr = json.loads(gh("pr", "view", number, "--json", "state,isDraft,isCrossRepository,author,baseRefName,headRefName,headRefOid,labels,files,additions,deletions,statusCheckRollup"))
        try:
            pr["changes"] = head_changes(number, args.sha) if pr["headRefOid"] == args.sha and pr["baseRefName"] == "main" else []
        except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
            print(f"PR #{number}: skip: cannot list git changes: {exc}")
            continue
        ok, reason = eligible(pr)
        print(f"PR #{number}: {'merge' if ok else 'skip'}: {reason}")
        if ok and pr["headRefOid"] == args.sha and not args.dry_run:
            gh("pr", "merge", number, "--squash", "--match-head-commit", args.sha)
            print(f"PR #{number}: merged")


if __name__ == "__main__":
    sys.exit(main())
