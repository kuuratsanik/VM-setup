"""Tests for scripts/vm-mig (VM migration plan rev 9.1, §7a/§7c).

Every host command (systemctl, systemd-run, ufw, nft, virsh, tailscale, ss, ip, curl, logger, snap)
is a shim on PATH backed by files in a scratch dir, so nothing touches the real host. The "timer"
is a file; `fire()` runs its command the way systemd would when --on-active expires.
"""
import os
import signal
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "vm-mig"

SHIMS = {
    "systemd-run": r"""
        unit=""; delay=""; args=()
        for a in "$@"; do case $a in --user) ;; --unit=*) unit=${a#--unit=} ;; --on-active=*) delay=${a#--on-active=} ;;
          --timer-property=*|--setenv=*) ;; *) args+=("$a") ;; esac; done
        [[ $delay =~ ^[0-9]+(ms|s|min|h)?$ ]] || { echo "Failed to parse timer value: $delay" >&2; exit 1; }
        [ -n "${SHIM_RUN_FAIL:-}" ] && exit 1
        mkdir -p "$S/timers"; printf '%q ' "${args[@]}" > "$S/timers/$unit"; echo "run $unit" >> "$S/log"
    """,
    "systemctl": r"""
        A=active; [ "${1:-}" = --user ] && { shift; A=active.user; [ -n "${SHIM_USER_FAIL:-}" ] && exit 1; }
        echo "systemctl $*" >> "$S/log"
        case $1 in
          is-active) shift; q=0; [ "$1" = --quiet ] && { q=1; shift; }; [ $q = 1 ] && exec >/dev/null
            for u in "$@"; do case $u in *.timer) [ -e "$S/timers/${u%.timer}" ] && echo active || { echo inactive; r=3; } ;;
              *) grep -qx "$u" "$S/$A" 2>/dev/null && echo active || { echo inactive; r=3; } ;; esac; done; exit ${r:-0} ;;
          stop) shift; for u in "$@"; do rm -f "$S/timers/${u%.timer}"; sed -i "\|^$u\$|d" "$S/$A" 2>/dev/null; done ;;
          start) shift; for u in "$@"; do grep -qx "$u" "$S/startfail" 2>/dev/null && exit 1; grep -qx "$u" "$S/$A" 2>/dev/null || echo "$u" >> "$S/$A"; done ;;
          list-units) f="$S/$A"; [ -e "$f" ] || exit 0
            if printf '%s ' "$@" | grep -q 'type=timer'; then grep '\.timer$' "$f" | sed 's/$/ loaded active waiting x/'
            elif printf '%s ' "$@" | grep -q 'type=service'; then grep '\.service$' "$f" | sed 's/$/ loaded active running x/'; fi ;;
          show) u=${!#}; case "$*" in *ExecStart*) cat "$S/exec/$u" 2>/dev/null ;; *FragmentPath*) cat "$S/frag/$u" 2>/dev/null ;;
            *Persistent*) grep -qx "$u" "$S/persistent" 2>/dev/null && echo yes || echo no ;; esac ;;
          list-timers) cat "$S/$A" 2>/dev/null ;;
        esac; exit 0
    """,
    "ufw": r"""
        echo "ufw $*" >> "$S/log"
        if [ "$1" = --force ] && [ "$2" = delete ]; then shift 2; r="$*"
          grep -qxF "$r" "$UFWDIR/user.rules" 2>/dev/null || { echo "Could not delete non-existent rule"; exit 0; }
          grep -vxF "$r" "$UFWDIR/user.rules" > "$UFWDIR/.t"; mv "$UFWDIR/.t" "$UFWDIR/user.rules"; fi
        exit 0
    """,
    "nft": r"""
        case "$*" in "list ruleset") cat "$S/nft" ;; "-a list chain"*) cat "$S/nftchain" 2>/dev/null ;;
          "delete rule"*) echo "nft $*" >> "$S/log"; : > "$S/nftchain" ;; esac
    """,
    "virsh": r"""
        echo "virsh $*" >> "$S/log"
        case $1 in
          net-info) [ -e "$S/nets/$2" ] || exit 1; echo "Active:         $(cat "$S/nets/$2")" ;;
          net-dhcp-leases) echo hdr; echo ---; cat "$S/leases/$2" 2>/dev/null ;;
          list) cat "$S/doms" 2>/dev/null ;;
          domiflist) echo hdr; echo ---; cat "$S/domif/$2" 2>/dev/null ;;
          net-destroy) echo no > "$S/nets/$2" ;;
          net-undefine) rm -f "$S/nets/$2" ;;
        esac
    """,
    "tailscale": r"""cat "$S/ts.json" """,
    "ss": r"""cat "$S/ss" """,
    "ip": r"""echo "1.1.1.1 via 192.168.88.1 dev eno1 src 192.168.88.246" """,
    "curl": r"""echo 200""",
    "logger": r"""shift 2; [ "$1" = -- ] && shift; echo "$*" >> "$S/journal" """,
    "snap": r"""echo "snap $*" >> "$S/log"; [ "$1 $2" = "get system" ] && cat "$S/snaphold" 2>/dev/null; exit 0""",
    "crontab": r"""cat "$S/crontab" 2>/dev/null""",
    "pgrep": r"""exit 1""",
    "pg_restore": r"""exit 0""",
    "zfs": r"""echo "zfs $*" >> "$S/log"; exit 1""",
    "restic": r"""echo "restic $*" >> "$S/log"; exit 1""",
    "runuser": r"""shift 3; exec "$@" """,
}


@pytest.fixture
def env(tmp_path):
    s = tmp_path / "shim"
    (s / "bin").mkdir(parents=True)
    for name, body in SHIMS.items():
        p = s / "bin" / name
        p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
        p.chmod(0o755)
    ufw = tmp_path / "etc-ufw"
    ufw.mkdir()
    (ufw / "user.rules").write_text("allow 22/tcp\n")
    (ufw / "ufw.conf").write_text("ENABLED=yes\n")
    (s / "nft").write_text("table inet filter { }\n")
    (s / "ts.json").write_text('{"Self":{"Online":true,"DNSName":"m93p.example.ts.net."},"BackendState":"Running"}')
    (s / "ss").write_text("LISTEN 0 128 100.126.113.62:22 0.0.0.0:*\n")
    (s / "nets").mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "expected.env").write_text("TS_IP=100.126.113.62\nROUTE_DEV=eno1\nHTTPS_PATH=/code\n")
    bus = tmp_path / "bus.jsonl"
    bus.write_text("")
    e = dict(os.environ)
    e.update(
        PATH=f"{s / 'bin'}:{os.environ['PATH']}", S=str(s), UFWDIR=str(ufw),
        VMMIG_STATE=str(state), VMMIG_ROOT=str(tmp_path / "root"), VMMIG_UFW_DIR=str(ufw),
        VMMIG_BUS=str(bus), VMMIG_REVERT=str(SCRIPTS / "vm-mig-revert"), VMMIG_DELAY="10min", VMMIG_STAGGER="0",
    )
    return {"env": e, "tmp": tmp_path, "shim": s, "state": state, "ufw": ufw, "bus": bus}


def run(env, *args, check=False):
    return subprocess.run([str(SCRIPTS / args[0]), *args[1:]], env=env["env"], capture_output=True,
                          text=True, check=check, timeout=60)


def manifest(env, text):
    p = env["tmp"] / f"m{time.monotonic_ns()}.src"
    p.write_text(textwrap.dedent(text))
    return str(p)


def armed(env):
    d = env["state"] / "armed"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def fire(env, unit):
    """What systemd does when the dead-man timer elapses."""
    t = env["shim"] / "timers" / unit
    cmd = t.read_text()
    t.unlink()
    return subprocess.run(["bash", "-c", cmd], env=env["env"], capture_output=True, text=True, timeout=60)


def journal(env):
    j = env["shim"] / "journal"
    return j.read_text() if j.exists() else ""


# ---------------------------------------------------------------- vm-mig-revert

def test_revert_refuses_unknown_action_before_executing_anything(env):
    proof = env["tmp"] / "touched"
    m = env["tmp"] / "x.list"
    m.write_text(f"noop-touch {proof}\nrm-rf /\n")
    r = run(env, "vm-mig-revert", str(m))
    assert r.returncode == 3
    assert not proof.exists()


@pytest.mark.parametrize("line", ["unit-disable snap.tailscale.tailscaled.service", "unit-disable ssh.service",
                                  "unit-disable NetworkManager.service", "nft-delete inet filter input notanumber",
                                  "file-restore onlyone", "noop-touch"])
def test_revert_refuses_forbidden_or_malformed(env, line):
    m = env["tmp"] / "x.list"
    m.write_text(line + "\n")
    assert run(env, "vm-mig-revert", "--validate", str(m)).returncode == 3


def test_revert_reverse_order_and_idempotent(env):
    order = env["tmp"] / "order"
    a, b = env["tmp"] / "a", env["tmp"] / "b"
    m = env["tmp"] / "vm-mig-deadman-T1-x.list"
    m.write_text(f"noop-touch {a}\nnoop-touch {b}\n")
    assert run(env, "vm-mig-revert", str(m)).returncode == 0
    j = journal(env)
    assert j.index(str(b)) < j.index(str(a))
    assert run(env, "vm-mig-revert", str(m)).returncode == 0  # second run: still fine


def test_revert_skips_when_confirmed(env):
    U = "vm-mig-deadman-T1-c"
    (env["state"] / "armed").mkdir()
    (env["state"] / "armed" / f"{U}.confirmed").write_text("")
    p = env["tmp"] / "p"
    m = env["tmp"] / f"{U}.list"
    m.write_text(f"noop-touch {p}\n")
    assert run(env, "vm-mig-revert", str(m)).returncode == 0
    assert not p.exists()


def test_ufw_restore_skipped_when_no_diff(env):
    saved = env["tmp"] / "saved"
    subprocess.run(["cp", "-a", str(env["ufw"]), str(saved)], check=True)
    m = env["tmp"] / "u.list"
    m.write_text(f"ufw-restore {saved}\n")
    assert run(env, "vm-mig-revert", str(m)).returncode == 0
    log = (env["shim"] / "log").read_text() if (env["shim"] / "log").exists() else ""
    assert "ufw reload" not in log
    assert "copy and reload skipped" in journal(env)


def test_ufw_restore_restores_exactly_and_reloads(env):
    saved = env["tmp"] / "saved"
    subprocess.run(["cp", "-a", str(env["ufw"]), str(saved)], check=True)
    (env["ufw"] / "user.rules").write_text("allow 22/tcp\nroute allow in on virbr-svc out on eno1\n")
    (env["ufw"] / "extra.rules").write_text("new\n")
    m = env["tmp"] / "u.list"
    m.write_text(f"ufw-restore {saved}\n")
    assert run(env, "vm-mig-revert", str(m)).returncode == 0
    assert subprocess.run(["diff", "-r", str(saved), str(env["ufw"])]).returncode == 0
    assert "ufw reload" in (env["shim"] / "log").read_text()


def test_net_destroy_refused_with_running_domain(env):
    (env["shim"] / "nets" / "svc-net").write_text("yes")
    (env["shim"] / "doms").write_text("rehearse\n")
    (env["shim"] / "domif").mkdir()
    (env["shim"] / "domif" / "rehearse").write_text("vnet0 network svc-net virtio 52:54:00:00:00:01\n")
    m = env["tmp"] / "vm-mig-deadman-P0b-n.list"
    m.write_text("net-destroy svc-net\n")
    (env["state"] / "armed").mkdir()
    (env["state"] / "armed" / "vm-mig-deadman-P0b-n").write_text(str(m))
    assert run(env, "vm-mig-revert", str(m)).returncode == 1
    assert (env["shim"] / "nets" / "svc-net").exists()
    assert "vm-mig-deadman-P0b-n" in armed(env)  # kept so boot-revert retries


def test_net_destroy_absent_is_noop(env):
    m = env["tmp"] / "n.list"
    m.write_text("net-destroy svc-net\n")
    assert run(env, "vm-mig-revert", str(m)).returncode == 0


# ---------------------------------------------------------------- vm-mig-guard (proof + drills)

def test_proof1_noop_arm_not_cancelled_fires(env):
    target = env["state"] / "proof-timer"
    U = run(env, "vm-mig-guard", "arm", "T1", manifest(env, f"noop-touch {target}\n")).stdout.strip()
    assert U.startswith("vm-mig-deadman-T1-") and U in armed(env)
    r = fire(env, U)
    assert r.returncode == 0, r.stderr
    assert target.exists() and armed(env) == []
    assert journal(env).count("revert complete") == 1


def test_proof2_boot_path(env):
    target = env["state"] / "proof-boot"
    m = env["state"] / "manifests"
    m.mkdir()
    (m / "vm-mig-deadman-T1-boot.list").write_text(f"noop-touch {target}\n")
    (env["state"] / "armed").mkdir()
    (env["state"] / "armed" / "vm-mig-deadman-T1-boot").write_text(str(m / "vm-mig-deadman-T1-boot.list"))
    assert run(env, "vm-mig-boot-revert").returncode == 0
    assert target.exists() and armed(env) == []


def test_proof3_arming_failure_exit2_no_change(env):
    env["env"]["VMMIG_DELAY"] = "ten-minutes"
    flag = env["tmp"] / "changed"
    r = run(env, "vm-mig-guard", "run", "T1", manifest(env, "noop-touch /dev/null\n"), "--", "touch", str(flag))
    assert r.returncode == 2 and "arming failed: ABORT, no change made" in r.stderr
    assert not flag.exists() and armed(env) == []


def test_unique_unit_names(env):
    src = manifest(env, "noop-touch /dev/null\n")
    names = {run(env, "vm-mig-guard", "arm", "T1", src).stdout.strip() for _ in range(3)}
    assert len(names) == 3


def _svcnet_change(env):
    """The drill change: a UFW route rule + svc-net, as P0b would make."""
    return ["bash", "-c", f"echo 'route allow in on virbr-svc out on eno1' >> {env['ufw']}/user.rules;"
                          f" echo yes > {env['shim']}/nets/svc-net"]


def test_drill1_failed_checks_dead_man_reverts(env):
    nft_before = (env["shim"] / "nft").read_text()
    src = manifest(env, "ufw-restore @P@/etc-ufw\nnet-destroy svc-net\n")
    chg = _svcnet_change(env)
    chg[-1] += f"; echo TS_IP=100.126.113.99 > {env['state']}/expected.env"  # checks deliberately failed after the change
    r = run(env, "vm-mig-guard", "run", "P0b", src, "--", *chg)
    assert r.returncode == 1
    (U,) = armed(env)
    assert fire(env, U).returncode == 0
    P = env["tmp"] / "root" / "P0b" / U
    assert subprocess.run(["diff", "-r", str(P / "etc-ufw"), str(env["ufw"])]).returncode == 0
    assert not (env["shim"] / "nets" / "svc-net").exists()
    assert (env["shim"] / "nft").read_text() == nft_before == (P / "nft.before").read_text()
    assert armed(env) == []


def test_drill2_arming_process_killed_timer_still_fires(env):
    src = manifest(env, "ufw-restore @P@/etc-ufw\nnet-destroy svc-net\n")
    chg = ["bash", "-c", f"echo 'route allow x' >> {env['ufw']}/user.rules; echo yes > {env['shim']}/nets/svc-net;"
                         " sleep 30"]
    p = subprocess.Popen([str(SCRIPTS / "vm-mig-guard"), "run", "P0b", src, "--", *chg], env=env["env"],
                         start_new_session=True)
    for _ in range(100):
        if armed(env) and (env["shim"] / "nets" / "svc-net").exists():
            break
        time.sleep(0.1)
    os.killpg(p.pid, signal.SIGKILL)
    p.wait()
    (U,) = armed(env)
    assert fire(env, U).returncode == 0
    assert not (env["shim"] / "nets" / "svc-net").exists()
    assert "route allow x" not in (env["ufw"] / "user.rules").read_text()


def test_drill3_power_cut_boot_revert(env):
    src = manifest(env, "ufw-restore @P@/etc-ufw\nnet-destroy svc-net\n")
    U = run(env, "vm-mig-guard", "arm", "P0b", src).stdout.strip()
    subprocess.run(_svcnet_change(env), check=True)
    (env["shim"] / "timers" / U).unlink()  # the transient timer is lost in the cut; marker stays
    assert run(env, "vm-mig-boot-revert").returncode == 0
    assert not (env["shim"] / "nets" / "svc-net").exists() and armed(env) == []


def test_drill3b_confirmed_is_not_reverted_at_boot(env):
    src = manifest(env, "ufw-restore @P@/etc-ufw\nnet-destroy svc-net\n")
    U = run(env, "vm-mig-guard", "arm", "P0b", src).stdout.strip()
    subprocess.run(_svcnet_change(env), check=True)
    (env["state"] / "armed" / f"{U}.confirmed").write_text("")  # confirmed, cut before the marker removal
    (env["shim"] / "timers" / U).unlink()
    assert run(env, "vm-mig-boot-revert").returncode == 0
    assert (env["shim"] / "nets" / "svc-net").exists()  # change still active
    assert "virbr-svc" in (env["ufw"] / "user.rules").read_text()
    assert armed(env) == []


def test_timer_firing_between_confirm_and_stop_is_noop(env):
    src = manifest(env, "net-destroy svc-net\n")
    U = run(env, "vm-mig-guard", "arm", "P0b", src).stdout.strip()
    (env["shim"] / "nets" / "svc-net").write_text("yes")
    (env["state"] / "armed" / f"{U}.confirmed").write_text("")
    assert fire(env, U).returncode == 0
    assert (env["shim"] / "nets" / "svc-net").exists()


def test_drill4_overlapping_manifests_only_second_reverted(env):
    (env["shim"] / "nets" / "svc-net").write_text("yes")
    (env["shim"] / "doms").write_text("rehearse\n")
    U1 = run(env, "vm-mig-guard", "arm", "P0b", manifest(env, "net-destroy svc-net\n")).stdout.strip()
    (env["state"] / "armed" / f"{U1}.confirmed").write_text("")
    assert run(env, "vm-mig-guard", "confirm", U1).returncode == 0
    (env["shim"] / "nftchain").write_text("  iifname eno1 udp dport 1514 dnat to 10.20.0.10 # handle 42\n")
    U2 = run(env, "vm-mig-guard", "arm", "P4", manifest(env, "nft-delete ip nat PREROUTING 42\n")).stdout.strip()
    assert U1 != U2
    assert fire(env, U2).returncode == 0
    assert (env["shim"] / "nftchain").read_text() == ""          # DNAT gone
    assert (env["shim"] / "nets" / "svc-net").read_text() == "yes"  # svc-net untouched
    assert "net-destroy" not in (env["shim"] / "log").read_text()


def test_guard_success_confirms_and_cancels(env):
    U_change = env["tmp"] / "c"
    r = run(env, "vm-mig-guard", "run", "T1", manifest(env, "noop-touch /dev/null\n"), "--", "touch", str(U_change))
    assert r.returncode == 0, r.stderr
    assert armed(env) == [] and not list((env["shim"] / "timers").iterdir())


def test_boot_revert_no_markers_is_noop(env):
    assert run(env, "vm-mig-boot-revert").returncode == 0


# ---------------------------------------------------------------- vm-mig-freeze / resume (dry-run + real-mode on shims)

def _freeze_env(env, tmp):
    s = env["shim"]
    (s / "active").write_text("\n".join([
        "logrotate.timer", "systemd-tmpfiles-clean.timer", "zfs-replicate.timer", "sshd-config-check.timer",
        "anacron.timer", "cron.service", "ops-bot.service", "writer.service", "blocky.service"]) + "\n")
    code = tmp / "ops-bot.py"
    code.write_text("import subprocess\nsubprocess.run(['systemctl', 'restart', x])\n")
    passive = tmp / "writer.py"
    passive.write_text("print(open('/etc/hostname').read())\n")
    (s / "exec").mkdir()
    (s / "exec" / "ops-bot.service").write_text(f"{{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 {code} ; }}")
    (s / "exec" / "writer.service").write_text(f"{{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 {passive} ; }}")
    (s / "crontab").write_text("0 * * * * true\n")


def test_freeze_dry_run_changes_nothing(env):
    _freeze_env(env, env["tmp"])
    env["env"]["VMMIG_DRY_DIR"] = str(env["tmp"] / "dry")
    before = (env["shim"] / "active").read_text()
    r = run(env, "vm-mig-freeze", "T2", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert (env["shim"] / "active").read_text() == before
    assert "zfs-replicate.timer" in r.stdout and "sshd-config-check.timer" in r.stdout
    assert "logrotate.timer\n" not in r.stdout.split("WOULD stop")[1]
    assert "ops-bot.service" in (env["tmp"] / "dry" / "agents.found").read_text()
    assert "writer.service" in (env["tmp"] / "dry" / "agents.unproven").read_text()


def test_freeze_then_resume_roundtrip(env):
    _freeze_env(env, env["tmp"])
    (env["shim"] / "snaphold").write_text("")
    before = sorted((env["shim"] / "active").read_text().split())
    r = run(env, "vm-mig-freeze", "T2", "--window-end", "2026-10-06T04:00:00Z")
    left = sorted((env["shim"] / "active").read_text().split())
    assert "zfs-replicate.timer" not in left and "ops-bot.service" not in left and "cron.service" not in left
    assert "logrotate.timer" in left and "writer.service" in left
    assert "gate FAIL snap" in r.stderr  # shim snap never sets a hold: the gate must notice
    r = run(env, "vm-mig-resume", "T2")
    assert "verify PASS system timers" in r.stderr, r.stderr
    after = sorted((env["shim"] / "active").read_text().split())
    assert set(before) <= set(after)
    assert "snap refresh --unhold" in (env["shim"] / "log").read_text()


def test_resume_refuses_without_recording(env):
    assert run(env, "vm-mig-resume", "T9").returncode == 2


def test_shellcheck_clean():
    sc = subprocess.run(["bash", "-c", "command -v shellcheck"], capture_output=True)
    if sc.returncode:
        pytest.skip("shellcheck not installed")
    files = [str(p) for p in SCRIPTS.iterdir() if p.suffix in ("", ".sh") and p.is_file()]
    r = subprocess.run(["shellcheck", "-x", "-S", "warning", *files], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_print_unit_never_withholds_tailscaled():
    r = subprocess.run([str(SCRIPTS / "install.sh"), "--print-unit"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "After=local-fs.target libvirtd.service ufw.service snap.docker.dockerd.service" in r.stdout
    assert not [l for l in r.stdout.splitlines() if l.startswith("Before=")]
    assert "TimeoutStartSec=300" in r.stdout and "ExecStart=/usr/bin/timeout 240 " in r.stdout


def test_install_seeds_https_path():
    assert "HTTPS_PATH=/code" in (SCRIPTS / "install.sh").read_text()


# ---------------------------------------------------------------- adversary-review regressions

def test_run_precheck_red_exits2_without_arming_or_change(env):
    (env["state"] / "expected.env").write_text("TS_IP=100.126.113.62\nROUTE_DEV=eno1\n")  # HTTPS_PATH missing
    flag = env["tmp"] / "changed"
    r = run(env, "vm-mig-guard", "run", "T1", manifest(env, "noop-touch /dev/null\n"), "--", "touch", str(flag))
    assert r.returncode == 2 and "pre-check failed" in r.stderr
    assert not flag.exists() and armed(env) == [] and not (env["shim"] / "timers").exists()
    assert "HTTPS_PATH not set" in r.stderr


def test_confirm_refused_while_revert_holds_lock(env):
    U = run(env, "vm-mig-guard", "arm", "T1", manifest(env, "noop-touch /dev/null\n")).stdout.strip()
    holder = subprocess.Popen(["flock", str(env["state"] / "revert.lock"), "sleep", "20"])
    try:
        for _ in range(50):
            if subprocess.run(["flock", "-n", str(env["state"] / "revert.lock"), "true"]).returncode:
                break
            time.sleep(0.05)
        r = run(env, "vm-mig-guard", "confirm", U)
    finally:
        holder.kill(); holder.wait()
    assert r.returncode == 1 and U in armed(env) and f"{U}.confirmed" not in armed(env)


def test_confirm_refused_while_revert_service_active(env):
    U = run(env, "vm-mig-guard", "arm", "T1", manifest(env, "noop-touch /dev/null\n")).stdout.strip()
    (env["shim"] / "active").write_text(f"{U}.service\n")
    r = run(env, "vm-mig-guard", "confirm", U)
    assert r.returncode == 1 and "is running" in r.stderr and f"{U}.confirmed" not in armed(env)


@pytest.mark.parametrize("line", ["ufw-delete 3", "ufw-delete 12 ", "ufw-delete --force 3", "ufw-delete allow"])
def test_ufw_delete_by_index_or_partial_refused(env, line):
    m = env["tmp"] / "x.list"
    m.write_text(line + "\n")
    assert run(env, "vm-mig-revert", "--validate", str(m)).returncode == 3


def test_ufw_delete_full_rule_text(env):
    (env["ufw"] / "user.rules").write_text("allow 22/tcp\nroute allow in on virbr-svc out on eno1\n")
    m = env["tmp"] / "d.list"
    m.write_text("ufw-delete route allow in on virbr-svc out on eno1\n")
    assert run(env, "vm-mig-revert", str(m)).returncode == 0
    assert (env["ufw"] / "user.rules").read_text() == "allow 22/tcp\n"


def test_freeze_twice_refused_and_resume_restores_original(env):
    _freeze_env(env, env["tmp"])
    before = sorted((env["shim"] / "active").read_text().split())
    run(env, "vm-mig-freeze", "T2", "--window-end", "2026-10-06T04:00:00Z")
    r2 = run(env, "vm-mig-freeze", "T2", "--window-end", "2026-10-06T04:00:00Z")
    assert r2.returncode == 3 and "already exists" in r2.stderr
    run(env, "vm-mig-resume", "T2")
    assert set(before) <= set((env["shim"] / "active").read_text().split())


def test_freeze_user_listing_error_fails_closed(env):
    _freeze_env(env, env["tmp"])
    env["env"]["SHIM_USER_FAIL"] = "1"
    before = (env["shim"] / "active").read_text()
    r = run(env, "vm-mig-freeze", "T2")
    assert r.returncode == 4 and "ABORT before any stop" in r.stderr
    assert (env["shim"] / "active").read_text() == before


def test_gate_user_listing_error_is_fail(env):
    env["env"]["SHIM_USER_FAIL"] = "1"
    (env["shim"] / "active").write_text("logrotate.timer\nsystemd-tmpfiles-clean.timer\n")
    r = run(env, "vm-mig-freeze", "T2", "--gate")
    assert "gate FAIL user timer listing failed" in r.stderr and "gate PASS user" not in r.stderr


def test_resume_leaves_cron_stopped_if_it_was_inactive(env):
    _freeze_env(env, env["tmp"])
    a = env["shim"] / "active"
    a.write_text(a.read_text().replace("cron.service\n", ""))
    run(env, "vm-mig-freeze", "T2")
    r = run(env, "vm-mig-resume", "T2")
    assert "cron.service" not in a.read_text().split()
    assert "systemctl start cron.service" not in (env["shim"] / "log").read_text()
    assert "verify PASS cron inactive" in r.stderr


def test_resume_starts_persistent_timers_last(env):
    _freeze_env(env, env["tmp"])
    (env["shim"] / "persistent").write_text("zfs-replicate.timer\n")
    run(env, "vm-mig-freeze", "T2")
    run(env, "vm-mig-resume", "T2")
    starts = [l for l in (env["shim"] / "log").read_text().splitlines() if l.startswith("systemctl start ") and ".timer" in l]
    assert starts[-1] == "systemctl start zfs-replicate.timer"


def test_g7_verify_hdd_only_rejects_non_extpool_key(env, tmp_path):
    k = tmp_path / "key"; k.write_text("x"); pw = tmp_path / "pw"; pw.write_text("x")
    e = dict(env["env"], G7_HDD_KEY=str(k), G7_HDD_RESTIC_PW=str(pw))  # default parent: /dev/shm
    r = subprocess.run([str(SCRIPTS / "g7-verify"), "--hdd-only", "2026-10-05"], env=e, capture_output=True, text=True)
    assert r.returncode == 7 and "not on an ext-pool dataset" in r.stderr
    assert not list(Path("/dev/shm").glob("g7-test.*"))


def test_g7_verify_refuses_plaintext_on_unencrypted_disk(env):
    d = Path.home() / ".cache" / f"g7t-{os.getpid()}"
    e = dict(env["env"], G7_TEST_DIR=str(d), G7_RESTIC_PASSWORD_FILE="/dev/null")
    try:
        r = subprocess.run([str(SCRIPTS / "g7-verify"), "2026-10-05"], env=e, capture_output=True, text=True)
        assert r.returncode == 8 and "REFUSED" in r.stderr
    finally:
        if d.exists():
            d.rmdir()


def test_resume_then_freeze_after_reboot(env):
    _freeze_env(env, env["tmp"])
    assert run(env, "vm-mig-freeze", "T2").returncode in (0, 1)
    r = run(env, "vm-mig-resume", "T2")
    assert r.returncode == 0, r.stderr
    root = env["tmp"] / "root"
    assert [p.name for p in root.iterdir() if p.name.startswith("T2.resumed-")]
    assert run(env, "vm-mig-freeze", "T2").returncode != 3  # records afresh


def test_resume_listing_error_is_verify_fail(env):
    _freeze_env(env, env["tmp"])
    run(env, "vm-mig-freeze", "T2")
    env["env"]["SHIM_USER_FAIL"] = "1"
    r = run(env, "vm-mig-resume", "T2")
    assert r.returncode == 1 and "verify FAIL user timers" in r.stderr
    assert (env["tmp"] / "root" / "T2" / "timers.sys.active").exists()  # RED: not archived


def test_g7_verify_never_wipes_dev_shm(env, tmp_path):
    sentinel = Path("/dev/shm") / f"g7-sentinel-{os.getpid()}"
    sentinel.write_text("keep")
    k = tmp_path / "key"; k.write_text("x")
    e = dict(env["env"], G7_HDD_KEY=str(k), G7_HDD_RESTIC_PW=str(k), G7_TEST_DIR="/dev/shm")
    try:
        r = subprocess.run([str(SCRIPTS / "g7-verify"), "--hdd-only", "2026-10-05"], env=e, capture_output=True, text=True)
        assert r.returncode == 7
        assert sentinel.read_text() == "keep"
        assert not list(Path("/dev/shm").glob("g7-test.*"))
    finally:
        sentinel.unlink()


def test_g7_verify_never_wipes_backup_dir(env, tmp_path):
    bc = tmp_path / "backup-critical"  # stands in for /srv/backup-critical (tmpfs-backed in the test)
    (bc / "restic").mkdir(parents=True); (bc / "restic" / "config").write_text("repo")
    (bc / "dumps" / "2026-10-05").mkdir(parents=True); (bc / "dumps" / "2026-10-05" / "SHA256SUMS").write_text("")
    if subprocess.run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(bc)], capture_output=True, text=True).stdout.strip() != "tmpfs":
        pytest.skip("needs a tmpfs-backed tmp dir")
    e = dict(env["env"], G7_DIR=str(bc), G7_TEST_DIR=str(bc), G7_RESTIC_PASSWORD_FILE="/dev/null")
    subprocess.run([str(SCRIPTS / "g7-verify"), "2026-10-05"], env=e, capture_output=True, text=True)
    assert (bc / "restic" / "config").read_text() == "repo"
    assert (bc / "dumps" / "2026-10-05" / "SHA256SUMS").exists()
    assert not list(bc.glob("g7-test.*"))


def test_g7_verify_refuses_missing_parent(env, tmp_path):
    e = dict(env["env"], G7_TEST_DIR=str(tmp_path / "nope"), G7_RESTIC_PASSWORD_FILE="/dev/null")
    r = subprocess.run([str(SCRIPTS / "g7-verify"), "2026-10-05"], env=e, capture_output=True, text=True)
    assert r.returncode == 8 and not (tmp_path / "nope").exists()


def test_boot_unit_retries_within_boot():
    u = (SCRIPTS / "vm-mig-boot-revert.service.in").read_text()
    for k in ("Restart=on-failure", "RestartSec=60", "StartLimitBurst=3", "StartLimitIntervalSec=1200",
              "KillMode=control-group"):
        assert k in u


def test_resume_daemon_start_failure_is_red_and_keeps_recording(env):
    _freeze_env(env, env["tmp"])
    run(env, "vm-mig-freeze", "T2")
    (env["shim"] / "startfail").write_text("ops-bot.service\n")
    r = run(env, "vm-mig-resume", "T2")
    assert r.returncode == 1
    assert "verify FAIL 1 unit(s) did not start: ops-bot.service" in r.stderr
    assert "verify FAIL sys services not running (unexplained): ops-bot.service" in r.stderr
    assert (env["tmp"] / "root" / "T2" / "timers.sys.active").exists()  # RED: recording kept
    # once the operator explains the missing service, a re-run of the verify can go green
    (env["tmp"] / "root" / "T2" / "svc.diff.explained").write_text("ops-bot.service\n")
    (env["shim"] / "startfail").write_text("")
    r = run(env, "vm-mig-resume", "T2")
    assert r.returncode == 0, r.stderr
