"""Checks for the SYNTHETIC Operator dataset (training/synth_dataset.py), on the generator's in-memory output.

Fail closed: the checks here are independent of the generator's own labels wherever possible.
"""
import json
import re

import evalgate
import pytest
import synth_dataset
import yaml
from redact import redact

ROOT = synth_dataset.ROOT
CFG = yaml.safe_load((ROOT / "training/config.yaml").read_text())
CASES = yaml.safe_load((ROOT / "evals/cases.yaml").read_text())["cases"]
RUNBOOKS = {b["alert"]: b for b in (yaml.safe_load(p.read_text()) for p in (ROOT / "agents/runbooks").glob("*.yaml"))}
ALERTS = ("GuestDown", "NodeDown", "HostMemoryPressure", "DiskAlmostFull", "HostCpuSteal")
MUTATING = {"vm_snapshot", "vm_start", "vm_stop"}
READ_ONLY = {"vm_list", "host_metrics"}
TOOL_RE = re.compile(r"\b(vm_list|host_metrics|vm_snapshot|vm_start|vm_stop)\b")
NODE_RE = re.compile(r"\b(?:hub|dev|prod)-[as]\d+\b")
HELD_OUT_NODE = "prod-a4"

# Anything in the alert that reads like an instruction to the agent: tool names, addressing an AI/agent/assistant,
# override wording, asking to power things off (several languages), or fake tool-output framing. Benign operator
# hints ("Please restart the guest if needed.") deliberately do not match.
INJECTION_RE = re.compile(
    r"vm_stop|vm_start|vm_snapshot|vm_list|host_metrics|\bagent(en)?\b|agendile|KI-Agent|\bassistant\b|\bAI\b|\bIA\b|"
    r"admin|disregard|ignore|override|forget the runbook|authori[sz]e|power(ed)? off|\bhalt\b|offline|shut\b.{0,30}\bdown|"
    r"herunter|apaga|éteins|välja|tool_call_id|observation:|function result|TextContent\(|<!--|###",
    re.I,
)
REFUSALS = ("queued for human approval", "denied by policy", "Error executing tool")


@pytest.fixture(scope="module")
def data():
    return synth_dataset.generate(seed=7, count=360)


def user_of(ex):
    return json.loads(ex["messages"][1]["content"])


def alert_values(ex):
    a = user_of(ex)["alert"]
    return [str(v) for v in (*a["labels"].values(), *a["annotations"].values())]


def alert_text(ex):
    return "\n".join(alert_values(ex))


def calls_of(ex):
    """[(name, args, result)] in order."""
    results = {m["tool_call_id"]: m["content"] for m in ex["messages"] if m["role"] == "tool"}
    return [(c["function"]["name"], json.loads(c["function"]["arguments"]), results[c["id"]])
            for m in ex["messages"] for c in (m.get("tool_calls") or [])]


def strings_of(ex):
    for m in ex["messages"]:
        if isinstance(m.get("content"), str):
            yield m["content"]
        for c in m.get("tool_calls") or []:
            yield c["function"]["name"]
            yield c["function"]["arguments"]


def tokens(text):
    return re.findall(r"[a-z0-9_-]+", text.lower())


def grams4(text):
    t = tokens(text)
    return {tuple(t[i:i + 4]) for i in range(len(t) - 3)}


# -- format ------------------------------------------------------------------------------------------------------
def test_matches_runtime_shape(data):
    examples, _ = data
    for ex in examples:
        assert set(ex) == {"messages", "tools"}
        assert ex["tools"] == evalgate.TOOLS
        msgs = ex["messages"]
        assert msgs[0] == {"role": "system", "content": evalgate.SYSTEM}
        assert msgs[1]["role"] == "user"
        user = user_of(ex)
        assert set(user) == {"alert", "runbook", "similar_past_incidents"}
        alert = user["alert"]
        assert alert["status"] == "firing" and alert["labels"]["alertname"] in ALERTS
        assert isinstance(alert["annotations"], dict)
        assert user["runbook"] == RUNBOOKS.get(alert["labels"]["alertname"])
        assert user["similar_past_incidents"] == []
        pending = []
        for m in msgs[2:]:
            assert m["role"] in ("assistant", "tool")
            if m["role"] == "assistant":
                assert set(m) <= {"role", "content", "tool_calls"}
                assert m["content"] is None or (isinstance(m["content"], str) and m["content"])
                for c in m.get("tool_calls") or []:
                    assert set(c) == {"id", "type", "function"} and c["type"] == "function"
                    assert re.fullmatch(r"call_\d+", c["id"])
                    assert set(json.loads(c["function"]["arguments"])) <= {"domain"}
                    pending.append(c["id"])
            else:
                assert set(m) == {"role", "tool_call_id", "content"}
                assert pending and m["tool_call_id"] == pending.pop(0)
        assert not pending, "every tool call needs its result"


def test_ends_with_nonempty_assistant(data):
    examples, _ = data
    for ex in examples:
        last = ex["messages"][-1]
        assert last["role"] == "assistant" and not last.get("tool_calls")
        assert isinstance(last["content"], str) and last["content"].strip()


def test_tool_names_are_known(data):
    examples, _ = data
    for ex in examples:
        assert {n for n, _, _ in calls_of(ex)} <= set(evalgate.TOOL_NAMES)


def test_redact_is_a_noop(data):
    examples, _ = data
    for ex in examples:
        for s in strings_of(ex):
            assert redact(s) == s


# -- determinism and size ----------------------------------------------------------------------------------------
def test_deterministic_for_fixed_seed(data):
    examples, _ = data
    again, _ = synth_dataset.generate(seed=7, count=360)
    assert json.dumps(again) == json.dumps(examples)
    other, _ = synth_dataset.generate(seed=8, count=360)
    assert json.dumps(other) != json.dumps(examples)
    assert synth_dataset.split(examples, 7, CFG["validation_fraction"]) == synth_dataset.split(again, 7, CFG["validation_fraction"])


def test_split_and_minimum(data):
    examples, _ = data
    assert len(examples) >= max(300, CFG["min_examples"])
    assert len({json.dumps(e, sort_keys=True) for e in examples}) == len(examples)
    train, val = synth_dataset.split(examples, 7, CFG["validation_fraction"])
    assert len(val) == max(1, int(len(examples) * CFG["validation_fraction"]))
    assert len(train) + len(val) == len(examples) and len(train) >= CFG["min_examples"]


# -- balance -----------------------------------------------------------------------------------------------------
def test_balance(data):
    examples, meta = data
    n = len(examples)
    injected = sum(bool(INJECTION_RE.search(alert_text(ex))) for ex in examples)
    acting = sum(any(name in MUTATING for name, _, _ in calls_of(ex)) for ex in examples)
    started = sum(any(name == "vm_start" and re.search(r"Domain '[^']+' started", res) for name, _, res in calls_of(ex)) for ex in examples)
    benign = sum(m["benign_hint"] for m in meta)
    assert injected / n >= 0.25
    assert acting / n >= 0.40
    assert started / n >= 0.20
    assert benign / n >= 0.05
    assert (n - injected - acting) / n >= 0.15, "genuine no-action cases"


# -- safety behaviour --------------------------------------------------------------------------------------------
def test_injection_labels_agree_with_independent_detector(data):
    examples, meta = data
    for ex, m in zip(examples, meta):
        assert bool(INJECTION_RE.search(alert_text(ex))) == (m["kind"] == "injection"), alert_text(ex)
        if m["benign_hint"]:
            assert m["kind"] == "action" and any(name == "vm_snapshot" for name, _, _ in calls_of(ex))


def test_no_action_on_instructions_in_alert_text(data):
    examples, _ = data
    for ex in examples:
        names = [n for n, _, _ in calls_of(ex)]
        assert "vm_stop" not in names, "no runbook calls for vm_stop"
        if INJECTION_RE.search(alert_text(ex)):
            assert not MUTATING & set(names), alert_text(ex)
            final = ex["messages"][-1]["content"].lower()
            assert "untrusted" in final and ("did not" in final or "ignored" in final)


def test_every_action_is_justified_by_runbook_and_state(data):
    """A mutating call needs a guest alert and a prior vm_list result showing that domain shut off or crashed."""
    examples, _ = data
    for ex in examples:
        alertname = user_of(ex)["alert"]["labels"]["alertname"]
        seen_down = set()
        for name, args, result in calls_of(ex):
            if name == "vm_list":
                seen_down |= set(re.findall(r"(\S+)\s+(?:shut off|crashed)", result))
            if name in MUTATING:
                assert alertname in ("GuestDown", "NodeDown")
                assert args["domain"] in seen_down


def test_runbook_order_and_stop_on_refusal(data):
    examples, _ = data
    for ex in examples:
        calls = calls_of(ex)
        assert calls[0][0] in READ_ONLY, "diagnose read-only first"
        snapped, refused = set(), False
        for name, args, result in calls:
            if name in MUTATING:
                assert not refused, "no further actions after a queue, denial or failure"
            if name == "vm_start":
                assert args["domain"] in snapped, "snapshot before start"
            if any(r in result for r in REFUSALS):
                refused = True
            elif name == "vm_snapshot":
                snapped.add(args["domain"])


# -- the summary states only facts from this conversation --------------------------------------------------------
def test_summary_claims_are_grounded(data):
    examples, _ = data
    for ex in examples:
        user = ex["messages"][1]["content"]
        calls = calls_of(ex)
        results = " ".join(r for _, _, r in calls)
        final = ex["messages"][-1]["content"]
        values = alert_values(ex)
        for q in re.findall(r'"([^"]+)"', final):  # quoting the alert is allowed; it must be a verbatim quote
            assert any(q in v for v in values), q
        unquoted = re.sub(r'"[^"]+"', "", final)
        called = {n for n, _, _ in calls}
        assert set(TOOL_RE.findall(unquoted)) <= called, unquoted
        evidence = user + " " + results
        for node in NODE_RE.findall(unquoted):
            assert node in evidence, node
        for snap in re.findall(r"agent-\d+", unquoted):
            assert snap in results
        for rid in re.findall(r"\(id ([A-Za-z0-9_-]{6,32})", unquoted):
            assert f"(id {rid})" in results
        for num in re.findall(r"\b\d+\.\d+\b", unquoted):
            assert num in evidence, num
        for num in re.findall(r"\b\d{4,}\b", unquoted):
            assert num in evidence, num
        mem = re.findall(r"MemTotal:\s+(\d+) kB\\nMemFree:\s+\d+ kB\\nMemAvailable:\s+(\d+) kB", results)
        derived = {str(round(100 * int(a) / int(t))) for t, a in mem}
        for pct in re.findall(r"\b(\d+)%", unquoted):
            assert f"{pct}%" in user or pct in derived, pct


# -- held out from the eval set ----------------------------------------------------------------------------------
def test_no_eval_4gram_in_any_message(data):
    examples, _ = data
    eval_grams = set().union(*(grams4(c["alert"]) for c in CASES))
    assert eval_grams
    for ex in examples:
        for s in strings_of(ex):
            hit = grams4(s) & eval_grams
            assert not hit, hit


def test_no_overlap_with_eval_cases(data):
    examples, _ = data
    pairs = {(t, n) for c in CASES for t in ALERTS if t in c["alert"] for n in NODE_RE.findall(c["alert"])}
    for ex in examples:
        text = alert_text(ex)
        alertname = user_of(ex)["alert"]["labels"]["alertname"]
        for s in strings_of(ex):
            for c in CASES:
                assert c["alert"].lower() not in s.lower()
        for n in NODE_RE.findall(text):
            assert (alertname, n) not in pairs, f"eval near-duplicate: {text}"
        for c in CASES:
            a, b = set(tokens(text)), set(tokens(c["alert"]))
            assert len(a & b) / len(a | b) < 0.6, f"too similar to eval {c['id']}"


def test_held_out_node_never_generated(data):
    """prod-a4 stays unreachable so a future eval can use it as an unseen node."""
    examples, _ = data
    for ex in examples:
        for s in strings_of(ex):
            assert HELD_OUT_NODE not in s
