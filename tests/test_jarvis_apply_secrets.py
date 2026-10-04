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
