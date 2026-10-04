import pytest

import server as media


@pytest.fixture(autouse=True)
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_DIR", tmp_path.resolve())
    return tmp_path


def test_inside_accepts_files_in_media_dir(media_dir):
    (media_dir / "a.png").write_bytes(b"x")
    assert media._inside("a.png").name == "a.png"


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "missing.png"])
def test_inside_rejects_escapes_and_missing(bad):
    with pytest.raises(ValueError):
        media._inside(bad)


def test_save_enforces_size_limit(monkeypatch):
    monkeypatch.setattr(media, "MAX_BYTES", 3)
    with pytest.raises(ValueError):
        media._save(b"toolong", ".bin")


def test_provider_selects_model_alias():
    assert media._model("image", "auto") == "image"
    assert media._model("tts", "local") == "tts-local"
    with pytest.raises(ValueError):
        media._model("stt", "bogus")
