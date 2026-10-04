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
  if (res.status === 401) { showLogin(); throw new Error("login required"); }
  if (!res.ok) { let d = res.statusText; try { d = (await res.json()).detail || d; } catch {} throw new Error(d); }
  return res;
};
const getJSON = async (p) => (await api(p)).json();
const post = async (p, json) => (await api(p, { method: "POST", json: json ?? {} })).json();

const TABS = ["Overview", "Chat", "Autonomy", "Compute", "Setup", "Incidents"];
let current = "Overview";

function showLogin() { $("app").classList.add("hidden"); $("login").classList.remove("hidden"); }
function showApp() { $("login").classList.add("hidden"); $("app").classList.remove("hidden"); buildTabs(); open(current); }

function buildTabs() {
  const nav = $("tabs"); nav.replaceChildren();
  for (const t of TABS) nav.append(h("button", { class: t === current ? "on" : "", onclick: () => open(t) }, t));
}
function open(t) {
  current = t; buildTabs();
  for (const name of TABS) $("tab-" + name.toLowerCase()).classList.toggle("hidden", name !== t);
  ({ Overview: renderOverview, Autonomy: renderAutonomy, Compute: renderCompute, Setup: renderSetup, Incidents: renderIncidents }[t] || (() => {}))();
}

async function renderOverview() {
  const box = $("tab-overview"); const s = await getJSON("/api/status");
  $("paused").classList.toggle("hidden", !s.paused);
  const pct = (x) => (x == null ? "n/a" : (x * 100).toFixed(1) + "%");
  box.replaceChildren(h("div", { class: "grid" },
    h("div", { class: "card" }, h("h3", {}, "Services"), ...s.services.map((x) => h("div", {}, h("span", { class: "dot " + (x.ok ? "ok" : "bad") }), x.name))),
    h("div", { class: "card" }, h("h3", {}, "Host"), h("div", {}, "Profile: ", s.profile || "n/a"), h("div", {}, "Memory available: ", pct(s.host.mem_available_ratio)), h("div", {}, "CPU steal: ", pct(s.host.cpu_steal))),
    h("div", { class: "card" }, h("h3", {}, "AI spend (30 days, estimate)"), h("div", {}, "$", s.spend.estimated_usd_30d, s.spend.budget_usd_month ? " of $" + s.spend.budget_usd_month : ""), h("div", { class: "muted" }, s.spend.tokens_30d, " tokens")),
  ), h("h3", {}, "Nodes"), h("table", {}, h("tr", {}, ...["Node", "Cluster", "Role", "vCPU", "RAM GB", "IP"].map((c) => h("th", {}, c))),
    ...s.nodes.map((n) => h("tr", {}, h("td", {}, n.name), h("td", {}, n.cluster), h("td", {}, n.role), h("td", {}, n.vcpu), h("td", {}, n.ram_gb), h("td", {}, n.ip)))));
}

async function renderIncidents() {
  const rows = await getJSON("/api/incidents");
  $("tab-incidents").replaceChildren(h("table", {}, h("tr", {}, ...["When", "Alert", "Outcome", "Summary"].map((c) => h("th", {}, c))),
    ...rows.map((r) => h("tr", {}, h("td", {}, new Date(r.ts * 1000).toLocaleString()), h("td", {}, r.alert), h("td", {}, r.outcome), h("td", {}, r.detail)))));
}

/* ---- chat ---- */
const history = [];
const log = () => $("log");
function line(cls, text) { const el = h("div", { class: "msg " + cls }, text); log().append(el); log().scrollTop = log().scrollHeight; return el; }
function speak(text) { if ($("speak").checked && !$("server-voice").checked && "speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(text)); }
async function speakServer(text) { const r = await api("/api/tts", { method: "POST", json: { text } }); new Audio(URL.createObjectURL(await r.blob())).play(); }

function pendingCard(ev) {
  const card = h("div", { class: "pending" }, h("div", {}, "Jarvis proposes: ", ev.name, " ", JSON.stringify(ev.args)));
  const out = h("div", { class: "muted" });
  card.append(
    h("button", { onclick: async () => { try { out.textContent = JSON.stringify((await post(`/api/actions/${ev.id}/confirm`)).result); } catch (e) { out.textContent = e.message; } } }, "Confirm"),
    " ", h("button", { class: "secondary", onclick: async () => { await post(`/api/actions/${ev.id}/discard`); card.remove(); } }, "Discard"), out);
  log().append(card);
}

async function send(text) {
  line("user", text); history.push({ role: "user", content: text });
  const reply = line("assistant", ""); let full = "";
  try {
    const res = await api("/api/chat", { method: "POST", json: { messages: history, model: $("model").value, tools: $("use-tools").checked } });
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i; while ((i = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!raw.startsWith("data: ")) continue;
        const ev = JSON.parse(raw.slice(6));
        if (ev.type === "delta") { full += ev.text; reply.textContent = full; }
        else if (ev.type === "tool") line("tool", `${ev.name}: ${ev.result}`);
        else if (ev.type === "pending") pendingCard(ev);
        else if (ev.type === "error") line("tool", "error: " + ev.text);
      }
      log().scrollTop = log().scrollHeight;
    }
  } catch (e) { reply.textContent = "error: " + e.message; }
  if (full) { history.push({ role: "assistant", content: full }); speak(full); if ($("speak").checked && $("server-voice").checked) speakServer(full.slice(0, 1000)).catch(() => {}); }
}

async function startMic() {
  if ($("server-voice").checked && navigator.mediaDevices) {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); const rec = new MediaRecorder(stream); const chunks = [];
    rec.ondataavailable = (e) => chunks.push(e.data);
    rec.onstop = async () => { stream.getTracks().forEach((t) => t.stop()); const fd = new FormData(); fd.append("file", new Blob(chunks, { type: "audio/webm" }), "voice.webm");
      try { const r = await (await api("/api/stt", { method: "POST", body: fd })).json(); if (r.text) send(r.text); } catch (e) { line("tool", "voice error: " + e.message); } };
    rec.start(); setTimeout(() => rec.state === "recording" && rec.stop(), 8000); return;
  }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { line("tool", "This browser has no speech recognition; enable server voice."); return; }
  const r = new SR(); r.onresult = (e) => send(e.results[0][0].transcript); r.start();
}

async function initChat() {
  for (const m of await getJSON("/api/models")) $("model").append(h("option", {}, m));
  $("chat-form").addEventListener("submit", (e) => { e.preventDefault(); const t = $("msg").value.trim(); if (t) { $("msg").value = ""; send(t); } });
  $("mic").addEventListener("click", () => startMic().catch((e) => line("tool", e.message)));
  $("img").addEventListener("click", async () => {
    const prompt = $("msg").value.trim(); if (!prompt) return; $("msg").value = ""; line("user", "image: " + prompt);
    try { const r = await post("/api/image", { prompt }); if (r.b64) log().append(h("img", { class: "gen", src: "data:image/png;base64," + r.b64 })); } catch (e) { line("tool", e.message); }
  });
}

/* ---- autonomy ---- */
async function renderAutonomy() {
  const a = await getJSON("/api/autonomy");
  const note = h("p", { class: "muted" });
  const act = (id, verb) => async () => { try { note.textContent = JSON.stringify((await post(`/api/actions/${id}/${verb}`)).result ?? "done"); } catch (e) { note.textContent = e.message; } renderAutonomy(); };
  $("tab-autonomy").replaceChildren(
    h("div", { class: "grid" },
      h("div", { class: "card" }, h("h3", {}, "Mode"), h("div", {}, a.mode), h("div", { class: "muted" }, "Changed only by a reviewed PR to agents/autonomy.yaml"), h("div", { class: "err" }, a.error || "")),
      h("div", { class: "card" }, h("h3", {}, "Circuit breaker"), h("div", {}, h("span", { class: "dot " + (a.breaker.tripped ? "bad" : "ok") }), a.breaker.tripped ? "tripped: " + a.breaker.reason : "closed"), a.breaker.tripped ? h("button", { onclick: async () => { await post("/api/autonomy/breaker/reset"); renderAutonomy(); } }, "Reset") : ""),
      h("div", { class: "card" }, h("h3", {}, "Audit log"), h("div", {}, h("span", { class: "dot " + (a.audit.chain_ok ? "ok" : "bad") }), a.audit.chain_ok ? "chain intact" : "CHAIN BROKEN at entry " + a.audit.first_bad), h("div", { class: "muted" }, a.audit.entries, " entries")),
      h("div", { class: "card" }, h("h3", {}, "Actions in 24 h"), h("div", {}, a.actions_24h))),
    h("h3", {}, "Waiting for you"), note,
    a.approvals.length ? h("table", {}, h("tr", {}, ...["Action", "Target", "Why", ""].map((c) => h("th", {}, c))),
      ...a.approvals.map((p) => h("tr", {}, h("td", {}, p.action), h("td", {}, p.target), h("td", {}, (p.context || {}).reason || "", " ", (p.context || {}).alert || ""), h("td", {}, h("button", { onclick: act(p.id, "confirm") }, "Confirm"), " ", h("button", { class: "secondary", onclick: act(p.id, "discard") }, "Discard"))))) : h("p", { class: "muted" }, "Nothing is waiting."),
    ...(a.promotion_candidates.length ? [h("h3", {}, "Earned trust"), ...a.promotion_candidates.map((c) => h("p", {}, `${c.action}: ${c.streak} approved successes in a row. Consider level: auto in agents/autonomy.yaml (a reviewed PR).`))] : []),
    h("h3", {}, "Recent decisions"),
    h("table", {}, h("tr", {}, ...["When", "Kind", "Actor", "Action", "Target", "Result"].map((c) => h("th", {}, c))),
      ...a.audit_tail.map((e) => h("tr", {}, h("td", {}, new Date(e.ts * 1000).toLocaleString()), h("td", {}, e.kind), h("td", {}, e.actor), h("td", {}, e.action), h("td", {}, e.target), h("td", {}, e.level || (e.ok === undefined ? e.reason || "" : e.ok ? "ok" : "failed"), e.reason && e.level ? ": " + e.reason : "")))));
}

/* ---- compute ---- */
async function renderCompute() {
  const box = $("tab-compute"); const gpus = await getJSON("/api/compute/gpus"); const msg = h("p", { class: "muted" });
  let pods = [];
  try { pods = await getJSON("/api/compute/runpod/pods"); } catch (e) { msg.textContent = "RunPod: " + e.message; }
  const name = h("input", { placeholder: "name (a-z, 0-9, -)" }); const gpu = h("select", {}, ...gpus.map((g) => h("option", {}, g))); const hours = h("input", { type: "number", value: "2", min: "0.5", step: "0.5" });
  const slug = h("input", { placeholder: "kaggle slug" }); const code = h("textarea", { placeholder: "Python script to run on a Kaggle GPU (private, internet off)" });
  const result = h("p", {});
  box.replaceChildren(
    h("h3", {}, "RunPod (paid, hourly cap and auto-terminate apply)"), msg,
    h("table", {}, h("tr", {}, ...["Pod", "Status", "$/h", "Image", ""].map((c) => h("th", {}, c))),
      ...pods.map((p) => h("tr", {}, h("td", {}, p.name), h("td", {}, p.desiredStatus), h("td", {}, p.costPerHr), h("td", {}, p.image), h("td", {}, h("button", { class: "danger", onclick: async () => { await post(`/api/compute/runpod/pods/${p.id}/terminate`); renderCompute(); } }, "Terminate"))))),
    h("form", { onsubmit: async (e) => { e.preventDefault(); const r = await post("/api/compute/runpod/pods", { name: name.value, gpu_type: gpu.value, hours: Number(hours.value) }); result.textContent = ""; result.append("Proposed. ", h("button", { type: "button", onclick: async () => { try { result.textContent = JSON.stringify((await post(`/api/actions/${r.pending}/confirm`)).result); } catch (er) { result.textContent = er.message; } renderCompute(); } }, "Confirm rent")); } }, name, gpu, hours, h("button", {}, "Propose pod")),
    h("h3", {}, "Kaggle (free GPU, weekly quota)"),
    h("form", { onsubmit: async (e) => { e.preventDefault(); const r = await post("/api/compute/kaggle/run", { slug: slug.value, code: code.value, gpu: true }); result.textContent = ""; result.append("Proposed. ", h("button", { type: "button", onclick: async () => { try { result.textContent = JSON.stringify((await post(`/api/actions/${r.pending}/confirm`)).result); } catch (er) { result.textContent = er.message; } } }, "Confirm run")); } }, slug, code, h("button", {}, "Propose run")),
    result);
}

/* ---- setup ---- */
async function renderSetup() {
  const box = $("tab-setup"); const list = await getJSON("/api/providers");
  box.replaceChildren(h("p", { class: "muted" }, "You create the accounts; Jarvis never automates registration or verification. Open the sign-up page, create a token, paste it here. Tokens are checked with a read-only call, then handed to a root helper; they are never shown again."),
    h("div", { class: "grid" }, ...list.map((p) => {
      const inputs = Object.entries(p.fields).map(([k, kind]) => h("input", { name: k, placeholder: k, type: kind === "secret" ? "password" : "text", autocomplete: "off" }));
      const out = h("div", { class: "muted" });
      return h("div", { class: "card" }, h("h3", {}, p.label, " (", p.cost, ")"), h("div", { class: "muted" }, p.kind), h("div", {}, h("span", { class: "dot " + (p.configured ? "ok" : "bad") }), p.configured ? "configured" : "not set up"),
        h("ol", {}, ...p.steps.map((s) => h("li", {}, s))), h("div", {}, h("a", { href: p.signup, target: "_blank", rel: "noopener noreferrer" }, "Sign up"), " · ", h("a", { href: p.keys, target: "_blank", rel: "noopener noreferrer" }, "Create token")),
        h("form", { onsubmit: async (e) => { e.preventDefault(); const values = {}; inputs.forEach((i) => { values[i.name] = i.value; }); try { await post(`/api/providers/${p.id}/key`, { values }); inputs.forEach((i) => { i.value = ""; }); out.textContent = "Accepted and staged."; } catch (er) { out.textContent = er.message; } } }, ...inputs, h("button", {}, "Verify and save")), out);
    })));
}

/* ---- boot ---- */
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault(); $("login-err").textContent = "";
  try { await post("/api/login", { username: $("u").value, password: $("p").value, code: $("c").value }); $("p").value = ""; $("c").value = ""; await initChat(); showApp(); }
  catch (er) { $("login-err").textContent = er.message; }
});
$("logout").addEventListener("click", async () => { await post("/api/logout"); location.reload(); });
(async () => {
  const s = await (await fetch("/api/auth/state")).json();
  if (!s.configured) $("login-note").textContent = "No account yet. On the host run: python -m jarvis.manage set-password";
  if (s.logged_in) { await initChat(); showApp(); } else showLogin();
  setInterval(() => current === "Overview" && !$("app").classList.contains("hidden") && renderOverview().catch(() => {}), 15000);
})();
