#!/usr/bin/env bash
# Run the CI-equivalent checks for changed files. Usage: run.sh [--all]
cd "$(git rev-parse --show-toplevel)" || exit 2
ALL=0; [ "${1:-}" = "--all" ] && ALL=1

changed=""
if [ $ALL -eq 0 ]; then
  if changed=$(git diff --name-only origin/main...HEAD 2>/dev/null); then
    changed="$changed
$(git diff --name-only HEAD 2>/dev/null)
$(git ls-files --others --exclude-standard 2>/dev/null)"
  else
    ALL=1
  fi
fi

want() { [ $ALL -eq 1 ] || printf '%s\n' "$changed" | grep -Eq "$1"; }

RESULTS=(); FAILED=0
LOGDIR=$(mktemp -d)
record() { RESULTS+=("$1|$2|$3"); [ "$2" = FAIL ] && FAILED=1; }
run() { # name tool cmd...
  local name=$1 tool=$2; shift 2
  if ! command -v "$tool" >/dev/null 2>&1; then record "$name" SKIP "$tool not installed"; return; fi
  local log="$LOGDIR/$(echo "$name" | tr -c 'A-Za-z0-9' _).log"
  if bash -c "$*" >"$log" 2>&1; then record "$name" PASS ""; else record "$name" FAIL "$log"; fi
}

want '^jarvis/static/' && run "node --check app.js" node "node --check jarvis/static/app.js"
want '\.py$|^tests/' && run "py_compile" python "python -m py_compile detect.py agents/*.py infra-mcp/server.py media-mcp/server.py training/*.py scripts/*.py jarvis/*.py jarvis/compute/*.py"
want '\.py$|^tests/|^jarvis/|^agents/|^profiles/' && run "pytest" python "python -m pytest -q tests"
if want '^terraform/'; then
  run "terraform init" terraform "terraform -chdir=terraform init -backend=false"
  run "terraform validate" terraform "terraform -chdir=terraform validate"
  run "terraform fmt -check" terraform "terraform -chdir=terraform fmt -check"
fi
want '^ansible/' && run "ansible-lint" ansible-lint "ansible-lint ansible/"
want '^(profiles|agents)/.*\.ya?ml$' && run "yamllint" yamllint "yamllint -d relaxed profiles agents"
if want '^gitops/'; then
  for c in hub dev prod; do run "kustomize $c" kubectl "kubectl kustomize gitops/clusters/$c >/dev/null"; done
fi

printf '\n%-28s %-5s %s\n' CHECK RESULT NOTE
printf '%-28s %-5s %s\n' ---------------------------- ----- ----
for r in "${RESULTS[@]}"; do IFS='|' read -r n s d <<<"$r"; printf '%-28s %-5s %s\n' "$n" "$s" "$d"; done
[ ${#RESULTS[@]} -eq 0 ] && echo "(no relevant checks for the changed files)"
if [ $FAILED -eq 1 ]; then echo; echo "FAILED. Logs in $LOGDIR"; exit 1; fi
exit 0
