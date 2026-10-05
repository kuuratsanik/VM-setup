"use strict";
const $ = (id) => document.getElementById(id);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid ?? "")));
  return el;
};
const api = async (path, opts = {}) => {
  const res = await fetch(path, { credentials: "same-origin", ...opts, headers: { "X-Requested-With": "jarvis", ...(opts.json ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) }, body: opts.json ? JSON.stringify(opts.json) : opts.body });
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("login required"); }
  if (!res.ok) {
    let d = res.statusText;
    try { const x = (await res.json()).detail; if (x) d = Array.isArray(x) ? x.map((i) => (i && i.msg) || String(i)).join("; ") : typeof x === "string" ? x : JSON.stringify(x); } catch {}
    const err = new Error(d); err.status = res.status; throw err;
  }
  return res;
};
const getJSON = async (p, signal) => (await api(p, signal ? { signal } : {})).json();
const post = async (p, json) => (await api(p, { method: "POST", json: json ?? {} })).json();
// A confirm/discard that finds the action claimed or gone (409/404) was handled by an earlier request.
const settled = (e) => e.status === 409 || e.status === 404;
const failText = (e) => (e.status === 409 ? "already being confirmed by another request" : e.status === 404 ? "not run: " + e.message : e.message);

// Defensive helpers: server values are untrusted and may be missing or the wrong type.
const DASH = "\u2014";
const obj = (x) => (x && typeof x === "object" && !Array.isArray(x) ? x : {});
const arr = (x) => (Array.isArray(x) ? x : []);
const val = (x) => (x == null || x === "" ? DASH : typeof x === "object" ? JSON.stringify(x) : String(x));
const fmtTs = (ts) => {
  if (ts == null || ts === "") return DASH;
  let d = typeof ts === "number" ? new Date(ts * 1000) : /^\d+(\.\d+)?$/.test(String(ts).trim()) ? new Date(Number(ts) * 1000) : new Date(String(ts));
  return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleString();
};
const safeLink = (url, label) => {
  try { const u = new URL(url); if (u.protocol === "https:") return h("a", { href: u.href, target: "_blank", rel: "noopener noreferrer" }, label); } catch {}
  return h("span", {}, label);
};
// Accessibility helpers.
const srOnly = (t) => h("span", { class: "sr-only" }, t);
// Status dot: with a label it is an image with a text equivalent; without, it is decorative (adjacent text says it).
const dot = (cls, label) => h("span", label ? { class: "dot " + cls, role: "img", "aria-label": label } : { class: "dot " + cls, "aria-hidden": "true" });
// Table with caption, thead/tbody and scoped column headers. `rows` are <tr> elements.
const tbl = (caption, cols, rows) => h("table", {}, h("caption", { class: "sr-only" }, caption),
  h("thead", {}, h("tr", {}, ...cols.map((c) => h("th", { scope: "col" }, c === "" ? srOnly("Actions") : c)))), h("tbody", {}, ...rows));
// Visually hidden label bound to a control by id; returns both so they can be spread into a parent.
const field = (id, text, el) => { el.id = id; return [h("label", { for: id, class: "sr-only" }, text), el]; };
// A confirm result may itself be a JSON string; show it readably, falling back to the raw string.
const resText = (r) => { let x = obj(r).result ?? "done"; if (typeof x === "string") { try { x = JSON.parse(x); } catch {} } return typeof x === "string" ? x : JSON.stringify(x); };
const isAbort = (e) => e && e.name === "AbortError";
// One controller per tab load: starting a new load aborts the previous one so a stale response cannot overwrite it.
let tabCtl = null;
const newSignal = () => { if (tabCtl) tabCtl.abort(); tabCtl = new AbortController(); return tabCtl.signal; };

const TABS = ["Overview", "Chat", "Autonomy", "Compute", "Setup", "Incidents"];
let current = "Overview";

function showLogin() { $("app").classList.add("hidden"); $("login").classList.remove("hidden"); }
function showApp() { $("login").classList.add("hidden"); $("app").classList.remove("hidden"); buildTabs(); open(current); }

const tabId = (t) => "tabbtn-" + t.toLowerCase();
function buildTabs() {
  const nav = $("tabs");
  if (!nav.children.length) {
    for (const t of TABS) nav.append(h("button", { id: tabId(t), role: "tab", type: "button", "aria-controls": "tab-" + t.toLowerCase(), onclick: () => open(t) }, t));
    nav.addEventListener("keydown", (e) => {
      const i = TABS.indexOf(current); let n;
      if (e.key === "ArrowRight") n = (i + 1) % TABS.length; else if (e.key === "ArrowLeft") n = (i - 1 + TABS.length) % TABS.length;
      else if (e.key === "Home") n = 0; else if (e.key === "End") n = TABS.length - 1; else return;
      e.preventDefault(); open(TABS[n]); $(tabId(TABS[n])).focus();
    });
  }
  for (const t of TABS) { // roving tabindex: only the selected tab is in the tab order
    const b = $(tabId(t)); const on = t === current;
    b.className = on ? "on" : ""; b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1;
  }
}
function open(t) {
  current = t; buildTabs();
  if (notice.dataset.tab === t) { notice.textContent = ""; delete notice.dataset.tab; } // the user is now on the tab the notice refers to
  for (const name of TABS) $("tab-" + name.toLowerCase()).classList.toggle("hidden", name !== t);
  pollErr.classList.toggle("hidden", t !== "Overview");
  runTab(t);
}
const RENDERERS = { Overview: renderOverview, Autonomy: renderAutonomy, Compute: renderCompute, Setup: renderSetup, Incidents: renderIncidents };
async function runTab(t, ...args) {
  const fn = RENDERERS[t]; if (!fn) return;
  if (t !== current) { if (typeof args[0] === "string" && args[0]) notify(t, args[0]); return; } // surface the result of an action finished after the user left; a post-action re-render for a tab the user already left must not abort the current tab's load
  const signal = newSignal();
  try { await fn(signal, ...args); }
  catch (e) { if (!isAbort(e) && e.message !== "login required") $("tab-" + t.toLowerCase()).replaceChildren(h("p", { class: "err", role: "alert" }, "Error: " + e.message), ...(typeof args[0] === "string" && args[0] ? [h("p", { class: "muted" }, "Earlier result: " + args[0])] : [])); }
}
// Timer refresh: keep the existing content, report a failed poll in a small status line.
// Global status line for results that finish after the user has left the tab they belong to.
const notice = h("p", { class: "muted", role: "status" });
function notify(tab, text) { if (!notice.isConnected) $("tab-overview").before(notice); notice.dataset.tab = tab; notice.textContent = ""; notice.textContent = tab + ": " + text; } // clear first so an identical repeat is re-announced
const pollErr = h("p", { class: "err", role: "status" });
const setPoll = (t) => { if (pollErr.textContent !== t) pollErr.textContent = t; }; // unchanged text must not be re-announced every poll
async function pollOverview() {
  if (!pollErr.isConnected) $("tab-overview").before(pollErr);
  try { await renderOverview(newSignal()); setPoll(""); }
  catch (e) { if (!isAbort(e) && e.message !== "login required") setPoll("Refresh failed: " + e.message); }
}

async function renderOverview(signal) {
  const box = $("tab-overview"); const s = obj(await getJSON("/api/status", signal));
  const host = obj(s.host), spend = obj(s.spend);
  $("paused").classList.toggle("hidden", !s.paused);
  const pct = (x) => (typeof x === "number" && Number.isFinite(x) ? (x * 100).toFixed(1) + "%" : DASH);
  box.replaceChildren(h("div", { class: "grid" },
    h("div", { class: "card" }, h("h3", {}, "Services"), ...(Array.isArray(s.services) ? arr(s.services).map((x) => { x = obj(x); return h("div", {}, dot(x.ok ? "ok" : "bad", x.ok ? "ok" : "down"), val(x.name)); }) : [h("div", { class: "muted" }, DASH)])),
    h("div", { class: "card" }, h("h3", {}, "Host"), h("div", {}, "Profile: ", val(s.profile)), h("div", {}, "Memory available: ", pct(host.mem_available_ratio)), h("div", {}, "CPU steal: ", pct(host.cpu_steal))),
    h("div", { class: "card" }, h("h3", {}, "AI spend (30 days, estimate)"), h("div", {}, spend.estimated_usd_30d == null ? DASH : "$" + spend.estimated_usd_30d, spend.budget_usd_month ? " of $" + spend.budget_usd_month : ""), h("div", { class: "muted" }, val(spend.tokens_30d), " tokens")),
  ), h("h3", {}, "Nodes"), tbl("Nodes", ["Node", "Cluster", "Role", "vCPU", "RAM GB", "IP"],
    arr(s.nodes).map((n) => { n = obj(n); return h("tr", {}, h("td", {}, val(n.name)), h("td", {}, val(n.cluster)), h("td", {}, val(n.role)), h("td", {}, val(n.vcpu)), h("td", {}, val(n.ram_gb)), h("td", {}, val(n.ip))); })));
}

async function renderIncidents(signal) {
  const rows = arr(await getJSON("/api/incidents", signal));
  $("tab-incidents").replaceChildren(tbl("Incidents", ["When", "Alert", "Outcome", "Summary"],
    rows.map((r) => { r = obj(r); return h("tr", {}, h("td", {}, fmtTs(r.ts)), h("td", {}, val(r.alert)), h("td", {}, val(r.outcome)), h("td", {}, val(r.detail))); })));
}

/* ---- chat ---- */
const history = [];
const log = () => $("log");
function line(cls, text) { const el = h("div", { class: "msg " + cls }, text); log().append(el); log().scrollTop = log().scrollHeight; return el; }
function speak(text) { if ($("speak").checked && !$("server-voice").checked && "speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(text)); }
async function speakServer(text) {
  const r = await api("/api/tts", { method: "POST", json: { text } }); const url = URL.createObjectURL(await r.blob());
  const a = new Audio(url); const free = () => URL.revokeObjectURL(url);
  a.addEventListener("ended", free); a.addEventListener("error", free);
  try { await a.play(); } catch (e) { free(); throw e; } // autoplay blocked or undecodable audio
}

function pendingCard(ev) {
  const card = h("div", { class: "pending", role: "group", "aria-label": "Proposed action" }, h("div", {}, "Jarvis proposes: ", val(ev.name), " ", JSON.stringify(ev.args ?? {})));
  const out = h("div", { class: "muted", role: "status" });
  const ok = h("button", {}, "Confirm"); const no = h("button", { class: "secondary" }, "Discard");
  const run = async (verb) => {
    ok.disabled = no.disabled = true;
    try {
      const r = await post(`/api/actions/${encodeURIComponent(ev.id)}/${verb}`);
      if (verb === "discard") { card.remove(); return; }
      out.textContent = resText(r); // success: buttons stay disabled
    } catch (e) {
      out.textContent = failText(e);
      if (!settled(e)) ok.disabled = no.disabled = false; // still pending, allow retry
    }
  };
  ok.addEventListener("click", () => run("confirm")); no.addEventListener("click", () => run("discard"));
  card.append(ok, " ", no, out);
  log().append(card);
}

async function send(text) {
  const userEl = line("user", text); history.push({ role: "user", content: text });
  const reply = line("assistant", ""); let full = "";
  const live = h("span", { "aria-hidden": "true" }); reply.append(live); let failed = false; // stream into a hidden span; the finished text node is announced once by the log
  const mine = history[history.length - 1];
  try {
    let msgs = history.slice(-30); while (msgs.length && msgs[0].role !== "user") msgs.shift();
    let res;
    try { res = await api("/api/chat", { method: "POST", json: { messages: msgs, model: $("model").value, tools: $("use-tools").checked } }); }
    catch (e) { const i = history.indexOf(mine); if (i >= 0) history.splice(i, 1); if (e.message === "login required") { userEl.remove(); reply.remove(); return; } throw e; } // rejected message must not poison later sends
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      buf = (buf + dec.decode(value, { stream: true })).replace(/\r\n/g, "\n"); // accept CRLF separators; a lone trailing \r waits for the next chunk
      let i; while ((i = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!raw.startsWith("data: ")) continue;
        let ev;
        try { ev = JSON.parse(raw.slice(6)); } catch (e) { console.warn("skipping malformed SSE event", e); continue; }
        if (!ev || typeof ev !== "object") continue;
        if (ev.type === "delta") { full += String(ev.text ?? ""); live.textContent = full; }
        else if (ev.type === "tool") line("tool", `${val(ev.name)}: ${typeof ev.result === "string" ? ev.result : JSON.stringify(ev.result)}`);
        else if (ev.type === "pending") pendingCard(ev);
        else if (ev.type === "error") line("tool", "error: " + val(ev.text));
      }
      log().scrollTop = log().scrollHeight;
    }
  } catch (e) { failed = true; reply.textContent = "error: " + e.message; }
  if (!failed) reply.textContent = full;
  if (full) { history.push({ role: "assistant", content: full }); speak(full); if ($("speak").checked && $("server-voice").checked) speakServer(full.slice(0, 1000)).catch(() => {}); }
}

let micStop = null, micBusy = false; // micStop: stopper of the active session; micBusy: permission prompt in flight
const micState = (on) => $("mic").setAttribute("aria-pressed", on ? "true" : "false");
const stopTracks = (stream) => stream && stream.getTracks().forEach((t) => t.stop());
async function startMic() {
  if (micStop) { micStop(); return; }
  if (micBusy) return; // second click during the permission prompt must not start another recorder
  if ($("server-voice").checked && navigator.mediaDevices) {
    micBusy = true; let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } finally { micBusy = false; }
    let rec;
    try { rec = new MediaRecorder(stream); } catch (e) { stopTracks(stream); throw e; }
    const chunks = []; const mine = () => { if (rec.state === "recording") rec.stop(); };
    rec.ondataavailable = (e) => chunks.push(e.data);
    rec.onstop = async () => { stopTracks(stream); if (micStop === mine) { micStop = null; micState(false); } const fd = new FormData(); fd.append("file", new Blob(chunks, { type: "audio/webm" }), "voice.webm");
      try { const r = await (await api("/api/stt", { method: "POST", body: fd })).json(); if (r.text) send(r.text); } catch (e) { line("tool", "voice error: " + e.message); } };
    try { rec.start(); } catch (e) { stopTracks(stream); throw e; }
    micStop = mine; micState(true); setTimeout(mine, 8000); return;
  }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { line("tool", "This browser has no speech recognition; enable server voice."); return; }
  const r = new SR(); const mine = () => r.stop(); const done = () => { if (micStop === mine) { micStop = null; micState(false); } };
  r.onresult = (e) => send(e.results[0][0].transcript); r.onend = done; r.onerror = done;
  r.start(); micStop = mine; micState(true);
}

let chatWired = false;
async function initChat() {
  const models = await getJSON("/api/models"); const sel = $("model"); const prev = sel.value;
  sel.replaceChildren(...models.map((m) => h("option", {}, m)));
  if (prev && models.includes(prev)) sel.value = prev;
  if (chatWired) return;
  chatWired = true;
  $("chat-form").addEventListener("submit", (e) => { e.preventDefault(); const t = $("msg").value.trim(); if (t) { $("msg").value = ""; send(t); } });
  $("mic").addEventListener("click", () => startMic().catch((e) => line("tool", e.message)));
  $("img").addEventListener("click", async () => {
    const prompt = $("msg").value.trim(); if (!prompt) return; $("msg").value = ""; line("user", "image: " + prompt);
    const btn = $("img"); btn.disabled = true; btn.setAttribute("aria-busy", "true"); // action button, not a toggle: expose busy state instead of aria-pressed
    try { const r = await post("/api/image", { prompt }); if (typeof r.b64 === "string" && /^[A-Za-z0-9+/]+={0,2}$/.test(r.b64)) log().append(h("img", { class: "gen", alt: "Generated image: " + prompt, src: "data:image/png;base64," + r.b64 })); else line("tool", "image: no valid image returned"); } catch (e) { line("tool", e.message); }
    btn.disabled = false; btn.setAttribute("aria-busy", "false");
  });
}

/* ---- autonomy ---- */
async function renderAutonomy(signal, msg = "") {
  const a = obj(await getJSON("/api/autonomy", signal));
  const breaker = obj(a.breaker), audit = obj(a.audit);
  const note = h("p", { class: "muted", role: "status" }, msg);
  const approvals = arr(a.approvals), promo = arr(a.promotion_candidates), tail = arr(a.audit_tail);
  const act = (id, verb) => async (ev) => {
    const cell = ev.currentTarget.parentElement; const btns = [...cell.querySelectorAll("button")];
    btns.forEach((b) => { b.disabled = true; });
    let text;
    try { text = resText(await post(`/api/actions/${encodeURIComponent(id)}/${verb}`)); }
    catch (e) { text = failText(e); if (!settled(e)) { btns.forEach((b) => { b.disabled = false; }); note.textContent = text; return; } }
    await runTab("Autonomy", text); // re-render first; message goes on the new element
  };
  const panel = $("tab-autonomy"); // a panel with no focusable content must itself be reachable by keyboard
  if (approvals.length || breaker.tripped) panel.removeAttribute("tabindex"); else panel.tabIndex = 0;
  panel.replaceChildren(
    h("div", { class: "grid" },
      h("div", { class: "card" }, h("h3", {}, "Mode"), h("div", {}, val(a.mode)), h("div", { class: "muted" }, "Changed only by a reviewed PR to agents/autonomy.yaml"), h("div", { class: "err", role: "status" }, a.error || "")),
      h("div", { class: "card" }, h("h3", {}, "Circuit breaker"), h("div", {}, dot(a.breaker == null ? "" : breaker.tripped ? "bad" : "ok"), a.breaker == null ? DASH : breaker.tripped ? "tripped: " + val(breaker.reason) : "closed"), breaker.tripped ? h("button", { onclick: async (ev) => {
        if (!confirm("Reset the circuit breaker? Autonomous actions will resume.")) return;
        const b = ev.currentTarget; b.disabled = true;
        try { await post("/api/autonomy/breaker/reset"); } catch (e) { b.disabled = false; if (e.message !== "login required") note.textContent = "Reset failed: " + e.message; return; }
        runTab("Autonomy");
      } }, "Reset") : ""),
      h("div", { class: "card" }, h("h3", {}, "Audit log"), h("div", {}, dot(audit.chain_ok === undefined ? "" : audit.chain_ok ? "ok" : "bad"), audit.chain_ok === undefined ? DASH : audit.chain_ok ? "chain intact" : "CHAIN BROKEN at entry " + val(audit.first_bad)), h("div", { class: "muted" }, val(audit.entries), " entries")),
      h("div", { class: "card" }, h("h3", {}, "Actions in 24 h"), h("div", {}, val(a.actions_24h)))),
    h("h3", {}, "Waiting for you"), note,
    approvals.length ? tbl("Actions waiting for approval", ["Action", "Target", "Why", ""],
      approvals.map((p) => { p = obj(p); const ctx = obj(p.context); return h("tr", {}, h("td", {}, val(p.action)), h("td", {}, val(p.target)), h("td", {}, ctx.reason || "", " ", ctx.alert || ""), h("td", {}, h("button", { onclick: act(p.id, "confirm") }, "Confirm"), " ", h("button", { class: "secondary", onclick: act(p.id, "discard") }, "Discard"))); })) : h("p", { class: "muted" }, "Nothing is waiting."),
    ...(promo.length ? [h("h3", {}, "Earned trust"), ...promo.map((c) => { c = obj(c); return h("p", {}, `${val(c.action)}: ${val(c.streak)} approved successes in a row. Consider level: auto in agents/autonomy.yaml (a reviewed PR).`); })] : []),
    h("h3", {}, "Recent decisions"),
    tbl("Recent decisions", ["When", "Kind", "Actor", "Action", "Target", "Result"],
      tail.map((e) => { e = obj(e); return h("tr", {}, h("td", {}, fmtTs(e.ts)), h("td", {}, val(e.kind)), h("td", {}, val(e.actor)), h("td", {}, val(e.action)), h("td", {}, val(e.target)), h("td", {}, e.level || (e.ok === undefined ? e.reason || "" : e.ok ? "ok" : "failed"), e.reason && e.level ? ": " + e.reason : "")); })));
}

/* ---- compute ---- */
async function renderCompute(signal, msgText = "") {
  const box = $("tab-compute"); const gpus = arr(await getJSON("/api/compute/gpus", signal)); const msg = h("p", { class: "muted", role: "status" });
  const result = h("p", { role: "status" }, msgText);
  let pods = [];
  try { pods = arr(await getJSON("/api/compute/runpod/pods", signal)); } catch (e) { if (isAbort(e) || e.message === "login required") throw e; msg.textContent = "RunPod: " + e.message; }
  const name = h("input", { placeholder: "name (a-z, 0-9, -)" }); const gpu = h("select", {}, ...gpus.map((g) => h("option", {}, g))); const hours = h("input", { type: "number", value: "2", min: "0.5", step: "0.5" });
  const slug = h("input", { placeholder: "kaggle slug" }); const code = h("textarea", { placeholder: "Python script to run on a Kaggle GPU (private, internet off)" });
  const podForm = [...field("pod-name", "Pod name", name), ...field("pod-gpu", "GPU type", gpu), ...field("pod-hours", "Hours", hours)];
  const kaggleForm = [...field("kaggle-slug", "Kaggle slug", slug), ...field("kaggle-code", "Python script", code)];
  // Confirm button: disabled while in flight; re-enabled only if the action is still pending.
  const confirmBtn = (label, id, rerender) => h("button", { type: "button", onclick: async (ev) => {
    const b = ev.currentTarget; b.disabled = true;
    let text;
    try { text = resText(await post(`/api/actions/${encodeURIComponent(id)}/confirm`)); }
    catch (er) { text = failText(er); if (!settled(er)) { b.disabled = false; result.textContent = text; return; } }
    if (rerender) await runTab("Compute", text); else result.textContent = text; // re-render first, then show the message on the new element
  } }, label);
  box.replaceChildren(
    h("h3", {}, "RunPod (paid, hourly cap and auto-terminate apply)"), msg,
    tbl("RunPod pods", ["Pod", "Status", "$/h", "Image", ""],
      pods.map((p) => { p = obj(p); return h("tr", {}, h("td", {}, val(p.name)), h("td", {}, val(p.desiredStatus)), h("td", {}, val(p.costPerHr)), h("td", {}, val(p.image)), h("td", {}, h("button", { class: "danger", onclick: async (ev) => {
        const b = ev.currentTarget; b.disabled = true;
        let r;
        try { r = obj(await post(`/api/compute/runpod/pods/${encodeURIComponent(p.id)}/terminate`)); }
        catch (e) { b.disabled = false; if (e.message !== "login required") result.textContent = "Terminate failed: " + e.message; return; }
        // Termination is gated: the server returns a pending action that must be confirmed (same flow as pod create).
        if (typeof r.pending === "string" && r.pending) { result.textContent = ""; result.append(`Proposed termination of ${val(p.name)} (deletes it). `, confirmBtn("Confirm terminate", r.pending, true)); }
        else { b.disabled = false; result.textContent = "Terminate failed: unexpected server response"; }
      } }, "Terminate"))); })),
    h("form", { onsubmit: async (e) => { e.preventDefault(); let r; try { r = await post("/api/compute/runpod/pods", { name: name.value, gpu_type: gpu.value, hours: Number(hours.value) }); } catch (er) { result.textContent = er.message; return; } if (!r || !r.pending) { result.textContent = "Unexpected server response"; return; } result.textContent = ""; result.append("Proposed. ", confirmBtn("Confirm rent", r.pending, true)); } }, ...podForm, h("button", {}, "Propose pod")),
    h("h3", {}, "Kaggle (free GPU, weekly quota)"),
    h("form", { onsubmit: async (e) => { e.preventDefault(); let r; try { r = await post("/api/compute/kaggle/run", { slug: slug.value, code: code.value, gpu: true }); } catch (er) { result.textContent = er.message; return; } if (!r || !r.pending) { result.textContent = "Unexpected server response"; return; } result.textContent = ""; result.append("Proposed. ", confirmBtn("Confirm run", r.pending, false)); } }, ...kaggleForm, h("button", {}, "Propose run")),
    result);
}

/* ---- setup ---- */
async function renderSetup(signal) {
  const box = $("tab-setup"); const list = arr(await getJSON("/api/providers", signal));
  box.replaceChildren(h("p", { class: "muted" }, "You create the accounts; Jarvis never automates registration or verification. Open the sign-up page, create a token, paste it here. Tokens are checked with a read-only call, then handed to a root helper; they are never shown again."),
    h("div", { class: "grid" }, ...list.map((p, pi) => {
      p = obj(p);
      const inputs = Object.entries(obj(p.fields)).map(([k, kind]) => h("input", { name: k, placeholder: k, type: kind === "secret" ? "password" : "text", autocomplete: "off" }));
      const labelled = inputs.flatMap((i, ii) => field(`prov-${pi}-${ii}`, `${val(p.label)} ${i.name}`, i));
      const out = h("div", { class: "muted", role: "status" });
      return h("div", { class: "card" }, h("h3", {}, val(p.label), " (", val(p.cost), ")"), h("div", { class: "muted" }, val(p.kind)), h("div", {}, dot(p.configured ? "ok" : "bad"), p.configured ? "configured" : "not set up"),
        h("ol", {}, ...arr(p.steps).map((s) => h("li", {}, val(s)))), h("div", {}, safeLink(p.signup, "Sign up"), " · ", safeLink(p.keys, "Create token")),
        h("form", { onsubmit: async (e) => { e.preventDefault(); const values = {}; inputs.forEach((i) => { values[i.name] = i.value; }); try { await post(`/api/providers/${encodeURIComponent(p.id)}/key`, { values }); inputs.forEach((i) => { i.value = ""; }); out.textContent = "Accepted and staged."; } catch (er) { out.textContent = er.message; } } }, ...labelled, h("button", {}, "Verify and save")), out);
    })));
}

/* ---- boot ---- */
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault(); $("login-err").textContent = "";
  try { await post("/api/login", { username: $("u").value, password: $("p").value, code: $("c").value }); $("p").value = ""; $("c").value = ""; await initChat(); showApp(); }
  catch (er) { $("login-err").textContent = er.message; }
});
$("logout").addEventListener("click", async () => {
  try { await post("/api/logout"); location.reload(); }
  catch (e) { if (e.message !== "login required") alert("Sign out failed: " + e.message); }
});
(async () => {
  try {
    const res = await fetch("/api/auth/state", { credentials: "same-origin" });
    if (!res.ok) throw new Error("server returned " + res.status + (res.statusText ? " " + res.statusText : ""));
    const s = obj(await res.json());
    if (!s.configured) $("login-note").textContent = "No account yet. On the host run: python -m jarvis.manage set-password";
    if (s.logged_in) { await initChat(); showApp(); } else showLogin();
  } catch (e) {
    if (e.message !== "login required") { showLogin(); $("login-err").textContent = "Cannot reach Jarvis: " + e.message + ". Reload to retry."; }
  }
  setInterval(() => current === "Overview" && !$("app").classList.contains("hidden") && pollOverview(), 15000);
})();
