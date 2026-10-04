"""RunPod through its REST API (https://rest.runpod.io/v1) with spend guards: hourly cap, short TTL, reaper."""
import json
import os
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
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps(reg))

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
        body = {
            "name": f"{NAME_PREFIX}{name}"[:60], "imageName": image, "gpuTypeIds": [gpu_type], "gpuCount": gpu_count,
            "containerDiskInGb": disk_gb, "volumeInGb": 0, "cloudType": cloud, "interruptible": False,
            "ports": ["8888/http", "22/tcp"], "env": env or {},
        }
        resp = await self.client.post("/pods", json=body)
        resp.raise_for_status()
        pod = resp.json()
        if (pod.get("costPerHr") or 0) > self.max_hourly_usd:
            await self.terminate(pod["id"])
            raise SpendGuard(f"pod costs ${pod['costPerHr']}/h, above the ${self.max_hourly_usd}/h cap; deleted")
        reg = self._registry()
        reg[pod["id"]] = {"expires_at": time.time() + hours * 3600, "costPerHr": pod.get("costPerHr")}
        self._save(reg)
        return {"id": pod["id"], "name": pod.get("name"), "costPerHr": pod.get("costPerHr"), "expires_in_hours": hours}

    async def stop(self, pod_id):
        (await self.client.post(f"/pods/{pod_id}/stop")).raise_for_status()

    async def terminate(self, pod_id):
        (await self.client.delete(f"/pods/{pod_id}")).raise_for_status()
        reg = self._registry()
        if reg.pop(pod_id, None) is not None:
            self._save(reg)

    async def reap(self, now=None):
        """Delete registered pods past their TTL and any jarvis-* pod RunPod lists that we no longer track."""
        now = now or time.time()
        reg, deleted = self._registry(), []
        for pod in await self.list_pods():
            tracked = reg.get(pod["id"])
            if pod["id"] in reg and tracked["expires_at"] < now or (pod["id"] not in reg and (pod["name"] or "").startswith(NAME_PREFIX)):
                await self.terminate(pod["id"])
                deleted.append(pod["id"])
        live = {p["id"] for p in await self.list_pods()}
        self._save({k: v for k, v in self._registry().items() if k in live})
        return deleted
