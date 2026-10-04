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
