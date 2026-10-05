import json
import time
from types import SimpleNamespace

import pyotp
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from jarvis import auth

H = {"X-Requested-With": "jarvis"}
PW = "correct horse battery"


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "STATE_DIR", tmp_path)
    monkeypatch.delenv("JARVIS_TRUSTED_PROXIES", raising=False)
    auth.set_password("owner", PW)
    return tmp_path


def req(peer, xff=None):
    raw = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=Headers(raw=raw))


def fail(ip, n=1, now=1000.0):
    for _ in range(n):
        auth.verify(ip, "owner", "nope-nope-nope", now=now)


def test_xff_ignored_without_trusted_proxy():
    assert auth.client_ip(req("10.0.0.1", "6.6.6.6")) == "10.0.0.1"


def test_xff_ignored_from_untrusted_peer(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUSTED_PROXIES", "10.0.0.0/24")
    assert auth.client_ip(req("203.0.113.9", "6.6.6.6")) == "203.0.113.9"


def test_xff_honored_from_trusted_proxy(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUSTED_PROXIES", "10.0.0.1")
    assert auth.client_ip(req("10.0.0.1", "198.51.100.7")) == "198.51.100.7"


def test_spoofed_leftmost_entries_ignored(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUSTED_PROXIES", "10.0.0.0/24, 10.1.0.1")
    assert auth.client_ip(req("10.0.0.1", "6.6.6.6, 198.51.100.7, 10.1.0.1")) == "198.51.100.7"
    assert auth.client_ip(req("10.0.0.1", "10.0.0.5")) == "10.0.0.1"  # all hops trusted -> peer


def test_malformed_xff_falls_back_to_peer(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUSTED_PROXIES", "10.0.0.1")
    for bad in ("garbage", "", ",,", "nope, also-nope"):
        assert auth.client_ip(req("10.0.0.1", bad)) == "10.0.0.1"
    assert auth.client_ip(req("10.0.0.1")) == "10.0.0.1"  # header absent


def test_garbage_hops_are_skipped_not_trusted(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUSTED_PROXIES", "10.0.0.1")
    assert auth.client_ip(req("10.0.0.1", "198.51.100.7, garbage")) == "198.51.100.7"
    assert auth.client_ip(req("10.0.0.1", "198.51.100.7, , 10.0.0.1")) == "198.51.100.7"


def test_port_suffix_hop_is_rejected_consistently(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUSTED_PROXIES", "10.0.0.1")
    assert auth.client_ip(req("10.0.0.1", "198.51.100.7:4444")) == "10.0.0.1"
    assert auth.client_ip(req("10.0.0.1", "198.51.100.7, 203.0.113.5:99")) == "198.51.100.7"


def test_ipv4_mapped_peer_and_hops_are_unwrapped(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUSTED_PROXIES", "10.0.0.1")
    assert auth.client_ip(req("::ffff:10.0.0.1", "198.51.100.7")) == "198.51.100.7"
    assert auth.client_ip(req("::ffff:10.0.0.1", "::ffff:198.51.100.7")) == "198.51.100.7"
    assert auth.client_ip(req("::ffff:203.0.113.9", "6.6.6.6")) == "203.0.113.9"


def test_multiple_xff_headers_are_joined(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUSTED_PROXIES", "10.0.0.1")
    r = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"),
                        headers=Headers(raw=[(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-for", b"198.51.100.7")]))
    assert auth.client_ip(r) == "198.51.100.7"


def test_trust_all_means_xff_ignored(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUSTED_PROXIES", "0.0.0.0/0")
    assert auth.client_ip(req("10.0.0.1", "6.6.6.6")) == "10.0.0.1"


def test_ipv6_lockout_keyed_by_slash_64():
    fail("2001:db8:1:2::1", 2)
    fail("2001:db8:1:2:aaaa::9", 2)
    fail("2001:db8:1:2:ffff:ffff:ffff:ffff", 1)  # rotating inside the /64 keeps counting
    assert auth.locked("2001:db8:1:2::77", 1001.0)
    assert not auth.locked("2001:db8:1:3::1", 1001.0)  # a different /64 is unaffected
    fail("::ffff:9.9.9.9", auth.MAX_ATTEMPTS)
    assert auth.locked("9.9.9.9", 1001.0)  # mapped form shares the IPv4 key


def test_enforcement_holds_when_state_dir_unwritable(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(auth, "_lock_file", lambda: blocker / "lockout.json")  # parent is a file: every write fails
    auth.reset_lockout_state()
    with caplog.at_level("ERROR", logger="jarvis.auth"):
        fail("4.4.4.4", auth.MAX_ATTEMPTS)
        fail("4.4.4.4", 3)
    assert auth.locked("4.4.4.4", 1001.0)
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1  # rate-limited


def test_bad_proxy_setting_trusts_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_TRUSTED_PROXIES", "not-an-ip")
    assert auth.client_ip(req("10.0.0.1", "6.6.6.6")) == "10.0.0.1"


def test_lockout_survives_restart(state):
    fail("9.9.9.9", auth.MAX_ATTEMPTS)
    assert (state / "lockout.json").stat().st_mode & 0o077 == 0
    auth.reset_lockout_state()  # a new process only has the file
    assert auth.locked("9.9.9.9", 1001.0)
    assert not auth.locked("9.9.9.9", 1000.0 + auth.LOCKOUT_S + 1)


def test_corrupt_or_missing_state_is_empty(state):
    assert not auth.locked("1.1.1.1")
    for junk in ("{not json", "[]", '{"ips": 5, "global": 1}', '{"ips": {"a": "x"}, "global": [1]}'):
        (state / "lockout.json").write_text(junk)
        auth.reset_lockout_state()  # force a reload from the (corrupt) file
        assert not auth.locked("1.1.1.1")
        assert auth.verify("1.1.1.1", "owner", PW)


def test_expired_pruned_and_keys_capped(state, monkeypatch):
    monkeypatch.setattr(auth, "MAX_KEYS", 5)
    doc = {"ips": {f"1.0.0.{i}": [1, 1000.0 + i, 0] for i in range(20)}, "global": [0, 1000.0, 0, 0]}
    doc["ips"]["old"] = [1, 1.0, 0]
    (state / "lockout.json").write_text(json.dumps(doc))
    fail("2.2.2.2", now=1030.0)
    saved = json.loads((state / "lockout.json").read_text())["ips"]
    assert "old" not in saved and len(saved) <= 6 and "2.2.2.2" in saved


def trip_global(now, salt=0):
    for i in range(auth.GLOBAL_MAX):
        fail(f"7.{salt}.{i // 250}.{i % 250}", now=now)  # one failure per IP: no per-IP lock


def test_global_tarpit_triggers_expires_and_resets():
    assert auth.tarpit_delay(1000.0) == 0
    trip_global(1000.0)
    assert 0 < auth.tarpit_delay(1001.0) <= auth.GLOBAL_DELAY_MAX_S
    assert not auth.locked("5.5.5.5", 1001.0)  # no hard global lock
    assert auth.tarpit_delay(1000.0 + auth.WINDOW_S + 1) == 0  # expires
    trip_global(2000.0)
    assert auth.verify("5.5.5.5", "owner", PW, now=2001.0)  # success resets
    assert auth.tarpit_delay(2002.0) == 0


def test_global_tarpit_escalates_but_is_capped():
    now, delays = 1000.0, []
    for n in range(12):
        trip_global(now, salt=n)
        delays.append(auth.tarpit_delay(now))
        now += 1
    assert delays == sorted(delays) and delays[0] < delays[-1] == auth.GLOBAL_DELAY_MAX_S


def test_owner_logs_in_while_attacker_retrips():
    """Attacker re-trips the global mechanism every window from throwaway IPs; the owner is never rejected."""
    now, ok = 1000.0, 0
    for round_ in range(20):
        for i in range(auth.GLOBAL_MAX):
            fail(f"8.{round_}.0.{i}", now=now)
        assert auth.tarpit_delay(now) > 0
        ok += auth.verify("203.0.113.50", "owner", PW, now=now + 1)  # owner, correct password
        assert auth.tarpit_delay(now + 1) == 0  # and success clears the tarpit
        now += 30
    assert ok == 20  # 100% availability (the hard-lock design allowed about 0.002%)


def test_http_failed_login_is_tarpitted_but_correct_login_is_not(monkeypatch):
    from jarvis.app import create_app

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("jarvis.app.asyncio", SimpleNamespace(sleep=fake_sleep, Semaphore=__import__("asyncio").Semaphore))
    monkeypatch.setenv("JARVIS_SESSION_SECRET", "x" * 32)
    trip_global(time.time())
    client = TestClient(create_app())
    assert client.post("/api/login", headers=H, json={"username": "owner", "password": "bad-bad-bad-bad"}).status_code == 401
    assert slept and slept[-1] > 0
    n = len(slept)
    assert client.post("/api/login", headers=H, json={"username": "owner", "password": PW}).status_code == 200
    assert len(slept) == n


def test_unknown_username_still_runs_hash(monkeypatch):
    calls = []
    real = auth._hasher

    class Spy:
        def verify(self, h, p):
            calls.append(h)
            return real.verify(h, p)

    monkeypatch.setattr(auth, "_hasher", Spy())
    assert not auth.verify("6.6.6.6", "nobody", PW, now=1000.0)
    assert calls == [auth._DUMMY_HASH]


def test_success_resets_ip_counter():
    fail("3.3.3.3", auth.MAX_ATTEMPTS - 1)
    assert auth.verify("3.3.3.3", "owner", PW, now=1000.0)
    fail("3.3.3.3", auth.MAX_ATTEMPTS - 1)
    assert not auth.locked("3.3.3.3", 1001.0)


def test_http_429_with_retry_after(monkeypatch):
    from jarvis.app import create_app

    monkeypatch.setenv("JARVIS_SESSION_SECRET", "x" * 32)
    client = TestClient(create_app())
    for _ in range(auth.MAX_ATTEMPTS):
        assert client.post("/api/login", headers=H, json={"username": "owner", "password": "bad-bad-bad-bad"}).status_code == 401
    r = client.post("/api/login", headers=H, json={"username": "owner", "password": PW})
    assert r.status_code == 429
    assert 0 < int(r.headers["Retry-After"]) <= auth.LOCKOUT_S
    assert "detail" in r.json()


def enable_totp():
    secret, _ = auth.enroll_totp()
    assert auth.confirm_totp(pyotp.TOTP(secret).now())
    doc = json.loads((auth.STATE_DIR / "auth.json").read_text())
    doc["totp_last_step"] = -1  # tests use arbitrary clocks; confirm's step recording has its own test
    (auth.STATE_DIR / "auth.json").write_text(json.dumps(doc))
    return pyotp.TOTP(secret)


def test_confirm_totp_records_step_so_enrollment_code_cannot_log_in():
    secret, _ = auth.enroll_totp()
    code = pyotp.TOTP(secret).now()
    assert auth.confirm_totp(code)
    assert not auth.verify("1.1.1.1", "owner", PW, code)  # same step as the enrollment code
    assert auth.verify("1.1.1.1", "owner", PW, pyotp.TOTP(secret).at(time.time() + 60), now=time.time() + 60)


def test_concurrent_same_code_accepted_exactly_once():
    import threading

    totp = enable_totp()
    code, results = totp.at(9000.0), []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(auth._accept_totp(code, 9000.0))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1


def test_session_gen_bumps_survive_concurrent_login_writes():
    import threading

    totp = enable_totp()
    before = auth.session_gen()
    stop = threading.Event()

    def logins():
        t = 20000.0
        while not stop.is_set():
            auth._accept_totp(totp.at(t), t)  # rewrites auth.json like a login does
            t += 30

    bumpers = [threading.Thread(target=lambda: [auth.bump_session_gen() for _ in range(10)]) for _ in range(4)]
    writer = threading.Thread(target=logins)
    writer.start()
    [t.start() for t in bumpers]
    [t.join() for t in bumpers]
    stop.set()
    writer.join()
    assert auth.session_gen() == before + 40


def test_clock_stepping_backwards_warns(caplog):
    totp = enable_totp()
    assert auth._accept_totp(totp.at(50000.0), 50000.0)
    auth._last_skew_warn = 0.0
    with caplog.at_level("WARNING", logger="jarvis.auth"):
        assert not auth._accept_totp(totp.at(40000.0), 40000.0)
        assert not auth._accept_totp(totp.at(40000.0), 40000.0)  # rate-limited
    assert len([r for r in caplog.records if "clock" in r.message]) == 1


def lock_out(client):
    for _ in range(auth.MAX_ATTEMPTS):
        client.post("/api/login", headers=H, json={"username": "owner", "password": "bad-bad-bad-bad", "code": "000000"})


def app_client(monkeypatch):
    from jarvis.app import create_app

    monkeypatch.setenv("JARVIS_SESSION_SECRET", "x" * 32)
    return TestClient(create_app())


def test_totp_bypasses_lock_and_resets(monkeypatch):
    totp = enable_totp()
    client = app_client(monkeypatch)
    lock_out(client)
    r = client.post("/api/login", headers=H, json={"username": "owner", "password": PW, "code": totp.now()})
    assert r.status_code == 200
    # counters were reset: the client is no longer locked and a wrong password is a plain 401 again
    r = client.post("/api/login", headers=H, json={"username": "owner", "password": "bad-bad-bad-bad", "code": "000000"})
    assert r.status_code == 401


def test_wrong_totp_or_password_never_bypasses_lock(monkeypatch):
    totp = enable_totp()
    client = app_client(monkeypatch)
    lock_out(client)
    for body in ({"password": PW, "code": "000000"}, {"password": "bad-bad-bad-bad", "code": totp.now()}, {"password": PW}):
        r = client.post("/api/login", headers=H, json={"username": "owner", **body})
        assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0  # same answer whichever factor was wrong
    r = client.post("/api/login", headers=H, json={"username": "owner", "password": PW, "code": totp.now()})
    assert r.status_code == 200


SHARED = "127.0.0.1"  # every client behind an unconfigured proxy


def test_wrong_password_attacker_cannot_block_owner_bypass():
    """Adversary scenario: 5 wrong passwords + 10 bogus bypass attempts per round on the shared key."""
    totp = enable_totp()
    now = 10_000.0
    for _ in range(30):
        fail(SHARED, auth.MAX_ATTEMPTS + 10, now=now)
        assert auth.locked(SHARED, now + 1)
        assert auth.verify(SHARED, "owner", PW, totp.at(now + 2), now=now + 2)  # owner gets in every round
        now += 31  # next TOTP step
    assert not auth.locked(SHARED, now)


def test_correct_password_wrong_totp_spends_account_budget_and_warns(caplog):
    totp = enable_totp()
    fail(SHARED, auth.MAX_ATTEMPTS, now=5000.0)
    with caplog.at_level("WARNING", logger="jarvis.auth"):
        for _ in range(auth.TOTP_MAX):
            assert not auth.verify(SHARED, "owner", PW, "000000", now=5001.0)
        assert "rotate" in caplog.text and "compromised" in caplog.text
        assert not auth.verify(SHARED, "owner", PW, totp.at(5002.0), now=5002.0)  # refused even with a valid code
        other = "198.51.100.9"  # the budget is per account, not per key
        fail(other, auth.MAX_ATTEMPTS, now=5002.0)
        assert not auth.verify(other, "owner", PW, totp.at(5003.0), now=5003.0)
    assert auth.verify(SHARED, "owner", PW, totp.at(5001.0 + auth.TOTP_WINDOW_S + 40), now=5001.0 + auth.TOTP_WINDOW_S + 40)  # budget expires


def test_totp_code_cannot_be_replayed():
    totp = enable_totp()
    assert auth.verify("1.1.1.1", "owner", PW, totp.at(7000.0), now=7000.0)
    assert not auth.verify("1.1.1.1", "owner", PW, totp.at(7000.0), now=7001.0)  # same step
    assert not auth.verify("1.1.1.1", "owner", PW, totp.at(6970.0), now=7001.0)  # older step
    assert auth.verify("1.1.1.1", "owner", PW, totp.at(7031.0), now=7031.0)
    fail(SHARED, auth.MAX_ATTEMPTS, now=7100.0)
    assert auth.verify(SHARED, "owner", PW, totp.at(7101.0), now=7101.0)  # bypass path
    assert not auth.verify("1.1.1.1", "owner", PW, totp.at(7101.0), now=7102.0)  # replay across paths
    assert json.loads((auth.STATE_DIR / "auth.json").read_text())["totp_last_step"] == int(7101.0 // 30)


BAD_CODES = ["12345é", "12a456", "1234567", "", "١٢٣٤٥٦", " 12345", "12345\n"]


def fresh_lockout():
    auth.reset_lockout_state()
    (auth.STATE_DIR / "lockout.json").unlink(missing_ok=True)


def test_malformed_codes_are_plain_wrong_codes_over_http(monkeypatch):
    enable_totp()
    client = app_client(monkeypatch)
    for code in BAD_CODES:
        fresh_lockout()
        right = client.post("/api/login", headers=H, json={"username": "owner", "password": PW, "code": code})
        wrong = client.post("/api/login", headers=H, json={"username": "owner", "password": "bad-bad-bad-bad", "code": code})
        assert right.status_code == wrong.status_code == 401, code  # no 500, no oracle
        assert right.json() == wrong.json()
        fresh_lockout()
        for _ in range(auth.MAX_ATTEMPTS):  # the correct-password probes are counted and lock the client
            assert client.post("/api/login", headers=H, json={"username": "owner", "password": PW, "code": code}).status_code == 401
        assert auth.locked("testclient") or auth.locked("127.0.0.1")
        # while locked: same uniform 429 either way, and only the correct-password probes spend the TOTP budget
        a = client.post("/api/login", headers=H, json={"username": "owner", "password": PW, "code": code})
        b = client.post("/api/login", headers=H, json={"username": "owner", "password": "bad-bad-bad-bad", "code": code})
        assert a.status_code == b.status_code == 429 and a.json() == b.json()
        assert auth._totp_budget_exhausted(time.time()) is False
        assert auth._mem["totp"][0] == 1, code


def test_lone_surrogate_password_is_a_wrong_password():
    assert not auth.verify("1.1.1.1", "owner", "bad\ud800bad-bad")  # direct call; the HTTP layer rejects it earlier
    assert auth.verify("1.1.1.2", "owner", PW)


def test_totp_confirm_rejects_malformed_codes_cleanly(monkeypatch):
    client = app_client(monkeypatch)
    assert client.post("/api/login", headers=H, json={"username": "owner", "password": PW}).status_code == 200
    secret, _ = auth.enroll_totp()
    for code in BAD_CODES + [None, 123456, ["1"]]:
        r = client.post("/api/totp/confirm", headers=H, json={"code": code})
        assert r.status_code == 400, code
    assert client.post("/api/totp/confirm", headers=H, json={"code": pyotp.TOTP(secret).now()}).status_code == 200


def test_no_totp_lock_is_not_bypassed(monkeypatch):
    client = app_client(monkeypatch)
    lock_out(client)
    assert client.post("/api/login", headers=H, json={"username": "owner", "password": PW}).status_code == 429


def test_unconfigured_proxy_warning(caplog):
    auth._last_warn = 0.0
    with caplog.at_level("WARNING", logger="jarvis.auth"):
        assert auth.client_ip(req("127.0.0.1", "6.6.6.6")) == "127.0.0.1"
        auth.client_ip(req("127.0.0.1", "6.6.6.6"))  # rate-limited
        auth.client_ip(req("203.0.113.9", "6.6.6.6"))  # public peer: no warning
        auth.client_ip(req("127.0.0.1"))  # no XFF: no warning
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    assert "JARVIS_TRUSTED_PROXIES" in caplog.text


def test_hash_saturation_returns_429_without_hashing_and_loop_stays_responsive(monkeypatch):
    import asyncio
    import threading

    import httpx

    from jarvis.app import create_app

    monkeypatch.setenv("JARVIS_SESSION_SECRET", "x" * 32)
    release, started, calls = threading.Event(), threading.Semaphore(0), []

    def slow_verify(*args, **kwargs):
        calls.append(1)
        started.release()
        release.wait(10)
        return False

    monkeypatch.setattr(auth, "verify", slow_verify)
    body = {"username": "owner", "password": "bad-bad-bad-bad"}

    async def scenario():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            tasks = [asyncio.create_task(c.post("/api/login", headers=H, json=body)) for _ in range(4)]
            for _ in range(4):
                assert await asyncio.to_thread(started.acquire, True, 5)
            extra = await c.post("/api/login", headers=H, json=body)  # slots full
            state = await asyncio.wait_for(c.get("/api/auth/state"), 2)  # event loop not blocked by the hashing
            release.set()
            results = await asyncio.gather(*tasks)
            return extra, state, results

    extra, state, results = asyncio.run(scenario())
    assert extra.status_code == 429 and extra.headers["Retry-After"] == "1"
    assert len(calls) == 4  # the refused request never reached verify
    assert state.status_code == 200
    assert all(r.status_code == 401 for r in results)
