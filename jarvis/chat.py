"""Jarvis chat: streams a model reply, runs read-only tools, and turns every state-changing call into a pending action
that only the logged-in owner can confirm in the UI."""
import json
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

import runtime  # noqa: E402  (connect_servers, MUTATING, LIVE)
from redact import redact  # noqa: E402

PROMPT = (ROOT / "jarvis/prompts/jarvis.txt").read_text().strip()
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_\w{20,}|hf_[A-Za-z0-9]{20,}|xox[bap]-\S+|rpa_[A-Za-z0-9]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
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


def contains_secret(text):
    return bool(SECRET_RE.search(text or ""))


class Hub:
    """MCP tools from agents/mcp_servers.yaml plus the compute tools, behind one call interface."""

    def __init__(self, compute):
        self.compute, self.routes, self.specs, self.stack = compute, {}, [], None
        self.pending = {}

    async def start(self):
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

    async def execute(self, name, args):
        if name in LOCAL_TOOLS:
            return json.dumps(await self.compute.run(name, args))[:4000]
        if name not in self.routes:
            return "error: unknown tool"
        server, session, tool = self.routes[name]
        if server == "infra" and tool in runtime.MUTATING:
            args = {**args, "dry_run": not runtime.LIVE}
        return str((await session.call_tool(tool, args)).content)[:4000]

    def queue(self, name, args):
        self._expire()
        pid = secrets.token_urlsafe(8)
        self.pending[pid] = {"name": name, "args": args, "ts": time.time()}
        return pid

    def _expire(self):
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if now - v["ts"] < PENDING_TTL_S}

    async def confirm(self, pid):
        self._expire()
        item = self.pending.pop(pid, None)
        if item is None:
            raise KeyError("unknown or expired action")
        return await self.execute(item["name"], item["args"])

    def discard(self, pid):
        return self.pending.pop(pid, None) is not None


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
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        yield {"type": "done"}
