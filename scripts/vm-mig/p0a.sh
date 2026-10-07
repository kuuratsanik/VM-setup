#!/usr/bin/env bash
# p0a.sh [--dry-run] [--force-g8]   OWNER-RUN phase P0a of the VM migration plan rev 9.2 (§6 P0a row,
# §2d host memory, §7b one-time setup, §7c dead-man). Host prep, NO reboot. Run as root:
#   sudo bash scripts/vm-mig/p0a.sh --dry-run     # reads only; prints every step and gate
#   sudo bash scripts/vm-mig/p0a.sh               # asks y/N before every mutating step
# Steps: gates -> intent/lease -> G9 + pre-state -> snapshots -> libvirt under the dead-man (autostart
# link moved aside inside the guarded change) -> rpool/vm, ext-pool/vm -> zfs-replicate SOURCES += rpool/vm
# -> zswap + ARC 2 GiB at runtime + tmpfiles.d -> rpool/backup-critical + `x /var/tmp/vm-mig` -> post-gates
# + host diff -> release -> AUDIT_LOG. Never touches tailscaled, sshd, NetworkManager, resolved/blocky or
# existing ufw rules; never adds rpool/backup-critical to zfs-replicate; no initramfs, no modprobe.d edit.
set -uo pipefail
SELF=$(readlink -f "$0")
. "$(dirname "$SELF")/vm-mig-lib.sh"
. "$(dirname "$SELF")/vm-mig-phase-lib.sh"
PHASE=P0a

LV_LINK=${LV_LINK:-/etc/libvirt/qemu/networks/autostart/default.xml}
ZREP=${ZREP:-/usr/local/sbin/zfs-replicate.sh}
ZSWAP=${ZSWAP:-/sys/module/zswap/parameters}
ARC=${ARC:-/sys/module/zfs/parameters/zfs_arc_max}
TMPFILES=${TMPFILES:-/etc/tmpfiles.d}
G7KEY=${G7KEY:-/root/.config/g7/g7.key}
EXPOSURE_PROM=${EXPOSURE_PROM:-/var/snap/node-exporter/current/textfile/exposure.prom}
ARC_NEW=2147483648

dns53() { ss -lunH 2>/dev/null | awk '{print $4}' | grep -c ':53$'; }
lv_units() { # the daemon(s) the package uses: libvirtd, or virtqemud + virtnetworkd (plan P0a)
  if systemctl cat libvirtd.service >/dev/null 2>&1; then
    LV_SVCS="libvirtd.service"; LV_SOCKS="libvirtd.socket libvirtd-ro.socket libvirtd-admin.socket"
  else
    LV_SVCS="virtqemud.service virtnetworkd.service"
    LV_SOCKS="virtqemud.socket virtqemud-ro.socket virtqemud-admin.socket virtnetworkd.socket virtnetworkd-ro.socket virtnetworkd-admin.socket"
  fi
}

# ---- the ONE guarded change (run by vm-mig-guard under the armed dead-man) -------------------------
if [ "${1:-}" = __libvirt_change ]; then
  lv_units
  if [ -L "$LV_LINK" ] || [ -e "$LV_LINK" ]; then
    mv "$LV_LINK" "$D/default.xml.autostart-link" || exit 1
  fi
  # shellcheck disable=SC2086
  systemctl enable --now $LV_SVCS || exit 1
  sleep "${LV_SETTLE:-5}"
  ok=0
  # shellcheck disable=SC2086
  systemctl is-active --quiet $LV_SVCS && say "PASS libvirt daemon(s) active" || { say "FAIL libvirt not active"; ok=1; }
  info=$(virsh -c qemu:///system net-info default 2>/dev/null)
  if grep -qE '^Active:[[:space:]]+yes' <<<"$info"; then say "FAIL libvirt network 'default' is ACTIVE"; ok=1
  else say "PASS libvirt network 'default' inactive"; fi
  if ip link show virbr0 >/dev/null 2>&1; then say "FAIL virbr0 exists"; ok=1; else say "PASS virbr0 does not exist"; fi
  n=$(dns53); [ "$n" = "$DNS53_BEFORE" ] && say "PASS :53 listeners $n == $DNS53_BEFORE" || { say "FAIL :53 listeners $n != $DNS53_BEFORE"; ok=1; }
  exit $ok
fi

# shellcheck disable=SC2034  # PH_FORCE_G8 is read by g8_gate in vm-mig-phase-lib.sh
for a in "$@"; do
  case $a in
    --dry-run) PH_DRY=1 ;;
    --force-g8) PH_FORCE_G8=1 ;;
    *) echo "usage: p0a.sh [--dry-run] [--force-g8]" >&2; exit 64 ;;
  esac
done

[ "$PH_DRY" = 1 ] || ph_lock "$PHASE"
say "P0a (plan rev 9.2). $( [ "$PH_DRY" = 1 ] && echo 'DRY-RUN: nothing will be changed.' )"
# ---- pre-gates (read-only; a red gate stops before anything is changed) ------------------------------
g8_gate
if "$VMMIG_GUARD" check >/dev/null 2>&1; then say "PASS vm-mig-guard check (reachability)"
else say "RED  vm-mig-guard check: fix reachability/expected.env first"; [ "$PH_DRY" = 1 ] || exit 3; fi
if [ "$PH_DRY" = 0 ]; then
  ph_require_root
  systemctl is-enabled --quiet vm-mig-boot-revert.service || { say "RED  vm-mig-boot-revert.service not enabled (T1 not installed)"; exit 3; }
  for t in vm-mig-guard vm-mig-revert; do   # the dead-man that will run must be the reviewed one
    cmp -s "$VMMIG_BIN/$t" "$(dirname "$SELF")/$t" || { say "RED  installed $VMMIG_BIN/$t != reviewed copy (re-run install.sh)"; exit 3; }
  done
  say "PASS installed vm-mig-guard + vm-mig-revert == reviewed copies"
  [ -z "$(ls -A "$VMMIG_STATE/armed" 2>/dev/null)" ] || { say "RED  something is still armed in $VMMIG_STATE/armed; resolve it first"; exit 3; }
  D=$VMMIG_ROOT/$PHASE; mkdir -p "$D" && chmod 700 "$D"
else
  D=$(mktemp -d)
fi
export D
lv_units
say "libvirt units: $LV_SVCS ($LV_SOCKS)"

trap 'lease_release' EXIT
lease_acquire "$PHASE" libvirt host/libvirt,host/zfs 'host/libvirt,host/zfs,host/sysfs,host/tmpfiles' \
  "VM migration P0a: libvirt enable under dead-man, datasets, zswap/ARC, backup-critical (owner-approved)"

stop() { # rc why
  say ""; say "STOPPED: $2"
  audit "STOPPED ($2)" "rollback per plan P0a row if needed; see $D"
  exit "$1"
}

# ---- G9 + pre-state ------------------------------------------------------------------------------
snapshot_state() { # suffix
  ip -br addr > "$D/ip-addr.$1" 2>/dev/null
  zfs list -H -o name > "$D/zfs-list.$1" 2>/dev/null
  systemctl list-units --type=timer --state=active --no-legend --plain 2>/dev/null | awk '{print $1}' | sort > "$D/timers.$1"
  systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null | awk '{print $1}' | sort > "$D/services.$1"
  ufw status numbered > "$D/ufw.$1" 2>/dev/null
  dns53 > "$D/dns53.$1"
}
# Re-run safe: the FIRST run's artifacts and pre-state are the rollback reference; never overwrite them.
g9_done() { local f; for f in ufw.txt iptables.txt nft.txt exposure.prom dns53.before ufw.before; do [ -s "$D/$f" ] || return 1; done; }
g9_do() {
  ufw status numbered > "$D/ufw.txt" && iptables-save > "$D/iptables.txt" && nft list ruleset > "$D/nft.txt" \
    && cp "$EXPOSURE_PROM" "$D/exposure.prom" && snapshot_state before
}
g9_verify() { local f; for f in ufw.txt iptables.txt nft.txt exposure.prom; do [ -s "$D/$f" ] || { say "   empty: $f"; return 1; }; done; }
if [ "$PH_DRY" = 1 ]; then
  say ""; say "== G9: WOULD save ufw/iptables/nft/exposure.prom + pre-state (ip addr, zfs list, timers, services) to /root/vm-mig/P0a"
else
  step G9 "firewall rollback artifacts + pre-state" g9_done g9_do g9_verify; rc=$?; [ $rc = 0 ] || stop $rc "G9"
fi
if [ -s "$D/dns53.before" ]; then DNS53_BEFORE=$(cat "$D/dns53.before"); else DNS53_BEFORE=$(dns53); fi
export DNS53_BEFORE
say ":53 listeners before: $DNS53_BEFORE (plan expects 5)"

# ---- snapshots -----------------------------------------------------------------------------------
SNAP_DS="rpool/ROOT rpool/var bpool/BOOT"
# shellcheck disable=SC2086
snap_done() { ph_snapshots_complete pre-P0a $SNAP_DS >/dev/null; }
# shellcheck disable=SC2086
snap_do() { ph_snapshot_per_pool pre-P0a $SNAP_DS; }
step snapshots "zfs snapshot -r rpool/ROOT@pre-P0a rpool/var@pre-P0a; then zfs snapshot -r bpool/BOOT@pre-P0a (one atomic snapshot per pool)" \
  snap_done snap_do snap_done; rc=$?; [ $rc = 0 ] || stop $rc snapshots

# ---- libvirt under the dead-man --------------------------------------------------------------------
lv_done() { systemctl is-enabled --quiet $LV_SVCS 2>/dev/null && [ ! -e "$LV_LINK" ] && [ ! -L "$LV_LINK" ]; }
lv_do() {
  local m="$D/libvirt.manifest" rc
  { echo "# P0a libvirt enable: reverse order = disable daemons/sockets, then put the autostart link back"
    if [ -L "$LV_LINK" ] || [ -e "$LV_LINK" ] || [ -L "$D/default.xml.autostart-link" ]; then
      echo "file-restore $D/default.xml.autostart-link $LV_LINK"; fi
    for u in $LV_SOCKS $LV_SVCS; do echo "unit-disable $u"; done; } > "$m"
  say "   arming the dead-man ($VMMIG_DELAY) and running the change; cancel only if every check passes"
  "$VMMIG_GUARD" run P0a "$m" -- "$SELF" __libvirt_change 2>&1 | tee -a "$D/deadman.log"
  rc=${PIPESTATUS[0]}
  case $rc in
    0) say "   dead-man CONFIRMED and cancelled"; return 0 ;;
    2) say "   arming or pre-check failed: no change made"; return 1 ;;
    *) say "   NOT confirmed: the dead-man reverts within $VMMIG_DELAY. Waiting for it (do not intervene)..."
       local _; for _ in $(seq 1 80); do [ -z "$(ls -A "$VMMIG_STATE/armed" 2>/dev/null)" ] && break; sleep 10; done
       if [ -L "$LV_LINK" ] && ! systemctl is-enabled --quiet $LV_SVCS 2>/dev/null; then
         say "   REVERTED: autostart link back, libvirt disabled"; else say "   revert state UNCLEAR: check journalctl -t vm-mig-revert"; fi
       return 1 ;;
  esac
}
lv_verify() {
  # shellcheck disable=SC2086
  systemctl is-active --quiet $LV_SVCS && [ ! -e "$LV_LINK" ] && ! ip link show virbr0 >/dev/null 2>&1 && [ "$(dns53)" = "$DNS53_BEFORE" ]
}
step libvirt "move the default-network autostart link aside + enable --now $LV_SVCS, under vm-mig-guard" \
  lv_done lv_do lv_verify; rc=$?; [ $rc = 0 ] || stop $rc "libvirt (see $D/deadman.log)"

# ---- datasets ------------------------------------------------------------------------------------
ds_done() { zfs list -H rpool/vm >/dev/null 2>&1 && zfs list -H ext-pool/vm >/dev/null 2>&1; }
ds_do() {
  zfs list -H rpool/vm >/dev/null 2>&1 || zfs create -o mountpoint=none rpool/vm || return 1
  zfs list -H ext-pool/vm >/dev/null 2>&1 || zfs create -o mountpoint=none ext-pool/vm
}
step datasets "zfs create rpool/vm ext-pool/vm (containers for zvols, mountpoint=none)" ds_done ds_do ds_done
rc=$?; [ $rc = 0 ] || stop $rc datasets

# ---- zfs-replicate SOURCES += rpool/vm (never rpool/backup-critical) -------------------------------
zr_done() { grep -qE '^SOURCES="[^"]*\<rpool/vm\>' "$ZREP"; }
zr_do() {
  local bak; bak="$ZREP.bak-$(date +%Y%m%d%H%M%S)"
  [ "$(grep -c '^SOURCES="' "$ZREP")" = 1 ] || { say "   expected exactly one SOURCES= line"; return 1; }
  cp -a "$ZREP" "$bak" || return 1; say "   backup: $bak"
  sed -i -E 's|^SOURCES="([^"]*)"$|SOURCES="\1 rpool/vm"|' "$ZREP" || return 1
  diff "$bak" "$ZREP" | sed 's/^/   /'
  bash -n "$ZREP" || { say "   syntax error: restoring the backup"; cp -a "$bak" "$ZREP.tmp" && mv "$ZREP.tmp" "$ZREP"; return 1; }
}
zr_verify() { zr_done && ! grep -q 'backup-critical' "$ZREP" && bash -n "$ZREP"; }
step zfs-replicate "add rpool/vm to SOURCES in $ZREP (.bak-<ts> first)" zr_done zr_do zr_verify
rc=$?; [ $rc = 0 ] || stop $rc zfs-replicate

# ---- zswap + ARC at runtime, persisted with tmpfiles.d (no initramfs, no reboot) -----------------
mem_done() {
  [ "$(cat "$ZSWAP/enabled")" = Y ] && [ "$(cat "$ARC")" = $ARC_NEW ] \
    && [ -s "$TMPFILES/vm-mig-zswap.conf" ] && [ -s "$TMPFILES/vm-mig-arc.conf" ]
}
mem_do() {
  [ -s "$D/memory.before" ] || { echo "zswap.enabled=$(cat "$ZSWAP/enabled") zswap.compressor=$(cat "$ZSWAP/compressor")" \
         "zswap.max_pool_percent=$(cat "$ZSWAP/max_pool_percent") zfs_arc_max=$(cat "$ARC")"; } > "$D/memory.before"
  say "   rollback values in $D/memory.before (first run's values are kept on a re-run)"
  echo zstd > "$ZSWAP/compressor" && echo 20 > "$ZSWAP/max_pool_percent" && echo Y > "$ZSWAP/enabled" || return 1
  echo $ARC_NEW > "$ARC" || return 1
  printf '%s\n' "# VM migration P0a (plan §2d): zswap at runtime, no bootloader change" \
    "w /sys/module/zswap/parameters/compressor - - - - zstd" \
    "w /sys/module/zswap/parameters/max_pool_percent - - - - 20" \
    "w /sys/module/zswap/parameters/enabled - - - - Y" > "$TMPFILES/vm-mig-zswap.conf"
  printf '%s\n' "# VM migration P0a (plan §2d): ARC 2 GiB at runtime, applied every boot after the module loads" \
    "w /sys/module/zfs/parameters/zfs_arc_max - - - - $ARC_NEW" > "$TMPFILES/vm-mig-arc.conf"
}
mem_verify() { mem_done && [ "$(cat "$ZSWAP/compressor")" = zstd ]; }
step memory "zswap (zstd, 20 %, on) + zfs_arc_max=2 GiB via sysfs + tmpfiles.d" mem_done mem_do mem_verify
rc=$?; [ $rc = 0 ] || stop $rc memory

# ---- rpool/backup-critical (§7b one-time setup) + `x /var/tmp/vm-mig` ------------------------------
bc_done() { zfs list -H rpool/backup-critical >/dev/null 2>&1 && grep -qx 'x /var/tmp/vm-mig' "$TMPFILES/vm-mig.conf" 2>/dev/null; }
bc_do() {
  if ! zfs list -H rpool/backup-critical >/dev/null 2>&1; then
    if [ -e "$G7KEY" ] && [ ! -e "$D/g7.key.created-by-p0a" ]; then
      say "   $G7KEY already exists but the dataset does not, and this phase did not create it: owner decides (never overwritten)"; return 1
    fi
    if [ ! -e "$G7KEY" ]; then   # created once; a re-run after a failed zfs create reuses this run's key
      install -d -m 700 "$(dirname "$G7KEY")" && (umask 077; dd if=/dev/urandom of="$G7KEY" bs=32 count=1 status=none) \
        && chmod 400 "$G7KEY" && touch "$D/g7.key.created-by-p0a" || return 1
    fi
    zfs create -o encryption=aes-256-gcm -o keyformat=raw -o keylocation="file://$G7KEY" \
      -o mountpoint=/srv/backup-critical -o compression=zstd -o quota=12G rpool/backup-critical || return 1
  fi
  printf '%s\n' "# VM migration (plan §7a/P0a): never age-clean the staging copies" "x /var/tmp/vm-mig" > "$TMPFILES/vm-mig.conf"
  install -d -m 700 "${VMTMP:-/var/tmp/vm-mig}"
}
bc_verify() {
  [ "$(zfs get -H -o value encryption rpool/backup-critical)" = aes-256-gcm ] \
    && [ "$(zfs get -H -o value keystatus rpool/backup-critical)" = available ] \
    && [ "$(zfs get -H -o value mounted rpool/backup-critical)" = yes ] \
    && [ "$(zfs get -Hp -o value quota rpool/backup-critical)" = 12884901888 ] \
    && [ "$(stat -c %a "$G7KEY")" = 400 ] && grep -qx 'x /var/tmp/vm-mig' "$TMPFILES/vm-mig.conf"
}
step backup-critical "g7.key (root 0400, never shown) + encrypted rpool/backup-critical (quota 12G) + tmpfiles x /var/tmp/vm-mig" \
  bc_done bc_do bc_verify; rc=$?; [ $rc = 0 ] || stop $rc backup-critical

# ---- post-gates + host diff ------------------------------------------------------------------------
say ""; say "== post-gates"
chk() { [ "$PH_DRY" = 1 ] && { say "WOULD check: $1"; return 0; }
  if eval "$2"; then say "PASS $1"; else say "FAIL $1"; PH_FAILED+=("post:$1"); fi; }
# shellcheck disable=SC2016
{
chk "libvirt daemon active" 'systemctl is-active --quiet $LV_SVCS'
chk "zfs list rpool/vm ext-pool/vm rpool/backup-critical" 'zfs list -H rpool/vm ext-pool/vm rpool/backup-critical >/dev/null 2>&1'
chk "zfs_arc_max = 2147483648" '[ "$(cat "$ARC")" = $ARC_NEW ]'
chk "zswap enabled" '[ "$(cat "$ZSWAP/enabled")" = Y ]'
chk "libvirt default network inactive" '! grep -qE "^Active:[[:space:]]+yes" <<<"$(virsh -c qemu:///system net-info default 2>/dev/null)"'
chk "virbr0 does not exist" '! ip link show virbr0 >/dev/null 2>&1'
chk ":53 listener count unchanged ($DNS53_BEFORE)" '[ "$(dns53)" = "$DNS53_BEFORE" ]'
chk "zfs-replicate SOURCES has rpool/vm, not backup-critical" 'zr_verify'
chk "rpool cap <= 80%" '[ "$(zpool list -H -o cap rpool | tr -d %)" -le 80 ]'
chk "vm-mig-guard check (reachability)" '"$VMMIG_GUARD" check >/dev/null 2>&1'
chk "nothing left armed" '[ -z "$(ls -A "$VMMIG_STATE/armed" 2>/dev/null)" ]'
}
say "DEFERRED (next zfs-replicate run): zfs list -r ext-pool/replica | grep -c rpool_vm  -> >= 1"
if [ "$PH_DRY" = 0 ]; then
  snapshot_state after
  chk "existing ufw rules unchanged" 'diff -q "$D/ufw.before" "$D/ufw.after" >/dev/null'
  say ""; say "== host diff (before -> after)"
  for f in ip-addr zfs-list timers services ufw dns53; do
    say "-- $f"; diff "$D/$f.before" "$D/$f.after" | sed 's/^/   /' || true
  done
fi

if [ ${#PH_FAILED[@]} -eq 0 ]; then
  [ "$PH_DRY" = 1 ] && say "" && say "DRY-RUN complete: nothing was changed."
  audit "$([ "$PH_DRY" = 1 ] && echo DRY-RUN || echo PASS)" "no rollback. Artifacts: $D. Rollback per plan P0a row (N=7)."
  exit 0
fi
audit "POST-GATE FAIL" "owner decides rollback per plan P0a row; artifacts in $D"
exit 1
