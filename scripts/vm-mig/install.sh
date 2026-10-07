#!/usr/bin/env bash
# install.sh [--print-unit]: install the vm-mig tools into /usr/local/sbin and enable
# vm-mig-boot-revert.service. Root only. Run by the owner after review (human-approved PR);
# the agents do not run it. --print-unit only renders the unit (no root, changes nothing).
#
# The unit is NOT ordered before tailscaled (a hung revert must never withhold the remote path).
set -euo pipefail
here=$(dirname "$(readlink -f "$0")")
if [ "${1:-}" = --print-unit ]; then cat "$here/vm-mig-boot-revert.service.in"; exit 0; fi
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
install -d -m 755 /usr/local/sbin
install -d -m 700 /var/lib/vm-mig /var/lib/vm-mig/armed /var/lib/vm-mig/manifests /root/vm-mig
for f in vm-mig-lib.sh vm-mig-phase-lib.sh vm-mig-revert vm-mig-guard vm-mig-boot-revert vm-mig-freeze vm-mig-resume g7-backup g7-verify; do
  install -m 755 "$here/$f" /usr/local/sbin/
done
install -m 644 "$here/vm-mig-boot-revert.service.in" /etc/systemd/system/vm-mig-boot-revert.service
# HTTPS_PATH: a tailscale-served path that answered 2xx/3xx when probed read-only on 2026-10-05
# (curl --resolve <dns>:443:100.126.113.62 https://<dns>/code -> 302; code-server stays on the host, §2a,
# and is not a freeze target). Services move between phases: RE-CHECK THIS PATH BEFORE EVERY PHASE.
[ -e /var/lib/vm-mig/expected.env ] || printf 'TS_IP=100.126.113.62\nROUTE_DEV=eno1\nHTTPS_PATH=/code\n' > /var/lib/vm-mig/expected.env
systemctl daemon-reload
systemctl enable vm-mig-boot-revert.service
# §7c static checks
systemd-analyze verify /etc/systemd/system/vm-mig-boot-revert.service
systemctl is-enabled vm-mig-boot-revert.service
systemctl list-dependencies --after vm-mig-boot-revert.service | grep -c -E 'libvirtd|ufw'
