"""Auto-merge agent PRs that only change low-risk files once CI and the review gate pass; everything else waits for a human."""
import argparse
import json
import subprocess
import sys

import reviewer

REQUIRED_CHECKS = ("lint", "review")
MAX_FILES, MAX_LINES = 5, 300


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def eligible(pr):
    """(ok, reason) for a PR dict from `gh pr view --json ...`."""
    if pr["state"] != "OPEN" or pr.get("isDraft"):
        return False, "not an open, ready PR"
    if pr["baseRefName"] != "main" or not pr["headRefName"].startswith("agent/"):
        return False, "not an agent PR into main"
    if any(label["name"] in ("hold", "needs-human") for label in pr.get("labels", [])):
        return False, "held by a label"
    files = [f["path"] for f in pr["files"]]
    if not files or len(files) > MAX_FILES or pr["additions"] + pr["deletions"] > MAX_LINES:
        return False, "too many files or lines for auto-merge"
    outside = [f for f in files if not reviewer.is_tier0(f)]
    if outside:
        return False, f"touches files that need a human: {outside}"
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
        pr = json.loads(gh("pr", "view", number, "--json", "state,isDraft,baseRefName,headRefName,headRefOid,labels,files,additions,deletions,statusCheckRollup"))
        ok, reason = eligible(pr)
        print(f"PR #{number}: {'merge' if ok else 'skip'}: {reason}")
        if ok and pr["headRefOid"] == args.sha and not args.dry_run:
            gh("pr", "merge", number, "--squash", "--match-head-commit", args.sha)
            print(f"PR #{number}: merged")


if __name__ == "__main__":
    sys.exit(main())
