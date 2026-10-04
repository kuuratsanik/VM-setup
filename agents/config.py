"""Load /etc/vmsetup/secrets.env into the environment when running outside systemd."""
import os
from pathlib import Path

ENV_FILE = Path(os.environ.get("VMSETUP_ENV_FILE", "/etc/vmsetup/secrets.env"))


def load():
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if value:
                os.environ.setdefault(key.strip(), value.strip())


load()
