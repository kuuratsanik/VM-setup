import time

import pyotp
import pytest

from jarvis import auth


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "STATE_DIR", tmp_path)
    auth._attempts.clear()


def test_no_default_account():
    assert not auth.configured()
    assert not auth.verify("1.1.1.1", "owner", "anything-at-all")


def test_password_roundtrip_and_minimum_length():
    with pytest.raises(ValueError):
        auth.set_password("owner", "short")
    auth.set_password("owner", "correct horse battery")
    assert auth.verify("1.1.1.1", "owner", "correct horse battery")
    assert not auth.verify("1.1.1.1", "owner", "wrong horse battery")
    assert not auth.verify("1.1.1.1", "root", "correct horse battery")


def test_hash_is_not_plaintext_and_file_is_private(tmp_path):
    auth.set_password("owner", "correct horse battery")
    text = (tmp_path / "auth.json").read_text()
    assert "correct horse" not in text and "argon2" in text
    assert (tmp_path / "auth.json").stat().st_mode & 0o077 == 0


def test_lockout_after_repeated_failures():
    auth.set_password("owner", "correct horse battery")
    for _ in range(auth.MAX_ATTEMPTS):
        assert not auth.verify("9.9.9.9", "owner", "nope-nope-nope")
    assert auth.locked("9.9.9.9")
    assert not auth.verify("9.9.9.9", "owner", "correct horse battery")  # even the right password is refused while locked
    assert auth.verify("8.8.8.8", "owner", "correct horse battery")  # other clients are unaffected


def test_totp_is_required_only_after_confirmation():
    auth.set_password("owner", "correct horse battery")
    secret, uri = auth.enroll_totp()
    assert uri.startswith("otpauth://") and not auth.totp_enabled()
    assert auth.verify("1.1.1.1", "owner", "correct horse battery")  # pending secret does not lock the owner out
    assert auth.confirm_totp(pyotp.TOTP(secret).now()) and auth.totp_enabled()
    assert not auth.verify("1.1.1.1", "owner", "correct horse battery")
    assert not auth.verify("1.1.1.1", "owner", "correct horse battery", "000000")
    assert not auth.verify("1.1.1.1", "owner", "correct horse battery", pyotp.TOTP(secret).now())  # the confirm code cannot be reused
    later = time.time() + 30  # a code from the next time step
    assert auth.verify("1.1.1.1", "owner", "correct horse battery", pyotp.TOTP(secret).at(later), now=later)
