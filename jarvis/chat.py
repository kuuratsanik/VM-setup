"""Jarvis chat: streams a model reply, runs read-only tools, and turns every state-changing call into a pending action
that only the logged-in owner can confirm in the UI."""
import json
import logging
import os
import re
import secrets
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

import runtime  # noqa: E402  (connect_servers, MUTATING)
import approvals  # noqa: E402
from actions import gate  # noqa: E402
from policy import Policy  # noqa: E402
from redact import redact  # noqa: E402

PROMPT = (ROOT / "jarvis/prompts/jarvis.txt").read_text().strip()
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_\w{20,}|hf_[A-Za-z0-9]{20,}|xox[bap]-\S+|rpa_[A-Za-z0-9]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
log = logging.getLogger(__name__)
CLAIM_STALE_S = 300
PENDING_TTL_S = 600
MAX_STEPS = 8

LOCAL_TOOLS = {
    "runpod_list_pods": ("List RunPod pods.", {}, False),
    "kaggle_status": ("Status of a Kaggle notebook run.", {"ref": {"type": "string", "description": "user/slug"}}, False),
    "runpod_create_pod": ("Propose renting a RunPod GPU pod (owner confirms; hourly cap and TTL apply).", {
        "name": {"type": "string"}, "gpu_type": {"type": "string"}, "hours": {"type": "number"}}, True),
    "kaggle_run_script": ("Propose running a Python script on Kaggle (free GPU; owner confirms).", {
        "slug": {"type": "string"}, "code": {"type": "string"}, "gpu": {"type": "boolean"}}, True),
}

REST_ONLY = {"runpod_terminate_pod"}  # queued only by the REST endpoint, never offered to or callable by the model; run only via confirm


def contains_secret(text):
    return bool(SECRET_RE.search(text or ""))


def history_has_secret(messages):
    """Guard against the owner pasting a secret by accident; NOT a security boundary (it checks only user turns, and a client can send any history).
    Only the owner's own turns are screened; an assistant answer may legitimately quote e.g. a PEM header."""
    return any(m.get("role") == "user" and contains_secret(str(m.get("content", ""))) for m in messages)


class ActionInProgress(Exception):
    """Another request already claimed this approval."""


class Hub:
    """MCP tools from agents/mcp_servers.yaml plus the compute tools, behind one call interface."""

    def __init__(self, compute):
        self.compute, self.routes, self.specs, self.stack = compute, {}, [], None
        self.pending = {}
        self.inflight, self.discarded = set(), set()  # compute ids being executed / discarded meanwhile

    async def start(self):
        self.sweep_claimed(max_age=0)  # a fresh process cannot have a live confirm, so every claim is an orphan
        self.stack = AsyncExitStack()
        self.routes, self.specs = await runtime.connect_servers(self.stack)
        for name, (desc, props, _) in LOCAL_TOOLS.items():
            self.specs.append({"type": "function", "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": props}}})

    async def stop(self):
        if self.stack:
            await self.stack.aclose()

    def needs_confirmation(self, name):
        if name in LOCAL_TOOLS:
            return LOCAL_TOOLS[name][2]
        route = self.routes.get(name)
        return bool(route and route[0] == "infra" and route[2] in runtime.MUTATING)

    async def execute(self, name, args, confirmed=False):
        if confirmed and name in REST_ONLY:
            return json.dumps(await self.compute.run(name, args))[:4000]
        if name in LOCAL_TOOLS:
            return json.dumps(await self.compute.run(name, args))[:4000]
        if name not in self.routes:
            return "error: unknown tool"
        server, session, tool = self.routes[name]
        if server == "infra" and tool in runtime.MUTATING:
            raise PermissionError("state-changing tools run only through confirmed approvals")
        return str((await session.call_tool(tool, args)).content)[:4000]

    def queue(self, name, args):
        self._expire()
        route = self.routes.get(name)
        if route and route[0] == "infra" and route[2] in runtime.MUTATING:  # shared, persistent queue (also shown in the Autonomy tab)
            return approvals.queue(name, str(args.get("domain", "")), {k: v for k, v in args.items() if k != "dry_run"}, {"source": "jarvis chat"})
        pid = secrets.token_urlsafe(8)
        self.pending[pid] = {"name": name, "args": args, "ts": time.time()}
        return pid

    def _expire(self):
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if now - v["ts"] < PENDING_TTL_S}

    async def confirm(self, pid):
        """Run an action the owner approved: compute actions directly, infra actions through the policy as human-confirmed."""
        self._expire()
        item = self.pending.pop(pid, None)
        if item is not None:
            self.inflight.add(pid)
            try:
                return await self.execute(item["name"], item["args"], confirmed=True)
            except Exception:  # the pop above is synchronous (double-click safe); a failed run must stay retryable
                if pid not in self.discarded:  # unless the owner discarded it while it ran
                    self.pending[pid] = item
                raise
            finally:
                self.inflight.discard(pid)
                self.discarded.discard(pid)
        doc = approvals.get(pid)
        if doc is None:
            raise KeyError("unknown or expired action")
        route = self.routes.get(doc["action"])
        if not route or route[0] != "infra":
            raise RuntimeError("the infra tools are not available in Jarvis right now")
        session = route[1]
        claimed = self._claim(pid)  # from here on exactly one caller owns this approval

        async def call(tool, a):
            try:
                res = await session.call_tool(tool, a)
                return not res.isError, str(res.content)[:4000]
            except Exception as exc:
                return False, f"error: {exc}"

        try:
            outcome = await gate(Policy(), call, doc["action"], doc["target"], doc["args"], actor="owner", context={"source": "approval"}, confirmed=True)
        except Exception as exc:
            self._finish_claimed(pid, claimed, "error", f"error: {exc}")
            raise
        except BaseException as exc:  # e.g. CancelledError: the tool may or may not have run
            self._finish_claimed(pid, claimed, "unknown", f"interrupted ({type(exc).__name__}); the tool may still have run, check before retrying")
            raise
        self._finish_claimed(pid, claimed, outcome.status, outcome.text)
        return outcome.text

    @staticmethod
    def _claim(pid):
        """Atomically take ownership of a pending approval by renaming its file out of the pending set.
        rename(2) succeeds for exactly one caller, across requests and processes."""
        base = approvals._dir("approvals")
        src, dst = base / f"{pid}.json", base / f"{pid}.claimed"
        try:
            os.rename(src, dst)
        except FileNotFoundError:
            if dst.exists():
                raise ActionInProgress(pid)
            raise KeyError("unknown or expired action")
        return dst

    @staticmethod
    def _finish_claimed(pid, claimed, status, result):
        """Same record approvals.finish writes, taken from the claimed file."""
        try:
            doc = json.loads(claimed.read_text())
        except (OSError, ValueError):
            doc = {"id": pid}
        doc.update({"status": status, "result": str(result)[:500], "finished": time.time()})
        old = os.umask(0o007)
        try:
            (approvals._dir("approvals-done") / f"{pid}.json").write_text(json.dumps(doc))
        except OSError as exc:
            log.error("jarvis: could not record outcome of approval %s (%s): %s", pid, status, exc)
        finally:
            os.umask(old)
            claimed.unlink(missing_ok=True)

    @staticmethod
    def sweep_claimed(max_age=CLAIM_STALE_S):
        """Record claims orphaned by a crash as 'interrupted' (never re-run: the tool may have executed)."""
        try:
            files = list(approvals._dir("approvals").glob("*.claimed"))
        except OSError:
            return 0
        n = 0
        for path in files:
            try:
                if time.time() - path.stat().st_mtime < max_age:
                    continue
            except OSError:
                continue
            Hub._finish_claimed(path.stem, path, "interrupted", "Jarvis stopped while this was running; it may or may not have executed")
            n += 1
        return n

    def discard(self, pid):
        if pid in self.inflight:
            self.discarded.add(pid)
            return True
        if self.pending.pop(pid, None) is not None:
            return True
        try:
            return approvals.finish(pid, "discarded") is not None
        except ValueError:
            return False


class Chat:
    def __init__(self, hub):
        self.hub = hub
        self.llm = AsyncOpenAI(base_url=os.environ.get("LITELLM_URL", "http://127.0.0.1:4000"), api_key=os.environ.get("LITELLM_KEY", "none"))

    async def run(self, history, model, use_tools=True):
        messages = [{"role": "system", "content": PROMPT}] + [{"role": m["role"], "content": m["content"]} for m in history if m["role"] in ("user", "assistant")]
        specs = self.hub.specs if use_tools else []
        for _ in range(MAX_STEPS):
            stream = await self.llm.chat.completions.create(model=model, messages=messages, tools=specs or None, stream=True)
            text, calls = "", {}
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    text += delta.content
                    yield {"type": "delta", "text": delta.content}
                for tc in delta.tool_calls or []:
                    call = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    call["id"] = tc.id or call["id"]
                    call["name"] += (tc.function.name or "") if tc.function else ""
                    call["args"] += (tc.function.arguments or "") if tc.function else ""
            if not calls:
                yield {"type": "done"}
                return
            messages.append({"role": "assistant", "content": text or None, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}} for c in calls.values()]})
            for call in calls.values():
                try:
                    args = json.loads(call["args"] or "{}")
                    if self.hub.needs_confirmation(call["name"]):
                        pid = self.hub.queue(call["name"], args)
                        yield {"type": "pending", "id": pid, "name": call["name"], "args": args}
                        result = f"queued for the owner's confirmation (id {pid}); do not retry"
                    else:
                        result = await self.hub.execute(call["name"], args)
                        yield {"type": "tool", "name": call["name"], "result": redact(result)[:600]}
                except Exception as exc:
                    result = f"error: {exc}"
                    yield {"type": "tool", "name": call["name"], "result": result}
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": redact(result)})  # the model may be cloud-hosted
        yield {"type": "done"}
