"""Kaggle notebooks (free GPU, weekly quota) through the official `kaggle` CLI with KAGGLE_API_TOKEN."""
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,49}$")
REF_RE = re.compile(r"^[A-Za-z0-9_.-]+/[a-z0-9-]+$")


class Kaggle:
    def __init__(self, token, username, runner=None):
        self.username, self.runner = username, runner or self._run
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": tempfile.gettempdir(), "KAGGLE_API_TOKEN": token, "KAGGLE_USERNAME": username}

    async def _run(self, *args, timeout=300):
        proc = await asyncio.create_subprocess_exec("kaggle", *args, env=self.env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode, out.decode(errors="replace")

    async def push_script(self, slug, code, gpu=True, internet=False):
        """Upload a Python script as a private notebook and start it; returns the kernel reference (user/slug)."""
        if not SLUG_RE.match(slug):
            raise ValueError("slug must be 3-50 characters: lowercase letters, digits, dashes")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "script.py").write_text(code)
            Path(tmp, "kernel-metadata.json").write_text(json.dumps({
                "id": f"{self.username}/{slug}", "title": slug, "code_file": "script.py", "language": "python", "kernel_type": "script",
                "is_private": True, "enable_gpu": gpu, "enable_internet": internet, "dataset_sources": [], "competition_sources": [], "kernel_sources": [],
            }))
            rc, out = await self.runner("kernels", "push", "-p", tmp)
        if rc != 0:
            raise RuntimeError(f"kaggle push failed: {out[-300:]}")
        return f"{self.username}/{slug}"

    async def status(self, ref):
        if not REF_RE.match(ref):
            raise ValueError("invalid kernel reference")
        rc, out = await self.runner("kernels", "status", ref)
        match = re.search(r"(?i)status\s*\"?([\w.]+)\"?", out)
        return {"ref": ref, "status": match.group(1).split(".")[-1].lower() if match else out.strip()[-120:], "ok": rc == 0}

    async def output(self, ref, dest):
        if not REF_RE.match(ref):
            raise ValueError("invalid kernel reference")
        Path(dest).mkdir(parents=True, exist_ok=True)
        rc, out = await self.runner("kernels", "output", ref, "-p", str(dest))
        if rc != 0:
            raise RuntimeError(f"kaggle output failed: {out[-300:]}")
        return sorted(p.name for p in Path(dest).iterdir())
