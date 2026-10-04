"""Try the dashboard without any hardware or accounts: python -m jarvis.demo

Starts Jarvis on 127.0.0.1:8088 with sample data, a scripted stand-in for the AI gateway (no real model is called), a fake
infra tool set that never touches anything, and a throwaway owner account whose password is printed once.
"""
import argparse
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ("vm_list", "vm_snapshot", "vm_start", "vm_stop")


def _chunk(delta, finish=None):
    return "data: " + json.dumps({"id": "demo", "object": "chat.completion.chunk", "created": int(time.time()), "model": "demo",
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


def script(messages):
    """The demo model: keyword rules that exercise streaming, tool calls and the approval flow."""
    if any(m.get("role") == "tool" for m in messages):
        return {"text": "Done. That action is waiting for your confirmation in the card above; nothing changes until you press Confirm."}
    last = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "").lower()
    for tool, word in (("vm_snapshot", "snapshot"), ("vm_start", "start"), ("vm_stop", "stop")):
        if word in last:
            node = next((w for w in last.replace(",", " ").split() if w.count("-") == 1 and w[0] in "hdp"), "hub-a1")
            return {"text": f"I'll propose {tool} on {node}. ", "tool": (tool, {"domain": node})}
    if "list" in last or "vm" in last or "node" in last:
        return {"text": "Checking the nodes. ", "tool": ("vm_list", {})}
    if "runpod" in last or "gpu" in last or "kaggle" in last:
        return {"text": "Kaggle gives free GPU time each week; RunPod rents GPUs by the hour with a price cap and auto-terminate. Use the Compute tab, and the Setup tab for the sign-up links."}
    return {"text": "This is the demo model, so I only follow a few phrases: try 'list the nodes', 'snapshot hub-a1', 'stop hub-a1', or 'tell me about runpod'. With real providers set up in the Setup tab, a real model answers here."}


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_response(501)
            self.end_headers()
            return
        reply = script(body.get("messages", []))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(_chunk({"role": "assistant", "content": reply["text"]}).encode())
        if "tool" in reply:
            name, args = reply["tool"]
            self.wfile.write(_chunk({"tool_calls": [{"index": 0, "id": "call_demo", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "tool_calls").encode())
        else:
            self.wfile.write(_chunk({}, "stop").encode())
        self.wfile.write(b"data: [DONE]\n\n")


def seed(base):
    """Sample profile, incidents, approvals and audit entries."""
    import detect
    import yaml

    facts = {"cores": 16, "ram_gb": 64, "disk_free_gb": 1000, "gpu_vram_gb": 0, "virtualization": True}
    profile = yaml.safe_load((ROOT / "profiles/std.yaml").read_text())
    (base / "profile.json").write_text(json.dumps({"profile": "std", "facts": facts, **profile, **detect.size(facts, profile)}))
    now = time.time()
    rows = [("GuestDown", "handled", "Restarted hub-a1 after libvirt reported it stopped.", "default", 1200, 150, 5400),
            ("HostMemoryPressure", "handled", "Memory pressure came from a Longhorn rebuild; no action needed.", "cloud-small", 2400, 300, 40000),
            ("NodeDown", "step_limit", "Could not reach dev-a2 after three attempts; escalated.", "default", 3000, 400, 90000)]
    (base / "incidents.jsonl").write_text("".join(
        json.dumps({"ts": now - age, "alert": a, "outcome": o, "detail": d, "model": m, "usage": {"prompt_tokens": p, "completion_tokens": c}}) + "\n"
        for a, o, d, m, p, c, age in rows))


def seed_autonomy():
    import approvals
    from policy import Policy

    policy = Policy()
    for action, target in (("vm_snapshot", "hub-a1"), ("vm_start", "dev-a2")):
        policy.decide(action, target, "operator")
        policy.record(action, target, True, "operator", note="demo")
    policy.decide("vm_stop", "hub-a1", "operator")
    approvals.queue("vm_stop", "hub-a1", {"domain": "hub-a1"}, {"reason": "policy requires approval for this action", "alert": "GuestDown"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    base = Path(tempfile.mkdtemp(prefix="jarvis-demo-"))
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    password = secrets.token_urlsafe(12)
    os.environ.update({
        "JARVIS_STATE_DIR": str(base / "state"), "JARVIS_SESSION_SECRET": secrets.token_urlsafe(32),
        "LITELLM_URL": f"http://127.0.0.1:{gateway.server_address[1]}", "LITELLM_KEY": "demo",
        "VMSETUP_PROFILE": str(base / "profile.json"), "VMSETUP_INCIDENTS": str(base / "incidents.jsonl"),
        "VMSETUP_AUTONOMY_DIR": str(base / "autonomy"), "VMSETUP_KILL_SWITCH": str(base / "NOT_PAUSED"),
    })
    sys.path[:0] = [str(ROOT), str(ROOT / "agents")]
    seed(base)
    from jarvis import auth
    from jarvis.chat import Hub
    from jarvis.compute.service import Compute
    import approvals
    from policy import Policy

    seed_autonomy()
    auth.set_password("owner", password)

    class DemoHub(Hub):
        async def start(self):
            self.specs = [{"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}}}} for n in TOOLS]

        def needs_confirmation(self, name):
            return name in TOOLS and name != "vm_list"

        async def execute(self, name, a):
            return "hub-s1 running, hub-a1 running, hub-a2 running, dev-s1 running, dev-a1 running (demo data)" if name == "vm_list" else "error: unknown tool"

        def queue(self, name, a):
            return approvals.queue(name, str(a.get("domain", "")), a, {"source": "demo chat"})

        async def confirm(self, pid):
            doc = approvals.get(pid)
            if doc is None:
                raise KeyError("unknown or expired action")
            policy = Policy()
            policy.decide(doc["action"], doc["target"], "owner", True)
            policy.record(doc["action"], doc["target"], True, "owner", True, "demo")
            approvals.finish(pid, "executed", "demo")
            return f"demo: {doc['action']} on {doc['target']} acknowledged (nothing was changed)"

    import uvicorn
    from jarvis.app import create_app

    print(f"\nJarvis demo: http://{args.host}:{args.port}\n  username: owner\n  password: {password}\n  (sample data, scripted model, no real actions; Ctrl+C to stop)\n", flush=True)
    compute = Compute(env={})
    uvicorn.run(create_app(compute=compute, hub=DemoHub(compute), start_tools=True), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
