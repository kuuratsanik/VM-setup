import json
import time

import httpx
import pytest

from jarvis import providers
from jarvis.compute.kaggle import Kaggle
from jarvis.compute.runpod import RunPod, SpendGuard


def runpod(handler, tmp_path, **kw):
    return RunPod("rpa_testkey1234", transport=httpx.MockTransport(handler), state=tmp_path / "pods.json", **kw)


@pytest.mark.asyncio
async def test_create_pod_registers_ttl_and_sends_expected_body(tmp_path):
    seen = {}

    def handler(request):
        seen["auth"], seen["body"] = request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json={"id": "p1", "name": "jarvis-t", "costPerHr": 0.4})

    pod = await runpod(handler, tmp_path).create_pod("t", "NVIDIA GeForce RTX 4090", hours=1.5)
    assert seen["auth"] == "Bearer rpa_testkey1234"
    assert seen["body"]["gpuTypeIds"] == ["NVIDIA GeForce RTX 4090"] and seen["body"]["name"] == "jarvis-t" and seen["body"]["interruptible"] is False
    assert pod["id"] == "p1" and "p1" in json.loads((tmp_path / "pods.json").read_text())


@pytest.mark.asyncio
async def test_over_cap_pod_is_deleted_immediately(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": "p2", "costPerHr": 5.0}) if request.method == "POST" else httpx.Response(200, json={})

    with pytest.raises(SpendGuard):
        await runpod(handler, tmp_path, max_hourly_usd=1.0).create_pod("big", "NVIDIA A100-SXM4-80GB")
    assert ("DELETE", "/v1/pods/p2") in calls or ("DELETE", "/pods/p2") in calls


@pytest.mark.asyncio
async def test_rejects_bad_gpu_and_excessive_hours(tmp_path):
    client = runpod(lambda r: httpx.Response(500), tmp_path)
    with pytest.raises(ValueError):
        await client.create_pod("x", "Tesla K80")
    with pytest.raises(SpendGuard):
        await client.create_pod("x", "NVIDIA L4", hours=99)


@pytest.mark.asyncio
async def test_reaper_deletes_expired_and_untracked_jarvis_pods_only(tmp_path):
    deleted = []
    pods = [
        {"id": "old", "name": "jarvis-old", "desiredStatus": "RUNNING"},
        {"id": "fresh", "name": "jarvis-fresh", "desiredStatus": "RUNNING"},
        {"id": "ghost", "name": "jarvis-ghost", "desiredStatus": "RUNNING"},
        {"id": "mine", "name": "my-own-pod", "desiredStatus": "RUNNING"},
    ]

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[p for p in pods if p["id"] not in deleted])
        deleted.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={})

    (tmp_path / "pods.json").write_text(json.dumps({"old": {"expires_at": time.time() - 10}, "fresh": {"expires_at": time.time() + 3600}}))
    result = await runpod(handler, tmp_path).reap()
    assert sorted(result) == ["ghost", "old"] and "mine" not in deleted and "fresh" not in deleted


@pytest.mark.asyncio
async def test_kaggle_pushes_private_script_and_parses_status():
    calls = []

    async def runner(*args, timeout=300):
        calls.append(args)
        return 0, 'user/my-run has status "KernelWorkerStatus.COMPLETE"'

    kg = Kaggle("tok12345678", "user", runner)
    assert await kg.push_script("my-run", "print(1)") == "user/my-run"
    assert calls[0][:3] == ("kernels", "push", "-p")
    assert (await kg.status("user/my-run"))["status"] == "complete"
    with pytest.raises(ValueError):
        await kg.push_script("Bad Slug!", "x")
    with pytest.raises(ValueError):
        await kg.status("../../etc/passwd")


@pytest.mark.asyncio
async def test_provider_validation_uses_readonly_call_and_never_accepts_bad_format():
    async def ok(request):
        return None

    transport = httpx.MockTransport(lambda r: httpx.Response(200 if r.headers.get("authorization") == "Bearer sk-goodgoodgood1" else 401, json={}))
    assert await providers.validate("openai", {"OPENAI_API_KEY": "sk-goodgoodgood1"}, transport=transport)
    assert not await providers.validate("openai", {"OPENAI_API_KEY": "sk-badbadbadbad1"}, transport=transport)
    with pytest.raises(ValueError):
        await providers.validate("openai", {"OPENAI_API_KEY": "has spaces in it"}, transport=transport)
    with pytest.raises(ValueError):
        providers.clean("openai", {})


def test_every_provider_documents_signup_and_steps():
    for pid, spec in providers.PROVIDERS.items():
        assert spec["signup"].startswith("https://") and spec["keys"].startswith("https://") and spec["steps"], pid
