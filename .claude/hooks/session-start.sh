#!/usr/bin/env bash
# SessionStart hook: cloud sessions only. Quick, idempotent, no network, never fails the session.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0

if command -v dockerd >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
  (nohup dockerd >/tmp/dockerd.log 2>&1 &) >/dev/null 2>&1
  for _ in 1 2 3 4 5 6; do
    docker info >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

missing=""
for t in terraform kubectl helm k3d ansible-lint yamllint; do
  command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
python3 -c 'import playwright' >/dev/null 2>&1 || missing="$missing playwright(py)"
docker info >/dev/null 2>&1 || missing="$missing dockerd(running)"

if [ -n "$missing" ]; then
  echo "cloud tools MISSING:$missing (see /tmp/dockerd.log for docker; check the environment setup script)"
else
  echo "cloud tools OK"
fi
exit 0
