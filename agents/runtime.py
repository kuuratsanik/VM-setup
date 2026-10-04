"""Operator agent: receives Alertmanager webhooks, runs Tier-0 runbooks, escalates to an LLM that uses MCP tools."""
import asyncio
import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import config  # noqa: F401  (loads /etc/vmsetup/secrets.env)
import memory
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI
from redact import redact

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = yaml.safe_load((ROOT / "agents/manifest.yaml").read_text())
KILL_SWITCH = Path(MANIFEST["guardrails"]["kill_switch_file"])
MAX_STEPS = MANIFEST["guardrails"]["max_steps_per_task"]
INCIDENTS = Path(os.environ.get("VMSETUP_INCIDENTS", "/var/lib/vmsetup/incidents.jsonl"))
LIVE = os.environ.get("VMSETUP_LIVE") == "1"  # otherwise mutating tools stay dry-run
MUTATING = {"vm_snapshot", "vm_start", "vm_stop"}  # served by the infra MCP server only
MODEL = os.environ.get("VMSETUP_MODEL", "default")

llm = OpenAI(base_url=os.environ.get("LITELLM_URL", "http://127.0.0.1:4000"), api_key=os.environ.get("LITELLM_KEY", "none"))

SYSTEM = (ROOT / "agents/prompts/operator.txt").read_text().strip()


def load_runbooks():
    return [yaml.safe_load(p.read_text()) for p in sorted((ROOT / "agents/runbooks").glob("*.yaml"))]


def similar_incidents(alert, limit=3):
    name = alert["labels"].get("alertname", "unknown")
    if memory.enabled():
        try:
            return memory.similar(json.dumps({"labels": alert["labels"], "annotations": alert.get("annotations", {})}), limit)
        except Exception as exc:
            print(f"operator: vector lookup failed, using file history: {exc}", file=sys.stderr)
    if not INCIDENTS.exists():
        return []
    rows = [json.loads(line) for line in INCIDENTS.read_text().splitlines() if line]
    return [r for r in rows if r.get("alert") == name][-limit:]


def record(alert, outcome, detail, usage=None, model=None, transcript=None):
    row = {"ts": time.time(), "alert": alert, "outcome": outcome, "detail": detail, "model": model, "usage": usage or {}}
    INCIDENTS.parent.mkdir(parents=True, exist_ok=True)
    with INCIDENTS.open("a") as fh:
        fh.write(json.dumps(row) + "\n")  # file stays the source for cost.py
    if memory.enabled():
        try:
            memory.add({**row, "transcript": transcript})
        except Exception as exc:
            print(f"operator: could not store incident in memory: {exc}", file=sys.stderr)


def _sub(value):
    return str(value).replace("{python}", sys.executable).replace("{root}", str(ROOT))


async def connect_servers(stack):
    """Start every enabled MCP server whose prerequisites exist; returns ({exposed tool: (session, tool)}, specs)."""
    servers = yaml.safe_load((ROOT / "agents/mcp_servers.yaml").read_text())["servers"]
    routes, specs = {}, []
    for name, cfg in servers.items():
        if not cfg.get("enabled", True):
            continue
        if any(not os.environ.get(v) for v in cfg.get("requires_env", [])) or any(not Path(f).exists() for f in cfg.get("requires_file", [])):
            print(f"operator: MCP server '{name}' skipped (missing prerequisites)", file=sys.stderr)
            continue
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/root")}
        env.update({k: os.path.expandvars(_sub(v)) for k, v in cfg.get("env", {}).items()})
        params = StdioServerParameters(command=_sub(cfg["command"]), args=[_sub(a) for a in cfg.get("args", [])], env=env)
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = (await session.list_tools()).tools
        except Exception as exc:
            print(f"operator: MCP server '{name}' failed to start: {exc}", file=sys.stderr)
            continue
        allow = [re.compile(p) for p in cfg.get("allow", [".*"])]
        for tool in tools:
            if not any(p.search(tool.name) for p in allow):
                continue
            exposed = tool.name if name == "infra" else f"{name}__{tool.name}"
            routes[exposed] = (name, session, tool.name)
            specs.append({"type": "function", "function": {"name": exposed, "description": (tool.description or "")[:1000], "parameters": tool.inputSchema}})
    return routes, specs


async def handle_alert(alert):
    name = alert["labels"].get("alertname", "unknown")
    if KILL_SWITCH.exists():
        return record(name, "skipped", "kill switch active")

    async with AsyncExitStack() as stack:
        routes, specs = await connect_servers(stack)
        runbook = next((b for b in load_runbooks() if b["alert"] == name), None)
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps({"alert": alert, "runbook": runbook, "similar_past_incidents": similar_incidents(alert)})},
        ]
        transcript = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        for _ in range(MAX_STEPS):
            resp = llm.chat.completions.create(model=MODEL, messages=messages, tools=specs or None)
            if resp.usage:
                usage["prompt_tokens"] += resp.usage.prompt_tokens
                usage["completion_tokens"] += resp.usage.completion_tokens
            msg = resp.choices[0].message
            messages.append(msg)
            calls = [{"name": c.function.name, "arguments": c.function.arguments} for c in (msg.tool_calls or [])]
            transcript.append({"role": "assistant", "content": msg.content or "", "tool_calls": calls})
            if not msg.tool_calls:
                return record(name, "handled", msg.content, usage, MODEL, transcript)
            for call in msg.tool_calls:
                result_text = "error: unknown tool"
                if call.function.name in routes:
                    server, session, tool_name = routes[call.function.name]
                    args = json.loads(call.function.arguments or "{}")
                    if server == "infra" and tool_name in MUTATING:
                        args["dry_run"] = not LIVE
                    try:
                        result_text = str((await session.call_tool(tool_name, args)).content)[:4000]
                    except Exception as exc:
                        result_text = f"error: {exc}"
                transcript.append({"role": "tool", "name": call.function.name, "content": redact(result_text)})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result_text})
        record(name, "step_limit", "escalate to human", usage, MODEL, transcript)


class Webhook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.send_response(204)
        self.end_headers()
        for alert in body.get("alerts", []):
            if alert.get("status") == "firing":
                asyncio.run(handle_alert(alert))

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8085), Webhook).serve_forever()
