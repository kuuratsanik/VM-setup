#!/usr/bin/env bash
# t3.sh [--dry-run] [--force-g8]   OWNER-RUN phase T3 of the VM migration plan rev 9.2 (§7b G7).
# Needs P0a (rpool/backup-critical + g7.key) and the installed g7-backup/g7-verify. Run as root:
#   sudo bash scripts/vm-mig/t3.sh --dry-run
#   sudo bash scripts/vm-mig/t3.sh            # y/N before every mutating step
# Steps: gates -> intent/lease -> G7 restic password (root 0400, never shown) + /root/.config/g7/g7.env
# -> ext-pool/g7 container (auto-snapshot off) -> restic init SSD + HDD repos -> HDD-side key copies in
# ext-pool/g7/keys (passphrase-encrypted, canmount=noauto) -> g7-backup -> g7-verify (both disks)
# -> g7-verify --hdd-only -> lock ext-pool/g7/keys -> escrow-extra.paths -> escrow reminder -> AUDIT_LOG.
set -uo pipefail
SELF=$(readlink -f "$0")
. "$(dirname "$SELF")/vm-mig-lib.sh"
. "$(dirname "$SELF")/vm-mig-phase-lib.sh"
PHASE=T3
# shellcheck disable=SC2034  # used inside the eval'd prereq strings
HERE=$(dirname "$SELF")

G7DIR=${G7DIR:-/root/.config/g7}
G7KEY=${G7KEY:-$G7DIR/g7.key}
PASSF=${PASSF:-$G7DIR/restic.pass}
ENVF=${ENVF:-$G7DIR/g7.env}
BCDIR=${G7_DIR:-/srv/backup-critical}
HDD_REPO=${G7_HDD_REPO:-/ext-pool/backups/restic-critical}
KEYS_DS=${KEYS_DS:-ext-pool/g7/keys}
KEYS_MNT=${KEYS_MNT:-/ext-pool/g7-keys}
EXTRA=${EXTRA:-/root/.config/m93p-backup/escrow-extra.paths}
G7_BACKUP=${G7_BACKUP:-$VMMIG_BIN/g7-backup}
G7_VERIFY=${G7_VERIFY:-$VMMIG_BIN/g7-verify}
TODAY=$(date +%F)

for a in "$@"; do
  case $a in
    --dry-run) PH_DRY=1 ;;
    --force-g8) PH_FORCE_G8=1 ;;
    *) echo "usage: t3.sh [--dry-run] [--force-g8]" >&2; exit 64 ;;
  esac
done
[ "$PH_DRY" = 1 ] || ph_lock "$PHASE"
say "T3 (plan rev 9.2 §7b). $( [ "$PH_DRY" = 1 ] && echo 'DRY-RUN: nothing will be changed.' )"

# ---- pre-gates (read-only) -------------------------------------------------------------------------
g8_gate
pre_red=0
prereq() { if eval "$2"; then say "PASS $1"; else say "RED  $1${3:+ ($3)}"; pre_red=1; fi; }
# shellcheck disable=SC2016
{
prereq "rpool/backup-critical exists, key loaded, mounted (P0a)" \
  '[ "$(zfs get -H -o value keystatus,mounted rpool/backup-critical 2>/dev/null | tr "\n" " ")" = "available yes " ]' "run p0a.sh first"
prereq "pg_restore runs (g7-backup/g7-verify prerequisite)" 'pg_restore --version >/dev/null 2>&1' "owner: apt install postgresql-client"
prereq "installed g7-backup == reviewed copy" 'cmp -s "$G7_BACKUP" "$HERE/g7-backup"' "re-run install.sh"
prereq "installed g7-verify == reviewed copy" 'cmp -s "$G7_VERIFY" "$HERE/g7-verify"' "re-run install.sh"
prereq "HDD repo is not the existing restic repo" '[ "$(readlink -m "$HDD_REPO")" != /ext-pool/backups/restic ]'
}
if [ $pre_red = 1 ] && [ "$PH_DRY" = 0 ]; then say "pre-gates RED: nothing changed"; exit 5; fi
if [ "$PH_DRY" = 0 ]; then
  ph_require_root
  D=$VMMIG_ROOT/$PHASE; mkdir -p "$D" && chmod 700 "$D"
else
  D=$(mktemp -d)
fi
[ "$PH_FORCE_G8" = 1 ] && export G7_SKIP_G8=1   # g7-backup has its own G8 gate; the owner overrode it above

trap 'lease_release' EXIT
lease_acquire "$PHASE" backup host/zfs/backup-critical 'rpool/backup-critical,ext-pool/g7,ext-pool/backups/restic-critical' \
  "VM migration T3: G7 critical-set backup to both disks + verified restores (owner-approved)"
stop() { say ""; say "STOPPED: $2"; audit "STOPPED ($2)" "artifacts in $D"; exit "$1"; }
never() { false; }

# ---- G7 restic password (one password for both repos, as the plan's single RESTIC_PASSWORD_FILE) ------
pw_done() { [ -s "$PASSF" ] && [ -s "$ENVF" ]; }
pw_do() {
  install -d -m 700 "$G7DIR" || return 1
  if [ ! -s "$PASSF" ]; then
    (umask 077; head -c 48 /dev/urandom | base64 -w0 > "$PASSF") && chmod 400 "$PASSF" || return 1
  fi
  # escrow.sh discovers G7_RESTIC_PASSWORD_FILE / G7_HDD_RESTIC_PW from /root/.config/g7/*.env (paths only)
  (umask 077; printf '%s\n' "G7_RESTIC_PASSWORD_FILE=$PASSF" "G7_HDD_RESTIC_PW=$KEYS_MNT/restic.pass" \
     "G7_HDD_KEY=$KEYS_MNT/g7.key" > "$ENVF")
}
pw_verify() { [ "$(stat -c '%a %U' "$PASSF")" = "400 root" ] && [ "$(stat -c %a "$ENVF")" = 600 ]; }
step password "generate the G7 restic password into $PASSF (root 0400, never displayed) + $ENVF" \
  pw_done pw_do pw_verify; rc=$?; [ $rc = 0 ] || stop $rc password

# ---- ext-pool/g7 container ---------------------------------------------------------------------------
ct_done() { zfs list -H ext-pool/g7 >/dev/null 2>&1; }
ct_do() { zfs create -o mountpoint=none -o canmount=off -o com.sun:auto-snapshot=false ext-pool/g7; }
ct_verify() { [ "$(zfs get -H -o value com.sun:auto-snapshot ext-pool/g7)" = false ]; }
step container "zfs create ext-pool/g7 (canmount=off, com.sun:auto-snapshot=false; outside ext-pool/replica)" \
  ct_done ct_do ct_verify; rc=$?; [ $rc = 0 ] || stop $rc container

# ---- restic repos ------------------------------------------------------------------------------------
rs_done() { [ -s "$BCDIR/restic/config" ]; }
rs_do() { RESTIC_PASSWORD_FILE=$PASSF restic init -q --repo "$BCDIR/restic" >/dev/null; }
rs_verify() { RESTIC_PASSWORD_FILE=$PASSF restic -q --repo "$BCDIR/restic" cat config >/dev/null 2>&1; }
step restic-ssd "restic init $BCDIR/restic (inside the encrypted dataset)" rs_done rs_do rs_verify
rc=$?; [ $rc = 0 ] || stop $rc restic-ssd
rh_done() { [ -s "$HDD_REPO/config" ]; }
rh_do() { install -d -m 700 "$HDD_REPO" && RESTIC_PASSWORD_FILE=$PASSF restic init -q --repo "$HDD_REPO" >/dev/null; }
rh_verify() { RESTIC_PASSWORD_FILE=$PASSF restic -q --repo "$HDD_REPO" cat config >/dev/null 2>&1; }
step restic-hdd "restic init $HDD_REPO (separate from /ext-pool/backups/restic)" rh_done rh_do rh_verify
rc=$?; [ $rc = 0 ] || stop $rc restic-hdd

# ---- HDD-side key copies (for the --hdd-only restore) -------------------------------------------------
# Passphrase-encrypted (keylocation=prompt) so its key is NOT on rpool; canmount=noauto + unloaded after
# the test, so boot never mounts it or asks for the passphrase. The passphrase is the owner's to escrow.
hk_done() { zfs list -H "$KEYS_DS" >/dev/null 2>&1; }
hk_do() {
  say "   ext-pool/g7/keys gets its OWN passphrase (you type it twice; it is never stored on this box)."
  zfs create -o encryption=aes-256-gcm -o keyformat=passphrase -o keylocation=prompt -o canmount=noauto \
    -o mountpoint="$KEYS_MNT" -o com.sun:auto-snapshot=false "$KEYS_DS" || return 1
}
hk_open() {
  [ "$(zfs get -H -o value keystatus "$KEYS_DS")" = available ] || zfs load-key "$KEYS_DS" || return 1
  [ "$(zfs get -H -o value mounted "$KEYS_DS")" = yes ] || zfs mount "$KEYS_DS"
}
hk_verify() {
  if [ "$PH_DRY" = 1 ]; then say "   WOULD load-key (passphrase), mount, copy g7.key + restic.pass (0400)"; return 0; fi
  hk_open || return 1
  install -m 400 "$G7KEY" "$KEYS_MNT/g7.key" && install -m 400 "$PASSF" "$KEYS_MNT/restic.pass" || return 1
  cmp -s "$G7KEY" "$KEYS_MNT/g7.key" && cmp -s "$PASSF" "$KEYS_MNT/restic.pass"
}
step hdd-keys "encrypted $KEYS_DS (passphrase, canmount=noauto) holding copies of g7.key + restic.pass" \
  hk_done hk_do hk_verify; rc=$?; [ $rc = 0 ] || stop $rc hdd-keys

# ---- backup + verified restores ----------------------------------------------------------------------
bk_done() { [ -s "$BCDIR/dumps/$TODAY/SHA256SUMS" ]; }
bk_do() {
  G7_RESTIC_PASSWORD_FILE=$PASSF "$G7_BACKUP"; local rc=$?
  [ $rc = 75 ] && { say "   g7-backup: G8 WAIT (exit 75)"; return 1; }
  return $rc
}
bk_verify() {
  [ -s "$BCDIR/dumps/$TODAY/STATUS" ] || return 1
  sed 's/^/   /' "$BCDIR/dumps/$TODAY/STATUS"
  ! grep -q ' FAILED' "$BCDIR/dumps/$TODAY/STATUS"
}
step backup "g7-backup: vaultwarden/n8n safe copies, gitea, coordination, both disks, both formats" \
  bk_done bk_do bk_verify; rc=$?; [ $rc = 0 ] || stop $rc backup

vf_rc=1
vf_do() { G7_RESTIC_PASSWORD_FILE=$PASSF "$G7_VERIFY" "$TODAY" 2>&1 | tee "$D/verify.log"; vf_rc=${PIPESTATUS[0]}; }
vf_verify() { [ "$vf_rc" = 0 ]; }
step verify "g7-verify $TODAY: sha256sum -c, integrity_check, ciphers count, restic check --read-data (SSD + HDD)" \
  never vf_do vf_verify; rc=$?; [ $rc = 0 ] || stop $rc verify

vh_rc=1
vh_do() {
  G7_HDD_KEY=$KEYS_MNT/g7.key G7_HDD_RESTIC_PW=$KEYS_MNT/restic.pass "$G7_VERIFY" --hdd-only "$TODAY" 2>&1 \
    | tee "$D/verify-hdd-only.log"; vh_rc=${PIPESTATUS[0]}
}
vh_verify() { [ "$vh_rc" = 0 ]; }
step verify-hdd-only "g7-verify --hdd-only: restore using ONLY HDD-resident key material" never vh_do vh_verify
rc=$?; [ $rc = 0 ] || stop $rc verify-hdd-only

lk_done() { [ "$(zfs get -H -o value keystatus "$KEYS_DS" 2>/dev/null)" = unavailable ]; }
lk_do() { { [ "$(zfs get -H -o value mounted "$KEYS_DS")" = no ] || zfs unmount "$KEYS_DS"; } && zfs unload-key "$KEYS_DS"; }
step lock-hdd-keys "unmount + unload-key $KEYS_DS (locked at rest)" lk_done lk_do lk_done
rc=$?; [ $rc = 0 ] || stop $rc lock-hdd-keys

# ---- escrow paths ------------------------------------------------------------------------------------
ex_done() { grep -qF "|$G7KEY" "$EXTRA" 2>/dev/null && grep -qF "|$PASSF" "$EXTRA" 2>/dev/null; }
ex_do() {
  install -d -m 700 "$(dirname "$EXTRA")" || return 1
  [ -e "$EXTRA" ] || (umask 077; : > "$EXTRA")
  grep -qF "|$G7KEY" "$EXTRA" || echo "G7 ZFS key (t3.sh)|$G7KEY" >> "$EXTRA"
  grep -qF "|$PASSF" "$EXTRA" || echo "G7 restic password (t3.sh)|$PASSF" >> "$EXTRA"
}
step escrow-paths "list g7.key + the G7 restic password in $EXTRA (label|path)" ex_done ex_do ex_done
rc=$?; [ $rc = 0 ] || stop $rc escrow-paths

say ""
say "REMINDER (owner): escrow is NOT done until you re-run the escrow (rotation):"
say "    cd ~/Projektid/_inventory/escrow && sudo bash escrow.sh"
say "  New secrets since the last escrow generation: $G7KEY, $PASSF (listed in $EXTRA)."
say "  The ext-pool/g7/keys PASSPHRASE is not a file: escrow it yourself (it is never stored on this box)."
cap=$(zpool list -H -o cap rpool 2>/dev/null | tr -d %)
[ -n "$cap" ] && [ "$cap" -le 80 ] && say "PASS rpool cap ${cap}% <= 80%" || { say "FAIL rpool cap ${cap:-?}%"; PH_FAILED+=(rpool-cap); }
[ "$PH_DRY" = 1 ] && say "" && say "DRY-RUN complete: nothing was changed."
if [ ${#PH_FAILED[@]} -eq 0 ]; then
  audit "$([ "$PH_DRY" = 1 ] && echo DRY-RUN || echo PASS)" "dumps $BCDIR/dumps/$TODAY; escrow rotation pending"; exit 0
fi
audit FAIL "see $D"; exit 1
