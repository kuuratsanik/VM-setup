import json

import pytest

from jarvis import apply_secrets as ap


@pytest.fixture
def env(tmp_path, monkeypatch):
    staged, secrets, jenv = tmp_path / "staged.json", tmp_path / "secrets.env", tmp_path / "jarvis.env"
    secrets.write_text("LITELLM_KEY=keepme\nOPENAI_API_KEY=old\n")
    monkeypatch.setattr(ap, "STAGED", staged)
    monkeypatch.setattr(ap, "SECRETS", secrets)
    monkeypatch.setattr(ap, "JARVIS_ENV", jenv)
    monkeypatch.setattr(ap, "ALLOWED", {"OPENAI_API_KEY": [secrets], "RUNPOD_API_KEY": [secrets, jenv]})
    monkeypatch.setattr(ap, "RESTARTS", ["true"])
    return staged, secrets, jenv


def test_applies_allowlisted_values_and_preserves_the_rest(env):
    staged, secrets, jenv = env
    staged.write_text(json.dumps({"OPENAI_API_KEY": "sk-newvalue12345678", "RUNPOD_API_KEY": "rpa_abcdefgh1234"}))
    ap.main()
    lines = secrets.read_text().splitlines()
    assert "LITELLM_KEY=keepme" in lines and "OPENAI_API_KEY=sk-newvalue12345678" in lines and "RUNPOD_API_KEY=rpa_abcdefgh1234" in lines
    assert jenv.read_text().strip() == "RUNPOD_API_KEY=rpa_abcdefgh1234"
    assert not staged.exists()
    assert "OPENAI_API_KEY" not in jenv.read_text()  # Jarvis itself never gets keys it does not need


def test_rejects_unknown_names_and_injection_attempts(env):
    staged, secrets, jenv = env
    staged.write_text(json.dumps({
        "LD_PRELOAD": "/tmp/evil.so", "PATH": "/tmp", "OPENAI_API_KEY": "abc\nLITELLM_KEY=owned", "RUNPOD_API_KEY": "has space here",
    }))
    ap.main()
    text = secrets.read_text()
    assert "LD_PRELOAD" not in text and "owned" not in text and "LITELLM_KEY=keepme" in text and "OPENAI_API_KEY=old" in text
    assert not jenv.exists()


def test_garbage_staged_file_is_discarded(env):
    staged, secrets, _ = env
    staged.write_text("{not json")
    with pytest.raises(SystemExit):
        ap.main()
    assert not staged.exists() and "LITELLM_KEY=keepme" in secrets.read_text()


def _marker_env(env, tmp_path, monkeypatch, owner_uid=0, mode=0o755):
    import os
    import types

    root_dir = tmp_path / "etc"
    root_dir.mkdir()
    marker = root_dir / "jarvis-configured.json"
    monkeypatch.setattr(ap, "MARKER", marker)
    real = type(root_dir).stat

    def fake_stat(self, *a, **k):
        st = real(self, *a, **k)
        if self == root_dir:
            return types.SimpleNamespace(st_uid=owner_uid, st_mode=0o040000 | mode)
        return st

    monkeypatch.setattr(type(root_dir), "stat", fake_stat)
    return root_dir, marker


def test_marker_lists_names_only(env, tmp_path, monkeypatch):
    staged, secrets, jenv = env
    root_dir, marker = _marker_env(env, tmp_path, monkeypatch)
    staged.write_text(json.dumps({"RUNPOD_API_KEY": "rpa_abcdefgh1234"}))
    ap.main()
    assert json.loads(marker.read_text()) == ["OPENAI_API_KEY", "RUNPOD_API_KEY"]
    assert "rpa_abcdefgh1234" not in marker.read_text() and "old" not in marker.read_text()


def test_marker_never_follows_a_planted_symlink(env, tmp_path, monkeypatch):
    staged, secrets, jenv = env
    root_dir, marker = _marker_env(env, tmp_path, monkeypatch)
    victim = tmp_path / "victim"
    victim.write_text("root-owned content")
    victim.chmod(0o600)
    (root_dir / "jarvis-configured.json.tmp").symlink_to(victim)
    marker.symlink_to(victim)
    ap.write_marker([secrets])
    assert victim.read_text() == "root-owned content" and (victim.stat().st_mode & 0o777) == 0o600
    assert not marker.is_symlink() and json.loads(marker.read_text()) == ["OPENAI_API_KEY"]


def test_marker_refuses_a_parent_not_owned_by_root_or_writable(env, tmp_path, monkeypatch):
    staged, secrets, jenv = env
    for uid, mode in ((1000, 0o755), (0, 0o775), (0, 0o757)):
        sub = tmp_path / f"{uid}-{mode}"
        sub.mkdir()
        root_dir, marker = _marker_env(env, sub, monkeypatch, uid, mode)
        with pytest.raises(RuntimeError):
            ap.write_marker([secrets])
        assert not marker.exists()
    staged.write_text(json.dumps({"OPENAI_API_KEY": "sk-newvalue12345678"}))
    ap.main()  # a refused marker must not block applying the key
    assert "OPENAI_API_KEY=sk-newvalue12345678" in secrets.read_text()
