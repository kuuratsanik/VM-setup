import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for sub in ("agents", "media-mcp", "training"):
    sys.path.insert(0, str(ROOT / sub))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _jarvis_lockout_isolation(tmp_path, monkeypatch):
    """Every test starts with empty login-lockout state and a private Jarvis state dir."""
    try:
        from jarvis import auth
    except ImportError:  # Jarvis deps absent: nothing to isolate
        yield
        return
    monkeypatch.setattr(auth, "STATE_DIR", tmp_path)
    auth.reset_lockout_state()
    yield
    auth.reset_lockout_state()
