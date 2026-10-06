"""RunPod through its REST API (https://rest.runpod.io/v1) with spend guards: hourly cap, short TTL, reaper."""
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
BASE = "https://rest.runpod.io/v1"
STATE = Path(os.environ.get("JARVIS_STATE_DIR", "/var/lib/vmsetup/jarvis")) / "runpod-pods.json"
GPUS = [
    "NVIDIA GeForce RTX 3090", "NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 5090", "NVIDIA RTX A4000", "NVIDIA RTX A5000",
    "NVIDIA L4", "NVIDIA L40", "NVIDIA L40S", "NVIDIA A40", "NVIDIA A100-SXM4-80GB",
]
DEFAULT_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
NAME_PREFIX = "jarvis-"
PROVISIONAL_TTL_S = 900  # a create that never finishes stops shielding its pod name after this
PROVISIONAL = "pending:"  # registry key prefix; never a valid pod id
POD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REGISTRY_LOCK = threading.Lock()  # in-process callers; the reaper is a separate process, so _locked() also takes a flock


def _norm(name):
    """Compare pod names loosely: RunPod may normalise case or whitespace."""
    return str(name or "").casefold().strip()


def _expiry(entry):
    """The entry's expires_at as a number, or None if the entry is corrupt (hand-edited, wrong type)."""
    value = entry.get("expires_at") if isinstance(entry, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class SpendGuard(Exception):
    pass


class RegistryCorrupt(Exception):
    """The pod registry file is unreadable; refusing to overwrite it."""


class RunPod:
    def __init__(self, key, max_hourly_usd=1.0, max_ttl_hours=4.0, transport=None, state=STATE):
        self.max_hourly_usd, self.max_ttl_hours, self.state = max_hourly_usd, max_ttl_hours, state
        self.client = httpx.AsyncClient(base_url=BASE, headers={"Authorization": f"Bearer {key}"}, timeout=30, transport=transport)

    def _read(self):
        """(registry, ok). A missing file is an empty, healthy registry; unreadable or non-object content is not ok."""
        try:
            reg = json.loads(self.state.read_text())
        except FileNotFoundError:
            return {}, True
        except (OSError, ValueError):
            return {}, False
        return (reg, True) if isinstance(reg, dict) else ({}, False)

    def _registry(self):
        return self._read()[0]

    def _save(self, reg):
        """Atomic write: temp file in the same directory, then os.replace, so a crash never leaves a torn registry."""
        self.state.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state.parent, prefix=".runpod-pods-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(reg))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @contextlib.contextmanager
    def _locked(self):
        """Exclusive across threads and processes (flock on a sidecar file). Never hold it across a network await."""
        self.state.parent.mkdir(parents=True, exist_ok=True)
        with _REGISTRY_LOCK, open(self.state.with_name(self.state.name + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield  # closing the file releases the flock

    def _update(self, fn):
        """Locked read-modify-write; fn mutates the registry dict in place."""
        with self._locked():
            reg, ok = self._read()
            if not ok:  # saving {} over a damaged file would turn every other tracked pod into an orphan
                raise RegistryCorrupt(f"pod registry {self.state} is unreadable; fix or remove it")
            fn(reg)
            self._save(reg)

    async def list_pods(self):
        resp = await self.client.get("/pods")
        resp.raise_for_status()
        return [{k: p.get(k) for k in ("id", "name", "desiredStatus", "costPerHr", "image", "publicIp")} for p in resp.json()]

    async def create_pod(self, name, gpu_type, image=DEFAULT_IMAGE, gpu_count=1, disk_gb=40, hours=2.0, cloud="COMMUNITY", env=None):
        """Create a pod, then enforce the hourly cap against the price RunPod reports; over-cap pods are deleted at once."""
        if gpu_type not in GPUS:
            raise ValueError(f"unsupported GPU type; choose from {GPUS}")
        if not 0 < hours <= self.max_ttl_hours:
            raise SpendGuard(f"hours must be between 0 and {self.max_ttl_hours}")
        full_name = f"{NAME_PREFIX}{name}"[:60]
        body = {
            "name": full_name, "imageName": image, "gpuTypeIds": [gpu_type], "gpuCount": gpu_count,
            "containerDiskInGb": disk_gb, "volumeInGb": 0, "cloudType": cloud, "interruptible": False,
            "ports": ["8888/http", "22/tcp"], "env": env or {},
        }
        # Provisional entry first: until the pod id is known, the reaper must not mistake the new pod for an orphan.
        token = PROVISIONAL + secrets.token_hex(6)
        await asyncio.to_thread(self._update, lambda reg: reg.__setitem__(token, {"provisional_name": full_name, "expires_at": time.time() + PROVISIONAL_TTL_S}))
        try:
            resp = await self.client.post("/pods", json=body)
            resp.raise_for_status()
            pod = resp.json()
            if (pod.get("costPerHr") or 0) > self.max_hourly_usd:
                await self.terminate(pod["id"])
                raise SpendGuard(f"pod costs ${pod['costPerHr']}/h, above the ${self.max_hourly_usd}/h cap; deleted")
            expires_at = time.time() + hours * 3600

            def finalize(reg):
                reg.pop(token, None)
                reg[pod["id"]] = {"expires_at": expires_at, "costPerHr": pod.get("costPerHr")}

            await asyncio.to_thread(self._update, finalize)
        except BaseException:
            try:
                await asyncio.to_thread(self._update, lambda reg: reg.pop(token, None))
            except RegistryCorrupt:
                log.error("could not remove provisional entry %s: registry is unreadable", token)
            raise
        return {"id": pod["id"], "name": pod.get("name"), "costPerHr": pod.get("costPerHr"), "expires_in_hours": hours}

    async def stop(self, pod_id):
        (await self.client.post(f"/pods/{pod_id}/stop")).raise_for_status()

    async def terminate(self, pod_id):
        if not POD_ID_RE.match(str(pod_id)):
            raise ValueError("invalid pod id")
        (await self.client.delete(f"/pods/{pod_id}")).raise_for_status()
        try:
            await asyncio.to_thread(self._update, lambda reg: reg.pop(pod_id, None))
        except RegistryCorrupt:
            log.warning("pod %s was terminated but the registry is unreadable, so its entry was not removed", pod_id)

    def _shielded(self, pod, now):
        """Under the lock: (is this pod tracked by id or covered by a live provisional create with its name, is any provisional create live)."""
        with self._locked():
            reg = self._registry()
        live_prov = [v for k, v in reg.items() if k.startswith(PROVISIONAL) and isinstance(v, dict) and (_expiry(v) or 0) >= now]
        tracked = _expiry(reg.get(pod["id"])) is not None  # a corrupt entry does not shield
        return tracked or any(_norm(v.get("provisional_name")) == _norm(pod["name"]) for v in live_prov), bool(live_prov)

    async def reap(self, now=None):
        """Delete registered pods past their TTL and any jarvis-* pod RunPod lists that we no longer track.
        Fails closed: a corrupt entry for a real pod is repaired with a fresh TTL (not terminated); an unreadable registry disables orphan reaping."""
        now = now or time.time()
        snapshot, registry_ok = self._read()
        if not registry_ok:
            log.error("pod registry %s is unreadable or not an object; skipping orphan reaping this pass", self.state)
        registry_missing = not self.state.exists()
        deleted, names = [], {}
        for pod in await self.list_pods():
            names[pod["id"]] = pod.get("name")
            tracked = snapshot.get(pod["id"])
            if tracked is not None and _expiry(tracked) is None:
                log.warning("registry entry for pod %s is corrupt; repairing it with a fresh TTL and not terminating now", pod["id"])
                continue
            expired = tracked is not None and _expiry(tracked) < now  # explicit: expired OR (untracked AND jarvis-named)
            orphan = registry_ok and tracked is None and (pod.get("name") or "").startswith(NAME_PREFIX)  # exact: never widen what counts as ours
            if orphan:  # a create may have landed after the snapshot; re-check just before deleting
                shielded, any_provisional = await asyncio.to_thread(self._shielded, pod, now)
                if shielded:
                    continue
                if registry_missing:
                    log.warning("pod registry %s is absent but jarvis-* pod %s is about to be reaped as an orphan", self.state, pod["id"])
                if any_provisional:
                    log.warning("reaping orphan %s (%r) while a create is in flight; if that was the new pod the shield missed", pod["id"], pod["name"])
            if expired or orphan:
                await self.terminate(pod["id"])
                deleted.append(pod["id"])
        if not registry_ok:
            return deleted  # leave the file as it is for the owner to inspect
        live = {p["id"] for p in await self.list_pods()}

        def prune(reg):  # drop vanished pods, stale provisional entries; repair corrupt entries of live pods; a pod created meanwhile survives
            for k in list(reg):
                exp = _expiry(reg[k])
                if k.startswith(PROVISIONAL):
                    if exp is None or exp < now:
                        del reg[k]
                elif exp is None:
                    if k in live:
                        reg[k] = {"name": str(names.get(k) or "")[:80], "expires_at": now + self.max_ttl_hours * 3600}
                    else:
                        del reg[k]
                elif k not in live and (k in snapshot or exp < now):
                    del reg[k]

        await asyncio.to_thread(self._update, prune)
        return deleted
