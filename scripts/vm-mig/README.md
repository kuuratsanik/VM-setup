# vm-mig tools (VM migration plan rev 9.2: T1 §7c, T2 §7a, T3 §7b)

Installed by the owner with `install.sh` after review (human-approved PR). Agents don't run it.

| Tool | Purpose |
|---|---|
| `vm-mig-guard` | dead-man around one network addition: pre-check, arm, change, check, confirm |
| `vm-mig-revert` | executes a per-change manifest (fixed vocabulary, reverse order, idempotent) |
| `vm-mig-boot-revert` + `.service.in` | reverts unconfirmed markers at boot; retries 3x on failure |
| `vm-mig-freeze` / `vm-mig-resume` | allowlist freeze and exact restore of timers, cron, agent daemons, snap hold |
| `g7-backup` / `g7-verify` | critical-set backup on both disks, and its verified restore |

## Rules worth knowing

- `/var/lib/vm-mig/expected.env` needs `TS_IP`, `ROUTE_DEV` and `HTTPS_PATH`. `HTTPS_PATH` must be a
  tailscale-served path answering 2xx/3xx. Services move between phases, so **re-check it before
  every phase**. `vm-mig-guard run` refuses (exit 2, no change) while the pre-check is red.
- `vm-mig-freeze` **refuses if a recording already exists** for the phase (exit 3). The first
  recording is the only one that knows what was running before the window.
- **After a reboot mid-phase** (power cut, plan §5 R10 step 8): the freeze does not survive a reboot
  (units re-enable at boot). Run **`vm-mig-resume <phase>`, then `vm-mig-freeze <phase>`** and its
  gate, before continuing. Resume restores and verifies against the original recording and, only
  when the verify is green, renames it to `<phase>.resumed-<ts>` (never deleted), so the new freeze
  can record. If the resume verify is red, the recording stays and freeze keeps refusing: stop.
- `vm-mig-resume` starts Persistent=yes timers last, with a random 0..`VMMIG_STAGGER` s (default 30)
  pause each, because a missed slot fires immediately on start.
- `g7-verify` treats `G7_TEST_DIR` as an existing **parent** directory (tmpfs or inside
  `rpool/backup-critical`). It works in its own `mktemp -d` child and removes only that.
