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


@pytest.mark.asyncio
async def test_registry_write_is_atomic_when_the_write_fails_midway(tmp_path, monkeypatch):
    import os

    rp = runpod(lambda r: httpx.Response(200, json={}), tmp_path)
    rp._save({"old": {"expires_at": 1}})
    before = (tmp_path / "pods.json").read_text()

    def boom(src, dst):
        raise OSError("disk died before the rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        rp._save({"new": {"expires_at": 2}})
    assert (tmp_path / "pods.json").read_text() == before  # old file intact
    assert [p.name for p in tmp_path.iterdir()] == ["pods.json"]  # temp file cleaned up


@pytest.mark.asyncio
async def test_registry_write_failure_during_json_dump_keeps_old_file(tmp_path, monkeypatch):
    rp = runpod(lambda r: httpx.Response(200, json={}), tmp_path)
    rp._save({"old": {"expires_at": 1}})
    monkeypatch.setattr(json, "dumps", lambda *_: (_ for _ in ()).throw(RuntimeError("mid-write")))
    with pytest.raises(RuntimeError):
        rp._save({"new": 1})
    assert json.loads((tmp_path / "pods.json").read_text()) == {"old": {"expires_at": 1}}


@pytest.mark.asyncio
async def test_terminate_rejects_path_injection_and_updates_registry(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    rp = runpod(handler, tmp_path)
    rp._save({"p1": {"expires_at": 1}})
    with pytest.raises(ValueError):
        await rp.terminate("../x")
    assert calls == []
    await rp.terminate("p1")
    assert json.loads((tmp_path / "pods.json").read_text()) == {}


@pytest.mark.asyncio
async def test_reap_deletes_expired_and_untracked_jarvis_pods_only(tmp_path):
    pods = {"exp": "jarvis-a", "fresh": "jarvis-b", "orphan": "jarvis-c", "foreign": "my-own-pod"}
    deleted = []

    def handler(request):
        if request.method == "DELETE":
            deleted.append(request.url.path.rsplit("/", 1)[1])
            pods.pop(deleted[-1], None)
            return httpx.Response(200, json={})
        return httpx.Response(200, json=[{"id": k, "name": v} for k, v in pods.items()])

    rp = runpod(handler, tmp_path)
    rp._save({"exp": {"expires_at": 10}, "fresh": {"expires_at": 10**12}})
    assert sorted(await rp.reap(now=100)) == ["exp", "orphan"]  # untracked non-jarvis pods are never touched
    assert list(json.loads((tmp_path / "pods.json").read_text())) == ["fresh"]


@pytest.mark.asyncio
async def test_pod_created_between_list_and_save_survives_reap(tmp_path):
    pods, deleted, rp_box = {"old": "jarvis-old"}, [], {}

    def handler(request):
        if request.method == "DELETE":
            deleted.append(request.url.path.rsplit("/", 1)[1])
            pods.pop(deleted[-1], None)
            return httpx.Response(200, json={})
        listing = [{"id": k, "name": v} for k, v in pods.items()]
        if rp_box.get("first_list_done") and "new" not in rp_box:  # a create lands (in another process) after the reaper's listing
            rp_box["new"] = True
            rp_box["other"]._update(lambda reg: reg.__setitem__("new", {"expires_at": 10**12}))
        rp_box["first_list_done"] = True
        return httpx.Response(200, json=listing)  # the second listing is stale: it lacks "new"

    rp, other = runpod(handler, tmp_path), runpod(handler, tmp_path)
    rp_box["other"] = other
    rp._save({"old": {"expires_at": 10**12}})
    assert await rp.reap(now=100) == []
    assert "new" in json.loads((tmp_path / "pods.json").read_text()) and deleted == []


@pytest.mark.asyncio
async def test_registry_lock_is_a_cross_process_flock(tmp_path):
    import fcntl

    rp = runpod(lambda r: httpx.Response(200, json={}), tmp_path)
    with rp._locked():
        with open(tmp_path / "pods.json.lock", "a") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with open(tmp_path / "pods.json.lock", "a") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.asyncio
async def test_create_landing_between_snapshot_and_listing_is_not_reaped(tmp_path):
    pods, deleted, state = {}, [], {}

    def handler(request):
        if request.method == "DELETE":
            deleted.append(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json={})
        if not state.get("landed"):  # the snapshot is already taken; now a create finishes
            state["landed"] = True
            pods["new"] = "jarvis-new"
            state["other"]._update(lambda reg: reg.__setitem__("new", {"expires_at": 10**12}))
        return httpx.Response(200, json=[{"id": k, "name": v} for k, v in pods.items()])

    rp, state["other"] = runpod(handler, tmp_path), runpod(handler, tmp_path)
    assert await rp.reap(now=100) == [] and deleted == []
    assert "new" in json.loads((tmp_path / "pods.json").read_text())


@pytest.mark.asyncio
async def test_reaper_running_between_post_and_registry_write_spares_the_new_pod(tmp_path):
    pods, deleted, state = {}, [], {}

    async def handler(request):
        if request.method == "DELETE":
            deleted.append(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json={})
        if request.method == "POST":
            pods["p9"] = "jarvis-race"  # RunPod has the pod, the registry does not know its id yet
            state["reaped"] = await state["reaper"].reap(now=time.time())
            return httpx.Response(200, json={"id": "p9", "name": "jarvis-race", "costPerHr": 0.3})
        return httpx.Response(200, json=[{"id": k, "name": v} for k, v in pods.items()])

    creator, state["reaper"] = runpod(handler, tmp_path), runpod(handler, tmp_path)
    pod = await creator.create_pod("race", "NVIDIA L4", hours=1)
    assert pod["id"] == "p9" and state["reaped"] == [] and deleted == []
    reg = json.loads((tmp_path / "pods.json").read_text())
    assert list(reg) == ["p9"]  # provisional entry replaced by the real one


@pytest.mark.asyncio
async def test_failed_create_removes_its_provisional_entry(tmp_path):
    rp = runpod(lambda r: httpx.Response(500, json={}), tmp_path)
    with pytest.raises(httpx.HTTPStatusError):
        await rp.create_pod("x", "NVIDIA L4", hours=1)
    assert json.loads((tmp_path / "pods.json").read_text()) == {}


@pytest.mark.asyncio
async def test_spend_guard_terminates_pod_and_removes_provisional_entry(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path.rsplit("/", 1)[-1]))
        return httpx.Response(200, json={"id": "p7", "costPerHr": 9.0}) if request.method == "POST" else httpx.Response(200, json={})

    rp = runpod(handler, tmp_path, max_hourly_usd=1.0)
    with pytest.raises(SpendGuard):
        await rp.create_pod("big", "NVIDIA A100-SXM4-80GB")
    assert ("DELETE", "p7") in calls
    assert json.loads((tmp_path / "pods.json").read_text()) == {}


def _lister(pods, deleted):
    def handler(request):
        if request.method == "DELETE":
            deleted.append(request.url.path.rsplit("/", 1)[1])
            pods.pop(deleted[-1], None)
            return httpx.Response(200, json={})
        return httpx.Response(200, json=[{"id": k, "name": v} for k, v in pods.items()])
    return handler


def test_shield_comparison_tolerates_name_normalisation(tmp_path):
    rp = runpod(lambda r: httpx.Response(200, json={}), tmp_path)
    rp._save({"pending:a": {"provisional_name": "jarvis-race", "expires_at": 10**12}})
    assert rp._shielded({"id": "p1", "name": "Jarvis-Race "}, 100) == (True, True)
    assert rp._shielded({"id": "p1", "name": "jarvis-other"}, 100) == (False, True)


@pytest.mark.asyncio
async def test_owner_pods_with_odd_names_are_never_reaped_and_real_orphans_warn(tmp_path, caplog):
    import logging

    pods, deleted = {"d": "Jarvis-Dev", "x": " jarvis-x ", "o": "jarvis-orphan"}, []
    rp = runpod(_lister(pods, deleted), tmp_path)
    rp._save({"pending:a": {"provisional_name": "jarvis-other", "expires_at": 10**12}})
    with caplog.at_level(logging.WARNING):
        assert await rp.reap(now=100) == ["o"]  # only the exact jarvis- prefix; the others are the owner's
    assert "create is in flight" in caplog.text


@pytest.mark.asyncio
async def test_tracked_pod_with_corrupt_entry_survives_and_is_repaired(tmp_path, caplog):
    import logging

    pods, deleted = {"a": "jarvis-a", "b": "jarvis-b"}, []
    rp = runpod(_lister(pods, deleted), tmp_path, max_ttl_hours=2)
    (tmp_path / "pods.json").write_text(json.dumps({"a": {"expires_at": "123"}, "b": "garbage", "pending:x": [1], "gone": None}))
    with caplog.at_level(logging.WARNING):
        assert await rp.reap(now=1000) == []  # fail closed: nothing is terminated on this pass
    reg = json.loads((tmp_path / "pods.json").read_text())
    assert set(reg) == {"a", "b"} and reg["a"] == {"name": "jarvis-a", "expires_at": 1000 + 2 * 3600}  # repaired; junk and pending:x dropped
    assert "corrupt" in caplog.text
    assert await rp.reap(now=1000 + 3 * 3600) == ["a", "b"]  # the TTL then reaps them normally


@pytest.mark.asyncio
async def test_unreadable_registry_file_disables_orphan_reaping(tmp_path, caplog):
    import logging

    pods, deleted = {"o": "jarvis-orphan"}, []
    rp = runpod(_lister(pods, deleted), tmp_path)
    for content in ("[1, 2]", "{not json"):
        (tmp_path / "pods.json").write_text(content)
        with caplog.at_level(logging.ERROR):
            assert await rp.reap(now=100) == [] and deleted == []
        assert (tmp_path / "pods.json").read_text() == content  # left untouched for the owner
    assert "unreadable" in caplog.text
    (tmp_path / "pods.json").unlink()  # a missing file is a healthy empty registry: orphans are reaped
    assert await rp.reap(now=100) == ["o"]
