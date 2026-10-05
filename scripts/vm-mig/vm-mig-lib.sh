# shellcheck shell=bash
# Shared config and helpers for the VM-migration tools (plan rev 9.1, §7a/§7b/§7c).
export LC_ALL=C   # sort/comm/diff set arithmetic must not depend on the et_EE collation
# Every path is overridable by environment so the tests can run in a scratch root.
VMMIG_STATE=${VMMIG_STATE:-/var/lib/vm-mig}
VMMIG_ROOT=${VMMIG_ROOT:-/root/vm-mig}
VMMIG_UFW_DIR=${VMMIG_UFW_DIR:-/etc/ufw}
VMMIG_BUS=${VMMIG_BUS:-/home/sven-katkosilt/coordination/messages.jsonl}
VMMIG_BIN=${VMMIG_BIN:-/usr/local/sbin}
VMMIG_REVERT=${VMMIG_REVERT:-$VMMIG_BIN/vm-mig-revert}
VMMIG_DELAY=${VMMIG_DELAY:-10min}
VMMIG_EXPECT=${VMMIG_EXPECT:-$VMMIG_STATE/expected.env}
VMMIG_USER=${VMMIG_USER:-sven-katkosilt}
# VMMIG_SCOPE=user runs the dead-man timer in the user manager (drills without root only).
VMMIG_SCOPE=${VMMIG_SCOPE:-system}
VMMIG_TAG=${VMMIG_TAG:-$(basename "$0")}

log() { # message -> stderr + journal (tagged); never fails
  printf '%s %s: %s\n' "$(date -u +%FT%TZ)" "$VMMIG_TAG" "$*" >&2
  logger -t "$VMMIG_TAG" -- "$*" 2>/dev/null || true
}

bus() { # best-effort, last, never blocks a revert: no network involved, 5 s cap
  [ -n "$VMMIG_BUS" ] && [ -w "$VMMIG_BUS" ] || return 0
  local line
  line=$(jq -nc --arg ts "$(date -u +%FT%TZ)" --arg from "$VMMIG_TAG" --arg text "$*" \
    '{ts:$ts, from:$from, to:"all", text:$text}' 2>/dev/null) || return 0
  timeout 5 sh -c 'printf "%s\n" "$1" >> "$2"' _ "$line" "$VMMIG_BUS" 2>/dev/null || true
}

dm_systemctl() { # systemctl in the scope the dead-man timer lives in
  if [ "$VMMIG_SCOPE" = user ]; then systemctl --user "$@"; else systemctl "$@"; fi
}

dm_systemd_run() {
  if [ "$VMMIG_SCOPE" = user ]; then systemd-run --user "$@"; else systemd-run "$@"; fi
}

user_systemctl() { # uid-1000 user manager, from root or from the user itself (§7a)
  if [ "$(id -u)" = 0 ]; then
    local uid; uid=$(id -u "$VMMIG_USER")
    runuser -u "$VMMIG_USER" -- env XDG_RUNTIME_DIR="/run/user/$uid" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user "$@"
  else
    systemctl --user "$@"
  fi
}
