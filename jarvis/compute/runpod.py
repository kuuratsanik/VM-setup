"""RunPod through its REST API (https://rest.runpod.io/v1) with spend guards: hourly cap, short TTL, reaper."""
import contextlib
import fcntl
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

import httpx

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


class SpendGuard(Exception):
    pass


class RunPod:
    def __init__(self, key, max_hourly_usd=1.0, max_ttl_hours=4.0, transport=None, state=STATE):
        self.max_hourly_usd, self.max_ttl_hours, self.state = max_hourly_usd, max_ttl_hours, state
        self.client = httpx.AsyncClient(base_url=BASE, headers={"Authorization": f"Bearer {key}"}, timeout=30, transport=transport)

    def _registry(self):
        try:
            return json.loads(self.state.read_text())
        except (OSError, ValueError):
            return {}

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
            reg = self._registry()
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
        self._update(lambda reg: reg.__setitem__(token, {"provisional_name": full_name, "expires_at": time.time() + PROVISIONAL_TTL_S}))
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

            self._update(finalize)
        except BaseException:
            self._update(lambda reg: reg.pop(token, None))
            raise
        return {"id": pod["id"], "name": pod.get("name"), "costPerHr": pod.get("costPerHr"), "expires_in_hours": hours}

    async def stop(self, pod_id):
        (await self.client.post(f"/pods/{pod_id}/stop")).raise_for_status()

    async def terminate(self, pod_id):
        if not POD_ID_RE.match(str(pod_id)):
            raise ValueError("invalid pod id")
        (await self.client.delete(f"/pods/{pod_id}")).raise_for_status()
        self._update(lambda reg: reg.pop(pod_id, None))

    def _shielded(self, pod, now):
        """Under the lock: is this pod tracked by id, or covered by a live provisional create with its name?"""
        with self._locked():
            reg = self._registry()
        return pod["id"] in reg or any(
            k.startswith(PROVISIONAL) and v.get("provisional_name") == pod["name"] and v.get("expires_at", 0) >= now for k, v in reg.items())

    async def reap(self, now=None):
        """Delete registered pods past their TTL and any jarvis-* pod RunPod lists that we no longer track."""
        now = now or time.time()
        snapshot = self._registry()
        deleted = []
        for pod in await self.list_pods():
            tracked = snapshot.get(pod["id"])
            expired = tracked is not None and tracked["expires_at"] < now  # explicit: expired OR (untracked AND jarvis-named)
            orphan = tracked is None and (pod["name"] or "").startswith(NAME_PREFIX)
            if orphan and self._shielded(pod, now):  # a create may have landed after the snapshot; re-check just before deleting
                continue
            if expired or orphan:
                await self.terminate(pod["id"])
                deleted.append(pod["id"])
        live = {p["id"] for p in await self.list_pods()}

        def prune(reg):  # drop pod entries we saw before listing that are gone, and stale provisional entries; a pod created meanwhile survives
            for k in list(reg):
                if k.startswith(PROVISIONAL):
                    if reg[k].get("expires_at", 0) < now:
                        del reg[k]
                elif k not in live and (k in snapshot or reg[k].get("expires_at", 0) < now):
                    del reg[k]

        self._update(prune)
        return deleted
