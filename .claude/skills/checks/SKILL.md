---
name: checks
description: Run the CI-equivalent checks relevant to the files changed against origin/main and print a pass/fail/skip table. Use before reporting any task complete; pass --all to run everything.
---
Run `.claude/skills/checks/run.sh` from the repo root (or `--all`). It detects changed files vs `origin/main`
(all checks if that fails) and runs only the matching checks: node/py_compile/pytest, terraform
(`-chdir=terraform` init -backend=false, validate, fmt -check), ansible-lint, yamllint, `kubectl kustomize`.
A missing tool is reported as SKIP, not FAIL. Exit status is non-zero if any check FAILs.
Include the table in your report. Fix FAILs; do not skip tests.
