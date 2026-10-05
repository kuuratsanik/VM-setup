import json

import pytest
from fastapi.testclient import TestClient

from jarvis import auth
from jarvis.app import create_app
from jarvis.chat import Hub, contains_secret
from jarvis.compute.service import Compute

H = {"X-Requested-With": "jarvis"}


class FakeCompute(Compute):
    def __init__(self):
        super().__init__(env={})
        self.ran = []

    async def run(self, name, args):
        self.ran.append((name, args))
        return {"ok": True}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "STATE_DIR", tmp_path)
    monkeypatch.setenv("JARVIS_SESSION_SECRET", "test-secret-test-secret")
    auth._attempts.clear()
    compute = FakeCompute()
    app = create_app(compute=compute, hub=Hub(compute), start_tools=False)
    with TestClient(app) as c:
        c.compute = compute
        yield c


def login(client):
    auth.set_password("owner", "correct horse battery")
    return client.post("/api/login", headers=H, json={"username": "owner", "password": "correct horse battery"})


def test_everything_requires_login(client):
    for path in ("/api/status", "/api/incidents", "/api/providers", "/api/compute/gpus", "/api/models"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/chat", headers=H, json={"messages": []}).status_code == 401


def test_login_blocked_until_an_account_exists_and_without_csrf_header(client):
    assert client.post("/api/login", headers=H, json={"username": "owner", "password": "x" * 12}).status_code == 503
    auth.set_password("owner", "correct horse battery")
    assert client.post("/api/login", json={"username": "owner", "password": "correct horse battery"}).status_code == 403
    assert client.post("/api/login", headers={**H, "Origin": "http://evil.example"}, json={"username": "owner", "password": "correct horse battery"}).status_code == 403
    assert client.post("/api/login", headers=H, json={"username": "owner", "password": "wrong password!!"}).status_code == 401


def test_security_headers_and_session_cookie_flags(client):
    resp = login(client)
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "default-src 'self'" in client.get("/api/auth/state").headers["content-security-policy"]


def test_chat_refuses_pasted_keys(client):
    login(client)
    resp = client.post("/api/chat", headers=H, json={"messages": [{"role": "user", "content": "my key is sk-" + "a" * 30}]})
    assert resp.status_code == 400 and "Setup" in resp.json()["detail"]
    assert contains_secret("token hf_" + "b" * 25) and not contains_secret("what is the weather")


def test_state_changing_actions_need_explicit_confirmation(client):
    login(client)
    pending = client.post("/api/compute/runpod/pods", headers=H, json={"name": "t", "gpu_type": "NVIDIA L4", "hours": 1}).json()["pending"]
    assert client.compute.ran == []  # proposing never executes
    assert client.post(f"/api/actions/{pending}/confirm", headers=H).status_code == 200
    assert client.compute.ran[0][0] == "runpod_create_pod"
    assert client.post(f"/api/actions/{pending}/confirm", headers=H).status_code == 404  # single use
    other = client.post("/api/compute/kaggle/run", headers=H, json={"slug": "abc", "code": "print(1)"}).json()["pending"]
    assert client.post(f"/api/actions/{other}/discard", headers=H).json() == {"discarded": True}
    assert client.post(f"/api/actions/{other}/confirm", headers=H).status_code == 404


def test_provider_key_endpoint_rejects_bad_input_without_staging(client, tmp_path, monkeypatch):
    from jarvis import providers
    monkeypatch.setattr(providers, "STATE_DIR", tmp_path)
    login(client)
    assert client.post("/api/providers/nope/key", headers=H, json={"values": {}}).status_code == 404
    assert client.post("/api/providers/openai/key", headers=H, json={"values": {"OPENAI_API_KEY": "bad key\nLITELLM_KEY=x"}}).status_code == 400
    assert not (tmp_path / "staged-secrets.json").exists()
    listing = client.get("/api/providers").json()
    assert {p["id"] for p in listing} >= {"runpod", "kaggle", "openai"} and all("signup" in p for p in listing)


def test_logout_ends_the_session(client):
    login(client)
    assert client.get("/api/models").status_code == 200
    client.post("/api/logout", headers=H)
    assert client.get("/api/models").status_code == 401


def test_autonomy_tab_api_lists_approvals_and_breaker(client, tmp_path, monkeypatch):
    import approvals
    import policy

    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "auto")
    monkeypatch.setattr(approvals, "STATE_DIR", tmp_path / "auto")
    assert client.get("/api/autonomy").status_code == 401
    login(client)
    rid = approvals.queue("vm_stop", "hub-a1", {"domain": "hub-a1"}, {"reason": "policy requires approval"})
    data = client.get("/api/autonomy").json()
    assert data["mode"] in ("dry_run", "supervised", "autonomous") and data["audit"]["chain_ok"] is True
    assert [a["id"] for a in data["approvals"]] == [rid]
    # No infra MCP server in this test, so confirming must refuse without consuming the request.
    assert client.post(f"/api/actions/{rid}/confirm", headers=H).status_code == 400
    assert approvals.get(rid) is not None
    assert client.post(f"/api/actions/{rid}/discard", headers=H).json() == {"discarded": True}
    assert client.get("/api/autonomy").json()["approvals"] == []


def test_breaker_reset_requires_login_and_is_audited(client, tmp_path, monkeypatch):
    import policy

    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "auto")
    assert client.post("/api/autonomy/breaker/reset", headers=H).status_code == 401
    login(client)
    assert client.post("/api/autonomy/breaker/reset", headers=H).json() == {"ok": True}
    tail = client.get("/api/autonomy").json()["audit_tail"]
    assert tail[0]["kind"] == "breaker" and tail[0]["action"] == "reset" and tail[0]["actor"] == "owner"


# ---- audit fixes ----

def _cookie(client):
    return client.cookies.get("jarvis")


def test_old_cookie_dies_on_logout(client):
    login(client)
    stolen = _cookie(client)
    assert client.post("/api/logout", headers=H).status_code == 200
    client.cookies.set("jarvis", stolen)
    assert client.get("/api/models").status_code == 401
    assert client.get("/api/auth/state").json()["logged_in"] is False


def test_old_cookie_dies_on_password_change(client):
    login(client)
    old = _cookie(client)
    auth.set_password("owner", "another correct horse")
    client.cookies.set("jarvis", old)
    assert client.get("/api/models").status_code == 401
    client.cookies.clear()
    assert client.post("/api/login", headers=H, json={"username": "owner", "password": "another correct horse"}).status_code == 200
    assert client.get("/api/models").status_code == 200


def test_logout_without_session_does_not_revoke_the_owner(client):
    login(client)
    mine = _cookie(client)
    from fastapi.testclient import TestClient
    other = TestClient(client.app)
    other.post("/api/logout", headers=H)  # anonymous
    client.cookies.set("jarvis", mine)
    assert client.get("/api/models").status_code == 200


def test_totp_cannot_be_downgraded_over_http(client):
    login(client)
    secret, _ = auth.enroll_totp()
    import pyotp
    assert auth.confirm_totp(pyotp.TOTP(secret).now())
    assert auth.totp_enabled()
    assert client.post("/api/totp/enroll", headers=H).status_code in (404, 405)
    assert auth.totp_enabled()


def test_chat_secret_check_applies_to_user_turns_only():
    from jarvis.chat import history_has_secret
    pem = "-----BEGIN RSA " + "PRIVATE KEY-----"
    assert not history_has_secret([{"role": "user", "content": "how do I rotate a key?"}, {"role": "assistant", "content": f"A key starts with {pem}"}])
    assert history_has_secret([{"role": "assistant", "content": "ok"}, {"role": "user", "content": pem}])


def test_chat_endpoint_accepts_assistant_pem_quote(client):
    login(client)
    msgs = [{"role": "assistant", "content": "-----BEGIN " + "PRIVATE KEY-----"}, {"role": "user", "content": "hi"}]
    resp = client.post("/api/chat", headers=H, json={"messages": msgs, "tools": False})
    assert resp.status_code == 200  # not rejected as a pasted secret


def test_provider_network_error_is_502_with_plain_detail(client, monkeypatch):
    import httpx
    from jarvis import providers

    async def boom(*a, **k):
        raise httpx.ConnectError("dns down")

    monkeypatch.setattr(providers, "validate", boom)
    login(client)
    resp = client.post("/api/providers/openai/key", headers=H, json={"values": {"OPENAI_API_KEY": "sk-goodgoodgood1"}})
    assert resp.status_code == 502 and isinstance(resp.json()["detail"], str) and "dns" not in resp.json()["detail"]


def test_kaggle_validation_timeout_is_502(client, monkeypatch):
    import subprocess
    from jarvis import providers

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("kaggle", 60)

    monkeypatch.setattr(providers, "_check_kaggle", slow)
    login(client)
    resp = client.post("/api/providers/kaggle/key", headers=H, json={"values": {"KAGGLE_API_TOKEN": "KGAT_abcdefgh1234", "KAGGLE_USERNAME": "me"}})
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_kaggle_validation_does_not_block_the_event_loop():
    import asyncio
    import threading
    from jarvis import providers
    seen = {}

    def runner(*a, **k):
        seen["thread"] = threading.current_thread()
        class R:
            returncode = 0
        return R()

    assert await providers.validate("kaggle", {"KAGGLE_API_TOKEN": "KGAT_abcdefgh1234", "KAGGLE_USERNAME": "me"}, runner=runner)
    assert seen["thread"] is not threading.main_thread()


def test_corrupt_incident_lines_do_not_break_status(client, tmp_path, monkeypatch):
    import time
    from jarvis import status
    f = tmp_path / "incidents.jsonl"
    good = {"ts": time.time(), "alert": "HighCPU", "outcome": "fixed", "model": "cloud-small", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    f.write_text("\n".join([json.dumps(good), '{"ts": 1, "ale', "not json", "[1,2]", json.dumps({"detail": "no ts"}), json.dumps({"ts": time.time(), "usage": {"prompt_tokens": 1}}), ""]))
    monkeypatch.setattr(status, "INCIDENTS", f)
    login(client)
    rows = client.get("/api/incidents").json()
    assert any(r["alert"] == "HighCPU" for r in rows) and all(r["alert"] and r["outcome"] for r in rows)
    assert status.spend()["tokens_30d"] >= 15


class _Session:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool, args):
        import asyncio
        self.calls.append(tool)
        await asyncio.sleep(0.05)  # keep the first confirm in flight while the second arrives

        class Res:
            isError = False
            content = "done"
        return Res()


@pytest.mark.asyncio
async def test_concurrent_confirms_run_the_tool_once(tmp_path, monkeypatch):
    import asyncio
    import approvals
    import policy
    from jarvis.chat import ActionInProgress

    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "auto")
    monkeypatch.setattr(approvals, "STATE_DIR", tmp_path / "auto")
    session = _Session()
    hub = Hub(FakeCompute())
    hub.routes = {"vm_stop": ("infra", session, "vm_stop")}
    rid = approvals.queue("vm_stop", "hub-a1", {"domain": "hub-a1"}, {"reason": "x"})
    results = await asyncio.gather(hub.confirm(rid), hub.confirm(rid), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    errs = [r for r in results if isinstance(r, Exception)]
    assert len(ok) == 1 and len(errs) == 1 and isinstance(errs[0], (ActionInProgress, KeyError))
    assert session.calls.count("vm_stop") == 1
    done = list((tmp_path / "auto" / "approvals-done").glob("*.json"))
    assert len(done) == 1 and json.loads(done[0].read_text())["status"] in ("executed", "failed")
    assert not list((tmp_path / "auto" / "approvals").glob("*"))
    with pytest.raises(KeyError):
        await hub.confirm(rid)  # finished -> 404


def test_action_in_progress_exception_maps_to_409(client, tmp_path, monkeypatch):
    import approvals
    import policy
    from jarvis.chat import ActionInProgress

    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "auto")
    monkeypatch.setattr(approvals, "STATE_DIR", tmp_path / "auto")

    async def busy(pid):
        raise ActionInProgress(pid)

    login(client)
    monkeypatch.setattr(Hub, "confirm", lambda self, pid: busy(pid))
    resp = client.post("/api/actions/abcdefgh/confirm", headers=H)
    assert resp.status_code == 409 and isinstance(resp.json()["detail"], str)


@pytest.mark.asyncio
async def test_failed_compute_confirm_stays_retryable():
    class Flaky(FakeCompute):
        n = 0

        async def run(self, name, args):
            Flaky.n += 1
            if Flaky.n == 1:
                raise RuntimeError("not configured")
            return {"ok": True}

    hub = Hub(Flaky())
    pid = hub.queue("runpod_create_pod", {"name": "t", "gpu_type": "NVIDIA L4"})
    with pytest.raises(RuntimeError):
        await hub.confirm(pid)
    assert "ok" in await hub.confirm(pid)
    with pytest.raises(KeyError):
        await hub.confirm(pid)


@pytest.mark.asyncio
async def test_cancelled_confirm_is_recorded_unknown_and_failed_write_still_unclaims(tmp_path, monkeypatch):
    import asyncio
    import approvals
    import policy

    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "auto")
    monkeypatch.setattr(approvals, "STATE_DIR", tmp_path / "auto")

    class Hang(_Session):
        async def call_tool(self, tool, args):
            await asyncio.sleep(10)

    hub = Hub(FakeCompute())
    hub.routes = {"vm_stop": ("infra", Hang(), "vm_stop")}
    rid = approvals.queue("vm_stop", "hub-a1", {"domain": "hub-a1"}, {"reason": "x"})
    task = asyncio.create_task(hub.confirm(rid))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rec = json.loads((tmp_path / "auto" / "approvals-done" / f"{rid}.json").read_text())
    assert rec["status"] == "unknown"
    # a failing write must not leave the claim behind
    claimed = tmp_path / "auto" / "approvals" / "zzzzzz.claimed"
    claimed.write_text("{}")
    monkeypatch.setattr(approvals, "_dir", lambda name, base=None: (_ for _ in ()).throw(OSError("disk full")) if name == "approvals-done" else tmp_path / "auto" / name)
    Hub._finish_claimed("zzzzzz", claimed, "executed", "ok")
    assert not claimed.exists()


def test_startup_sweep_records_stale_claims_only(tmp_path, monkeypatch):
    import os
    import time
    import approvals

    monkeypatch.setattr(approvals, "STATE_DIR", tmp_path / "auto")
    old = approvals._dir("approvals") / "oldold1.claimed"
    fresh = approvals._dir("approvals") / "fresh01.claimed"
    old.write_text(json.dumps({"id": "oldold1", "action": "vm_stop"}))
    fresh.write_text(json.dumps({"id": "fresh01", "action": "vm_stop"}))
    os.utime(old, (time.time() - 3600,) * 2)
    assert Hub.sweep_claimed() == 1
    assert not old.exists() and fresh.exists()
    assert json.loads((tmp_path / "auto" / "approvals-done" / "oldold1.json").read_text())["status"] == "interrupted"


def test_discarding_a_claimed_approval_is_refused(tmp_path, monkeypatch):
    import approvals

    monkeypatch.setattr(approvals, "STATE_DIR", tmp_path / "auto")
    rid = approvals.queue("vm_stop", "hub-a1", {}, {})
    Hub._claim(rid)
    assert Hub(FakeCompute()).discard(rid) is False


@pytest.mark.asyncio
async def test_tool_results_are_redacted_before_going_back_to_the_model(monkeypatch):
    import types
    from jarvis import chat as chatmod

    secret = "sk-" + "a" * 30
    captured = []

    class Hub2:
        specs = []

        def needs_confirmation(self, n):
            return False

        async def execute(self, n, a):
            return f"token {secret}"

    def chunk(**kw):
        d = types.SimpleNamespace(content=kw.get("content"), tool_calls=kw.get("tool_calls"))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=d)])

    async def stream(items):
        for i in items:
            yield i

    class LLM:
        n = 0

        class chat:
            class completions:
                @staticmethod
                async def create(model, messages, **k):
                    captured.append([dict(m) for m in messages])
                    LLM.n += 1
                    if LLM.n == 1:
                        tc = types.SimpleNamespace(index=0, id="c1", function=types.SimpleNamespace(name="x", arguments="{}"))
                        return stream([chunk(tool_calls=[tc])])
                    return stream([chunk(content="ok")])

    c = chatmod.Chat(Hub2())
    c.llm = LLM
    [e async for e in c.run([{"role": "user", "content": "hi"}], "default")]
    tool_msgs = [m for m in captured[1] if m["role"] == "tool"]
    assert tool_msgs and secret not in tool_msgs[0]["content"]


def test_sweep_with_max_age_zero_catches_a_just_created_claim(tmp_path, monkeypatch):
    import approvals

    monkeypatch.setattr(approvals, "STATE_DIR", tmp_path / "auto")
    f = approvals._dir("approvals") / "young01.claimed"
    f.write_text(json.dumps({"id": "young01"}))
    assert Hub.sweep_claimed() == 0 and f.exists()
    assert Hub.sweep_claimed(max_age=0) == 1 and not f.exists()
    assert json.loads((tmp_path / "auto" / "approvals-done" / "young01.json").read_text())["status"] == "interrupted"


@pytest.mark.asyncio
async def test_discard_during_failing_compute_confirm_is_not_resurrected():
    import asyncio

    class Slow(FakeCompute):
        async def run(self, name, args):
            await asyncio.sleep(0.05)
            raise RuntimeError("boom")

    hub = Hub(Slow())
    pid = hub.queue("runpod_create_pod", {"name": "t", "gpu_type": "NVIDIA L4"})
    task = asyncio.create_task(hub.confirm(pid))
    await asyncio.sleep(0.01)
    assert hub.discard(pid) is True
    with pytest.raises(RuntimeError):
        await task
    assert pid not in hub.pending and not hub.inflight and not hub.discarded


def test_terminate_is_queued_and_only_runs_on_confirm(client):
    login(client)
    resp = client.post("/api/compute/runpod/pods/pod_abc1/terminate", headers=H)
    assert resp.status_code == 200 and list(resp.json()) == ["pending"]
    assert client.compute.ran == []  # proposing never executes
    done = client.post(f"/api/actions/{resp.json()['pending']}/confirm", headers=H)
    assert done.status_code == 200 and client.compute.ran == [("runpod_terminate_pod", {"pod_id": "pod_abc1"})]
    assert client.post(f"/api/actions/{resp.json()['pending']}/confirm", headers=H).status_code == 404


def test_terminate_rejects_odd_pod_ids_and_needs_login(client):
    assert client.post("/api/compute/runpod/pods/p1/terminate", headers=H).status_code == 401
    login(client)
    assert client.post("/api/compute/runpod/pods/bad..id/terminate", headers=H).status_code == 422


def test_confirm_maps_provider_errors_to_502_without_leaking_body(client):
    import httpx

    login(client)

    async def refuse(name, args):
        req = httpx.Request("DELETE", "https://rest.runpod.io/v1/pods/x")
        raise httpx.HTTPStatusError("boom secret-body", request=req, response=httpx.Response(404, request=req, text="secret-body"))

    client.compute.run = refuse
    pid = client.post("/api/compute/runpod/pods/pod1/terminate", headers=H).json()["pending"]
    resp = client.post(f"/api/actions/{pid}/confirm", headers=H)
    assert resp.status_code == 502 and "secret" not in resp.text and "404" in resp.json()["detail"]


@pytest.mark.parametrize("path,body", [
    ("/api/compute/runpod/pods", {"name": "Bad Name!", "gpu_type": "NVIDIA L4", "hours": 1}),
    ("/api/compute/runpod/pods", {"name": "x" * 41, "gpu_type": "NVIDIA L4", "hours": 1}),
    ("/api/compute/runpod/pods", {"name": "ok", "gpu_type": "NVIDIA L4", "hours": 0}),
    ("/api/compute/runpod/pods", {"name": "ok", "gpu_type": "NVIDIA L4", "hours": -1}),
    ("/api/compute/runpod/pods", {"name": "ok", "gpu_type": "NVIDIA L4", "hours": 1000}),
    ("/api/compute/runpod/pods", {"name": "ok", "gpu_type": "", "hours": 1}),
    ("/api/compute/runpod/pods", {"name": "ok", "gpu_type": "Nonexistent GPU", "hours": 1}),
    ("/api/compute/kaggle/run", {"slug": "../etc", "code": "print(1)"}),
    ("/api/compute/kaggle/run", {"slug": "owner/name", "code": "print(1)"}),
    ("/api/compute/kaggle/run", {"slug": "ab", "code": "print(1)"}),
    ("/api/compute/kaggle/run", {"slug": "abc", "code": "x" * 200_001}),
])
def test_compute_input_is_validated_on_arrival(client, path, body):
    login(client)
    assert client.post(path, headers=H, json=body).status_code == 422
    assert client.compute.ran == []


def test_login_lockout_returns_429(client):
    auth.set_password("owner", "correct horse battery")
    bad = {"username": "owner", "password": "wrong password!!"}
    for _ in range(auth.MAX_ATTEMPTS):
        assert client.post("/api/login", headers=H, json=bad).status_code == 401
    assert client.post("/api/login", headers=H, json=bad).status_code == 429
    good = {"username": "owner", "password": "correct horse battery"}
    assert client.post("/api/login", headers=H, json=good).status_code == 429  # locked even with the right password


def test_no_terminate_tool_is_offered_to_the_model_or_runnable_from_chat():
    import asyncio

    from jarvis.chat import LOCAL_TOOLS

    assert not [n for n in LOCAL_TOOLS if "terminate" in n]
    hub = Hub(FakeCompute())
    assert not hub.needs_confirmation("runpod_terminate_pod")
    assert asyncio.run(hub.execute("runpod_terminate_pod", {"pod_id": "p1"})) == "error: unknown tool"  # a model-issued call never reaches compute
    assert hub.compute.ran == []


def test_queued_terminate_with_invalid_id_fails_cleanly_at_confirm(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(auth, "STATE_DIR", tmp_path)
    monkeypatch.setenv("JARVIS_SESSION_SECRET", "test-secret-test-secret")
    auth._attempts.clear()
    calls = []
    compute = Compute(env={"RUNPOD_API_KEY": "rpa_" + "k" * 12}, runpod_transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json=[])))
    hub = Hub(compute)
    with TestClient(create_app(compute=compute, hub=hub, start_tools=False)) as c:
        login(c)
        pid = hub.queue("runpod_terminate_pod", {"pod_id": "../x"})
        resp = c.post(f"/api/actions/{pid}/confirm", headers=H)
        assert resp.status_code == 400 and "invalid pod id" in resp.json()["detail"] and calls == []
        good = c.post("/api/compute/runpod/pods/pod1/terminate", headers=H).json()["pending"]
        assert hub.pending[good]["args"] == {"pod_id": "pod1"}  # no name known: the list is empty


def test_pod_name_on_the_confirm_card_is_printable_ascii(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(auth, "STATE_DIR", tmp_path)
    monkeypatch.setenv("JARVIS_SESSION_SECRET", "test-secret-test-secret")
    auth._attempts.clear()
    pods = [{"id": "pod1", "name": "safe\u202e\x07name"}]
    compute = Compute(env={"RUNPOD_API_KEY": "rpa_" + "k" * 12}, runpod_transport=httpx.MockTransport(lambda r: httpx.Response(200, json=pods)))
    hub = Hub(compute)
    with TestClient(create_app(compute=compute, hub=hub, start_tools=False)) as c:
        login(c)
        pid = c.post("/api/compute/runpod/pods/pod1/terminate", headers=H).json()["pending"]
        assert hub.pending[pid]["args"] == {"pod_id": "pod1", "name": "safename"}


def test_chat_proposals_are_validated_when_queued():
    hub = Hub(FakeCompute())
    for name, args in [("runpod_create_pod", {"name": "ok", "gpu_type": "NVIDIA L4", "hours": "abc"}),
                       ("runpod_create_pod", {"name": "Bad Name", "gpu_type": "NVIDIA L4", "hours": 1}),
                       ("kaggle_run_script", {"slug": "../x", "code": "print(1)"})]:
        with pytest.raises(ValueError):
            hub.queue(name, args)
    assert hub.pending == {}
    pid = hub.queue("runpod_create_pod", {"name": "ok", "gpu_type": "NVIDIA L4", "hours": "1.5", "extra": 1})
    assert hub.pending[pid]["args"] == {"name": "ok", "gpu_type": "NVIDIA L4", "hours": 1.5}
