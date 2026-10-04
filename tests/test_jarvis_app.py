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
    # jarvis.manage still enrolls on the host
    import sys
    from jarvis import manage
    monkey_argv = sys.argv
    try:
        sys.argv = ["manage", "enroll-totp"]
        manage.main()
    finally:
        sys.argv = monkey_argv


def test_chat_secret_check_applies_to_user_turns_only():
    from jarvis.chat import history_has_secret
    pem = "-----BEGIN RSA PRIVATE KEY-----"
    assert not history_has_secret([{"role": "user", "content": "how do I rotate a key?"}, {"role": "assistant", "content": f"A key starts with {pem}"}])
    assert history_has_secret([{"role": "assistant", "content": "ok"}, {"role": "user", "content": pem}])


def test_chat_endpoint_accepts_assistant_pem_quote(client):
    login(client)
    msgs = [{"role": "assistant", "content": "-----BEGIN PRIVATE KEY-----"}, {"role": "user", "content": "hi"}]
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


def test_concurrent_confirm_http_status_is_409(client, tmp_path, monkeypatch):
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
