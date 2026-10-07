# shellcheck shell=bash
# Helpers for the OWNER-RUN phase scripts (p0a.sh, t3.sh). Source after vm-mig-lib.sh.
# Contract: every mutating step asks y/N first; --dry-run never mutates (it prints WOULD); each passed
# step is appended to $D/steps.log (plan §5 R10 step 7: re-enter at the last verified step).
# sudo's env may lack the system dirs; append (never prepend, so test shims still win)
PATH=$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PH_DRY=${PH_DRY:-0}
PH_FORCE_G8=${PH_FORCE_G8:-0}
PH_OWNER_HOME=${PH_OWNER_HOME:-/home/$VMMIG_USER}
PH_AUDIT=${PH_AUDIT:-$PH_OWNER_HOME/Projektid/_inventory/AUDIT_LOG.md}
PH_SYSAGENT=${PH_SYSAGENT:-$PH_OWNER_HOME/coordination/bin/system-agent}
PH_BRAKE=${PH_BRAKE:-$PH_OWNER_HOME/coordination/bin/brake-advise}
PH_LOADAVG=${PH_LOADAVG:-/proc/loadavg}
VMMIG_GUARD=${VMMIG_GUARD:-$VMMIG_BIN/vm-mig-guard}
PH_PASSED=(); PH_FAILED=(); PH_LEASE=""; PH_INTENT=""

say() { printf '%s\n' "$*"; }
pass() { say "PASS $*"; PH_PASSED+=("$1"); [ "$PH_DRY" = 1 ] || echo "$(date -u +%FT%TZ) $1 PASS" >> "$D/steps.log"; }
failv() { say "FAIL $*"; PH_FAILED+=("$1"); }

ask() { # question -> 0 only on an explicit y/yes. Dry-run never asks and never says yes.
  local a
  [ "$PH_DRY" = 1 ] && { say "   WOULD ask: $* [y/N]"; return 1; }
  read -r -p "$* [y/N] " a || return 1
  [[ $a =~ ^([yY]|[yY][eE][sS])$ ]]
}

# step NAME "what it does" done_fn do_fn verify_fn
#   done_fn true  -> skip the action, still verify
#   dry-run       -> print what it would do, no verify of the (not made) change
# returns 0 pass, 1 fail, 2 declined
step() {
  local name=$1 desc=$2 done_fn=$3 do_fn=$4 verify_fn=$5
  say ""; say "== $name: $desc"
  if $done_fn; then
    say "   already in place; verifying only"
    if $verify_fn; then pass "$name"; return 0; else failv "$name" "(existing state does not verify)"; return 1; fi
  fi
  if [ "$PH_DRY" = 1 ]; then say "   WOULD do: $desc"; return 0; fi
  ask "   Run step $name?" || { say "   declined"; return 2; }
  if ! $do_fn; then failv "$name" "(action failed)"; return 1; fi
  if $verify_fn; then pass "$name"; return 0; fi
  failv "$name" "(verification)"; return 1
}

# G8 WAIT gate (plan §7): brake-advise exit 0, load1 < 3, swap <= 8 GiB. rpool cap <= 80 % is never
# overridable. Red -> exit 75 (WAIT) unless --force-g8 and the owner types FORCE G8.
g8_gate() {
  local red=0 load swap cap a
  if "$PH_BRAKE" --quiet >/dev/null 2>&1; then say "PASS G8 brake-advise"; else say "RED  G8 brake-advise (defer)"; red=1; fi
  load=$(awk '{print $1}' "$PH_LOADAVG")
  if awk -v l="$load" 'BEGIN{exit !(l<3)}'; then say "PASS G8 load1 $load < 3"; else say "RED  G8 load1 $load >= 3"; red=1; fi
  swap=$(free -g | awk '/^Swap/{print $3}')
  if [ "${swap:-99}" -le 8 ]; then say "PASS G8 swap ${swap} GiB <= 8"; else say "RED  G8 swap ${swap:-?} GiB > 8"; red=1; fi
  cap=$(zpool list -H -o cap rpool 2>/dev/null | tr -d %)
  if [ -n "$cap" ] && [ "$cap" -le 80 ]; then say "PASS rpool cap ${cap}% <= 80%"
  else say "RED  rpool cap ${cap:-?}% (> 80% or unreadable; never overridable)"; [ "$PH_DRY" = 1 ] || exit 75; fi
  [ $red = 0 ] && return 0
  if [ "$PH_DRY" = 1 ]; then say "   (dry-run: a real run would WAIT here, exit 75, unless --force-g8)"; return 0; fi
  if [ "$PH_FORCE_G8" = 1 ]; then
    read -r -p "G8 is RED. Type FORCE G8 to proceed anyway: " a || a=""
    [ "$a" = "FORCE G8" ] && { say "G8 overridden by the owner"; bus "$PHASE: G8 RED, overridden by the owner (--force-g8)"; return 0; }
  fi
  say "G8 RED: WAIT (exit 75). Re-check later or use --force-g8."; exit 75
}

lease_acquire() { # phase subsystem target blast rationale
  PH_INTENT="vm-mig-$1-$(date -u +%Y%m%dT%H%M%SZ)"
  if [ "$PH_DRY" = 1 ]; then say "WOULD: system-agent intent $PH_INTENT + acquire --subsystem $2"; return 0; fi
  local as=(runuser -u "$VMMIG_USER" -- env COORD_AGENT="owner-$1")
  [ "$(id -u)" = 0 ] || as=(env COORD_AGENT="owner-$1")
  if "${as[@]}" "$PH_SYSAGENT" intent --id "$PH_INTENT" --target "$3" --action "phase-$1" --tier 2 \
       --duration-min 180 --blast-radius "$4" --rationale "$5" >/dev/null 2>&1 \
     && "${as[@]}" "$PH_SYSAGENT" acquire --subsystem "$2" --intent-id "$PH_INTENT" --ttl 180 >/dev/null 2>&1; then
    PH_LEASE=$2; say "intent $PH_INTENT, lease on '$2' acquired"
  else
    say "system-agent intent/lease unavailable; logging to the bus instead (best-effort)"
    bus "$1 started by the owner (system-agent intent/lease unavailable); intent id $PH_INTENT"
  fi
}

lease_release() {
  [ -n "$PH_LEASE" ] || return 0
  local as=(runuser -u "$VMMIG_USER" -- env COORD_AGENT="owner-$PHASE")
  [ "$(id -u)" = 0 ] || as=(env COORD_AGENT="owner-$PHASE")
  "${as[@]}" "$PH_SYSAGENT" release --subsystem "$PH_LEASE" >/dev/null 2>&1 && say "lease '$PH_LEASE' released"
  PH_LEASE=""
}

audit() { # result text
  local line
  line="- $(date '+%F %T') (owner, $(basename "$0")) $PHASE: $1. passed: ${PH_PASSED[*]:-none}; failed: ${PH_FAILED[*]:-none}. $2"
  if [ "$PH_DRY" = 1 ]; then say "WOULD append to AUDIT_LOG: $line"; return 0; fi
  printf '%s\n' "$line" >> "$PH_AUDIT" 2>/dev/null || say "could not append to $PH_AUDIT"
  bus "$PHASE: $1"
}
