"""Allowlisted MCP tool server for libvirt guests. Mutating tools default to dry-run."""
import json
import os
import re
import subprocess

from mcp.server.fastmcp import FastMCP

PROFILE = os.environ.get("VMSETUP_PROFILE", "/opt/vm-setup/profile.generated.json")
KILL_SWITCH = "/etc/vmsetup/AGENTS_PAUSED"
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

mcp = FastMCP("infra")


def _allowed_domains():
    try:
        return set(json.load(open(PROFILE))["nodes"])
    except (OSError, KeyError, ValueError):
        return set()


def _virsh(*args):
    out = subprocess.run(["virsh", "-c", "qemu:///system", *args], capture_output=True, text=True, timeout=60)
    return (out.stdout + out.stderr).strip()


def _check(domain):
    if os.path.exists(KILL_SWITCH):
        raise RuntimeError("agents paused (kill switch present)")
    if not NAME_RE.match(domain) or domain not in _allowed_domains():
        raise ValueError(f"domain not allowed: {domain}")


@mcp.tool()
def vm_list() -> str:
    """List all guests and their state (read-only)."""
    return _virsh("list", "--all")


@mcp.tool()
def host_metrics() -> str:
    """Load average and memory summary (read-only)."""
    return open("/proc/loadavg").read() + "\n" + "".join(open("/proc/meminfo").readlines()[:3])


@mcp.tool()
def vm_snapshot(domain: str, dry_run: bool = True) -> str:
    """Create a snapshot of an allowed guest."""
    _check(domain)
    name = f"agent-{os.getpid()}"
    if dry_run:
        return f"dry-run: snapshot {domain} as {name}"
    return _virsh("snapshot-create-as", domain, name)


@mcp.tool()
def vm_start(domain: str, dry_run: bool = True) -> str:
    """Start an allowed guest."""
    _check(domain)
    return f"dry-run: start {domain}" if dry_run else _virsh("start", domain)


@mcp.tool()
def vm_stop(domain: str, dry_run: bool = True) -> str:
    """Gracefully shut down an allowed guest."""
    _check(domain)
    return f"dry-run: shutdown {domain}" if dry_run else _virsh("shutdown", domain)


if __name__ == "__main__":
    mcp.run()
