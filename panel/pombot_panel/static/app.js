"use strict";
/* PomBot VE – Web-Oberfläche (Single-Page-App ohne Build-Schritt) */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const S = { me: null, cfg: null, handlers: {}, live: false, hist: {}, showAll: true, lastSidebar: 0 };

// ------------------------------------------------------------------ API

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-PomBot": "1" }, credentials: "same-origin" };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch (e) { /* leer */ }
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    S.me = null;
    if (!location.hash.startsWith("#/login")) location.hash = "#/login";
    throw new Error("Bitte erneut anmelden");
  }
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => (d.loc ? d.loc[d.loc.length - 1] + ": " : "") + d.msg).join("; ");
    throw new Error(msg || `Fehler ${res.status}`);
  }
  return data;
}

// ------------------------------------------------------------------ Formatierung

const fmtBytes = (b) => {
  if (b == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(b >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
};
const fmtMB = (mb) => (mb >= 1024 ? `${+(mb / 1024).toFixed(1)} GB` : `${mb} MB`);
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "–");
const fmtUptime = (s) => {
  if (!s) return "–";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} T ${h} Std` : h ? `${h} Std ${m} Min` : `${m} Min`;
};
const pct = (a, b) => (b ? Math.min(100, Math.max(0, (a / b) * 100)) : 0);
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

// ------------------------------------------------------------------ Icons

const ICONS = {
  home: "M3 10.5 12 3l9 7.5V21a1 1 0 0 1-1 1h-5v-7h-6v7H4a1 1 0 0 1-1-1z",
  server: "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01",
  plus: "M12 5v14M5 12h14",
  node: "M3 5h18v10H3zM8 21h8M12 15v6",
  net: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18",
  users: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
  disk: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  user: "M20 21a8 8 0 0 0-16 0M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10z",
  play: "M6 4l14 8-14 8z",
  stop: "M6 6h12v12H6z",
  power: "M12 2v10M18.4 6.6a9 9 0 1 1-12.8 0",
  refresh: "M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6",
  terminal: "M4 17l6-5-6-5M12 19h8",
  menu: "M3 6h18M3 12h18M3 18h18",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
};
const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICONS[name]}"/></svg>`;
const discordIcon = `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M20.3 4.4A19.8 19.8 0 0 0 15.4 3l-.6 1.3a18.3 18.3 0 0 0-5.5 0L8.6 3a19.7 19.7 0 0 0-4.9 1.5C.6 9.1-.3 13.6.1 18.1a19.9 19.9 0 0 0 6 3l1.3-2a12.9 12.9 0 0 1-2-1l.5-.4a14.2 14.2 0 0 0 12.2 0l.5.4-2 1 1.3 2a19.8 19.8 0 0 0 6-3c.5-5.2-.8-9.7-3.6-13.7zM8.1 15.3c-1.2 0-2.2-1.1-2.2-2.4s1-2.4 2.2-2.4 2.2 1.1 2.2 2.4-1 2.4-2.2 2.4zm7.8 0c-1.2 0-2.2-1.1-2.2-2.4s1-2.4 2.2-2.4 2.2 1.1 2.2 2.4-1 2.4-2.2 2.4z"/></svg>`;

// ------------------------------------------------------------------ UI-Bausteine

function toast(msg, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), type === "bad" ? 7000 : 3500);
}
const fail = (e) => toast(e.message || String(e), "bad");

function badge(text, cls = "") { return `<span class="badge ${cls}">${esc(text)}</span>`; }

function guestBadge(g) {
  if (g.status === "creating") return badge("Wird erstellt", "info");
  if (g.status === "deleting") return badge("Wird gelöscht", "warn");
  if (g.status === "busy") return badge("Beschäftigt", "info");
  if (g.status === "error") return badge("Fehler", "bad");
  return {
    running: badge("Läuft", "good"), stopped: badge("Gestoppt"), paused: badge("Pausiert", "warn"),
    missing: badge("Nicht gefunden", "bad"), stopping: badge("Fährt herunter", "warn"),
  }[g.power] || badge("Unbekannt");
}

function meter(label, used, total, text) {
  const p = pct(used, total);
  const cls = p >= 90 ? "bad" : p >= 75 ? "warn" : "";
  return `<div class="meter"><div class="meter-top"><span class="label">${esc(label)}</span><span class="val">${esc(text)}</span></div>
    <div class="bar ${cls}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${p.toFixed(0)}" aria-label="${esc(label)}"><span style="width:${p.toFixed(1)}%"></span></div></div>`;
}

function sparkline(points, key, max = 100, unit = "%") {
  if (!points || points.length < 2) return `<div class="muted small" style="height:64px;display:grid;place-items:center">Verlauf wird gesammelt …</div>`;
  const w = 300, h = 64;
  const x = (i) => (i / (points.length - 1)) * w;
  const y = (v) => h - (Math.min(v ?? 0, max) / max) * h;
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join("");
  const data = JSON.stringify(points.map((p) => [p.t, p[key]]));
  return `<div class="spark-wrap" data-spark="${esc(data)}" data-unit="${unit}" data-max="${max}">
    <svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Verlauf">
      <line class="grid-line" x1="0" x2="${w}" y1="${h / 2}" y2="${h / 2}" vector-effect="non-scaling-stroke"/>
      <path class="area" d="${d}L${w},${h}L0,${h}Z"/>
      <path class="line" d="${d}" vector-effect="non-scaling-stroke"/>
      <line class="cursor hidden" y1="0" y2="${h}" vector-effect="non-scaling-stroke"/>
    </svg><div class="spark-tip hidden"></div></div>`;
}

document.addEventListener("mousemove", (e) => {
  const wrap = e.target.closest && e.target.closest(".spark-wrap");
  $$(".spark-wrap").forEach((w) => {
    if (w !== wrap) { $(".spark-tip", w).classList.add("hidden"); $(".cursor", w).classList.add("hidden"); }
  });
  if (!wrap) return;
  const data = JSON.parse(wrap.dataset.spark);
  const rect = wrap.getBoundingClientRect();
  const i = Math.round(Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)) * (data.length - 1));
  const [t, v] = data[i];
  const xr = i / (data.length - 1);
  const cur = $(".cursor", wrap);
  cur.setAttribute("x1", xr * 300); cur.setAttribute("x2", xr * 300); cur.classList.remove("hidden");
  const tip = $(".spark-tip", wrap);
  tip.textContent = `${(v ?? 0).toFixed(1)} ${wrap.dataset.unit} · ${new Date(t * 1000).toLocaleTimeString("de-DE")}`;
  tip.style.left = `${xr * 100}%`;
  tip.classList.remove("hidden");
});

function modal({ title, body, foot = "", wide = false, onClose }) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="modal-head"><h2>${esc(title)}</h2><button class="x" data-close aria-label="Schließen">×</button></div>
    <div class="modal-body">${body}</div>${foot ? `<div class="modal-foot">${foot}</div>` : ""}</div>`;
  document.body.appendChild(back);
  back.close = () => { back.remove(); if (onClose) onClose(); };
  back.addEventListener("mousedown", (e) => { if (e.target === back) back.close(); });
  back.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) back.close(); });
  const first = $("input, select, textarea", back);
  if (first) setTimeout(() => first.focus(), 30);
  return back;
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { const m = $$(".modal-back").pop(); if (m) m.close(); }
});

function confirmBox(title, text, { danger = false, ok = "Bestätigen", requireText = null } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const m = modal({
      title,
      body: `<p style="margin-top:0">${text}</p>${requireText ? `<label class="field"><span>Zur Bestätigung <code>${esc(requireText)}</code> eingeben</span><input type="text" id="confirm-text" autocomplete="off"></label>` : ""}`,
      foot: `<button class="btn" data-close>Abbrechen</button><button class="btn ${danger ? "danger solid" : "primary"}" id="confirm-ok" ${requireText ? "disabled" : ""}>${esc(ok)}</button>`,
      onClose: () => { if (!done) resolve(false); },
    });
    if (requireText) $("#confirm-text", m).addEventListener("input", (e) => { $("#confirm-ok", m).disabled = e.target.value.trim() !== String(requireText); });
    $("#confirm-ok", m).onclick = () => { done = true; m.close(); resolve(true); };
  });
}

/** Formular-Dialog. fields = HTML; onSubmit(data) → Promise; Fehler werden im Dialog angezeigt. */
function formModal({ title, fields, submit = "Speichern", wide = false, onSubmit, onReady }) {
  const m = modal({
    title, wide,
    body: `<form id="modal-form" novalidate>${fields}<div class="alert bad hidden" id="form-error"></div><button type="submit" class="hidden"></button></form>`,
    foot: `<button class="btn" data-close>Abbrechen</button><button class="btn primary" id="form-submit">${esc(submit)}</button>`,
  });
  const form = $("#modal-form", m);
  const go = async (e) => {
    if (e) e.preventDefault();
    const btn = $("#form-submit", m);
    const data = {};
    for (const el of form.elements) {
      if (!el.name) continue;
      if (el.type === "checkbox") data[el.name] = el.checked;
      else if (el.type === "number") data[el.name] = el.value === "" ? null : Number(el.value);
      else if (el.type === "radio") { if (el.checked) data[el.name] = el.value; }
      else data[el.name] = el.value;
    }
    btn.disabled = true;
    $("#form-error", m).classList.add("hidden");
    try {
      await onSubmit(data, m);
    } catch (err) {
      $("#form-error", m).textContent = err.message;
      $("#form-error", m).classList.remove("hidden");
    } finally { btn.disabled = false; }
  };
  form.addEventListener("submit", go);
  $("#form-submit", m).onclick = go;
  if (onReady) onReady(m);
  return m;
}

function copyText(text) {
  (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(
    () => toast("In die Zwischenablage kopiert"),
    () => toast("Kopieren nicht möglich – bitte manuell markieren", "bad"));
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (b) copyText(b.dataset.copy);
});

function showSecret(title, intro, secret, extra = "") {
  modal({
    title,
    body: `<p style="margin-top:0">${intro}</p><div class="secret"><span>${esc(secret)}</span><button class="btn sm" data-copy="${esc(secret)}">Kopieren</button></div>
      <p class="muted small">Dieses Passwort wird nur jetzt angezeigt und nirgends gespeichert.</p>${extra}`,
    foot: `<button class="btn primary" data-close>Verstanden</button>`,
  });
}

/** Zeigt den Live-Log einer Aufgabe. Liefert ein Promise mit dem Endstatus. */
function watchTask(taskId, title) {
  return new Promise((resolve) => {
    let stop = false;
    const m = modal({
      title, wide: true,
      body: `<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px" id="task-state"><span class="spinner"></span> Läuft …</div><div class="log" id="task-log">Warte auf Ausgabe …</div>`,
      foot: `<span class="muted small" style="margin-right:auto">Du kannst das Fenster schließen – die Aufgabe läuft weiter.</span><button class="btn" data-close>Schließen</button>`,
      onClose: () => { stop = true; },
    });
    const tick = async () => {
      if (stop) return;
      try {
        const t = await api(`/api/tasks/${taskId}`);
        const log = $("#task-log", m);
        if (log) {
          const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
          log.textContent = t.log || "Warte auf Ausgabe …";
          if (atBottom) log.scrollTop = log.scrollHeight;
        }
        if (t.status !== "running") {
          $("#task-state", m).innerHTML = t.status === "ok" ? badge("Erfolgreich abgeschlossen", "good") : badge("Fehlgeschlagen", "bad");
          resolve(t.status);
          if (S.live) route(true);
          return;
        }
      } catch (e) { /* weiter versuchen */ }
      setTimeout(tick, 1500);
    };
    tick();
  });
}

// ------------------------------------------------------------------ Shell & Navigation

function renderShell() {
  if ($(".app")) return;
  const me = S.me;
  const initials = esc((me.username || "?").slice(0, 2).toUpperCase());
  $("#root").innerHTML = `
  <div class="app">
    <header class="topbar">
      <button class="btn sm menu-btn" id="menu-btn" aria-label="Menü">${icon("menu")}</button>
      <a class="brand" href="#/dashboard"><span class="brand-logo">P</span>PomBot VE</a>
      <span class="spacer"></span>
      <a class="btn sm primary" href="#/create">${icon("plus")}<span>Server erstellen</span></a>
      <button class="btn sm" id="theme-btn" title="Hell/Dunkel umschalten" aria-label="Design umschalten">${icon("moon")}</button>
      <a class="user-chip" href="#/account"><span class="avatar">${me.avatar_url ? `<img src="${esc(me.avatar_url)}" alt="">` : initials}</span><span class="uname">${esc(me.username)}</span></a>
      <button class="btn sm" id="logout-btn" title="Abmelden" aria-label="Abmelden">${icon("logout")}</button>
    </header>
    <nav class="sidebar" id="sidebar"></nav>
    <main class="main" id="main"></main>
  </div>`;
  $("#logout-btn").onclick = async () => { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); S.me = null; location.hash = "#/login"; };
  $("#menu-btn").onclick = () => $("#sidebar").classList.toggle("open");
  $("#theme-btn").onclick = () => {
    const dark = getComputedStyle(document.documentElement).colorScheme === "dark";
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("pombot-theme", next); } catch (e) { /* egal */ }
  };
}

async function updateSidebar() {
  const sb = $("#sidebar");
  if (!sb) return;
  S.lastSidebar = Date.now();
  const admin = S.me.role === "admin";
  let guests = [], nodes = [];
  try {
    [guests, nodes] = await Promise.all([api(`/api/guests${admin ? "?all=true" : ""}`), admin ? api("/api/nodes") : []]);
  } catch (e) { return; }
  const dot = (g) => `<span class="dot-status ${g.power === "running" ? "good" : g.status === "error" || g.power === "missing" ? "bad" : ""}"></span>`;
  const gLink = (g, cls = "") => `<a href="#/guest/${g.id}" class="${cls}" data-nav="guest/${g.id}">${dot(g)}<span class="mono">${g.vmid}</span> ${esc(g.name)}</a>`;
  let tree = "";
  if (admin) {
    tree = nodes.map((n) => `<a href="#/node/${n.id}" class="tree-node" data-nav="node/${n.id}"><span class="dot-status ${n.status === "online" ? "good" : "bad"}"></span>${esc(n.name)}</a>
      ${guests.filter((g) => g.node_id === n.id).map((g) => gLink(g, "tree-guest")).join("")}`).join("")
      || `<div class="muted small" style="padding:4px 14px">Noch keine Nodes</div>`;
  } else {
    tree = guests.map((g) => gLink(g)).join("") || `<div class="muted small" style="padding:4px 14px">Noch keine Server</div>`;
  }
  sb.innerHTML = `
    <div class="nav">
      <a href="#/dashboard" data-nav="dashboard">${icon("home")}Übersicht</a>
      <a href="#/guests" data-nav="guests">${icon("server")}Server<span class="count">${guests.length}</span></a>
      <a href="#/create" data-nav="create">${icon("plus")}Server erstellen</a>
      <a href="#/tasks" data-nav="tasks">${icon("list")}Aufgaben</a>
      <a href="#/account" data-nav="account">${icon("user")}Mein Konto</a>
    </div>
    ${admin ? `<div class="nav-section">Rechenzentrum</div>
    <div class="nav">
      <a href="#/nodes" data-nav="nodes">${icon("node")}Nodes<span class="count">${nodes.length}</span></a>
      <a href="#/pools" data-nav="pools">${icon("net")}IP-Pools</a>
      <a href="#/templates" data-nav="templates">${icon("disk")}Vorlagen</a>
      <a href="#/users" data-nav="users">${icon("users")}Benutzer</a>
      <a href="#/audit" data-nav="audit">${icon("shield")}Protokoll</a>
      <a href="#/settings" data-nav="settings">${icon("gear")}Einstellungen</a>
    </div>` : ""}
    <div class="nav-section">${admin ? "Ressourcenbaum" : "Meine Server"}</div>
    <div class="nav tree">${tree}</div>`;
  markActive();
}

function markActive() {
  const h = location.hash.replace(/^#\//, "").split("?")[0];
  $$("#sidebar [data-nav]").forEach((a) => {
    const n = a.dataset.nav;
    a.classList.toggle("active", h === n || h.startsWith(n + "/"));
  });
}

function setMain(html) { const m = $("#main"); if (m) m.innerHTML = html; }

function pageHead(title, sub = "", actions = "") {
  return `<div class="page-head"><div class="grow"><h1>${title}</h1>${sub ? `<div class="sub">${sub}</div>` : ""}</div><div class="actions">${actions}</div></div>`;
}

function tabs(base, current, list) {
  return `<nav class="tabs">${list.map(([id, label]) => `<a href="${base}/${id}" class="${id === current ? "active" : ""}">${label}</a>`).join("")}</nav>`;
}

// Klick-Aktionen: <button data-act="name" data-...>
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const fn = S.handlers[el.dataset.act];
  if (fn) { e.preventDefault(); Promise.resolve(fn(el.dataset, el)).catch(fail); }
});

// ------------------------------------------------------------------ Router

const ROUTES = [
  [/^login$/, viewLogin, true],
  [/^dashboard$/, viewDashboard],
  [/^guests$/, viewGuests],
  [/^guest\/(\d+)(?:\/(\w+))?$/, viewGuest],
  [/^create$/, viewCreate],
  [/^nodes$/, viewNodes],
  [/^node\/(\d+)(?:\/(\w+))?$/, viewNode],
  [/^pools$/, viewPools],
  [/^pool\/(\d+)$/, viewPool],
  [/^users$/, viewUsers],
  [/^templates$/, viewTemplates],
  [/^tasks$/, viewTasks],
  [/^audit$/, viewAudit],
  [/^settings(?:\/(\w+))?$/, viewSettings],
  [/^account$/, viewAccount],
];

let routeSeq = 0;
async function route(silent = false) {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  const seq = ++routeSeq;
  if (!S.cfg) S.cfg = await api("/api/auth/config").catch(() => ({ discord: false }));
  let match = null, view = null, isPublic = false;
  for (const [re, fn, pub] of ROUTES) {
    const m = path.match(re);
    if (m) { match = m; view = fn; isPublic = !!pub; break; }
  }
  if (!view) { location.hash = "#/dashboard"; return; }
  if (!isPublic && !S.me) {
    try { S.me = await api("/api/me"); } catch (e) { return; }
  }
  if (isPublic) { S.live = false; await view(params, match); return; }
  renderShell();
  if (!silent) {
    $("#sidebar").classList.remove("open");
    S.handlers = {};
    setMain(`<div class="muted"><span class="spinner"></span> Lade …</div>`);
    updateSidebar();
  } else if (Date.now() - S.lastSidebar > 15000) {
    updateSidebar();
  }
  markActive();
  try {
    const res = await view(params, match, silent, seq);
    if (seq === routeSeq) S.live = !!(res && res.live);
  } catch (e) {
    if (!silent) setMain(`<div class="alert bad">${esc(e.message)}</div>`);
  }
}

const isStale = (seq) => seq !== undefined && seq !== routeSeq;
window.addEventListener("hashchange", () => route());
setInterval(() => {
  if (!S.live || document.hidden || $(".modal-back")) return;
  const a = document.activeElement;
  if (a && $("#main") && $("#main").contains(a) && /INPUT|SELECT|TEXTAREA/.test(a.tagName)) return;
  route(true);
}, 5000);

// ------------------------------------------------------------------ Login

async function viewLogin(params) {
  if (!S.me) { try { S.me = await api("/api/me"); } catch (e) { S.me = null; } }
  if (S.me) { location.hash = "#/dashboard"; return; }
  const err = params.get("error");
  const pending = params.get("pending");
  $("#root").innerHTML = `
  <div class="login-page"><div class="login-card">
    <div class="brand"><span class="brand-logo">P</span>PomBot VE</div>
    <div class="card"><div class="card-body" style="padding:24px">
      <h2 style="margin-bottom:4px">Anmelden</h2>
      <p class="muted" style="margin-top:0">Virtualisierung für deine Server.</p>
      ${err ? `<div class="alert bad" style="margin-bottom:14px">${esc(err)}</div>` : ""}
      ${pending ? `<div class="alert warn" style="margin-bottom:14px">Dein Konto wurde angelegt und wartet auf Freischaltung durch einen Administrator.</div>` : ""}
      ${S.cfg.discord ? `<a class="btn discord lg block" href="/api/auth/discord/login">${discordIcon} Mit Discord anmelden</a><div class="divider">oder mit Passwort</div>` : ""}
      <form id="login-form">
        <label class="field"><span>Benutzername</span><input type="text" name="username" autocomplete="username" required></label>
        <label class="field"><span>Passwort</span><input type="password" name="password" autocomplete="current-password" required></label>
        <div class="alert bad hidden" id="login-error" style="margin-bottom:12px"></div>
        <button class="btn ${S.cfg.discord ? "" : "primary"} block lg" type="submit">Anmelden</button>
      </form>
    </div></div>
    <p class="muted small" style="text-align:center">Version ${esc(S.cfg.version || "")}</p>
  </div></div>`;
  $("#login-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("/api/auth/login", { method: "POST", body: { username: f.username.value, password: f.password.value } });
      S.me = await api("/api/me");
      $("#root").innerHTML = "";
      location.hash = "#/dashboard";
    } catch (err2) {
      $("#login-error").textContent = err2.message;
      $("#login-error").classList.remove("hidden");
    }
  };
}

// ------------------------------------------------------------------ Dashboard

function quotaMeters(q, u) {
  return meter("Server", u.guests, q.guests, `${u.guests} / ${q.guests}`)
    + meter("CPU-Kerne", u.cores, q.cores, `${u.cores} / ${q.cores}`)
    + meter("Arbeitsspeicher", u.memory_mb, q.memory_mb, `${fmtMB(u.memory_mb)} / ${fmtMB(q.memory_mb)}`)
    + meter("Speicher", u.disk_gb, q.disk_gb, `${u.disk_gb} / ${q.disk_gb} GB`)
    + meter("IP-Adressen", u.ips, q.ips, `${u.ips} / ${q.ips}`);
}

function taskRows(tasks) {
  if (!tasks.length) return `<div class="empty">Noch keine Aufgaben</div>`;
  const labels = TASK_LABELS;
  return `<div class="table-wrap"><table><thead><tr><th>Aufgabe</th><th>Ziel</th><th>Benutzer</th><th>Start</th><th>Status</th></tr></thead><tbody>
    ${tasks.map((t) => `<tr class="click" data-act="task" data-id="${t.id}" data-title="${esc((labels[t.action] || t.action) + ": " + t.target)}">
      <td>${esc(labels[t.action] || t.action)}</td><td>${esc(t.target)}</td><td>${esc(t.user || "–")}</td><td class="nowrap">${fmtDate(t.started_at)}</td>
      <td>${t.status === "running" ? badge("Läuft", "info") : t.status === "ok" ? badge("OK", "good") : badge("Fehler", "bad")}</td></tr>`).join("")}
  </tbody></table></div>`;
}
const TASK_LABELS = {
  create: "Erstellen", delete: "Löschen", reinstall: "Neu installieren", resize: "Ressourcen ändern",
  snapshot: "Snapshot", "snapshot-delete": "Snapshot löschen", "snapshot-rollback": "Snapshot zurückspielen",
  network: "Netzwerk ändern", domain: "Domain & Zertifikat", "iso-fetch": "ISO laden", migrate: "Umzug", backup: "Backup", "backup-auto": "Automatisches Backup", restore: "Wiederherstellen", "node-install": "Node installieren",
};

async function viewDashboard(params, m, silent, seq) {
  const [d, tasks] = await Promise.all([api("/api/dashboard"), api("/api/tasks?limit=8")]);
  if (isStale(seq)) return;
  S.handlers.task = (ds) => watchTask(ds.id, ds.title);
  const my = d.my;
  const c = d.cluster;
  setMain(`
    ${pageHead(`Hallo, ${esc(S.me.username)}`, "Überblick über deine Server und Ressourcen", `<a class="btn primary" href="#/create">${icon("plus")}Server erstellen</a>`)}
    ${c && c.users_pending ? `<div class="alert warn" style="margin-bottom:16px">${plural(c.users_pending, "Benutzer wartet", "Benutzer warten")} auf Freischaltung. <a href="#/users">Jetzt prüfen</a></div>` : ""}
    ${c ? `<div class="grid grid-4" style="margin-bottom:16px">
      <div class="card stat"><div class="label">Nodes online</div><div class="value">${c.nodes_online} / ${c.nodes}</div><div class="foot">${c.cpu_cores} CPU-Kerne gesamt</div></div>
      <div class="card stat"><div class="label">Server laufen</div><div class="value">${c.guests_running} / ${c.guests}</div><div class="foot">${plural(c.users, "Benutzer", "Benutzer")}</div></div>
      <div class="card stat"><div class="label">CPU-Auslastung</div><div class="value">${c.cpu.toFixed(0)} %</div><div class="foot">über alle Nodes</div></div>
      <div class="card stat"><div class="label">Arbeitsspeicher</div><div class="value">${pct(c.memory_used, c.memory_total).toFixed(0)} %</div><div class="foot">${fmtBytes(c.memory_used)} von ${fmtBytes(c.memory_total)}</div></div>
    </div>` : ""}
    <div class="grid grid-2">
      <div class="card"><div class="card-head"><h2>Meine Server</h2><a href="#/guests" class="small">Alle anzeigen</a></div>
        <div class="card-body"><div class="grid grid-2">
          <div><div class="muted small">Gesamt</div><div style="font-size:26px;font-weight:650">${my.total}</div></div>
          <div><div class="muted small">Laufen</div><div style="font-size:26px;font-weight:650">${my.running}</div></div>
        </div>
        ${!my.total ? `<p class="muted">Du hast noch keinen Server. <a href="#/create">Jetzt einen erstellen</a> – IP-Adresse, Zugang und Netzwerk werden automatisch eingerichtet.</p>` : ""}
        </div></div>
      <div class="card"><div class="card-head"><h2>Mein Kontingent</h2></div><div class="card-body">
        ${S.me.role === "admin" ? `<p class="muted" style="margin:0">Als Administrator gelten für dich keine Limits. Kontingente anderer Benutzer legst du unter <a href="#/users">Benutzer</a> fest.</p>`
          : quotaMeters(my.quota, my.usage)}</div></div>
    </div>
    <div class="card" style="margin-top:16px"><div class="card-head"><h2>Letzte Aufgaben</h2><a href="#/tasks" class="small">Alle</a></div>${taskRows(tasks)}</div>`);
  return { live: true };
}

// ------------------------------------------------------------------ Serverliste

function guestIPs(g) { return g.ips.map((i) => i.address).join(", ") || "DHCP"; }

async function viewGuests(params, m, silent, seq) {
  const admin = S.me.role === "admin";
  const guests = await api(`/api/guests${admin && S.showAll ? "?all=true" : ""}`);
  if (isStale(seq)) return;
  const filter = $("#guest-filter") ? $("#guest-filter").value : "";
  S.handlers.toggleAll = () => { S.showAll = !S.showAll; route(); };
  S.handlers.open = (ds) => { location.hash = `#/guest/${ds.id}`; };
  S.handlers.power = async (ds) => {
    await api(`/api/guests/${ds.id}/action`, { method: "POST", body: { action: ds.action } });
    toast(ds.action === "start" ? "Server wird gestartet" : "Server wird heruntergefahren");
    route(true);
  };
  setMain(`
    ${pageHead("Server", `${plural(guests.length, "Server", "Server")}`, `
      ${admin ? `<div class="seg"><button data-act="toggleAll" class="${S.showAll ? "" : "active"}">Meine</button><button data-act="toggleAll" class="${S.showAll ? "active" : ""}">Alle</button></div>` : ""}
      <a class="btn primary" href="#/create">${icon("plus")}Server erstellen</a>`)}
    <div class="card">
      <div class="card-head"><input type="text" id="guest-filter" placeholder="Suchen nach Name, IP, VMID, Besitzer …" value="${esc(filter)}" style="max-width:360px"></div>
      ${guests.length ? `<div class="table-wrap"><table><thead><tr><th>Status</th><th>VMID</th><th>Name</th><th>Typ</th><th>IP-Adresse</th><th>Node</th>${admin ? "<th>Besitzer</th>" : ""}<th>Ressourcen</th><th></th></tr></thead><tbody>
        ${guests.map((g) => `<tr class="click" data-act="open" data-id="${g.id}" data-search="${esc([g.name, g.hostname, g.vmid, guestIPs(g), g.owner, g.node].join(" ").toLowerCase())}">
          <td>${guestBadge(g)}</td><td class="mono">${g.vmid}</td><td><strong>${esc(g.name)}</strong><div class="muted small">${esc(g.template || "")}</div></td>
          <td><span class="type-tag">${g.type === "kvm" ? "VM" : "CT"}</span></td><td class="mono">${esc(guestIPs(g))}</td><td>${esc(g.node || "–")}</td>
          ${admin ? `<td>${esc(g.owner)}</td>` : ""}
          <td class="nowrap small">${g.cores} CPU · ${fmtMB(g.memory_mb)} · ${g.disk_gb} GB</td>
          <td class="right nowrap">${g.status === "ready" ? (g.power === "running"
            ? `<button class="btn sm" data-act="power" data-action="shutdown" data-id="${g.id}" title="Herunterfahren">${icon("power")}</button>`
            : `<button class="btn sm" data-act="power" data-action="start" data-id="${g.id}" title="Starten">${icon("play")}</button>`) : ""}</td>
        </tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">Keine Server vorhanden. <a href="#/create">Ersten Server erstellen</a></div>`}
    </div>`);
  const apply = () => {
    const q = $("#guest-filter").value.trim().toLowerCase();
    $$("tr[data-search]").forEach((tr) => tr.classList.toggle("hidden", q && !tr.dataset.search.includes(q)));
  };
  $("#guest-filter").addEventListener("input", apply);
  apply();
  return { live: !filter };
}

// ------------------------------------------------------------------ Serverdetails

async function viewGuest(params, m, silent, seq) {
  const id = m[1];
  const tab = m[2] || "overview";
  const g = await api(`/api/guests/${id}`);
  if (isStale(seq)) return;
  const admin = S.me.role === "admin";
  const ready = g.status === "ready";
  const running = g.power === "running";
  const base = `#/guest/${id}`;

  S.handlers.power = async (ds) => {
    const labels = { start: "Starten", stop: "Hart stoppen", shutdown: "Herunterfahren", reboot: "Neu starten", suspend: "Pausieren", resume: "Fortsetzen" };
    if (ds.action === "stop" && !(await confirmBox("Server hart stoppen?", "Das entspricht dem Ziehen des Stromsteckers. Ungespeicherte Daten gehen verloren.", { danger: true, ok: "Stoppen" }))) return;
    toast(`${labels[ds.action]} …`);
    await api(`/api/guests/${id}/action`, { method: "POST", body: { action: ds.action } });
    route(true);
  };
  S.handlers.console = () => window.open(`/console?guest=${id}`, `console-${id}`, "width=1100,height=760");
  S.handlers.task = (ds) => watchTask(ds.id, ds.title);

  const actions = ready ? `
    ${running ? "" : `<button class="btn primary" data-act="power" data-action="${g.power === "paused" ? "resume" : "start"}">${icon("play")}Starten</button>`}
    ${running ? `<button class="btn" data-act="power" data-action="shutdown">${icon("power")}Herunterfahren</button>
      <button class="btn" data-act="power" data-action="reboot">${icon("refresh")}Neustart</button>
      <button class="btn danger" data-act="power" data-action="stop">${icon("stop")}Stopp</button>` : ""}
    <button class="btn" data-act="console" ${running ? "" : "disabled"}>${icon("terminal")}Konsole</button>` : "";

  const head = pageHead(
    `<span class="mono muted" style="font-weight:500">${g.vmid}</span> ${esc(g.name)} <span style="vertical-align:3px">${guestBadge(g)}</span>`,
    `${g.type === "kvm" ? "Virtuelle Maschine" : "Container"} · ${esc(g.template || "–")} · Node ${esc(g.node)}${admin ? ` · Besitzer ${esc(g.owner)}` : ""}`,
    actions);
  const tabList = [["overview", "Übersicht"], ["resources", "Ressourcen"], ["network", "Netzwerk"], ["firewall", "Firewall"], ["snapshots", "Snapshots"], ["backups", "Backups"], ["tasks", "Aufgaben"], ["settings", "Einstellungen"]];
  const shell = (content) => setMain(head
    + (g.status === "error" ? `<div class="alert bad" style="margin-bottom:16px"><strong>Fehler:</strong> ${esc(g.error || "unbekannt")}</div>` : "")
    + (g.status === "creating" ? `<div class="alert" style="margin-bottom:16px"><span class="spinner"></span> Der Server wird gerade eingerichtet (Image laden, IP setzen, Zugang konfigurieren). Das dauert beim ersten Mal einige Minuten.</div>` : "")
    + tabs(base, tab, tabList) + content);

  if (tab === "overview") return guestOverview(g, shell, seq);
  if (tab === "resources") return guestResources(g, shell);
  if (tab === "network") return guestNetwork(g, shell);
  if (tab === "firewall") return guestFirewall(g, shell);
  if (tab === "snapshots") return guestSnapshots(g, shell, seq);
  if (tab === "backups") return guestBackups(g, shell, seq);
  if (tab === "tasks") return guestTasks(g, shell, seq);
  return guestSettings(g, shell);
}

async function guestOverview(g, shell, seq) {
  let st = null, stErr = null;
  if (g.status !== "creating") {
    try { st = await api(`/api/guests/${g.id}/status`); } catch (e) { stErr = e.message; }
  }
  if (isStale(seq)) return;
  const hist = (S.hist[g.id] = S.hist[g.id] || []);
  if (st && st.state === "running") {
    hist.push({ t: Date.now() / 1000, cpu: st.cpu || 0, mem: st.memory_used != null ? pct(st.memory_used, st.memory_mb * 1048576) : 0 });
    if (hist.length > 120) hist.shift();
  }
  const ip4 = g.ips.find((i) => i.version === 4);
  const sshHost = ip4 ? ip4.address : g.ips[0] ? g.ips[0].address : null;
  const ssh = sshHost ? `ssh root@${sshHost}` : null;
  shell(`
    <div class="grid grid-2">
      <div class="card"><div class="card-head"><h2>Auslastung</h2>${st && st.state === "running" ? `<span class="muted small">live</span>` : ""}</div><div class="card-body">
        ${stErr ? `<div class="alert warn">${esc(stErr)}</div>` : !st || st.state !== "running" ? `<p class="muted" style="margin:0">Der Server läuft nicht.</p>` : `
          ${meter("CPU", st.cpu, 100, `${(st.cpu || 0).toFixed(1)} % von ${g.cores} ${g.cores === 1 ? "Kern" : "Kernen"}`)}
          ${meter("Arbeitsspeicher", st.memory_used || 0, g.memory_mb * 1048576, st.memory_used != null ? `${fmtBytes(st.memory_used)} / ${fmtMB(g.memory_mb)}` : `– / ${fmtMB(g.memory_mb)}`)}
          ${st.disk_total ? meter("Festplatte", st.disk_used, st.disk_total, `${fmtBytes(st.disk_used)} / ${fmtBytes(st.disk_total)}`) : ""}
          <div class="grid grid-2" style="margin-top:16px">
            <div><div class="muted small" style="margin-bottom:4px">CPU-Verlauf (0–100 %)</div>${sparkline(hist, "cpu")}</div>
            <div><div class="muted small" style="margin-bottom:4px">RAM-Verlauf (0–100 %)</div>${sparkline(hist, "mem")}</div>
          </div>
          <div class="muted small" style="margin-top:10px">Netzwerk gesamt: ↓ ${fmtBytes(st.net_rx)} · ↑ ${fmtBytes(st.net_tx)}</div>`}
      </div></div>
      <div class="card"><div class="card-head"><h2>Details</h2></div><div class="card-body">
        <dl class="kv">
          <dt>VMID</dt><dd class="mono">${g.vmid}</dd>
          <dt>Hostname</dt><dd>${esc(g.hostname)}</dd>
          <dt>IP-Adressen</dt><dd class="mono">${g.ips.map((i) => `${esc(i.address)}/${i.prefix}`).join("<br>") || "DHCP"}</dd>
          <dt>Ressourcen</dt><dd>${g.cores} CPU · ${fmtMB(g.memory_mb)} RAM · ${g.disk_gb} GB</dd>
          <dt>Betriebssystem</dt><dd>${esc(g.template || "–")}</dd>
          <dt>Node</dt><dd>${esc(g.node)}</dd>
          <dt>Festplatte</dt><dd>${g.storage ? `${esc(g.storage)} (gemeinsam)${g.ha ? ` ${badge("HA", "good")}` : ""}` : "lokal auf dem Node"}</dd>
          <dt>Erstellt</dt><dd>${fmtDate(g.created_at)}</dd>
          ${ssh ? `<dt>SSH</dt><dd><code>${esc(ssh)}</code> <button class="btn sm" data-copy="${esc(ssh)}">Kopieren</button></dd>` : ""}
        </dl>
        ${g.notes ? `<div style="margin-top:14px;white-space:pre-wrap" class="small">${esc(g.notes)}</div>` : ""}
      </div></div>
    </div>`);
  return { live: true };
}

function sliderRow(name, label, value, min, max, step, unit) {
  return `<div class="slider-row"><span class="lbl">${label}</span>
    <input type="range" name="${name}-range" min="${min}" max="${max}" step="${step}" value="${value}" aria-label="${label}">
    <span class="out"><input type="number" name="${name}" min="${min}" max="${max}" step="${step}" value="${value}"><span class="muted small">${unit}</span></span></div>`;
}
function bindSliders(root) {
  $$('input[type=range]', root).forEach((r) => {
    const num = $(`input[name="${r.name.replace("-range", "")}"]`, root);
    r.addEventListener("input", () => { num.value = r.value; num.dispatchEvent(new Event("input", { bubbles: true })); });
    num.addEventListener("input", () => { r.value = num.value; });
  });
}

async function guestResources(g, shell) {
  const me = await api("/api/me");
  const q = me.quota, u = me.usage;
  const own = g.owner_id === me.id;
  const admin = me.role === "admin";
  const maxC = admin || !own ? 64 : Math.max(g.cores, q.cores - u.cores + g.cores);
  const maxM = admin || !own ? 262144 : Math.max(g.memory_mb, q.memory_mb - u.memory_mb + g.memory_mb);
  const maxD = admin || !own ? 4096 : Math.max(g.disk_gb, q.disk_gb - u.disk_gb + g.disk_gb);
  shell(`<div class="card" style="max-width:760px"><div class="card-head"><h2>Ressourcen anpassen</h2></div><div class="card-body">
    <form id="res-form">
      ${sliderRow("cores", "CPU-Kerne", g.cores, 1, maxC, 1, "Kerne")}
      ${sliderRow("memory_mb", "Arbeitsspeicher", g.memory_mb, 256, maxM, 256, "MB")}
      ${sliderRow("disk_gb", "Speicher", g.disk_gb, g.disk_gb, Math.max(maxD, g.disk_gb), 1, "GB")}
      <div class="alert" style="margin:6px 0 16px">${g.type === "kvm"
        ? "Bei VMs werden CPU und RAM nach einem vollständigen <strong>Stoppen und Starten</strong> aktiv. Die Festplatte wächst sofort, die Partition beim nächsten Boot."
        : "Bei Containern gelten CPU- und RAM-Limits sofort. Zum Vergrößern der Festplatte wird der Container kurz gestoppt."} Speicher kann nur vergrößert werden.</div>
      <button class="btn primary" type="submit" ${g.status === "ready" ? "" : "disabled"}>Übernehmen</button>
    </form></div></div>`);
  bindSliders($("#res-form"));
  $("#res-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    const r = await api(`/api/guests/${g.id}/resize`, { method: "POST", body: { cores: +f.cores.value, memory_mb: +f.memory_mb.value, disk_gb: +f.disk_gb.value } }).catch(fail);
    if (!r) return;
    if (!r.task_id) return toast("Keine Änderung");
    await watchTask(r.task_id, "Ressourcen ändern");
    route();
  };
}

async function guestNetwork(g, shell) {
  const admin = S.me.role === "admin";
  const pools = await api("/api/pools");
  const usable = (v) => pools.filter((p) => p.version === v && (!p.node_id || p.node_id === g.node_id));
  const cur4 = g.ips.find((i) => i.version === 4);
  const cur6 = g.ips.find((i) => i.version === 6);
  const busy = !["ready", "error"].includes(g.status);
  const poolSelect = (v, cur) => `<select id="net-pool${v}">
      <option value="keep">${cur ? `Beibehalten (${esc(cur.address)})` : "Beibehalten (keine)"}</option>
      ${usable(v).map((p) => `<option value="${p.id}">${esc(p.name)} – ${p.mode === "routed" ? "geroutet" : `Bridge ${esc(p.bridge)}`} – ${p.size - p.used} frei</option>`).join("")}
      <option value="none">${v === 4 ? "Keine feste IPv4 (DHCP)" : "Keine IPv6"}</option></select>`;

  shell(`<div class="grid grid-2">
    <div class="card"><div class="card-head"><h2>Aktuelle Konfiguration</h2></div>
      ${g.ips.length ? `<div class="table-wrap"><table><thead><tr><th>Adresse</th><th>Gateway</th><th>Pool</th><th>Modus</th></tr></thead><tbody>
        ${g.ips.map((i) => `<tr><td class="mono">${esc(i.address)}/${i.prefix}</td><td class="mono">${esc(i.gateway || "–")}</td><td>${esc(i.pool)}</td>
          <td>${i.routed ? badge("Geroutet", "info") : badge("Bridge", "plain")}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">Keine feste IP – der Server bezieht seine Adresse per DHCP.</div>`}
      <div class="card-body" style="border-top:1px solid var(--border)"><dl class="kv">
        <dt>MAC-Adresse</dt><dd class="mono">${esc(g.mac)}</dd><dt>Schnittstelle</dt><dd>eth0 (virtio)</dd></dl></div></div>

    <div class="card"><div class="card-head"><h2>Netzwerk ändern</h2></div><div class="card-body">
      <label class="field"><span>IPv4-Adresse</span>${poolSelect(4, cur4)}</label>
      <div class="field hidden" id="net-addr4-wrap"><span>Bestimmte Adresse (optional)</span>
        <select id="net-addr4"><option value="">Nächste freie automatisch</option></select></div>
      <label class="field"><span>IPv6-Adresse</span>${poolSelect(6, cur6)}</label>
      <div class="field hidden" id="net-addr6-wrap"><span>Bestimmte Adresse (optional)</span>
        <select id="net-addr6"><option value="">Nächste freie automatisch</option></select></div>
      ${admin ? `<label class="field"><span>MAC-Adresse</span><input type="text" id="net-mac" class="mono" value="${esc(g.mac)}" maxlength="17">
        <div class="hint">Nur ändern, wenn der Hoster eine IP an eine bestimmte MAC bindet (z. B. <code>bc:24:11:11:dc:25</code> nach einem Umzug von Proxmox).
        Hat die gewählte IP im Pool eine feste MAC, wird diese automatisch übernommen.</div></label>` : ""}
      <div class="alert" style="margin-bottom:14px">Der Server wird für die Änderung kurz neu gestartet. ${g.type === "kvm"
        ? "cloud-init richtet das Netzwerk in der VM automatisch neu ein – Passwort, SSH-Schlüssel und Daten bleiben erhalten."
        : "Das Netzwerk im Container wird automatisch neu geschrieben – Daten bleiben erhalten."}</div>
      <button class="btn primary" data-act="netApply" ${busy ? "disabled" : ""}>Übernehmen</button>
    </div></div></div>
    ${admin && g.ips.length ? `<div class="card" style="margin-top:16px;max-width:760px"><div class="card-head"><h2>Domain zuweisen (Cloudflare)</h2></div><div class="card-body">
      <p class="muted" style="margin-top:0">Legt in Cloudflare einen ${g.ips.some((i) => i.version === 6) ? "A- und AAAA-Eintrag" : "A-Eintrag"} auf ${g.ips.map((i) => `<code>${esc(i.address)}</code>`).join(", ")} an bzw. aktualisiert ihn.</p>
      <div class="row"><input type="text" id="dns-host" placeholder="web.deinedomain.de" value="${esc(g.hostname.includes(".") ? g.hostname : "")}">
        <button class="btn" style="flex:none" data-act="guestDns">Eintragen</button></div>
      <label class="check" style="margin-top:10px"><input type="checkbox" id="dns-proxied"><span>Über Cloudflare-Proxy (nur für Webseiten; SSH geht dann nicht über den Namen)</span></label>
    </div></div>` : ""}`);

  const loadFree = async (v) => {
    const sel = $(`#net-pool${v}`).value;
    const wrap = $(`#net-addr${v}-wrap`);
    const target = $(`#net-addr${v}`);
    if (!/^\d+$/.test(sel)) { wrap.classList.add("hidden"); target.value = ""; return; }
    wrap.classList.remove("hidden");
    target.innerHTML = `<option value="">Lade …</option>`;
    const free = await api(`/api/pools/${sel}/free?limit=512`).catch(() => []);
    target.innerHTML = `<option value="">Nächste freie automatisch${free[0] ? ` (${esc(free[0].address)})` : ""}</option>`
      + free.map((f) => `<option value="${esc(f.address)}" data-mac="${esc(f.mac || "")}">${esc(f.address)}${f.mac ? ` – feste MAC ${esc(f.mac)}` : ""}</option>`).join("");
  };
  [4, 6].forEach((v) => $(`#net-pool${v}`).addEventListener("change", () => loadFree(v).catch(fail)));
  $("#net-addr4").addEventListener("change", (e) => {
    const mac = e.target.selectedOptions[0] && e.target.selectedOptions[0].dataset.mac;
    if (mac && $("#net-mac")) $("#net-mac").value = mac;
  });

  S.handlers.guestDns = async () => {
    const hostname = $("#dns-host").value.trim();
    if (!hostname) return toast("Bitte einen Hostnamen eingeben");
    const r = await api(`/api/admin/guests/${g.id}/dns`, { method: "POST", body: { hostname, proxied: $("#dns-proxied").checked } });
    toast(`DNS gesetzt: ${r.map((x) => `${x.type} ${x.name} → ${x.content}`).join(", ")}`);
  };
  S.handlers.netApply = async () => {
    const num = (v) => (/^\d+$/.test(v) ? +v : v);
    const body = {
      ipv4_pool: num($("#net-pool4").value), ipv4_address: $("#net-addr4").value || null,
      ipv6_pool: num($("#net-pool6").value), ipv6_address: $("#net-addr6").value || null,
    };
    if ($("#net-mac") && $("#net-mac").value.trim().toLowerCase() !== g.mac) body.mac = $("#net-mac").value.trim();
    if (body.ipv4_pool === "keep" && body.ipv6_pool === "keep" && !body.mac) return toast("Keine Änderung ausgewählt");
    if (!(await confirmBox("Netzwerk ändern?", `<strong>${esc(g.name)}</strong> wird dafür kurz neu gestartet.`, { ok: "Übernehmen" }))) return;
    const r = await api(`/api/guests/${g.id}/network`, { method: "PUT", body });
    if (!r.task_id) return toast("Keine Änderung nötig");
    await watchTask(r.task_id, "Netzwerk ändern");
    route();
  };
}

async function guestSnapshots(g, shell, seq) {
  const snaps = g.status === "creating" ? [] : await api(`/api/guests/${g.id}/snapshots`).catch((e) => { toast(e.message, "bad"); return []; });
  if (isStale(seq)) return;
  S.handlers.snapCreate = () => formModal({
    title: "Snapshot erstellen",
    fields: `<label class="field"><span>Name</span><input type="text" name="name" value="snap-${new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "")}" pattern="[A-Za-z0-9_-]+" required><div class="hint">Nur Buchstaben, Ziffern, _ und -</div></label>
      <label class="field"><span>Beschreibung (optional)</span><input type="text" name="description" maxlength="200"></label>
      ${g.type === "lxc" ? `<div class="alert warn">Der Container wird für den Snapshot kurz gestoppt und danach wieder gestartet.</div>` : `<div class="alert">Bei laufenden VMs wird auch der Arbeitsspeicher gesichert.</div>`}`,
    submit: "Erstellen",
    onSubmit: async (d, mm) => {
      const r = await api(`/api/guests/${g.id}/snapshots`, { method: "POST", body: d });
      mm.close();
      await watchTask(r.task_id, "Snapshot erstellen");
      route();
    },
  });
  S.handlers.snapRollback = async (ds) => {
    if (!(await confirmBox("Snapshot zurückspielen?", `Der Server wird auf den Stand von <strong>${esc(ds.name)}</strong> zurückgesetzt. Alle Änderungen seitdem gehen verloren.`, { danger: true, ok: "Zurückspielen" }))) return;
    const r = await api(`/api/guests/${g.id}/snapshots/${ds.name}/rollback`, { method: "POST" });
    await watchTask(r.task_id, "Snapshot zurückspielen");
    route();
  };
  S.handlers.snapDelete = async (ds) => {
    if (!(await confirmBox("Snapshot löschen?", `Snapshot <strong>${esc(ds.name)}</strong> wird endgültig gelöscht.`, { danger: true, ok: "Löschen" }))) return;
    const r = await api(`/api/guests/${g.id}/snapshots/${ds.name}`, { method: "DELETE" });
    await watchTask(r.task_id, "Snapshot löschen");
    route();
  };
  shell(`<div class="card"><div class="card-head"><h2>Snapshots</h2><button class="btn primary sm" data-act="snapCreate" ${g.status === "ready" ? "" : "disabled"}>${icon("plus")}Snapshot erstellen</button></div>
    ${snaps.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Erstellt</th><th>Beschreibung</th><th></th></tr></thead><tbody>
      ${snaps.map((s) => `<tr><td class="mono">${esc(s.name)}</td><td>${esc(s.created)}</td><td>${esc(s.description || "")}</td>
        <td class="right nowrap"><button class="btn sm" data-act="snapRollback" data-name="${esc(s.name)}">Zurückspielen</button>
        <button class="btn sm danger" data-act="snapDelete" data-name="${esc(s.name)}">Löschen</button></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">Noch keine Snapshots. Ein Snapshot sichert den aktuellen Zustand, damit du ihn später schnell wiederherstellen kannst.</div>`}</div>`);
}

const WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"];

function scheduleCard(g, s) {
  const pad = (n) => String(n).padStart(2, "0");
  const status = s.last_status === "ok" ? badge("Erfolgreich", "good") : s.last_status === "error" ? badge("Fehlgeschlagen", "bad")
    : s.last_status === "running" ? badge("Läuft", "info") : "";
  return `<div class="card" style="margin-bottom:16px"><div class="card-head"><h2>Zeitgesteuerte Backups</h2>${s.enabled ? badge("Aktiv", "good") : badge("Aus")}</div>
    <div class="card-body"><form id="sched-form">
      <label class="check"><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}><span>Automatisch sichern</span></label>
      <div class="row">
        <label class="field"><span>Häufigkeit</span><select name="frequency"><option value="daily" ${s.frequency === "daily" ? "selected" : ""}>Täglich</option><option value="weekly" ${s.frequency === "weekly" ? "selected" : ""}>Wöchentlich</option></select></label>
        <label class="field" id="sched-weekday"><span>Wochentag</span><select name="weekday">${WEEKDAYS.map((d, i) => `<option value="${i}" ${i === s.weekday ? "selected" : ""}>${d}</option>`).join("")}</select></label>
        <label class="field"><span>Uhrzeit (Serverzeit)</span><input type="time" name="time" value="${pad(s.hour)}:${pad(s.minute)}" required></label>
        <label class="field"><span>Aufbewahren (Anzahl)</span><input type="number" name="keep" value="${Math.min(s.keep, s.max_keep)}" min="1" max="${s.max_keep}"></label>
      </div>
      ${(s.targets || []).length ? `<div class="row"><label class="field"><span>Speicherort</span><select name="target_id"><option value="">Lokal auf dem Node</option>
        ${s.targets.map((t) => `<option value="${t.id}" ${t.id === s.target_id ? "selected" : ""}>${esc(t.name)} (${esc(t.type.toUpperCase())})</option>`).join("")}</select></label>
        <label class="check" style="align-self:end"><input type="checkbox" name="keep_local" ${s.keep_local ? "checked" : ""}><span>Zusätzlich lokal behalten</span></label></div>` : ""}
      <p class="muted small" style="margin-top:0">Sind mehr automatische Backups vorhanden als eingestellt, werden die ältesten gelöscht. Manuelle Backups bleiben unberührt.
        ${g.type === "lxc" ? "Container werden für das Backup kurz gestoppt." : "VMs laufen während des Backups weiter."}</p>
      ${s.exists ? `<p class="small" style="margin-top:0">Nächstes Backup: <strong>${s.next_run ? fmtDate(s.next_run) : "–"}</strong> · Letzter Lauf: ${s.last_status ? fmtDate(s.last_run) + " " + status : "noch keiner"}</p>` : ""}
      <button class="btn primary" type="submit">Zeitplan speichern</button>
    </form></div></div>`;
}

function bindSchedule(g) {
  const sf = $("#sched-form");
  if (!sf) return;
  const syncWeekday = () => $("#sched-weekday").classList.toggle("hidden", sf.frequency.value !== "weekly");
  sf.frequency.addEventListener("change", syncWeekday);
  syncWeekday();
  sf.onsubmit = async (e) => {
    e.preventDefault();
    const [hour, minute] = sf.time.value.split(":").map(Number);
    await api(`/api/guests/${g.id}/backup-schedule`, { method: "PUT", body: {
      enabled: sf.enabled.checked, frequency: sf.frequency.value, weekday: +sf.weekday.value, hour, minute, keep: +sf.keep.value,
      target_id: sf.target_id && sf.target_id.value ? +sf.target_id.value : null, keep_local: !!(sf.keep_local && sf.keep_local.checked),
    } }).then(() => { toast("Zeitplan gespeichert"); route(); }).catch(fail);
  };
}

async function guestBackups(g, shell, seq) {
  const [data, sched] = await Promise.all([
    g.status === "creating" ? { items: [], targets: [] } : api(`/api/guests/${g.id}/backups`).catch((e) => { toast(e.message, "bad"); return { items: [], targets: [] }; }),
    api(`/api/guests/${g.id}/backup-schedule`),
  ]);
  if (isStale(seq)) return;
  const backups = data.items || [];
  const targets = sched.targets || [];
  const q = (b) => (b.location ? `?target=${b.location}` : "");
  const find = (ds) => backups.find((b) => b.file === ds.file && String(b.location ?? "") === (ds.loc || ""));
  S.handlers.backupCreate = () => formModal({
    title: "Backup erstellen",
    fields: `<label class="field"><span>Speicherort</span><select name="target_id"><option value="">Lokal auf dem Node (${esc(g.node)})</option>
        ${targets.map((t) => `<option value="${t.id}">${esc(t.name)} (${esc(t.type.toUpperCase())})</option>`).join("")}</select></label>
      ${targets.length ? `<label class="check"><input type="checkbox" name="keep_local"><span>Bei externem Speicher zusätzlich lokal behalten</span></label>` : ""}
      <p class="muted small" style="margin:0">${g.type === "lxc" ? "Der Container wird für ein konsistentes Backup kurz gestoppt." : "Die VM läuft während des Backups weiter."}</p>`,
    submit: "Backup starten",
    onSubmit: async (d, mm) => {
      const r = await api(`/api/guests/${g.id}/backups`, { method: "POST", body: { target_id: d.target_id ? +d.target_id : null, keep_local: !!d.keep_local } });
      mm.close();
      await watchTask(r.task_id, "Backup erstellen");
      route();
    },
  });
  S.handlers.backupRestore = async (ds) => {
    const b = find(ds);
    if (!(await confirmBox("Backup wiederherstellen?", `Der aktuelle Inhalt des Servers wird durch das Backup <strong>${esc(b.file)}</strong> (${esc(b.location_name)}) ersetzt.${b.location ? " Es wird dafür zuerst auf den Node geladen." : ""}`, { danger: true, ok: "Wiederherstellen", requireText: g.vmid }))) return;
    const r = await api(`/api/guests/${g.id}/backups/${b.file}/restore${q(b)}`, { method: "POST" });
    await watchTask(r.task_id, "Backup wiederherstellen");
    route();
  };
  S.handlers.backupDelete = async (ds) => {
    const b = find(ds);
    if (!(await confirmBox("Backup löschen?", `<strong>${esc(b.file)}</strong> (${esc(b.location_name)}) wird endgültig gelöscht.`, { danger: true, ok: "Löschen" }))) return;
    await api(`/api/guests/${g.id}/backups/${b.file}${q(b)}`, { method: "DELETE" });
    toast("Backup gelöscht");
    route();
  };
  const errors = (data.targets || []).filter((t) => t.error);
  shell(scheduleCard(g, sched) + `<div class="card"><div class="card-head"><h2>Backups</h2><button class="btn primary sm" data-act="backupCreate" ${g.status === "ready" ? "" : "disabled"}>${icon("plus")}Backup jetzt</button></div>
    ${errors.map((t) => `<div class="alert warn" style="margin:12px 16px 0">Speicher <strong>${esc(t.name)}</strong> nicht erreichbar: ${esc(t.error)}</div>`).join("")}
    ${backups.length ? `<div class="table-wrap"><table><thead><tr><th>Datei</th><th>Speicherort</th><th>Erstellt</th><th>Größe</th><th></th></tr></thead><tbody>
      ${backups.map((b) => `<tr><td class="mono small">${esc(b.file)} ${b.auto ? badge("Automatisch", "plain") : ""}</td>
        <td>${b.location ? badge(b.location_name, "info") : `<span class="small">${esc(b.location_name)}</span>`}</td>
        <td class="nowrap">${fmtDate(new Date(b.created * 1000).toISOString())}</td><td>${fmtBytes(b.size)}</td>
        <td class="right nowrap"><button class="btn sm" data-act="backupRestore" data-file="${esc(b.file)}" data-loc="${b.location ?? ""}">Wiederherstellen</button>
        <button class="btn sm danger" data-act="backupDelete" data-file="${esc(b.file)}" data-loc="${b.location ?? ""}">Löschen</button></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">Noch keine Backups vorhanden.</div>`}</div>`);
  bindSchedule(g);
}

// ------------------------------------------------------------------ Backup-Speicher (Einstellungen)

const TARGET_FIELDS = {
  sftp: [["host", "Server", "storage.example.de"], ["port", "Port", "22"], ["user", "Benutzer", "u12345"], ["password", "Passwort", "", "secret"],
    ["private_key", "Privater SSH-Schlüssel (statt Passwort)", "-----BEGIN OPENSSH PRIVATE KEY-----", "secret-area"], ["path", "Ordner", "backups/pombot"]],
  s3: [["provider", "Anbieter", "", "select:Other=S3-kompatibel (MinIO, R2, B2, Wasabi …)|AWS=Amazon S3|Cloudflare=Cloudflare R2|Wasabi=Wasabi|Minio=MinIO"],
    ["endpoint", "Endpoint (bei AWS leer lassen)", "https://s3.eu-central-003.backblazeb2.com"], ["region", "Region", "eu-central-1"],
    ["bucket", "Bucket", "mein-backup-bucket"], ["access_key", "Access Key", ""], ["secret_key", "Secret Key", "", "secret"], ["path", "Ordner im Bucket", "pombot"]],
  nfs: [["server", "Server", "nas.local"], ["export", "Export", "/srv/backups"], ["options", "Mount-Optionen (optional)", "vers=4,soft"], ["path", "Unterordner", "pombot"]],
  smb: [["share", "Freigabe", "//nas.local/backups"], ["user", "Benutzer", ""], ["password", "Passwort", "", "secret"], ["domain", "Domäne (optional)", ""],
    ["version", "SMB-Version", "3.0"], ["path", "Unterordner", "pombot"]],
};

function targetDialog(t) {
  const st = { type: t ? t.type : "sftp" };
  const cfg = t ? t.config : {};
  const fieldsHtml = (type) => TARGET_FIELDS[type].map(([key, label, ph, kind]) => {
    if (kind && kind.startsWith("select:")) {
      const opts = kind.slice(7).split("|").map((o) => o.split("="));
      return `<label class="field"><span>${label}</span><select name="cfg_${key}">${opts.map(([v, l]) => `<option value="${v}" ${cfg[key] === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>`;
    }
    const secretSet = cfg[`${key}_set`];
    const hint = kind && kind.startsWith("secret") && secretSet ? "gesetzt – leer lassen, um es zu behalten" : ph;
    if (kind === "secret-area") return `<label class="field"><span>${label}</span><textarea name="cfg_${key}" rows="3" placeholder="${esc(hint)}"></textarea></label>`;
    return `<label class="field"><span>${label}</span><input type="${kind === "secret" ? "password" : "text"}" name="cfg_${key}" value="${kind ? "" : esc(cfg[key] ?? "")}" placeholder="${esc(hint)}" autocomplete="off"></label>`;
  }).join("");
  formModal({
    title: t ? `Backup-Speicher ${t.name}` : "Backup-Speicher hinzufügen", wide: true,
    fields: `<div class="row"><label class="field"><span>Name</span><input type="text" name="name" value="${esc(t ? t.name : "")}" placeholder="z. B. Storage Box" required></label>
        <label class="field"><span>Typ</span><select name="type" id="tgt-type">${["sftp", "s3", "nfs", "smb"].map((x) => `<option value="${x}" ${x === st.type ? "selected" : ""}>${{ sftp: "SFTP / SSH (z. B. Hetzner Storage Box)", s3: "S3-kompatibel", nfs: "NFS", smb: "SMB / CIFS (Windows-Freigabe, NAS)" }[x]}</option>`).join("")}</select></label></div>
      <div id="tgt-fields">${fieldsHtml(st.type)}</div>
      <label class="check"><input type="checkbox" name="user_visible" ${t && t.user_visible ? "checked" : ""}><span>Auch Benutzer dürfen hierhin sichern (sonst nur Admins)</span></label>
      <p class="muted small" style="margin:0">Die Nodes verbinden sich direkt mit dem Speicher. Pro Panel und Server wird ein eigener Unterordner angelegt (<code>pombot-…/pvXXX</code>).</p>`,
    onReady: (mm) => {
      $("#tgt-type", mm).onchange = (e) => { $("#tgt-fields", mm).innerHTML = fieldsHtml(e.target.value); };
    },
    onSubmit: async (d, mm) => {
      const body = { name: d.name, type: d.type, user_visible: d.user_visible, config: {} };
      Object.entries(d).forEach(([k, v]) => { if (k.startsWith("cfg_")) body.config[k.slice(4)] = v; });
      const saved = await api(t ? `/api/admin/backup-targets/${t.id}` : "/api/admin/backup-targets", { method: t ? "PUT" : "POST", body });
      mm.close();
      toast("Gespeichert – teste jetzt die Verbindung");
      route();
      return saved;
    },
  });
}

async function settingsBackupTargets(head) {
  const list = await api("/api/admin/backup-targets");
  S.handlers.tgtAdd = () => targetDialog(null);
  S.handlers.tgtEdit = (ds) => targetDialog(list.find((t) => t.id === +ds.id));
  S.handlers.tgtDelete = async (ds) => {
    if (!(await confirmBox("Backup-Speicher entfernen?", "Die Backups auf dem Speicher selbst bleiben erhalten. Zeitpläne, die dorthin sichern, sichern danach wieder lokal.", { danger: true, ok: "Entfernen" }))) return;
    await api(`/api/admin/backup-targets/${ds.id}`, { method: "DELETE" });
    route();
  };
  S.handlers.tgtTest = async (ds, el) => {
    el.disabled = true;
    el.innerHTML = `<span class="spinner"></span> Teste …`;
    const res = await api(`/api/admin/backup-targets/${ds.id}/test`, { method: "POST" }).catch((e) => [{ node: "–", ok: false, error: e.message }]);
    modal({ title: "Verbindungstest", body: res.map((r) => `<div class="alert ${r.ok ? "" : "bad"}" style="margin-bottom:8px"><strong>${esc(r.node)}:</strong> ${r.ok ? `OK – schreiben, lesen und löschen funktioniert${r.free ? ` · ${fmtBytes(r.free)} frei` : ""}` : esc(r.error)}</div>`).join(""),
      foot: `<button class="btn" data-close>Schließen</button>` });
    route();
  };
  setMain(head + `<div class="card"><div class="card-head"><h2>Externe Backup-Speicher</h2><button class="btn primary sm" data-act="tgtAdd">${icon("plus")}Speicher hinzufügen</button></div>
    ${list.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Typ</th><th>Ziel</th><th>Benutzer</th><th></th></tr></thead><tbody>
      ${list.map((t) => `<tr><td><strong>${esc(t.name)}</strong></td><td><span class="type-tag">${esc(t.type_label)}</span></td><td class="mono small">${esc(t.where)}</td>
        <td>${t.user_visible ? badge("freigegeben", "info") : `<span class="muted small">nur Admins</span>`}</td>
        <td class="right nowrap"><button class="btn sm" data-act="tgtTest" data-id="${t.id}">Verbindung testen</button> <button class="btn sm" data-act="tgtEdit" data-id="${t.id}">Bearbeiten</button>
          <button class="btn sm danger" data-act="tgtDelete" data-id="${t.id}">Entfernen</button></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">Noch kein externer Speicher. Backups liegen bisher nur lokal auf den Nodes – fällt ein Node aus, sind sie weg.<br><br>
      Unterstützt: SFTP (z. B. Hetzner Storage Box), S3-kompatibel (AWS, Backblaze B2, Cloudflare R2, Wasabi, MinIO), NFS und SMB.</div>`}</div>`);
}

// ------------------------------------------------------------------ Gemeinsamer Speicher (Einstellungen)

const STORAGE_FIELDS = {
  nfs: [["server", "Server", "nas.local"], ["export", "Export", "/srv/pombot"], ["options", "Mount-Optionen (optional)", "vers=4,hard,timeo=600,retrans=5"]],
  smb: [["share", "Freigabe", "//nas.local/pombot"], ["user", "Benutzer", ""], ["password", "Passwort", "", "secret"], ["domain", "Domäne (optional)", ""], ["version", "SMB-Version", "3.0"]],
  path: [["path", "Pfad (auf allen Nodes gleich eingehängt)", "/mnt/cephfs/pombot"]],
};

function storageDialog(t) {
  const cfg = t ? t.config : {};
  const fieldsHtml = (type) => STORAGE_FIELDS[type].map(([key, label, ph, kind]) => {
    const hint = kind === "secret" && cfg[`${key}_set`] ? "gesetzt – leer lassen, um es zu behalten" : ph;
    return `<label class="field"><span>${label}</span><input type="${kind === "secret" ? "password" : "text"}" name="cfg_${key}" value="${kind ? "" : esc(cfg[key] ?? "")}" placeholder="${esc(hint)}" autocomplete="off"></label>`;
  }).join("");
  const type = t ? t.type : "nfs";
  formModal({
    title: t ? `Speicher ${t.name}` : "Gemeinsamen Speicher hinzufügen", wide: true,
    fields: `<div class="row"><label class="field"><span>Name</span><input type="text" name="name" value="${esc(t ? t.name : "")}" placeholder="z. B. NAS" required></label>
        <label class="field"><span>Typ</span><select name="type" id="sto-type">${["nfs", "smb", "path"].map((x) => `<option value="${x}" ${x === type ? "selected" : ""}>${{ nfs: "NFS (empfohlen)", smb: "SMB / CIFS", path: "Vorhandener Mount (CephFS, GlusterFS …)" }[x]}</option>`).join("")}</select></label></div>
      <div id="sto-fields">${fieldsHtml(type)}</div>
      <label class="check"><input type="checkbox" name="user_visible" ${t && t.user_visible ? "checked" : ""}><span>Auch Benutzer dürfen Server hier anlegen (sonst nur Admins)</span></label>
      <p class="muted small" style="margin:0">Der Speicher wird auf allen Nodes unter <code>/mnt/pombot-storage/ID</code> eingehängt. Bei „Vorhandener Mount“ muss der Pfad auf jedem Node bereits eingehängt sein (z. B. CephFS über <code>/etc/fstab</code>).
        Für HA sollte der Speicher selbst ausfallsicher sein – ist er nicht erreichbar, stoppen die HA-Server darauf.</p>`,
    onReady: (mm) => { $("#sto-type", mm).onchange = (e) => { $("#sto-fields", mm).innerHTML = fieldsHtml(e.target.value); }; },
    onSubmit: async (d, mm) => {
      const body = { name: d.name, type: d.type, user_visible: d.user_visible, config: {} };
      Object.entries(d).forEach(([k, v]) => { if (k.startsWith("cfg_")) body.config[k.slice(4)] = v; });
      const saved = await api(t ? `/api/admin/storages/${t.id}` : "/api/admin/storages", { method: t ? "PUT" : "POST", body });
      mm.close();
      if (saved.sync_errors && saved.sync_errors.length) toast(`Gespeichert, aber: ${saved.sync_errors.join("; ")}`, "bad");
      else toast("Gespeichert und auf den Nodes eingehängt");
      route();
      return saved;
    },
  });
}

async function settingsStorages(head) {
  const list = await api("/api/admin/storages");
  S.handlers.stoAdd = () => storageDialog(null);
  S.handlers.stoEdit = (ds) => storageDialog(list.find((t) => t.id === +ds.id));
  S.handlers.stoDelete = async (ds) => {
    if (!(await confirmBox("Speicher entfernen?", "Er wird im Panel entfernt. Die Daten auf dem Speicher bleiben erhalten.", { danger: true, ok: "Entfernen" }))) return;
    await api(`/api/admin/storages/${ds.id}`, { method: "DELETE" });
    route();
  };
  S.handlers.stoSync = async (ds, el) => {
    el.disabled = true;
    el.innerHTML = `<span class="spinner"></span> Hänge ein …`;
    const r = await api("/api/admin/storages/sync", { method: "POST" }).catch((e) => ({ errors: [e.message] }));
    if (r.errors.length) toast(r.errors.join("; "), "bad"); else toast("Auf allen Nodes abgeglichen");
    route();
  };
  const nodeState = (n) => !n.online ? `<span class="muted small">${esc(n.node)}: offline</span>`
    : n.mounted ? `<span>${badge(esc(n.node), "good")} ${n.free != null ? `<span class="small muted">${fmtBytes(n.free)} frei</span>` : ""}</span>`
      : `<span>${badge(esc(n.node), "bad")} <span class="small muted">${esc(n.error || "nicht eingehängt")}</span></span>`;
  setMain(head + `<div class="card"><div class="card-head"><h2>Gemeinsamer Speicher</h2><div class="row" style="flex:none;gap:8px">
      ${list.length ? `<button class="btn sm" data-act="stoSync">Neu einhängen</button>` : ""}<button class="btn primary sm" data-act="stoAdd">${icon("plus")}Speicher hinzufügen</button></div></div>
    ${list.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Typ</th><th>Ziel</th><th>Nodes</th><th>Server</th><th></th></tr></thead><tbody>
      ${list.map((t) => `<tr><td><strong>${esc(t.name)}</strong>${t.user_visible ? ` ${badge("für Benutzer", "info")}` : ""}</td><td><span class="type-tag">${esc(t.type_label)}</span></td><td class="mono small">${esc(t.where)}</td>
        <td><div class="stack" style="gap:4px">${t.nodes.map(nodeState).join("") || `<span class="muted small">keine Nodes</span>`}</div></td><td>${t.guests}</td>
        <td class="right nowrap"><button class="btn sm" data-act="stoEdit" data-id="${t.id}">Bearbeiten</button>
          <button class="btn sm danger" data-act="stoDelete" data-id="${t.id}" ${t.guests ? "disabled" : ""}>Entfernen</button></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">Noch kein gemeinsamer Speicher. Server liegen bisher lokal auf ihrem Node.<br><br>
      Mit einem Speicher, den alle Nodes erreichen (NFS, SMB oder ein vorhandener CephFS-Mount), können Server ohne Kopieren umziehen und bei Ausfall eines Nodes automatisch woanders starten (HA).</div>`}</div>`);
}

// ------------------------------------------------------------------ Firewall

const FW_PRESETS = {
  ssh: { label: "SSH (22)", rule: { direction: "in", action: "accept", protocol: "tcp", port: "22", comment: "SSH" } },
  web: { label: "Web (80, 443)", rule: { direction: "in", action: "accept", protocol: "tcp", port: "80,443", comment: "Webserver" } },
  ping: { label: "Ping", rule: { direction: "in", action: "accept", protocol: "icmp", port: "", comment: "Ping" } },
  dns: { label: "DNS (53)", rule: { direction: "in", action: "accept", protocol: "udp", port: "53", comment: "DNS" } },
  mail: { label: "Mail", rule: { direction: "in", action: "accept", protocol: "tcp", port: "25,465,587,993", comment: "Mail" } },
};

function allowsSSH(st) {
  const covers22 = (port) => !port || port.split(",").some((p) => {
    const [a, b] = p.split("-").map(Number);
    return b ? a <= 22 && b >= 22 : a === 22;
  });
  return st.rules.some((r) => r.enabled && r.direction === "in" && r.action === "accept" && !r.source
    && (r.protocol === "any" || (r.protocol === "tcp" && covers22(r.port))));
}

async function guestFirewall(g, shell) {
  const fw = await api(`/api/guests/${g.id}/firewall`);
  const admin = S.me.role === "admin";
  const st = { ...fw, rules: fw.rules.map((r) => ({ source: "", comment: "", enabled: true, ...r })) };
  const sel = (key, value, opts, i) => `<select data-rule="${i}" data-key="${key}" aria-label="${key}">${opts.map(([v, l]) => `<option value="${v}" ${v === value ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  const collect = () => {
    st.enabled = $("#fw-enabled").checked;
    st.policy_in = $("#fw-in").value;
    st.policy_out = $("#fw-out").value;
    if ($("#fw-antispoof")) st.antispoof = $("#fw-antispoof").checked;
    $$("#main input[data-rule][type=text]").forEach((el) => { st.rules[+el.dataset.rule][el.dataset.key] = el.value; });
  };
  const draw = () => {
    shell(`<div class="card"><div class="card-head"><h2>Firewall</h2>${fw.enabled ? badge("Aktiv", "good") : badge("Aus")}</div><div class="card-body">
      <label class="check"><input type="checkbox" id="fw-enabled" ${st.enabled ? "checked" : ""}><span><strong>Firewall aktivieren</strong><br><span class="muted small">Gefiltert wird direkt auf dem Node, bevor der Datenverkehr den Server erreicht. Antworten auf bestehende Verbindungen sind immer erlaubt.</span></span></label>
      <div class="row" style="max-width:640px">
        <label class="field"><span>Eingehend, wenn keine Regel passt</span><select id="fw-in"><option value="drop" ${st.policy_in === "drop" ? "selected" : ""}>Blockieren (empfohlen)</option><option value="accept" ${st.policy_in === "accept" ? "selected" : ""}>Erlauben</option></select></label>
        <label class="field"><span>Ausgehend, wenn keine Regel passt</span><select id="fw-out"><option value="accept" ${st.policy_out === "accept" ? "selected" : ""}>Erlauben (empfohlen)</option><option value="drop" ${st.policy_out === "drop" ? "selected" : ""}>Blockieren</option></select></label>
      </div>
      ${admin ? `<label class="check"><input type="checkbox" id="fw-antispoof" ${st.antispoof ? "checked" : ""}><span>Spoofing-Schutz: Der Server darf nur mit seinen eigenen IP-Adressen senden (gilt auch bei ausgeschalteter Firewall)</span></label>`
        : `<p class="muted small" style="margin:0">Spoofing-Schutz: ${st.antispoof ? "aktiv" : "aus"} (verwaltet der Administrator).</p>`}
      ${st.enabled && st.policy_in === "drop" && !allowsSSH(st)
        ? `<div class="alert warn" style="margin-top:12px">Eingehend wird alles blockiert, und keine Regel erlaubt SSH (Port 22) von überall. Du erreichst den Server dann nur noch über die Konsole im Panel.</div>` : ""}
    </div>
    <div class="card-head" style="border-top:1px solid var(--border);flex-wrap:wrap"><h2>Regeln</h2><span class="muted small">Schnell hinzufügen:</span>
      ${Object.entries(FW_PRESETS).map(([k, p]) => `<button class="btn sm" data-act="fwPreset" data-key="${k}">${p.label}</button>`).join("")}
      <button class="btn sm primary" data-act="fwAdd">${icon("plus")}Regel</button></div>
    ${st.rules.length ? `<div class="table-wrap"><table><thead><tr><th>Aktiv</th><th>Richtung</th><th>Aktion</th><th>Protokoll</th><th>Port(s)</th><th>Quelle / Ziel</th><th>Kommentar</th><th></th></tr></thead><tbody>
      ${st.rules.map((r, i) => `<tr>
        <td><input type="checkbox" data-rule="${i}" data-key="enabled" ${r.enabled ? "checked" : ""} aria-label="Regel aktiv"></td>
        <td>${sel("direction", r.direction, [["in", "Eingehend"], ["out", "Ausgehend"]], i)}</td>
        <td>${sel("action", r.action, [["accept", "Erlauben"], ["drop", "Blockieren"]], i)}</td>
        <td>${sel("protocol", r.protocol, [["tcp", "TCP"], ["udp", "UDP"], ["icmp", "ICMP/Ping"], ["any", "Alle"]], i)}</td>
        <td><input type="text" data-rule="${i}" data-key="port" value="${esc(r.port)}" placeholder="${["tcp", "udp"].includes(r.protocol) ? "alle" : "–"}" ${["tcp", "udp"].includes(r.protocol) ? "" : "disabled"} style="min-width:110px"></td>
        <td><input type="text" data-rule="${i}" data-key="source" value="${esc(r.source)}" placeholder="überall" style="min-width:140px"></td>
        <td><input type="text" data-rule="${i}" data-key="comment" value="${esc(r.comment)}" maxlength="100" style="min-width:120px"></td>
        <td class="right nowrap"><button class="btn sm" data-act="fwUp" data-i="${i}" title="Nach oben" aria-label="Nach oben" ${i ? "" : "disabled"}>↑</button>
          <button class="btn sm danger" data-act="fwDel" data-i="${i}">Entfernen</button></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">Noch keine Regeln. Regeln werden von oben nach unten geprüft, die erste passende gilt.</div>`}
    <div class="card-body" style="border-top:1px solid var(--border);display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="btn primary" data-act="fwSave">Übernehmen</button>
      <span class="muted small">Ports: <code>22</code>, <code>80,443</code> oder <code>1000-2000</code>. Quelle/Ziel: IP oder Netz, z. B. <code>203.0.113.7</code> oder <code>10.0.0.0/8</code>. Änderungen gelten erst nach „Übernehmen“.</span>
    </div></div>`);
    $$("#main [data-rule]").forEach((el) => el.addEventListener("change", () => {
      const r = st.rules[+el.dataset.rule];
      r[el.dataset.key] = el.type === "checkbox" ? el.checked : el.value;
      if (el.dataset.key === "protocol" && !["tcp", "udp"].includes(r.protocol)) r.port = "";
      if (el.tagName === "SELECT") { collect(); draw(); }
    }));
    ["fw-enabled", "fw-in", "fw-out"].forEach((id) => $("#" + id).addEventListener("change", () => { collect(); draw(); }));
  };
  S.handlers.fwPreset = (ds) => { collect(); st.rules.push({ source: "", enabled: true, ...FW_PRESETS[ds.key].rule }); draw(); };
  S.handlers.fwAdd = () => { collect(); st.rules.push({ direction: "in", action: "accept", protocol: "tcp", port: "", source: "", comment: "", enabled: true }); draw(); };
  S.handlers.fwDel = (ds) => { collect(); st.rules.splice(+ds.i, 1); draw(); };
  S.handlers.fwUp = (ds) => { collect(); const i = +ds.i; [st.rules[i - 1], st.rules[i]] = [st.rules[i], st.rules[i - 1]]; draw(); };
  S.handlers.fwSave = async () => {
    collect();
    const body = { enabled: st.enabled, policy_in: st.policy_in, policy_out: st.policy_out, rules: st.rules };
    if (admin) body.antispoof = st.antispoof;
    const res = await api(`/api/guests/${g.id}/firewall`, { method: "PUT", body });
    Object.assign(fw, res);
    toast(res.enabled ? "Firewall übernommen und aktiv" : "Gespeichert – die Firewall ist ausgeschaltet");
    draw();
  };
  draw();
}

async function guestTasks(g, shell, seq) {
  const tasks = await api(`/api/tasks?guest_id=${g.id}&limit=50`);
  if (isStale(seq)) return;
  shell(`<div class="card">${taskRows(tasks)}</div>`);
  return { live: true };
}

async function guestSettings(g, shell) {
  const admin = S.me.role === "admin";
  const [templates, users, cdHtml] = await Promise.all([api("/api/templates"), admin ? api("/api/users/brief") : [], cdromCard(g)]);
  shell(`<div class="grid grid-2">
    <div class="card"><div class="card-head"><h2>Allgemein</h2></div><div class="card-body">
      <form id="gen-form">
        <label class="field"><span>Anzeigename</span><input type="text" name="name" value="${esc(g.name)}" maxlength="64" required></label>
        ${admin ? `<label class="field"><span>Besitzer</span><select name="owner_id">${users.map((u) => `<option value="${u.id}" ${u.id === g.owner_id ? "selected" : ""}>${esc(u.username)}</option>`).join("")}</select></label>` : ""}
        <label class="field"><span>Notizen</span><textarea name="notes" rows="4">${esc(g.notes)}</textarea></label>
        <button class="btn primary" type="submit">Speichern</button>
      </form></div></div>
    <div class="stack">
      ${cdHtml}
      <div class="card"><div class="card-head"><h2>root-Passwort zurücksetzen</h2></div><div class="card-body">
        <p class="muted" style="margin-top:0">Erzeugt ein neues zufälliges Passwort und setzt es im laufenden System${g.type === "kvm" ? " (über den qemu-guest-agent)" : ""}.</p>
        <button class="btn" data-act="resetPw" ${g.status === "ready" && g.power === "running" ? "" : "disabled"}>Neues Passwort erzeugen</button></div></div>
      ${admin && g.storage_id ? `<div class="card"><div class="card-head"><h2>Hochverfügbarkeit (HA)</h2>${g.ha ? badge("Aktiv", "good") : badge("Aus")}</div><div class="card-body">
        <p class="muted" style="margin-top:0">Die Festplatte liegt auf dem gemeinsamen Speicher <strong>${esc(g.storage)}</strong>. Mit HA wird der Server automatisch auf einem anderen Node gestartet, wenn sein Node länger als 2 Minuten ausfällt. Leases auf dem Speicher verhindern, dass er doppelt läuft.</p>
        <label class="check"><input type="checkbox" id="ha-toggle" ${g.ha ? "checked" : ""}><span>HA für diesen Server aktivieren</span></label></div></div>` : ""}
      ${admin ? `<div class="card"><div class="card-head"><h2>Auf anderen Node umziehen</h2></div><div class="card-body">
        <p class="muted" style="margin-top:0">${g.storage_id ? "Der Server liegt auf gemeinsamem Speicher: Er wird heruntergefahren, auf dem neuen Node registriert und dort wieder gestartet – ohne Daten zu kopieren. Die Unterbrechung dauert meist unter einer Minute." : "Der Server wird heruntergefahren, über das Panel auf den neuen Node übertragen, dort eingerichtet und wieder gestartet. Die Unterbrechung dauert je nach Festplattengröße einige Minuten."}</p>
        <button class="btn" data-act="migrate" ${["ready", "error"].includes(g.status) ? "" : "disabled"}>Umziehen …</button></div></div>` : ""}
      <div class="card"><div class="card-head"><h2>Neu installieren</h2></div><div class="card-body">
        <p class="muted" style="margin-top:0">Setzt den Server mit einem frischen Betriebssystem neu auf. IP-Adressen bleiben erhalten, <strong>alle Daten werden gelöscht</strong>.</p>
        <div class="row"><select id="reinstall-tpl">${templates.filter((t) => t.type === g.type).map((t) => `<option value="${t.id}" ${t.id === g.template_id ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select>
        <button class="btn danger" style="flex:none" data-act="reinstall" ${["ready", "error"].includes(g.status) ? "" : "disabled"}>Neu installieren</button></div></div></div>
      <div class="card" style="border-color:var(--bad)"><div class="card-head"><h2>Server löschen</h2></div><div class="card-body">
        <p class="muted" style="margin-top:0">Löscht den Server inklusive Festplatte und Snapshots. Die IP-Adressen werden wieder freigegeben.</p>
        <button class="btn danger solid" data-act="deleteGuest">Server endgültig löschen</button>
        ${admin ? `<label class="check" style="margin-top:12px"><input type="checkbox" id="force-del"><span class="small">Erzwingen (auch wenn der Node nicht erreichbar ist)</span></label>` : ""}
      </div></div>
    </div></div>`);
  $("#gen-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = { name: f.name.value, notes: f.notes.value };
    if (f.owner_id) body.owner_id = +f.owner_id.value;
    await api(`/api/guests/${g.id}`, { method: "PATCH", body }).then(() => { toast("Gespeichert"); updateSidebar(); }).catch(fail);
  };
  bindCdrom(g);
  if ($("#ha-toggle")) $("#ha-toggle").onchange = async (e) => {
    await api(`/api/guests/${g.id}`, { method: "PATCH", body: { ha: e.target.checked } })
      .then(() => { toast(e.target.checked ? "HA aktiviert" : "HA deaktiviert"); route(true); })
      .catch((err) => { e.target.checked = !e.target.checked; fail(err); });
  };
  S.handlers.resetPw = async () => {
    if (!(await confirmBox("Passwort zurücksetzen?", "Das bisherige root-Passwort funktioniert danach nicht mehr.", { ok: "Zurücksetzen" }))) return;
    const r = await api(`/api/guests/${g.id}/password`, { method: "POST", body: {} });
    showSecret("Neues root-Passwort", `Benutzer <code>root</code> auf <strong>${esc(g.name)}</strong>:`, r.password);
  };
  S.handlers.migrate = async () => {
    const [nodes, pools] = await Promise.all([api("/api/nodes"), api("/api/pools")]);
    const targets = nodes.filter((n) => n.id !== g.node_id && n.status === "online");
    if (!targets.length) return toast("Kein anderer Node online", "bad");
    const m = modal({
      title: `${g.name} umziehen`, wide: true,
      body: `<label class="field"><span>Ziel-Node</span><select id="mg-node">${targets.map((n) => `<option value="${n.id}">${esc(n.name)} – ${fmtMB(Math.max(0, Math.round((n.info.memory_total || 0) / 1048576) - n.committed_memory_mb))} RAM frei</option>`).join("")}</select></label>
        <div id="mg-net"></div><div class="alert bad hidden" id="mg-err"></div>`,
      foot: `<button class="btn" data-close>Abbrechen</button><button class="btn primary" id="mg-go">Umzug starten</button>`,
    });
    const renderNet = async () => {
      const nodeId = +$("#mg-node", m).value;
      const chk = await api(`/api/guests/${g.id}/migrate/check?node_id=${nodeId}`);
      const row = (v) => {
        const cur = chk.ips.find((i) => i.version === v);
        const avail = pools.filter((p) => p.version === v && (!p.node_id || p.node_id === nodeId));
        const keepOk = !cur || cur.usable;
        return `<label class="field"><span>IPv${v}</span><select id="mg-pool${v}">
          ${keepOk ? `<option value="keep">${cur ? `Behalten (${esc(cur.address)})` : "Keine (wie bisher)"}</option>` : ""}
          ${avail.map((p) => `<option value="${p.id}">Neue IP aus ${esc(p.name)} (${p.mode === "routed" ? "geroutet" : "Bridge"}) – ${p.size - p.used} frei</option>`).join("")}
          <option value="none">${v === 4 ? "Keine feste IPv4 (DHCP)" : "Keine IPv6"}</option></select>
          ${!keepOk ? `<div class="hint" style="color:var(--warn)">${esc(cur.address)} (Pool ${esc(cur.pool)}) ist auf dem Ziel-Node nicht nutzbar – bitte neue IP wählen.</div>` : ""}</label>`;
      };
      const sto = !chk.storage ? "" : chk.shared
        ? `<div class="alert" style="margin-bottom:12px">Gemeinsamer Speicher ${esc(chk.storage)} ist auf dem Ziel eingehängt – es werden keine Daten kopiert.</div>`
        : `<div class="alert bad" style="margin-bottom:12px">Speicher ${esc(chk.storage)} ist auf diesem Node nicht eingehängt – Umzug nicht möglich.</div>`;
      $("#mg-net", m).innerHTML = sto + row(4) + row(6);
    };
    $("#mg-node", m).onchange = () => renderNet().catch(fail);
    await renderNet();
    $("#mg-go", m).onclick = async () => {
      const num = (v) => (/^\d+$/.test(v) ? +v : v);
      const body = { node_id: +$("#mg-node", m).value, ipv4_pool: num($("#mg-pool4", m).value), ipv6_pool: num($("#mg-pool6", m).value) };
      try {
        const r = await api(`/api/guests/${g.id}/migrate`, { method: "POST", body });
        m.close();
        await watchTask(r.task_id, `${g.name} umziehen`);
        route();
      } catch (e) {
        $("#mg-err", m).textContent = e.message;
        $("#mg-err", m).classList.remove("hidden");
      }
    };
  };
  S.handlers.reinstall = async () => {
    if (!(await confirmBox("Server neu installieren?", "Alle Daten auf dem Server werden unwiderruflich gelöscht.", { danger: true, ok: "Neu installieren", requireText: g.vmid }))) return;
    const r = await api(`/api/guests/${g.id}/reinstall`, { method: "POST", body: { template_id: +$("#reinstall-tpl").value } });
    showSecret("Neues root-Passwort", "Nach der Neuinstallation gilt dieses Passwort:", r.password);
    watchTask(r.task_id, "Neu installieren");
  };
  S.handlers.deleteGuest = async () => {
    const force = $("#force-del") && $("#force-del").checked;
    if (!(await confirmBox("Server löschen?", `<strong>${esc(g.name)}</strong> wird mit allen Daten gelöscht.`, { danger: true, ok: "Endgültig löschen", requireText: g.vmid }))) return;
    const r = await api(`/api/guests/${g.id}${force ? "?force=true" : ""}`, { method: "DELETE" });
    const status = await watchTask(r.task_id, "Server löschen");
    if (status === "ok") { $$(".modal-back").forEach((x) => x.close()); location.hash = "#/guests"; }
  };
}

// ------------------------------------------------------------------ Server erstellen

async function viewCreate() {
  const admin = S.me.role === "admin";
  const [templates, nodes, pools, me, users, storages] = await Promise.all([
    api("/api/templates"), api("/api/nodes"), api("/api/pools"), api("/api/me"), admin ? api("/api/users/brief") : [],
    api("/api/storages").catch(() => []),
  ]);
  const q = me.quota, u = me.usage;
  const free = admin ? { cores: 64, memory_mb: 262144, disk_gb: 4096 }
    : { cores: q.cores - u.cores, memory_mb: q.memory_mb - u.memory_mb, disk_gb: q.disk_gb - u.disk_gb };
  const blocked = !admin && (u.guests >= q.guests || free.cores < 1 || free.memory_mb < 256 || free.disk_gb < 2);
  const C = { type: "kvm", template: null, iso: null, isoList: null, name: "", hostname: "", manualHost: false };

  const onlineNodes = nodes.filter((n) => n.status === "online");
  const nodeOptions = `<option value="auto">Automatisch (Node mit dem meisten freien RAM)</option>` + (admin ? onlineNodes.map((n) =>
    `<option value="${n.id}">${esc(n.name)} – ${fmtMB(Math.max(0, Math.round((n.info.memory_total || 0) / 1048576) - n.committed_memory_mb))} frei</option>`).join("") : "");
  const poolOpt = (v) => pools.filter((p) => p.version === v).map((p) =>
    `<option value="${p.id}" ${p.size - p.used <= 0 ? "disabled" : ""}>${esc(p.name)}${p.network ? ` (${esc(p.network)})` : ""} – ${p.size - p.used} frei</option>`).join("");

  const osCard = (t) => `<button type="button" class="os-card" data-tpl="${t.id}">
    <span class="os-icon ${esc(t.os_family)}">${{ debian: "D", ubuntu: "U", windows: "W" }[t.os_family] || "L"}</span>
    <span><div class="t">${esc(t.name.replace(" (Container)", ""))}</div><div class="s">${t.source === "iso" ? "ISO-Installation" : t.type === "lxc" ? "Container" : "Cloud-Image"}</div></span></button>`;

  setMain(`
    ${pageHead("Server erstellen", "IP-Adresse, Netzwerk, Hostname und Zugang werden automatisch eingerichtet.")}
    ${blocked ? `<div class="alert warn" style="margin-bottom:16px">Dein Kontingent ist ausgeschöpft. Lösche einen Server oder wende dich an einen Administrator.</div>` : ""}
    ${!onlineNodes.length ? `<div class="alert bad" style="margin-bottom:16px">Es ist kein Node online. ${admin ? `<a href="#/nodes">Node hinzufügen</a>` : "Bitte später erneut versuchen."}</div>` : ""}
    <form id="create-form" class="create-layout" novalidate>
      <div class="stack">
        <div class="card"><div class="card-head"><span class="step-num">1</span><h2>Typ und Betriebssystem</h2>
          <div class="seg"><button type="button" data-type="kvm" class="active">Virtuelle Maschine</button><button type="button" data-type="lxc">Container</button></div></div>
          <div class="card-body">
            <p class="muted small" style="margin-top:0" id="type-hint"></p>
            <div id="kvm-warn"></div>
            <div class="os-grid" id="os-grid"></div>
            <div id="iso-pick"></div>
          </div></div>

        <div class="card"><div class="card-head"><span class="step-num">2</span><h2>Ressourcen</h2></div><div class="card-body">
          <div class="presets">
            <button type="button" class="btn sm" data-preset="1,1024,10">Klein · 1 CPU · 1 GB</button>
            <button type="button" class="btn sm" data-preset="2,2048,25">Mittel · 2 CPU · 2 GB</button>
            <button type="button" class="btn sm" data-preset="4,4096,50">Groß · 4 CPU · 4 GB</button>
          </div>
          ${sliderRow("cores", "CPU-Kerne", Math.min(1, Math.max(1, free.cores)), 1, Math.max(1, free.cores), 1, "Kerne")}
          ${sliderRow("memory_mb", "Arbeitsspeicher", Math.min(1024, Math.max(256, free.memory_mb)), 256, Math.max(256, free.memory_mb), 256, "MB")}
          ${sliderRow("disk_gb", "Speicher", Math.min(10, Math.max(2, free.disk_gb)), 2, Math.max(2, free.disk_gb), 1, "GB")}
          ${admin ? "" : `<p class="muted small" style="margin:0">Noch verfügbar: ${free.cores} CPU · ${fmtMB(free.memory_mb)} · ${free.disk_gb} GB · ${q.ips - u.ips} IP-Adressen</p>`}
        </div></div>

        <div class="card"><div class="card-head"><span class="step-num">3</span><h2>Standort und Netzwerk</h2></div><div class="card-body">
          <div class="row">
            <label class="field"><span>Node</span><select name="node">${nodeOptions}</select></label>
            <label class="field"><span>IPv4-Adresse</span><select name="ipv4_pool"><option value="auto">Automatisch aus passendem Pool</option>${poolOpt(4)}<option value="none">Keine feste IP (DHCP)</option></select></label>
            <label class="field"><span>IPv6-Adresse</span><select name="ipv6_pool"><option value="none">Keine</option>${pools.some((p) => p.version === 6) ? `<option value="auto">Automatisch</option>` : ""}${poolOpt(6)}</select></label>
          </div>
          ${storages.length ? `<label class="field"><span>Festplatte liegt auf</span><select name="storage_id"><option value="">Lokal auf dem Node</option>
            ${storages.map((x) => `<option value="${x.id}" ${x.nodes.length ? "" : "disabled"}>${esc(x.name)} (${esc(x.type_label)}, gemeinsam)${x.nodes.length ? "" : " – auf keinem Node eingehängt"}</option>`).join("")}</select>
            <div class="hint">Auf gemeinsamem Speicher kann der Server ohne Kopieren umziehen und bei Ausfall eines Nodes automatisch woanders starten (HA).</div></label>` : ""}
          <p class="muted small" style="margin:0">Die nächste freie Adresse wird reserviert und samt Gateway und DNS im System eingetragen.</p>
        </div></div>

        <div class="card"><div class="card-head"><span class="step-num">4</span><h2>Name und Zugang</h2></div><div class="card-body">
          <div class="row">
            <label class="field"><span>Anzeigename</span><input type="text" name="name" maxlength="64" placeholder="z. B. Webserver" required></label>
            <label class="field"><span>Hostname</span><input type="text" name="hostname" maxlength="63" placeholder="webserver" pattern="[A-Za-z0-9][A-Za-z0-9.-]*" required></label>
          </div>
          <label class="field"><span>root-Passwort</span>
            <div class="input-group"><input type="password" name="password" autocomplete="new-password" placeholder="Leer lassen = sicheres Passwort erzeugen"><button type="button" class="btn" id="pw-toggle">Anzeigen</button></div>
            <div class="hint">Mindestens 8 Zeichen. Es wird nicht gespeichert und nach dem Erstellen einmalig angezeigt.</div></label>
          <label class="field"><span>SSH-Schlüssel (optional, einer pro Zeile)</span><textarea name="ssh_keys" rows="3" placeholder="ssh-ed25519 AAAA…">${esc(me.ssh_keys || "")}</textarea></label>
          ${admin ? `<label class="field"><span>Besitzer</span><select name="owner_id">${users.map((x) => `<option value="${x.id}" ${x.id === me.id ? "selected" : ""}>${esc(x.username)}</option>`).join("")}</select></label>` : ""}
        </div></div>
      </div>

      <aside class="summary"><div class="card"><div class="card-head"><h2>Zusammenfassung</h2></div><div class="card-body" id="summary"></div>
        <div class="card-body" style="border-top:1px solid var(--border)">
          <div class="alert bad hidden" id="create-error" style="margin-bottom:12px"></div>
          <button class="btn primary lg block" type="submit" id="create-btn" ${blocked || !onlineNodes.length ? "disabled" : ""}>Server erstellen</button>
        </div></div></aside>
    </form>`);

  const form = $("#create-form");
  bindSliders(form);
  const renderOS = () => {
    const list = templates.filter((t) => t.type === C.type && t.enabled);
    $("#os-grid").innerHTML = (list.map(osCard).join("") || `<div class="muted">Keine Vorlagen für diesen Typ.</div>`)
      + (C.type === "kvm" ? `<button type="button" class="os-card" data-tpl="iso"><span class="os-icon linux">ISO</span>
          <span><div class="t">Eigene ISO</div><div class="s">aus der ISO-Bibliothek</div></span></button>` : "");
    if (C.type !== "kvm") C.iso = null;
    if (!C.iso && !list.some((t) => t.id === (C.template && C.template.id))) C.template = list[0] || null;
    $$(".os-card").forEach((b) => b.classList.toggle("active", C.iso ? b.dataset.tpl === "iso" : C.template && +b.dataset.tpl === C.template.id));
    renderIsoPick();
    $("#type-hint").textContent = C.type === "kvm"
      ? "Vollwertige virtuelle Maschine mit eigenem Kernel – ideal für alles, inkl. Docker und eigene Kernelmodule."
      : "Leichtgewichtiger Linux-Container – startet in Sekunden und braucht kaum Overhead.";
    update();
  };
  const kvmWarning = () => {
    const box = $("#kvm-warn");
    if (C.type !== "kvm") { box.innerHTML = ""; return; }
    const chosen = form.node.value === "auto" ? onlineNodes : onlineNodes.filter((n) => String(n.id) === form.node.value);
    const noKvm = chosen.filter((n) => n.info.kvm === false);
    const nested = chosen.filter((n) => n.info.kvm && n.info.virtualization && n.info.virtualization !== "none");
    if (chosen.length && noKvm.length === chosen.length) {
      box.innerHTML = `<div class="alert bad" style="margin-bottom:12px"><strong>Keine Hardware-Beschleunigung:</strong> VMs laufen hier nur in Software-Emulation und sind extrem langsam. Nimm stattdessen einen <strong>Container</strong> – der läuft mit voller Geschwindigkeit.</div>`;
    } else if (nested.length) {
      box.innerHTML = `<div class="alert warn" style="margin-bottom:12px"><strong>Verschachtelte Virtualisierung:</strong> Der Node ist selbst eine VM (${esc(nested[0].info.virtualization)}). VMs funktionieren, sind aber langsamer und beim ersten Start träger. Für beste Leistung einen <strong>Container</strong> wählen.</div>`;
    } else {
      box.innerHTML = "";
    }
  };
  const update = () => {
    kvmWarning();
    const f = form;
    const t = C.template;
    if (t && +f.disk_gb.value < t.min_disk_gb) { f.disk_gb.value = t.min_disk_gb; f["disk_gb-range"].value = t.min_disk_gb; }
    if (!C.manualHost) {
      f.hostname.value = f.name.value.toLowerCase().normalize("NFKD").replace(/[^a-z0-9.-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
    }
    const sel = (el) => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : "";
    $("#summary").innerHTML = `
      <div class="sum-line"><span class="k">Typ</span><span class="v">${C.type === "kvm" ? "Virtuelle Maschine" : "Container"}</span></div>
      <div class="sum-line"><span class="k">System</span><span class="v">${esc(C.iso ? `ISO: ${C.iso.file || "–"}` : t ? t.name : "–")}</span></div>
      <div class="sum-line"><span class="k">CPU</span><span class="v">${f.cores.value} ${+f.cores.value === 1 ? "Kern" : "Kerne"}</span></div>
      <div class="sum-line"><span class="k">RAM</span><span class="v">${fmtMB(+f.memory_mb.value)}</span></div>
      <div class="sum-line"><span class="k">Speicher</span><span class="v">${f.disk_gb.value} GB</span></div>
      <div class="sum-line"><span class="k">Node</span><span class="v">${esc(sel(f.node).split(" – ")[0])}</span></div>
      <div class="sum-line"><span class="k">IPv4</span><span class="v">${esc(sel(f.ipv4_pool).split(" – ")[0])}</span></div>
      <div class="sum-line"><span class="k">Hostname</span><span class="v">${esc(f.hostname.value || "–")}</span></div>`;
  };
  $$(".seg button[data-type]").forEach((b) => b.onclick = () => {
    C.type = b.dataset.type;
    $$(".seg button[data-type]").forEach((x) => x.classList.toggle("active", x === b));
    renderOS();
  });
  const renderIsoPick = () => {
    const box = $("#iso-pick");
    if (!C.iso) { box.innerHTML = ""; return; }
    const opts = C.isoList.map((x) => `<option value="${x.node_id}|${esc(x.file)}" ${C.iso.file === x.file && C.iso.node_id === x.node_id ? "selected" : ""}>${esc(x.file)} – ${esc(x.node)} (${fmtBytes(x.size)})</option>`).join("");
    box.innerHTML = C.isoList.length
      ? `<label class="field" style="margin-top:14px"><span>ISO auswählen</span><select id="iso-select">${opts}</select>
          <div class="hint">Nach dem Start installierst du das System über die Konsole. Die IP wird reserviert, muss bei der Installation aber selbst eingetragen werden (steht danach im Netzwerk-Tab).</div></label>`
      : `<div class="alert warn" style="margin-top:14px">Noch keine ISO vorhanden. ${admin ? `Unter <a href="#/nodes">Nodes</a> → Node → ISOs hochladen.` : "Bitte einen Administrator, eine ISO bereitzustellen."}</div>`;
    const sel = $("#iso-select");
    if (sel) {
      const apply = () => {
        const [nid, file] = sel.value.split("|");
        C.iso = { node_id: +nid, file };
        form.node.value = nid;
        update();
      };
      sel.onchange = apply;
      apply();
    }
  };
  $("#os-grid").addEventListener("click", async (e) => {
    const b = e.target.closest(".os-card");
    if (!b) return;
    if (b.dataset.tpl === "iso") {
      if (!C.isoList) {
        C.isoList = [];
        for (const n of onlineNodes) {
          const l = await api(`/api/nodes/${n.id}/isos`).catch(() => []);
          C.isoList.push(...l.filter((x) => !x.template).map((x) => ({ ...x, node_id: n.id, node: n.name })));
        }
      }
      C.iso = C.isoList[0] ? { node_id: C.isoList[0].node_id, file: C.isoList[0].file } : { node_id: null, file: null };
      C.template = null;
      $$(".os-card").forEach((x) => x.classList.toggle("active", x === b));
      renderIsoPick();
      update();
      return;
    }
    C.iso = null;
    renderIsoPick();
    C.template = templates.find((t) => t.id === +b.dataset.tpl);
    $$(".os-card").forEach((x) => x.classList.toggle("active", x === b));
    update();
  });
  $$("[data-preset]").forEach((b) => b.onclick = () => {
    const [c, mem, d] = b.dataset.preset.split(",").map(Number);
    [["cores", c], ["memory_mb", mem], ["disk_gb", d]].forEach(([n, v]) => {
      const max = +form[n].max;
      form[n].value = Math.min(v, max);
      form[`${n}-range`].value = form[n].value;
    });
    update();
  });
  form.hostname.addEventListener("input", () => { C.manualHost = !!form.hostname.value; });
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  $("#pw-toggle").onclick = () => {
    const pw = form.password;
    pw.type = pw.type === "password" ? "text" : "password";
    $("#pw-toggle").textContent = pw.type === "password" ? "Anzeigen" : "Verbergen";
  };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const err = $("#create-error");
    err.classList.add("hidden");
    if (!C.template && !(C.iso && C.iso.file)) { err.textContent = "Bitte ein Betriebssystem oder eine ISO wählen."; err.classList.remove("hidden"); return; }
    if (!form.name.value.trim()) { err.textContent = "Bitte einen Namen angeben."; err.classList.remove("hidden"); form.name.focus(); return; }
    const num = (v) => (/^\d+$/.test(v) ? +v : v);
    const body = {
      template_id: C.template ? C.template.id : null, iso_file: C.iso ? C.iso.file : null,
      name: form.name.value.trim(), hostname: form.hostname.value.trim(),
      cores: +form.cores.value, memory_mb: +form.memory_mb.value, disk_gb: +form.disk_gb.value,
      node: num(form.node.value), ipv4_pool: num(form.ipv4_pool.value), ipv6_pool: num(form.ipv6_pool.value),
      password: form.password.value || null, ssh_keys: form.ssh_keys.value,
    };
    if (form.owner_id) body.owner_id = +form.owner_id.value;
    if (form.storage_id && form.storage_id.value) body.storage_id = +form.storage_id.value;
    const btn = $("#create-btn");
    btn.disabled = true;
    try {
      const r = await api("/api/guests", { method: "POST", body });
      location.hash = `#/guest/${r.guest_id}`;
      showSecret("Server wird erstellt", `Zugangsdaten für <strong>${esc(body.name)}</strong> (VMID ${r.vmid}) – Benutzer <code>root</code>:`, r.password,
        `<button class="btn" data-act="task" data-id="${r.task_id}" data-title="Erstellen: ${esc(body.name)}" data-close>Fortschritt anzeigen</button>`);
      S.handlers.task = (ds) => watchTask(ds.id, ds.title);
      updateSidebar();
    } catch (ex) {
      err.textContent = ex.message;
      err.classList.remove("hidden");
      btn.disabled = false;
    }
  };
  renderOS();
}

// ------------------------------------------------------------------ Nodes

function nodeMeters(n) {
  const s = n.stats;
  if (!s) return `<p class="muted" style="margin:0">Keine Live-Daten – Node ${n.status === "offline" ? "offline" : "noch nicht abgefragt"}.</p>`;
  return meter("CPU", s.cpu, 100, `${s.cpu.toFixed(0)} % · Load ${s.load.map((x) => x.toFixed(2)).join(" ")}`)
    + meter("Arbeitsspeicher", s.memory_used, s.memory_total, `${fmtBytes(s.memory_used)} / ${fmtBytes(s.memory_total)}`)
    + meter("Speicher (VM-Daten)", s.disk_used, s.disk_total, `${fmtBytes(s.disk_used)} / ${fmtBytes(s.disk_total)}`);
}

function addNodeDialog() {
  const m = modal({
    title: "Node hinzufügen", wide: true,
    body: `<div class="seg" style="margin-bottom:16px"><button class="active" data-mode="ssh">Automatisch per SSH</button><button data-mode="join">Join-Code</button></div>
      <form id="ssh-form">
        <p class="muted" style="margin-top:0">Das Panel verbindet sich per SSH, installiert KVM, LXC und den PomBot-Agent und bindet den Server automatisch ein. Unterstützt: Debian 12/13, Ubuntu 22.04/24.04. Zugangsdaten werden nicht gespeichert.</p>
        <div class="row"><label class="field"><span>Name</span><input type="text" name="name" placeholder="node1" required></label>
          <label class="field"><span>Adresse (IP oder Hostname)</span><input type="text" name="host" placeholder="203.0.113.5" required></label></div>
        <div class="row"><label class="field"><span>SSH-Benutzer</span><input type="text" name="ssh_user" value="root"></label>
          <label class="field"><span>SSH-Port</span><input type="number" name="ssh_port" value="22"></label>
          <label class="field"><span>Agent-Port</span><input type="number" name="agent_port" value="8007"></label></div>
        <div class="seg" style="margin-bottom:12px"><button type="button" class="active" data-auth="pw">Passwort</button><button type="button" data-auth="key">SSH-Schlüssel</button></div>
        <div id="auth-pw"><label class="field"><span>Passwort</span><input type="password" name="password" autocomplete="off"></label></div>
        <div id="auth-key" class="hidden"><label class="field"><span>Privater Schlüssel</span><textarea name="private_key" rows="5" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea></label>
          <label class="field"><span>Passphrase (optional)</span><input type="password" name="passphrase" autocomplete="off"></label></div>
        <label class="check"><input type="checkbox" name="create_bridge"><span><strong>Netzwerk-Bridge automatisch anlegen</strong><br><span class="muted small">Stellt die Haupt-Netzwerkkarte auf eine Bridge um, damit Server eigene öffentliche IPs nutzen können. Die Verbindung kann dabei kurz abbrechen. Nur nötig, wenn noch keine Bridge existiert.</span></span></label>
        <label class="field" style="max-width:200px"><span>Bridge-Name</span><input type="text" name="bridge_name" value="vmbr0"></label>
      </form>
      <form id="join-form" class="hidden">
        <p class="muted" style="margin-top:0">Führe auf dem neuen Server als root aus:</p>
        <div class="log" style="margin-bottom:14px">bash install-agent.sh --allow &lt;IP-des-Panels&gt;</div>
        <p class="muted">Das Skript liegt im Ordner <code>agent/</code> des Projekts. Am Ende wird ein <code>POMBOT_JOIN=…</code>-Code ausgegeben.</p>
        <label class="field"><span>Join-Code</span><textarea name="join_code" rows="4" required></textarea></label>
        <div class="row"><label class="field"><span>Name (optional)</span><input type="text" name="name"></label>
          <label class="field"><span>Adresse überschreiben (optional)</span><input type="text" name="host" placeholder="aus dem Code"></label></div>
      </form>
      <div class="alert bad hidden" id="node-error"></div>`,
    foot: `<button class="btn" data-close>Abbrechen</button><button class="btn primary" id="node-submit">Installieren und hinzufügen</button>`,
  });
  let mode = "ssh", auth = "pw";
  $$("[data-mode]", m).forEach((b) => b.onclick = () => {
    mode = b.dataset.mode;
    $$("[data-mode]", m).forEach((x) => x.classList.toggle("active", x === b));
    $("#ssh-form", m).classList.toggle("hidden", mode !== "ssh");
    $("#join-form", m).classList.toggle("hidden", mode !== "join");
    $("#node-submit", m).textContent = mode === "ssh" ? "Installieren und hinzufügen" : "Hinzufügen";
  });
  $$("[data-auth]", m).forEach((b) => b.onclick = () => {
    auth = b.dataset.auth;
    $$("[data-auth]", m).forEach((x) => x.classList.toggle("active", x === b));
    $("#auth-pw", m).classList.toggle("hidden", auth !== "pw");
    $("#auth-key", m).classList.toggle("hidden", auth !== "key");
  });
  $("#node-submit", m).onclick = async () => {
    const errEl = $("#node-error", m);
    errEl.classList.add("hidden");
    try {
      if (mode === "ssh") {
        const f = $("#ssh-form", m);
        const body = {
          name: f.name.value.trim(), host: f.host.value.trim(), ssh_user: f.ssh_user.value.trim() || "root",
          ssh_port: +f.ssh_port.value || 22, agent_port: +f.agent_port.value || 8007,
          password: auth === "pw" ? f.password.value : null, private_key: auth === "key" ? f.private_key.value : null,
          passphrase: auth === "key" ? f.passphrase.value || null : null,
          create_bridge: f.create_bridge.checked, bridge_name: f.bridge_name.value.trim() || "vmbr0",
        };
        const r = await api("/api/nodes/ssh", { method: "POST", body });
        m.close();
        await watchTask(r.task_id, `Node ${body.name} installieren`);
      } else {
        const f = $("#join-form", m);
        await api("/api/nodes/join", { method: "POST", body: { join_code: f.join_code.value, name: f.name.value.trim() || null, host: f.host.value.trim() || null } });
        m.close();
        toast("Node hinzugefügt");
      }
      route();
    } catch (e) {
      errEl.textContent = e.message;
      errEl.classList.remove("hidden");
    }
  };
}

async function viewNodes(params, m, silent, seq) {
  const nodes = await api("/api/nodes");
  if (isStale(seq)) return;
  S.handlers.addNode = addNodeDialog;
  setMain(`${pageHead("Nodes", "Physische Server, auf denen deine VMs und Container laufen", `<button class="btn primary" data-act="addNode">${icon("plus")}Node hinzufügen</button>`)}
    ${nodes.length ? `<div class="grid grid-3">${nodes.map((n) => `
      <a class="card" href="#/node/${n.id}" style="color:inherit;text-decoration:none">
        <div class="card-head"><span class="dot-status ${n.status === "online" ? "good" : "bad"}"></span><h2>${esc(n.name)}</h2>
          ${n.status === "online" ? badge("Online", "good") : badge("Offline", "bad")}${n.enabled ? "" : badge("Gesperrt", "warn")}</div>
        <div class="card-body">${nodeMeters(n)}
          <div class="muted small" style="margin-top:12px">${esc(n.info.os || "")} · ${n.info.cpu_cores || "?"} Kerne · ${plural(n.guests, "Server", "Server")}${n.info.kvm === false ? " · <span style='color:var(--warn)'>ohne KVM-Beschleunigung</span>"
            : n.info.virtualization && n.info.virtualization !== "none" ? ` · <span style='color:var(--warn)'>verschachtelt (${esc(n.info.virtualization)})</span>` : ""}</div>
        </div></a>`).join("")}</div>`
      : `<div class="card"><div class="empty">Noch keine Nodes. Füge deinen ersten Server hinzu – die Installation läuft automatisch per SSH.<br><br><button class="btn primary" data-act="addNode">${icon("plus")}Node hinzufügen</button></div></div>`}`);
  return { live: true };
}

async function viewNode(params, m, silent, seq) {
  const id = m[1], tab = m[2] || "overview";
  const n = await api(`/api/nodes/${id}`);
  if (isStale(seq)) return;
  const base = `#/node/${id}`;
  S.handlers.shell = () => window.open(`/console?node=${id}`, `shell-${id}`, "width=1100,height=760");
  const head = pageHead(`${esc(n.name)} <span style="vertical-align:3px">${n.status === "online" ? badge("Online", "good") : badge("Offline", "bad")}</span>`,
    `${esc(n.info.os || "")} · ${esc(n.host)}:${n.port}`,
    `<button class="btn" data-act="shell" ${n.status === "online" ? "" : "disabled"}>${icon("terminal")}Shell</button>`);
  const shell = (c) => setMain(head + tabs(base, tab, [["overview", "Übersicht"], ["guests", "Server"], ["isos", "ISOs"], ["images", "Images"], ["backups", "Backups"], ["settings", "Einstellungen"]]) + c);

  if (tab === "overview") {
    const i = n.info, s = n.stats;
    shell(`<div class="grid grid-2">
      <div class="card"><div class="card-head"><h2>Auslastung</h2></div><div class="card-body">${nodeMeters(n)}
        <div class="grid grid-2" style="margin-top:16px">
          <div><div class="muted small" style="margin-bottom:4px">CPU-Verlauf (0–100 %)</div>${sparkline(n.history, "cpu")}</div>
          <div><div class="muted small" style="margin-bottom:4px">RAM-Verlauf (0–100 %)</div>${sparkline(n.history, "mem")}</div>
        </div></div></div>
      <div class="card"><div class="card-head"><h2>System</h2></div><div class="card-body"><dl class="kv">
        <dt>Hostname</dt><dd>${esc(i.hostname || "–")}</dd><dt>System</dt><dd>${esc(i.os || "–")}</dd><dt>Kernel</dt><dd>${esc(i.kernel || "–")}</dd>
        <dt>CPU</dt><dd>${esc(i.cpu_model || "–")} (${i.cpu_cores || "?"} Threads)</dd>
        <dt>KVM</dt><dd>${i.kvm ? badge("Hardware-Beschleunigung", "good") : badge("Nicht verfügbar", "warn")}</dd>
        <dt>Host-Typ</dt><dd>${!i.virtualization ? "–" : i.virtualization === "none" ? badge("Echte Hardware", "good")
          : `${badge(`Selbst virtuell (${i.virtualization})`, "warn")}<div class="muted small">Verschachtelte Virtualisierung: VMs sind langsamer, Container empfohlen.</div>`}</dd>
        <dt>libvirt</dt><dd>${esc(i.libvirt || "–")}</dd><dt>LXC</dt><dd>${esc(i.lxc || "–")}</dd>
        <dt>Bridges</dt><dd>${(i.bridges || []).map((b) => `<code>${esc(b)}</code>`).join(" ") || `<span style="color:var(--warn)">keine – Server brauchen eine Bridge (z. B. vmbr0)</span>`}</dd>
        <dt>Laufzeit</dt><dd>${s ? fmtUptime(s.uptime) : "–"}</dd>
        <dt>Zugesagt</dt><dd>${n.committed_cores} vCPU · ${fmtMB(n.committed_memory_mb)} RAM in ${plural(n.guests, "Server", "Server")}</dd>
        <dt>Agent</dt><dd>${esc(i.agent_version || "–")}</dd>
        <dt>Zertifikat</dt><dd class="mono small">${esc(n.fingerprint)}</dd>
      </dl></div></div></div>`);
    return { live: true };
  }
  if (tab === "guests") {
    const guests = (await api("/api/guests?all=true")).filter((g) => g.node_id === n.id);
    S.handlers.open = (ds) => { location.hash = `#/guest/${ds.id}`; };
    shell(`<div class="card">${guests.length ? `<div class="table-wrap"><table><thead><tr><th>Status</th><th>VMID</th><th>Name</th><th>Typ</th><th>IP</th><th>Besitzer</th><th>Ressourcen</th></tr></thead><tbody>
      ${guests.map((g) => `<tr class="click" data-act="open" data-id="${g.id}"><td>${guestBadge(g)}</td><td class="mono">${g.vmid}</td><td>${esc(g.name)}</td><td><span class="type-tag">${g.type === "kvm" ? "VM" : "CT"}</span></td><td class="mono">${esc(guestIPs(g))}</td><td>${esc(g.owner)}</td><td class="small">${g.cores} CPU · ${fmtMB(g.memory_mb)} · ${g.disk_gb} GB</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">Auf diesem Node laufen keine Server.</div>`}</div>`);
    return { live: true };
  }
  if (tab === "isos") return nodeIsoTab(n, shell);
  if (tab === "images") {
    const imgs = await api(`/api/nodes/${id}/images`).catch((e) => { toast(e.message, "bad"); return []; });
    S.handlers.imgDelete = async (ds) => {
      if (!(await confirmBox("Image löschen?", "Das zwischengespeicherte Image wird gelöscht und beim nächsten Erstellen neu heruntergeladen (z. B. um eine aktuelle Version zu erhalten).", { danger: true, ok: "Löschen" }))) return;
      await api(`/api/nodes/${id}/images/${ds.id}`, { method: "DELETE" });
      route();
    };
    shell(`<div class="card"><div class="card-head"><h2>Zwischengespeicherte Images</h2></div>${imgs.length ? `<div class="table-wrap"><table><thead><tr><th>Art</th><th>Quelle</th><th>Größe</th><th>Geladen</th><th></th></tr></thead><tbody>
      ${imgs.map((i) => `<tr><td>${i.kind === "iso" ? "ISO" : "Cloud-Image"}</td><td class="small mono" style="word-break:break-all">${esc(i.url || i.id)}</td><td>${fmtBytes(i.size)}</td><td class="nowrap">${i.downloaded ? fmtDate(new Date(i.downloaded * 1000).toISOString()) : "–"}</td>
        <td class="right"><button class="btn sm danger" data-act="imgDelete" data-id="${esc(i.id)}">Löschen</button></td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">Noch keine Images. Sie werden beim ersten Erstellen eines Servers automatisch geladen.</div>`}</div>`);
    return;
  }
  if (tab === "backups") {
    const backups = await api(`/api/nodes/${id}/backups`).catch((e) => { toast(e.message, "bad"); return []; });
    const total = backups.reduce((a, b) => a + b.size, 0);
    shell(`<div class="card"><div class="card-head"><h2>Alle Backups auf diesem Node</h2><span class="muted small">${fmtBytes(total)} gesamt</span></div>${backups.length ? `<div class="table-wrap"><table><thead><tr><th>Datei</th><th>Gast</th><th>Erstellt</th><th>Größe</th></tr></thead><tbody>
      ${backups.map((b) => `<tr><td class="mono small">${esc(b.file)}</td><td class="mono">${esc(b.guest)}</td><td>${fmtDate(new Date(b.created * 1000).toISOString())}</td><td>${fmtBytes(b.size)}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">Keine Backups.</div>`}</div>`);
    return;
  }
  // Einstellungen
  shell(`<div class="grid grid-2"><div class="card"><div class="card-head"><h2>Einstellungen</h2></div><div class="card-body"><form id="node-form">
      <label class="field"><span>Name</span><input type="text" name="name" value="${esc(n.name)}" required></label>
      <label class="field"><span>Adresse</span><input type="text" name="host" value="${esc(n.host)}" required><div class="hint">Unter dieser Adresse erreicht das Panel den Agent (Port ${n.port}).</div></label>
      <label class="check"><input type="checkbox" name="enabled" ${n.enabled ? "checked" : ""}><span>Neue Server dürfen auf diesem Node angelegt werden</span></label>
      <button class="btn primary" type="submit">Speichern</button> <button type="button" class="btn" data-act="refreshNode">${icon("refresh")}Infos neu laden</button>
    </form></div></div>
    <div class="card" style="border-color:var(--bad)"><div class="card-head"><h2>Node entfernen</h2></div><div class="card-body">
      <p class="muted" style="margin-top:0">Entfernt den Node aus dem Panel. Der Agent auf dem Server bleibt installiert. Nur möglich, wenn keine Server mehr darauf liegen.</p>
      <button class="btn danger solid" data-act="deleteNode">Node entfernen</button></div></div></div>`);
  $("#node-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    await api(`/api/nodes/${id}`, { method: "PATCH", body: { name: f.name.value.trim(), host: f.host.value.trim(), enabled: f.enabled.checked } })
      .then(() => { toast("Gespeichert"); updateSidebar(); }).catch(fail);
  };
  S.handlers.refreshNode = async () => { await api(`/api/nodes/${id}/refresh`, { method: "POST" }); toast("Informationen aktualisiert"); route(); };
  S.handlers.deleteNode = async () => {
    if (!(await confirmBox("Node entfernen?", `<strong>${esc(n.name)}</strong> wird aus dem Panel entfernt.`, { danger: true, ok: "Entfernen", requireText: n.name }))) return;
    await api(`/api/nodes/${id}`, { method: "DELETE" });
    location.hash = "#/nodes";
  };
}

// ------------------------------------------------------------------ ISO-Bibliothek (Node)

const ISO_CHUNK = 32 * 1024 * 1024; // 32 MB je Anfrage – bleibt unter Proxy-Limits (z. B. Cloudflare 100 MB)

async function uploadIso(nodeId, file, name, onProgress, isCancelled) {
  const { upload_id: id } = await api(`/api/nodes/${nodeId}/isos/uploads`, { method: "POST" });
  let offset = 0;
  try {
    while (offset < file.size) {
      if (isCancelled()) throw new Error("Upload abgebrochen");
      const chunk = file.slice(offset, offset + ISO_CHUNK);
      let attempt = 0;
      for (;;) {
        try {
          const res = await fetch(`/api/nodes/${nodeId}/isos/uploads/${id}?offset=${offset}`, {
            method: "PUT", headers: { "X-PomBot": "1", "Content-Type": "application/octet-stream" }, body: chunk, credentials: "same-origin" });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(data.detail || `Fehler ${res.status}`);
          offset = data.offset;
          break;
        } catch (e) {
          if (++attempt >= 4 || isCancelled()) throw e;
          await new Promise((r) => setTimeout(r, 2000 * attempt));
          // Stand vom Node holen – vielleicht ist das Stück doch angekommen
          const st = await api(`/api/nodes/${nodeId}/isos/uploads/${id}`).catch(() => null);
          if (st) offset = st.offset;
        }
      }
      onProgress(offset, file.size);
    }
    return await api(`/api/nodes/${nodeId}/isos/uploads/${id}/finish`, { method: "POST", body: { name, size: file.size } });
  } catch (e) {
    api(`/api/nodes/${nodeId}/isos/uploads/${id}`, { method: "DELETE" }).catch(() => {});
    throw e;
  }
}

async function nodeIsoTab(n, shell) {
  const isoList = await api(`/api/nodes/${n.id}/isos`).catch((e) => { toast(e.message, "bad"); return []; });
  const total = isoList.reduce((a, i) => a + i.size, 0);
  S.handlers.isoDelete = async (ds) => {
    if (!(await confirmBox("ISO löschen?", `<code>${esc(ds.file)}</code> wird vom Node gelöscht.`, { danger: true, ok: "Löschen" }))) return;
    await api(`/api/nodes/${n.id}/isos/${encodeURIComponent(ds.file)}`, { method: "DELETE" });
    toast("ISO gelöscht");
    route();
  };
  shell(`<div class="grid grid-2" style="margin-bottom:16px">
    <div class="card"><div class="card-head"><h2>ISO hochladen</h2></div><div class="card-body">
      <label class="field"><span>Datei von deinem Rechner</span><input type="file" id="iso-file" accept=".iso,application/x-iso9660-image"></label>
      <label class="field"><span>Name auf dem Node</span><input type="text" id="iso-name" placeholder="wird aus dem Dateinamen übernommen"></label>
      <div id="iso-progress" class="hidden" style="margin-bottom:12px"></div>
      <div style="display:flex;gap:8px"><button class="btn primary" id="iso-upload">${icon("plus")}Hochladen</button><button class="btn hidden" id="iso-cancel">Abbrechen</button></div>
      <p class="muted small" style="margin-bottom:0">Wird in Stücken zu 32 MB übertragen – auch mehrere GB große ISOs sind kein Problem. Das Fenster muss bis zum Ende offen bleiben.</p>
    </div></div>
    <div class="card"><div class="card-head"><h2>Von URL laden</h2></div><div class="card-body">
      <label class="field"><span>Download-Adresse</span><input type="text" id="iso-url" placeholder="https://…/debian-13-amd64-netinst.iso"></label>
      <label class="field"><span>Name auf dem Node</span><input type="text" id="iso-url-name" placeholder="z. B. debian-13-netinst.iso"></label>
      <button class="btn" id="iso-fetch">Auf den Node laden</button>
      <p class="muted small" style="margin-bottom:0">Der Node lädt die Datei direkt herunter – meist deutlich schneller als der Umweg über deinen Rechner.</p>
    </div></div></div>
    <div class="card"><div class="card-head"><h2>ISOs auf ${esc(n.name)}</h2><span class="muted small">${plural(isoList.length, "Datei", "Dateien")} · ${fmtBytes(total)}</span></div>
      ${isoList.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Größe</th><th>Hinzugefügt</th><th>Herkunft</th><th></th></tr></thead><tbody>
        ${isoList.map((i) => `<tr><td class="mono small">${esc(i.file)}</td><td>${fmtBytes(i.size)}</td><td class="nowrap small">${fmtDate(new Date(i.added * 1000).toISOString())}</td>
          <td class="small" style="word-break:break-all;max-width:320px">${i.template ? "Vorlage (automatisch geladen)" : i.url ? esc(i.url) : "hochgeladen"}</td>
          <td class="right"><button class="btn sm danger" data-act="isoDelete" data-file="${esc(i.file)}">Löschen</button></td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">Noch keine ISOs. Lade eine hoch oder lass sie den Node von einer URL laden – danach steht sie beim Erstellen unter „Eigene ISO“ zur Wahl.</div>`}</div>`);

  const fileInput = $("#iso-file"), nameInput = $("#iso-name");
  fileInput.onchange = () => { if (fileInput.files[0] && !nameInput.value) nameInput.value = fileInput.files[0].name.replace(/[^A-Za-z0-9._+-]/g, "_"); };
  let cancelled = false;
  $("#iso-upload").onclick = async () => {
    const file = fileInput.files[0];
    if (!file) return toast("Bitte eine Datei auswählen");
    const name = (nameInput.value || file.name).trim();
    cancelled = false;
    S.live = false;
    const bar = $("#iso-progress");
    bar.classList.remove("hidden");
    $("#iso-upload").disabled = true;
    $("#iso-cancel").classList.remove("hidden");
    const started = Date.now();
    const show = (done, size) => {
      const secs = (Date.now() - started) / 1000;
      const rate = done / Math.max(secs, 1);
      const eta = rate ? Math.round((size - done) / rate) : 0;
      bar.innerHTML = meter("Übertragen", done, size, `${fmtBytes(done)} / ${fmtBytes(size)} · ${fmtBytes(rate)}/s · noch ca. ${eta > 60 ? Math.round(eta / 60) + " Min" : eta + " s"}`);
    };
    show(0, file.size);
    try {
      const r = await uploadIso(n.id, file, name, show, () => cancelled);
      toast(`${r.file} hochgeladen`);
      route();
    } catch (e) {
      bar.innerHTML = `<div class="alert bad">${esc(e.message)}</div>`;
      $("#iso-upload").disabled = false;
      $("#iso-cancel").classList.add("hidden");
    }
  };
  $("#iso-cancel").onclick = () => { cancelled = true; };
  $("#iso-fetch").onclick = async () => {
    const url = $("#iso-url").value.trim();
    const name = ($("#iso-url-name").value || url.split("/").pop().split("?")[0]).trim();
    if (!url) return toast("Bitte eine URL angeben");
    const r = await api(`/api/nodes/${n.id}/isos/fetch`, { method: "POST", body: { url, name } }).catch(fail);
    if (!r) return;
    await watchTask(r.task_id, `ISO laden: ${name}`);
    route();
  };
}

// ------------------------------------------------------------------ CD-Laufwerk (VM)

async function cdromCard(g) {
  if (g.type !== "kvm") return "";
  const [cd, isoList] = await Promise.all([
    api(`/api/guests/${g.id}/cdrom`).catch(() => null),
    api(`/api/nodes/${g.node_id}/isos`).catch(() => []),
  ]);
  if (!cd) return "";
  const media = cd.drives.find((d) => !d.seed);
  return `<div class="card"><div class="card-head"><h2>CD/DVD-Laufwerk</h2>${media && media.file ? badge(media.file, "info") : badge("leer")}</div><div class="card-body">
    <p class="muted" style="margin-top:0">ISO einlegen, z. B. für eine eigene Installation oder ein Rettungssystem. ISOs verwaltest du unter Nodes → ${esc(g.node)} → ISOs.</p>
    <label class="field"><span>ISO</span><select id="cd-iso"><option value="">– kein Medium (auswerfen) –</option>
      ${isoList.map((i) => `<option value="${esc(i.file)}" ${media && media.file === i.file ? "selected" : ""}>${esc(i.file)} (${fmtBytes(i.size)})</option>`).join("")}</select></label>
    <label class="check"><input type="checkbox" id="cd-boot" ${cd.boot_cdrom ? "checked" : ""}><span>Von CD starten (vor der Festplatte)</span></label>
    <button class="btn" data-act="cdApply">Übernehmen</button></div></div>`;
}

function bindCdrom(g) {
  S.handlers.cdApply = async () => {
    const r = await api(`/api/guests/${g.id}/cdrom`, { method: "POST", body: { iso_file: $("#cd-iso").value || null, boot_cdrom: $("#cd-boot").checked } });
    toast(r.restart_needed ? "Gespeichert – wird nach Stoppen und Starten der VM aktiv" : "Gespeichert");
    route();
  };
}

// ------------------------------------------------------------------ IP-Pools

function poolDialog(pool, nodes) {
  const p = pool || { name: "", mode: "routed", network: "", address_list: "", gateway: "", dns: "1.1.1.1, 8.8.8.8", bridge: "vmbr0",
    range_start: "", range_end: "", node_id: nodes.length === 1 ? nodes[0].id : null, admin_only: false };
  const st = { mode: p.mode || "bridged", source: p.address_list || !p.network ? "list" : "range" };
  formModal({
    title: pool ? `Pool ${pool.name} bearbeiten` : "IP-Pool anlegen",
    wide: true,
    submit: pool ? "Speichern" : "Anlegen",
    fields: `
      <div class="row"><label class="field"><span>Name</span><input type="text" name="name" value="${esc(p.name)}" placeholder="z. B. skrime Zusatz-IPs" required></label>
        <label class="field"><span>Node</span><select name="node_id" id="pool-node"><option value="">Alle Nodes</option>${nodes.map((n) => `<option value="${n.id}" ${n.id === p.node_id ? "selected" : ""}>${esc(n.name)}</option>`).join("")}</select></label></div>

      <div class="field"><span>Netzwerk-Modus</span>
        <div class="seg" id="pool-mode"><button type="button" data-mode="routed">Geroutet (empfohlen)</button><button type="button" data-mode="bridged">Bridge</button></div>
        <div class="hint" id="mode-hint"></div></div>

      <div class="field"><span>Adressen</span>
        <div class="seg" id="pool-source"><button type="button" data-source="list">Einzelne Adressen</button><button type="button" data-source="range">Ganzes Netz / Bereich</button></div></div>

      <div id="src-list">
        <label class="field"><span>IP-Adressen (eine pro Zeile oder als Bereich 1.2.3.10-15)</span>
          <textarea name="address_list" id="pool-list" rows="6" placeholder="185.12.34.56&#10;185.12.34.57 bc:24:11:11:dc:25&#10;91.200.1.20">${esc(p.address_list || "")}</textarea>
          <div class="hint">Ist eine IP beim Hoster an eine bestimmte MAC-Adresse gebunden, schreib die MAC dahinter
            (<code>77.90.52.70 bc:24:11:11:dc:25</code>) – der Server bekommt dann automatisch genau diese MAC.</div></label>
        <div style="display:flex;gap:10px;align-items:center;margin:-4px 0 14px;flex-wrap:wrap">
          <button type="button" class="btn sm" id="pool-detect">${icon("refresh")}IPs und Gateway vom Node erkennen</button>
          <span class="muted small" id="list-count"></span></div>
        <div id="detect-box" class="hidden" style="margin-bottom:14px"></div>
      </div>

      <div id="net-fields">
        <div class="row"><label class="field"><span id="net-label">Netz (CIDR)</span><input type="text" name="network" value="${esc(p.network || "")}" placeholder="203.0.113.0/24 oder 2001:db8::/64"></label>
          <label class="field" id="gw-field"><span>Gateway</span><input type="text" name="gateway" value="${esc(p.gateway || "")}" placeholder="203.0.113.1"><div class="hint">Darf außerhalb des Netzes liegen (wird dann on-link geroutet).</div></label></div>
        <div class="row" id="range-fields"><label class="field"><span>Erste vergebene IP (optional)</span><input type="text" name="range_start" value="${esc(p.range_start || "")}"></label>
          <label class="field"><span>Letzte vergebene IP (optional)</span><input type="text" name="range_end" value="${esc(p.range_end || "")}"></label></div>
      </div>

      <div class="row"><label class="field"><span>DNS-Server</span><input type="text" name="dns" value="${esc(p.dns)}" placeholder="1.1.1.1, 8.8.8.8"></label>
        <label class="field" id="bridge-field"><span>Bridge auf dem Node</span><input type="text" name="bridge" value="${esc(p.bridge === "pbr0" ? "vmbr0" : p.bridge)}"></label></div>
      <label class="check"><input type="checkbox" name="admin_only" ${p.admin_only ? "checked" : ""}><span>Nur für Administratoren</span></label>`,
    onReady: (mm) => {
      const form = $("#modal-form", mm);
      const nodeName = () => { const n = nodes.find((x) => x.id === +form.node_id.value) || (nodes.length === 1 ? nodes[0] : null); return n; };
      const sync = () => {
        $$("#pool-mode button", mm).forEach((b) => b.classList.toggle("active", b.dataset.mode === st.mode));
        $$("#pool-source button", mm).forEach((b) => b.classList.toggle("active", b.dataset.source === st.source));
        const routed = st.mode === "routed", list = st.source === "list";
        const n = nodeName();
        $("#mode-hint", mm).innerHTML = routed
          ? `Für Zusatz-IPs ohne eigene MAC-Adresse (z. B. skrime, Hetzner, OVH). Der Node leitet die IPs an die Server weiter; jeder Server bekommt seine IP als /32 mit Gateway <code>${esc((n && n.info && n.info.main_ipv4) || "Haupt-IP des Nodes")}</code> – bzw. IPv6 als /128 mit Gateway <code>fe80::1</code> (z. B. aus einem gerouteten /64). Beim Hoster muss nichts eingerichtet werden.`
          : "Server hängen direkt im Netz des Hosters. Nur nutzen, wenn du ein eigenes Netz/VLAN hast oder der Hoster für jede IP eine eigene MAC-Adresse vergibt.";
        $("#src-list", mm).classList.toggle("hidden", !list);
        $("#net-fields", mm).classList.toggle("hidden", routed && list);
        $("#range-fields", mm).classList.toggle("hidden", list);
        $("#gw-field", mm).classList.toggle("hidden", routed);
        $("#bridge-field", mm).classList.toggle("hidden", routed);
        $("#net-label", mm).textContent = list ? "Netz, zu dem die Adressen gehören (CIDR)" : "Netz (CIDR)";
        const count = form.address_list.value.split(/[\s,;]+/).filter((t) => t && !/^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$/i.test(t)).length;
        $("#list-count", mm).textContent = count ? `${count} ${count === 1 ? "Eintrag" : "Einträge"}` : "";
      };
      $$("#pool-mode button", mm).forEach((b) => b.onclick = () => { st.mode = b.dataset.mode; sync(); });
      $$("#pool-source button", mm).forEach((b) => b.onclick = () => { st.source = b.dataset.source; sync(); });
      form.addEventListener("input", sync);
      form.node_id.addEventListener("change", sync);
      $("#pool-detect", mm).onclick = async () => {
        const n = nodeName();
        const box = $("#detect-box", mm);
        if (!n) { box.innerHTML = `<div class="alert warn">Bitte zuerst oben den Node wählen.</div>`; box.classList.remove("hidden"); return; }
        box.innerHTML = `<span class="spinner"></span> Frage ${esc(n.name)} ab …`;
        box.classList.remove("hidden");
        let d;
        try { d = await api(`/api/nodes/${n.id}/network`); } catch (e) { box.innerHTML = `<div class="alert bad">${esc(e.message)}</div>`; return; }
        const current = new Set(form.address_list.value.split(/[\s,;]+/).filter(Boolean));
        const rows = d.addresses.filter((a) => !a.main);
        box.innerHTML = `<div class="card"><div class="card-body">
          <div class="small" style="margin-bottom:8px">Haupt-IP: <code>${esc(d.main_ipv4)}</code> auf <code>${esc(d.uplink)}</code> · Standard-Gateway: <code>${esc(d.default_gateway || "–")}</code></div>
          ${rows.length ? `<table><tbody>${rows.map((a) => {
            const taken = a.pool && !current.has(a.address);
            return `<tr><td style="width:30px"><input type="checkbox" class="det" value="${esc(a.address)}" data-net="${esc(a.network)}" ${taken ? "disabled" : a.public && !current.has(a.address) ? "checked" : ""} aria-label="${esc(a.address)}"></td>
              <td class="mono">${esc(a.address)}/${a.prefix}</td><td class="small">${esc(a.interface)}</td>
              <td class="small">${a.routed ? badge("an Server geroutet", "info") : a.pool ? badge(`in Pool ${a.pool}`, "plain") : a.public ? badge("öffentlich", "good") : badge("privat", "plain")}</td></tr>`;
          }).join("")}</tbody></table>
          <div style="margin-top:10px;display:flex;gap:8px"><button type="button" class="btn sm primary" id="det-apply">Ausgewählte übernehmen</button></div>`
          : `<div class="alert warn">Außer der Haupt-IP ist auf dem Node keine weitere IPv4-Adresse eingetragen. Deine Zusatz-IPs stehen im Kundenbereich deines Hosters (bei skrime unter dem Server → IP-Adressen) – bitte dort kopieren und oben einfügen.</div>`}
          <p class="muted small" style="margin:8px 0 0">Erkannt werden nur Adressen, die auf dem Server eingetragen sind. Im gerouteten Modus nimmt PomBot sie beim Vergeben automatisch von der Netzwerkkarte, damit sie beim Server ankommen.</p>
        </div></div>`;
        const apply = $("#det-apply", mm);
        if (apply) apply.onclick = () => {
          const picked = $$(".det:checked", mm);
          const merged = [...current, ...picked.map((c) => c.value)];
          form.address_list.value = [...new Set(merged)].join("\n");
          if (st.mode === "bridged" && picked.length) {
            if (!form.network.value) form.network.value = picked[0].dataset.net;
            if (!form.gateway.value && d.default_gateway) form.gateway.value = d.default_gateway;
          }
          st.source = "list";
          box.classList.add("hidden");
          sync();
          toast(`${picked.length} ${picked.length === 1 ? "Adresse" : "Adressen"} übernommen`);
        };
      };
      sync();
    },
    onSubmit: async (d, mm) => {
      d.node_id = d.node_id ? +d.node_id : null;
      d.mode = st.mode;
      if (st.source === "list") { d.range_start = null; d.range_end = null; if (st.mode === "routed") d.network = ""; }
      else d.address_list = "";
      if (st.mode === "routed") { d.gateway = null; d.bridge = "vmbr0"; }
      await api(pool ? `/api/pools/${pool.id}` : "/api/pools", { method: pool ? "PUT" : "POST", body: d });
      mm.close();
      toast("Gespeichert");
      route();
    },
  });
}

async function viewPools() {
  const [pools, nodes] = await Promise.all([api("/api/pools"), api("/api/nodes")]);
  S.handlers.addPool = () => poolDialog(null, nodes);
  S.handlers.editPool = (ds) => poolDialog(pools.find((p) => p.id === +ds.id), nodes);
  S.handlers.openPool = (ds) => { location.hash = `#/pool/${ds.id}`; };
  S.handlers.deletePool = async (ds) => {
    if (!(await confirmBox("Pool löschen?", "Reservierungen in diesem Pool werden ebenfalls entfernt.", { danger: true, ok: "Löschen" }))) return;
    await api(`/api/pools/${ds.id}`, { method: "DELETE" });
    route();
  };
  setMain(`${pageHead("IP-Pools", "Adressbereiche, aus denen neue Server automatisch eine IP bekommen", `<button class="btn primary" data-act="addPool">${icon("plus")}Pool anlegen</button>`)}
    <div class="card">${pools.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Modus</th><th>Adressen</th><th>Gateway</th><th>Node</th><th style="min-width:180px">Belegung</th><th></th></tr></thead><tbody>
      ${pools.map((p) => `<tr class="click" data-act="openPool" data-id="${p.id}"><td><strong>${esc(p.name)}</strong> ${p.admin_only ? badge("Admin", "plain") : ""}</td>
        <td>${p.mode === "routed" ? badge("Geroutet", "info") : badge(`Bridge ${p.bridge}`, "plain")}</td>
        <td class="mono small">${p.list_count ? `${p.list_count} einzelne` : esc(p.network)}</td>
        <td class="mono small">${p.mode === "routed" ? "Node-IP (auto)" : esc(p.gateway || "–")}</td><td>${esc(p.node || "Alle")}</td>
        <td>${meter("", p.used, p.size, `${p.used} / ${p.size}`)}</td>
        <td class="right nowrap"><button class="btn sm" data-act="editPool" data-id="${p.id}">Bearbeiten</button> <button class="btn sm danger" data-act="deletePool" data-id="${p.id}">Löschen</button></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">Noch keine IP-Pools. Lege einen Pool mit den IP-Adressen deines Hosters an – neue Server bekommen dann automatisch eine freie IP.<br><br><button class="btn primary" data-act="addPool">${icon("plus")}Pool anlegen</button></div>`}</div>`);
}

async function viewPool(params, m) {
  const id = m[1];
  const d = await api(`/api/pools/${id}/addresses`);
  const p = d.pool;
  S.handlers.reserve = () => formModal({
    title: "Adresse reservieren",
    fields: `<label class="field"><span>IP-Adresse</span><input type="text" name="address" value="${esc(d.next_free || "")}" required></label>
      <label class="field"><span>Notiz</span><input type="text" name="note" placeholder="z. B. Router, Mailserver"></label>
      <p class="muted small">Reservierte Adressen werden nie automatisch vergeben.</p>`,
    submit: "Reservieren",
    onSubmit: async (data, mm) => { await api(`/api/pools/${id}/reserve`, { method: "POST", body: data }); mm.close(); route(); },
  });
  S.handlers.release = async (ds) => {
    if (!(await confirmBox("Reservierung aufheben?", `${esc(ds.addr)} wird wieder frei.`, { ok: "Freigeben" }))) return;
    await api(`/api/pools/${id}/addresses/${ds.id}`, { method: "DELETE" });
    route();
  };
  setMain(`${pageHead(`Pool ${esc(p.name)}`, p.mode === "routed" ? `Geroutet · ${p.list_count ? `${p.list_count} einzelne Adressen` : esc(p.network)} · Node ${esc(p.node || "–")} · Gateway = Node-IP` : `${esc(p.network)} · Gateway ${esc(p.gateway || "–")} · Bridge ${esc(p.bridge)}`, `<a class="btn" href="#/pools">Zurück</a><button class="btn primary" data-act="reserve">Adresse reservieren</button>`)}
    <div class="grid grid-3" style="margin-bottom:16px">
      <div class="card stat"><div class="label">Belegt</div><div class="value">${p.used} / ${p.size}</div></div>
      <div class="card stat"><div class="label">Frei</div><div class="value">${p.size - p.used}</div></div>
      <div class="card stat"><div class="label">Nächste freie Adresse</div><div class="value mono" style="font-size:20px">${esc(d.next_free || "–")}</div></div>
    </div>
    <div class="card">${d.addresses.length ? `<div class="table-wrap"><table><thead><tr><th>Adresse</th><th>Feste MAC</th><th>Verwendung</th><th>Besitzer</th><th>Notiz</th><th></th></tr></thead><tbody>
      ${d.addresses.map((a) => `<tr><td class="mono">${esc(a.address)}</td><td class="mono small">${esc(a.mac || "–")}</td>
        <td>${a.guest ? `<a href="#/guest/${a.guest_id}">${esc(a.guest)}</a>` : a.reserved ? badge("Reserviert", "warn") : badge("Verwaist", "bad")}</td>
        <td>${esc(a.owner || "–")}</td><td>${esc(a.note)}</td>
        <td class="right">${a.guest ? "" : `<button class="btn sm" data-act="release" data-id="${a.id}" data-addr="${esc(a.address)}">Freigeben</button>`}</td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">Noch keine Adressen vergeben.</div>`}</div>`);
}

// ------------------------------------------------------------------ Benutzer

async function viewUsers() {
  const users = await api("/api/users");
  const pending = users.filter((u) => !u.active);
  S.handlers.approve = async (ds) => { await api(`/api/users/${ds.id}`, { method: "PATCH", body: { active: true } }); toast("Benutzer freigeschaltet"); route(); };
  S.handlers.addUser = () => formModal({
    title: "Lokalen Benutzer anlegen",
    fields: `<label class="field"><span>Benutzername</span><input type="text" name="username" required></label>
      <label class="field"><span>Passwort</span><input type="text" name="password" placeholder="Leer lassen = zufällig erzeugen"></label>
      <label class="field"><span>Rolle</span><select name="role"><option value="user">Benutzer</option><option value="admin">Administrator</option></select></label>
      <p class="muted small">Benutzer können sich auch einfach mit Discord anmelden – dann wird das Konto automatisch angelegt.</p>`,
    submit: "Anlegen",
    onSubmit: async (d, mm) => {
      const r = await api("/api/users", { method: "POST", body: { ...d, password: d.password || null } });
      mm.close();
      showSecret("Benutzer angelegt", `Passwort für <strong>${esc(d.username)}</strong>:`, r.password);
      route();
    },
  });
  S.handlers.editUser = (ds) => {
    const u = users.find((x) => x.id === +ds.id);
    formModal({
      title: `Benutzer ${u.username}`, wide: true,
      fields: `<div class="row"><label class="field"><span>Benutzername</span><input type="text" name="username" value="${esc(u.username)}"></label>
          <label class="field"><span>Rolle</span><select name="role"><option value="user" ${u.role === "user" ? "selected" : ""}>Benutzer</option><option value="admin" ${u.role === "admin" ? "selected" : ""}>Administrator</option></select></label></div>
        <label class="check"><input type="checkbox" name="active" ${u.active ? "checked" : ""}><span>Konto aktiv</span></label>
        <h3 style="margin:8px 0 12px">Kontingent</h3>
        <div class="row"><label class="field"><span>Max. Server</span><input type="number" name="max_guests" value="${u.quota.guests}" min="0"></label>
          <label class="field"><span>Max. CPU-Kerne</span><input type="number" name="max_cores" value="${u.quota.cores}" min="0"></label>
          <label class="field"><span>Max. IP-Adressen</span><input type="number" name="max_ips" value="${u.quota.ips}" min="0"></label></div>
        <div class="row"><label class="field"><span>Max. RAM (MB)</span><input type="number" name="max_memory_mb" value="${u.quota.memory_mb}" min="0" step="256"></label>
          <label class="field"><span>Max. Speicher (GB)</span><input type="number" name="max_disk_gb" value="${u.quota.disk_gb}" min="0"></label></div>
        <label class="check"><input type="checkbox" name="reset_password"><span>Neues Passwort erzeugen</span></label>
        <p class="muted small">Aktuell belegt: ${u.usage.guests} Server · ${u.usage.cores} CPU · ${fmtMB(u.usage.memory_mb)} · ${u.usage.disk_gb} GB · ${u.usage.ips} IPs</p>`,
      onSubmit: async (d, mm) => {
        const r = await api(`/api/users/${u.id}`, { method: "PATCH", body: d });
        mm.close();
        if (r.password) showSecret("Neues Passwort", `Passwort für <strong>${esc(r.username)}</strong>:`, r.password);
        else toast("Gespeichert");
        route();
      },
    });
  };
  S.handlers.deleteUser = async (ds) => {
    if (!(await confirmBox("Benutzer löschen?", `<strong>${esc(ds.name)}</strong> wird gelöscht.`, { danger: true, ok: "Löschen" }))) return;
    await api(`/api/users/${ds.id}`, { method: "DELETE" });
    route();
  };
  const avatar = (u) => `<span class="avatar">${u.avatar_url ? `<img src="${esc(u.avatar_url)}" alt="">` : esc(u.username.slice(0, 2).toUpperCase())}</span>`;
  setMain(`${pageHead("Benutzer", `${plural(users.length, "Benutzer", "Benutzer")}`, `<button class="btn primary" data-act="addUser">${icon("plus")}Lokalen Benutzer anlegen</button>`)}
    ${pending.length ? `<div class="card" style="margin-bottom:16px;border-color:var(--warn)"><div class="card-head"><h2>Warten auf Freischaltung</h2></div><div class="table-wrap"><table><tbody>
      ${pending.map((u) => `<tr><td style="width:44px">${avatar(u)}</td><td><strong>${esc(u.username)}</strong><div class="muted small">${u.discord ? "Discord · " : ""}registriert ${fmtDate(u.created_at)}</div></td>
        <td class="right"><button class="btn sm primary" data-act="approve" data-id="${u.id}">Freischalten</button> <button class="btn sm danger" data-act="deleteUser" data-id="${u.id}" data-name="${esc(u.username)}">Ablehnen</button></td></tr>`).join("")}
    </tbody></table></div></div>` : ""}
    <div class="card"><div class="table-wrap"><table><thead><tr><th></th><th>Benutzer</th><th>Rolle</th><th>Status</th><th>Anmeldung</th><th>Server</th><th>Ressourcen</th><th>Letzter Login</th><th></th></tr></thead><tbody>
      ${users.map((u) => `<tr><td style="width:44px">${avatar(u)}</td><td><strong>${esc(u.username)}</strong>${u.email ? `<div class="muted small">${esc(u.email)}</div>` : ""}</td>
        <td>${u.role === "admin" ? badge("Admin", "info") : badge("Benutzer", "plain")}</td>
        <td>${u.active ? badge("Aktiv", "good") : badge("Gesperrt", "warn")}</td>
        <td class="small">${[u.discord ? "Discord" : "", u.has_password ? "Passwort" : ""].filter(Boolean).join(" + ") || "–"}</td>
        <td class="num">${u.usage.guests} / ${u.quota.guests}</td>
        <td class="small nowrap">${u.usage.cores}/${u.quota.cores} CPU · ${fmtMB(u.usage.memory_mb)}/${fmtMB(u.quota.memory_mb)}</td>
        <td class="small nowrap">${fmtDate(u.last_login)}</td>
        <td class="right nowrap"><button class="btn sm" data-act="editUser" data-id="${u.id}">Bearbeiten</button>
          ${u.id !== S.me.id ? `<button class="btn sm danger" data-act="deleteUser" data-id="${u.id}" data-name="${esc(u.username)}">Löschen</button>` : ""}</td></tr>`).join("")}
    </tbody></table></div></div>`);
}

// ------------------------------------------------------------------ Vorlagen

function templateDialog(t) {
  const x = t || { name: "", type: "kvm", source: "cloud", url: "", lxc_dist: "", lxc_release: "", os_family: "linux", min_disk_gb: 5, enabled: true, sort: 100 };
  formModal({
    title: t ? "Vorlage bearbeiten" : "Vorlage hinzufügen", wide: true,
    fields: `<div class="row"><label class="field"><span>Name</span><input type="text" name="name" value="${esc(x.name)}" required></label>
        <label class="field"><span>Art</span><select name="kind" id="tpl-kind">
          <option value="kvm:cloud" ${x.type === "kvm" && x.source === "cloud" ? "selected" : ""}>VM · Cloud-Image (automatisch eingerichtet)</option>
          <option value="kvm:iso" ${x.source === "iso" ? "selected" : ""}>VM · ISO (manuelle Installation über Konsole)</option>
          <option value="lxc:lxc" ${x.type === "lxc" ? "selected" : ""}>Container (linuxcontainers.org)</option></select></label></div>
      <div id="tpl-url"><label class="field"><span>Download-URL</span><input type="text" name="url" value="${esc(x.url || "")}" placeholder="https://…"><div class="hint">Cloud-Images im qcow2/raw-Format mit cloud-init, z. B. von cloud.debian.org oder cloud-images.ubuntu.com.</div></label></div>
      <div id="tpl-lxc" class="row"><label class="field"><span>Distribution</span><input type="text" name="lxc_dist" value="${esc(x.lxc_dist || "")}" placeholder="debian"></label>
        <label class="field"><span>Release</span><input type="text" name="lxc_release" value="${esc(x.lxc_release || "")}" placeholder="bookworm"></label></div>
      <div class="row"><label class="field"><span>Symbol</span><select name="os_family">${["debian", "ubuntu", "windows", "linux"].map((o) => `<option ${o === x.os_family ? "selected" : ""}>${o}</option>`).join("")}</select></label>
        <label class="field"><span>Mindestgröße (GB)</span><input type="number" name="min_disk_gb" value="${x.min_disk_gb}" min="1"></label>
        <label class="field"><span>Sortierung</span><input type="number" name="sort" value="${x.sort}"></label></div>
      ${x.source === "iso" || !t ? `<p class="muted small">Hinweis: Bei ISO-Vorlagen wird die IP zwar reserviert, muss aber während der Installation manuell eingetragen werden.</p>` : ""}
      <label class="check"><input type="checkbox" name="enabled" ${x.enabled ? "checked" : ""}><span>Für Benutzer sichtbar</span></label>`,
    onReady: (mm) => {
      const sync = () => {
        const lxc = $("#tpl-kind", mm).value.startsWith("lxc");
        $("#tpl-url", mm).classList.toggle("hidden", lxc);
        $("#tpl-lxc", mm).classList.toggle("hidden", !lxc);
      };
      $("#tpl-kind", mm).addEventListener("change", sync);
      sync();
    },
    onSubmit: async (d, mm) => {
      const [type, source] = d.kind.split(":");
      delete d.kind;
      Object.assign(d, { type, source });
      if (type === "lxc") d.url = null; else { d.lxc_dist = null; d.lxc_release = null; }
      await api(t ? `/api/templates/${t.id}` : "/api/templates", { method: t ? "PUT" : "POST", body: d });
      mm.close();
      route();
    },
  });
}

async function viewTemplates() {
  const templates = await api("/api/templates");
  S.handlers.addTpl = () => templateDialog(null);
  S.handlers.editTpl = (ds) => templateDialog(templates.find((t) => t.id === +ds.id));
  S.handlers.deleteTpl = async (ds) => {
    if (!(await confirmBox("Vorlage löschen?", "Bestehende Server bleiben unverändert.", { danger: true, ok: "Löschen" }))) return;
    await api(`/api/templates/${ds.id}`, { method: "DELETE" });
    route();
  };
  setMain(`${pageHead("Vorlagen", "Betriebssysteme, die beim Erstellen zur Auswahl stehen", `<button class="btn primary" data-act="addTpl">${icon("plus")}Vorlage hinzufügen</button>`)}
    <div class="card"><div class="table-wrap"><table><thead><tr><th>Name</th><th>Typ</th><th>Quelle</th><th>Min.</th><th>Status</th><th></th></tr></thead><tbody>
      ${templates.map((t) => `<tr><td><strong>${esc(t.name)}</strong></td><td><span class="type-tag">${t.type === "kvm" ? "VM" : "CT"}</span></td>
        <td class="small mono" style="word-break:break-all;max-width:420px">${esc(t.type === "lxc" ? `${t.lxc_dist} ${t.lxc_release}` : t.url)}${t.source === "iso" ? " (ISO)" : ""}</td>
        <td>${t.min_disk_gb} GB</td><td>${t.enabled ? badge("Aktiv", "good") : badge("Ausgeblendet")}</td>
        <td class="right nowrap"><button class="btn sm" data-act="editTpl" data-id="${t.id}">Bearbeiten</button> <button class="btn sm danger" data-act="deleteTpl" data-id="${t.id}">Löschen</button></td></tr>`).join("")}
    </tbody></table></div></div>`);
}

// ------------------------------------------------------------------ Adminbereich: Einstellungen

async function viewSettings(params, m) {
  const tab = m[1] || "general";
  const base = "#/settings";
  const head = pageHead("Einstellungen", "Adminbereich – Panel, Anmeldung, Cloudflare und Domain")
    + tabs(base, tab, [["general", "Allgemein"], ["discord", "Discord-Login"], ["backups", "Backup-Speicher"], ["storage", "Gemeinsamer Speicher"], ["cloudflare", "Cloudflare-DNS"], ["domain", "Domain & HTTPS"]]);
  if (tab === "cloudflare") return settingsCloudflare(head);
  if (tab === "backups") return settingsBackupTargets(head);
  if (tab === "storage") return settingsStorages(head);
  if (tab === "domain") return settingsDomain(head);
  const s = await api("/api/admin/settings");
  const save = async (body) => {
    await api("/api/admin/settings", { method: "PUT", body });
    toast("Gespeichert – gilt sofort");
    route(true);
  };
  if (tab === "discord") {
    setMain(head + `<div class="grid grid-2"><div class="card"><div class="card-head"><h2>Discord-Login</h2>${s.discord_client_id && s.discord_client_secret_set ? badge("Aktiv", "good") : badge("Nicht eingerichtet")}</div><div class="card-body">
      <form id="set-form">
        <label class="field"><span>Client-ID</span><input type="text" name="discord_client_id" value="${esc(s.discord_client_id)}"></label>
        <label class="field"><span>Client-Secret</span><input type="password" name="discord_client_secret" autocomplete="off" placeholder="${s.discord_client_secret_set ? "gesetzt – leer lassen, um es zu behalten" : ""}"></label>
        <label class="field"><span>Nur Mitglieder dieses Discord-Servers (Server-ID, optional)</span><input type="text" name="discord_guild_id" value="${esc(s.discord_guild_id)}"></label>
        <label class="field"><span>Diese Discord-User-IDs werden automatisch Admin (Komma getrennt)</span><input type="text" name="discord_admin_ids" value="${esc(s.discord_admin_ids)}"></label>
        <button class="btn primary" type="submit">Speichern</button></form></div></div>
      <div class="card"><div class="card-head"><h2>So richtest du es ein</h2></div><div class="card-body small">
        <ol style="padding-left:18px;margin:0">
          <li><a href="https://discord.com/developers/applications" target="_blank" rel="noopener">discord.com/developers/applications</a> → <em>New Application</em></li>
          <li>Links <em>OAuth2</em> → bei <em>Redirects</em> diese Adresse eintragen:
            <div class="secret" style="margin:8px 0"><span>${esc(s.discord_redirect_uri)}</span><button class="btn sm" type="button" data-copy="${esc(s.discord_redirect_uri)}">Kopieren</button></div></li>
          <li><em>Client ID</em> und <em>Client Secret</em> (Reset Secret) hier eintragen und speichern.</li>
        </ol>
        <p class="muted" style="margin-bottom:0">Ändert sich die Adresse des Panels (Domain), muss die Redirect-URL bei Discord angepasst werden.</p></div></div></div>`);
  } else {
    setMain(head + `<div class="card" style="max-width:860px"><div class="card-body"><form id="set-form">
      <h3 style="margin:0 0 12px">Anmeldung</h3>
      <label class="field"><span>Neue Discord-Benutzer</span><select name="registration">
        <option value="approval" ${s.registration === "approval" ? "selected" : ""}>Müssen von einem Admin freigeschaltet werden</option>
        <option value="open" ${s.registration === "open" ? "selected" : ""}>Dürfen sofort loslegen</option>
        <option value="closed" ${s.registration === "closed" ? "selected" : ""}>Keine neuen Konten</option></select></label>
      <h3 style="margin:18px 0 12px">Standard-Kontingent für neue Benutzer</h3>
      <div class="row"><label class="field"><span>Server</span><input type="number" name="default_max_guests" value="${s.default_max_guests}" min="0"></label>
        <label class="field"><span>CPU-Kerne</span><input type="number" name="default_max_cores" value="${s.default_max_cores}" min="0"></label>
        <label class="field"><span>IP-Adressen</span><input type="number" name="default_max_ips" value="${s.default_max_ips}" min="0"></label></div>
      <div class="row"><label class="field"><span>RAM (MB)</span><input type="number" name="default_max_memory_mb" value="${s.default_max_memory_mb}" min="0" step="256"></label>
        <label class="field"><span>Speicher (GB)</span><input type="number" name="default_max_disk_gb" value="${s.default_max_disk_gb}" min="0"></label></div>
      <h3 style="margin:18px 0 12px">Netzwerk & Sicherheit</h3>
      <label class="field"><span>Standard-DNS-Server für neue Server</span><input type="text" name="default_dns" value="${esc(s.default_dns)}"></label>
      <label class="check"><input type="checkbox" name="antispoof" ${s.antispoof ? "checked" : ""}><span>Spoofing-Schutz für neue Server (Server senden nur mit eigenen IPs)</span></label>
      <label class="field" style="max-width:320px"><span>Max. automatische Backups pro Server (Benutzer)</span><input type="number" name="max_auto_backups" value="${s.max_auto_backups}" min="1"></label>
      <button class="btn primary" type="submit">Speichern</button></form></div></div>`);
  }
  $("#set-form").onsubmit = async (e) => {
    e.preventDefault();
    const body = {};
    for (const el of e.target.elements) {
      if (!el.name) continue;
      body[el.name] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
    }
    await save(body).catch(fail);
  };
}

function recordDialog(zone, rec) {
  const r = rec || { type: "A", name: "", content: "", ttl: 1, proxied: false, priority: null, comment: "" };
  formModal({
    title: rec ? `Eintrag bearbeiten – ${zone.name}` : `Neuer DNS-Eintrag – ${zone.name}`,
    fields: `<div class="row"><label class="field" style="max-width:130px"><span>Typ</span><select name="type">${["A", "AAAA", "CNAME", "TXT", "MX", "SRV", "CAA", "NS", "PTR"].map((t) => `<option ${t === r.type ? "selected" : ""}>${t}</option>`).join("")}</select></label>
        <label class="field"><span>Name</span><input type="text" name="name" value="${esc(r.name)}" placeholder="www.${esc(zone.name)} oder ${esc(zone.name)}" required></label></div>
      <label class="field"><span>Inhalt</span><input type="text" name="content" value="${esc(r.content)}" placeholder="IP-Adresse, Ziel-Hostname oder Text" required></label>
      <div class="row"><label class="field"><span>TTL (1 = automatisch)</span><input type="number" name="ttl" value="${r.ttl || 1}" min="1"></label>
        <label class="field"><span>Priorität (nur MX/SRV)</span><input type="number" name="priority" value="${r.priority ?? ""}" min="0"></label></div>
      <label class="field"><span>Kommentar</span><input type="text" name="comment" value="${esc(r.comment || "")}" maxlength="100"></label>
      <label class="check"><input type="checkbox" name="proxied" ${r.proxied ? "checked" : ""}><span>Über Cloudflare-Proxy (oranges Wölkchen) – nur für A/AAAA/CNAME, nur Web-Verkehr</span></label>`,
    submit: rec ? "Speichern" : "Anlegen",
    onSubmit: async (d, mm) => {
      if (d.priority === null || d.priority === "") delete d.priority;
      await api(`/api/admin/cloudflare/zones/${zone.id}/records${rec ? `/${rec.id}` : ""}`, { method: rec ? "PUT" : "POST", body: d });
      mm.close();
      toast("Gespeichert");
      route();
    },
  });
}

async function settingsCloudflare(head) {
  const s = await api("/api/admin/settings");
  let zones = [], zoneErr = "";
  if (s.cloudflare_token_set) zones = await api("/api/admin/cloudflare/zones").catch((e) => { zoneErr = e.message; return []; });
  const zoneId = new URLSearchParams(location.hash.split("?")[1] || "").get("zone") || (zones[0] && zones[0].id);
  const zone = zones.find((z) => z.id === zoneId);
  let records = [];
  if (zone) records = await api(`/api/admin/cloudflare/zones/${zone.id}/records`).catch((e) => { zoneErr = e.message; return []; });
  S.handlers.cfAdd = () => recordDialog(zone);
  S.handlers.cfEdit = (ds) => recordDialog(zone, records.find((r) => r.id === ds.id));
  S.handlers.cfDel = async (ds) => {
    const r = records.find((x) => x.id === ds.id);
    if (!(await confirmBox("DNS-Eintrag löschen?", `<code>${esc(r.type)} ${esc(r.name)} → ${esc(r.content)}</code>`, { danger: true, ok: "Löschen" }))) return;
    await api(`/api/admin/cloudflare/zones/${zone.id}/records/${r.id}`, { method: "DELETE" });
    route();
  };
  S.handlers.cfVerify = async () => { const r = await api("/api/admin/cloudflare/verify", { method: "POST" }); toast(`Token gültig – ${r.zones} Zone(n) gefunden`); route(); };
  setMain(head + `<div class="grid grid-2" style="margin-bottom:16px">
    <div class="card"><div class="card-head"><h2>API-Token</h2>${s.cloudflare_token_set ? badge("Hinterlegt", "good") : badge("Fehlt", "warn")}</div><div class="card-body">
      <form id="cf-form"><label class="field"><span>Cloudflare-API-Token</span><input type="password" name="cloudflare_token" autocomplete="off" placeholder="${s.cloudflare_token_set ? "hinterlegt – leer lassen, um ihn zu behalten" : "Token einfügen"}"></label>
      <div style="display:flex;gap:8px"><button class="btn primary" type="submit">Speichern</button>${s.cloudflare_token_set ? `<button type="button" class="btn" data-act="cfVerify">Token prüfen</button>` : ""}</div></form></div></div>
    <div class="card"><div class="card-head"><h2>Token erstellen</h2></div><div class="card-body small">
      <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" rel="noopener">dash.cloudflare.com → Mein Profil → API-Token</a> → <em>Token erstellen</em> → Vorlage <em>„DNS-Zone bearbeiten“</em>.
      Berechtigungen: <code>Zone → DNS → Bearbeiten</code> und <code>Zone → Zone → Lesen</code>, bei <em>Zonenressourcen</em> die gewünschten Domains wählen.
      <p class="muted" style="margin-bottom:0">Der Token wird nur im Panel gespeichert und nie wieder angezeigt. Damit kann PomBot DNS-Einträge verwalten, Servern Domains zuweisen und Let's-Encrypt-Zertifikate holen.</p></div></div></div>
    ${zoneErr ? `<div class="alert bad" style="margin-bottom:16px">${esc(zoneErr)}</div>` : ""}
    ${s.cloudflare_token_set && zones.length ? `<div class="card"><div class="card-head"><h2>DNS-Einträge</h2>
        <select id="cf-zone" style="max-width:280px">${zones.map((z) => `<option value="${z.id}" ${z.id === zoneId ? "selected" : ""}>${esc(z.name)}${z.status !== "active" ? ` (${esc(z.status)})` : ""}</option>`).join("")}</select>
        <button class="btn sm primary" data-act="cfAdd">${icon("plus")}Eintrag</button></div>
      ${records.length ? `<div class="table-wrap"><table><thead><tr><th>Typ</th><th>Name</th><th>Inhalt</th><th>Proxy</th><th>TTL</th><th></th></tr></thead><tbody>
        ${records.map((r) => `<tr><td><span class="type-tag">${esc(r.type)}</span></td><td class="mono small">${esc(r.name)}</td>
          <td class="mono small" style="word-break:break-all;max-width:360px">${esc(r.content)}${r.priority != null && ["MX", "SRV"].includes(r.type) ? ` <span class="muted">(Prio ${r.priority})</span>` : ""}</td>
          <td>${r.proxied ? badge("Proxy", "warn") : `<span class="muted small">nur DNS</span>`}</td><td class="small">${r.ttl === 1 ? "auto" : r.ttl}</td>
          <td class="right nowrap"><button class="btn sm" data-act="cfEdit" data-id="${r.id}">Bearbeiten</button> <button class="btn sm danger" data-act="cfDel" data-id="${r.id}">Löschen</button></td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">Keine Einträge in dieser Zone.</div>`}</div>` : ""}`);
  $("#cf-form").onsubmit = async (e) => {
    e.preventDefault();
    const token = e.target.cloudflare_token.value.trim();
    if (!token) return toast("Bitte einen Token einfügen");
    await api("/api/admin/settings", { method: "PUT", body: { cloudflare_token: token } });
    const r = await api("/api/admin/cloudflare/verify", { method: "POST" }).catch((err) => { toast(err.message, "bad"); return null; });
    if (r) toast(`Token gespeichert – ${r.zones} Zone(n) gefunden`);
    route();
  };
  if ($("#cf-zone")) $("#cf-zone").onchange = (e) => { location.hash = `#/settings/cloudflare?zone=${e.target.value}`; };
}

async function settingsDomain(head) {
  const d = await api("/api/admin/domain");
  const c = d.certificate;
  const selfSigned = !c || !/Let's Encrypt|\(STAGING\)/i.test(c.issuer);
  S.handlers.domainReset = async () => {
    if (!(await confirmBox("Domain entfernen?", "Das Panel ist danach wieder über die IP-Adresse mit dem selbstsignierten Zertifikat erreichbar (Port aus panel.env).", { danger: true, ok: "Zurücksetzen" }))) return;
    const r = await api("/api/admin/domain/reset", { method: "POST" });
    toast(r.restarting ? "Panel startet neu …" : "Bitte das Panel neu starten");
  };
  setMain(head + `<div class="grid grid-2">
    <div class="card"><div class="card-head"><h2>Aktuell</h2></div><div class="card-body"><dl class="kv">
      <dt>Adresse</dt><dd><a href="${esc(d.base_url)}">${esc(d.base_url)}</a></dd>
      <dt>Port</dt><dd>${d.port}</dd>
      <dt>Zertifikat</dt><dd>${c && /Let's Encrypt/i.test(c.issuer) ? `${badge(c.days_left > 14 ? "Let's Encrypt" : "läuft bald ab", c.days_left > 14 ? "good" : "warn")}<div class="small muted">${esc(c.domains.join(", "))} · gültig bis ${fmtDate(c.not_after)} (${c.days_left} Tage) · wird automatisch verlängert</div>`
        : badge("Selbstsigniert (Browser-Warnung)", "warn")}</dd>
      <dt>Öffentliche IP</dt><dd class="mono">${esc(d.public_ip || "–")}</dd></dl>
      ${d.acme_enabled ? `<button class="btn danger" style="margin-top:14px" data-act="domainReset">Domain entfernen</button>` : ""}</div></div>
    <div class="card"><div class="card-head"><h2>${d.acme_enabled ? "Domain ändern" : "Domain einrichten"}</h2></div><div class="card-body">
      ${d.cloudflare ? "" : `<div class="alert warn" style="margin-bottom:14px">Zuerst unter <a href="#/settings/cloudflare">Cloudflare-DNS</a> einen API-Token hinterlegen – darüber wird das Zertifikat bestätigt.</div>`}
      ${d.restart_supported ? "" : `<div class="alert" style="margin-bottom:14px">Hinweis: Das Panel läuft nicht als Systemdienst – nach dem Einrichten bitte selbst neu starten.</div>`}
      <form id="dom-form">
        <label class="field"><span>Domain für das Panel</span><input type="text" name="domain" value="${esc(d.domain || "")}" placeholder="panel.deinedomain.de" required></label>
        <div class="row"><label class="field"><span>Port</span><select name="port"><option value="443" ${d.port === 443 || !d.acme_enabled ? "selected" : ""}>443 (Standard – https://domain)</option><option value="8443" ${d.acme_enabled && d.port === 8443 ? "selected" : ""}>8443</option></select></label>
          <label class="field"><span>E-Mail für Let's Encrypt (optional)</span><input type="email" name="email" value="${esc(d.acme_email || "")}"></label></div>
        <label class="check"><input type="checkbox" name="create_dns" checked><span>DNS-Eintrag in Cloudflare automatisch setzen</span></label>
        <label class="field" id="dom-ip"><span>IP-Adresse des Panels</span><input type="text" name="dns_ip" value="${esc(d.public_ip || "")}"></label>
        <label class="check"><input type="checkbox" name="staging"><span>Testmodus (Let's-Encrypt-Staging, Zertifikat ist nicht vertrauenswürdig)</span></label>
        <div class="alert" style="margin-bottom:14px">Ablauf: DNS-Eintrag setzen → Zertifikat per Cloudflare-DNS bestätigen (Port 80 muss <strong>nicht</strong> offen sein) → Panel startet unter der neuen Adresse neu.
          ${selfSigned ? "" : ""}Vergiss nicht, den gewählten Port in der Firewall zu öffnen und die Discord-Redirect-URL anzupassen.</div>
        <button class="btn primary" type="submit" ${d.cloudflare ? "" : "disabled"}>Domain einrichten</button></form></div></div></div>`);
  const f = $("#dom-form");
  f.create_dns.onchange = () => $("#dom-ip").classList.toggle("hidden", !f.create_dns.checked);
  f.onsubmit = async (e) => {
    e.preventDefault();
    const body = { domain: f.domain.value.trim(), port: +f.port.value, email: f.email.value.trim(), create_dns: f.create_dns.checked,
      dns_ip: f.dns_ip.value.trim() || null, staging: f.staging.checked };
    const r = await api("/api/admin/domain", { method: "POST", body }).catch(fail);
    if (!r) return;
    const status = await watchTask(r.task_id, `Domain ${body.domain} einrichten`);
    if (status === "ok") {
      const url = `https://${body.domain}${body.port === 443 ? "" : ":" + body.port}`;
      modal({ title: "Fertig", body: `<p style="margin-top:0">Das Panel startet neu und ist gleich erreichbar unter:</p><p><a class="btn primary" href="${esc(url)}">${esc(url)}</a></p>
        <p class="muted small">Die DNS-Änderung kann einige Minuten brauchen. Passe die Redirect-URL bei Discord an: <code>${esc(url)}/api/auth/discord/callback</code></p>`,
        foot: `<button class="btn" data-close>Schließen</button>` });
    }
  };
}

// ------------------------------------------------------------------ Aufgaben, Protokoll

async function viewTasks(params, m, silent, seq) {
  const tasks = await api("/api/tasks?limit=200");
  if (isStale(seq)) return;
  S.handlers.task = (ds) => watchTask(ds.id, ds.title);
  setMain(`${pageHead("Aufgaben", "Klicke auf eine Aufgabe, um das Protokoll zu sehen")}<div class="card">${taskRows(tasks)}</div>`);
  return { live: true };
}

async function viewAudit() {
  const rows = await api("/api/audit?limit=500");
  setMain(`${pageHead("Protokoll", "Wer hat wann was getan")}
    <div class="card"><div class="table-wrap"><table><thead><tr><th>Zeit</th><th>Benutzer</th><th>Aktion</th><th>Details</th><th>IP</th></tr></thead><tbody>
      ${rows.map((a) => `<tr><td class="nowrap small">${fmtDate(a.created_at)}</td><td>${esc(a.username || "–")}</td><td><code>${esc(a.action)}</code></td><td class="small">${esc(a.detail)}</td><td class="mono small">${esc(a.ip)}</td></tr>`).join("")
        || `<tr><td colspan="5" class="empty">Noch keine Einträge</td></tr>`}
    </tbody></table></div></div>`);
}

// ------------------------------------------------------------------ Konto

async function viewAccount(params) {
  const me = await api("/api/me");
  S.me = { ...S.me, ...me };
  const err = params.get("error");
  S.handlers.unlink = async () => {
    if (!(await confirmBox("Discord trennen?", "Du kannst dich danach nur noch mit Passwort anmelden.", { ok: "Trennen" }))) return;
    await api("/api/me/unlink-discord", { method: "POST" });
    route();
  };
  setMain(`${pageHead("Mein Konto")}
    ${err ? `<div class="alert bad" style="margin-bottom:16px">${esc(err)}</div>` : ""}
    <div class="grid grid-2">
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Profil</h2></div><div class="card-body"><dl class="kv">
          <dt>Benutzername</dt><dd>${esc(me.username)}</dd><dt>Rolle</dt><dd>${me.role === "admin" ? "Administrator" : "Benutzer"}</dd>
          <dt>E-Mail</dt><dd>${esc(me.email || "–")}</dd><dt>Mitglied seit</dt><dd>${fmtDate(me.created_at)}</dd>
          <dt>Discord</dt><dd>${me.discord ? `${badge("Verknüpft", "good")} <button class="btn sm" data-act="unlink">Trennen</button>`
            : me.discord_enabled ? `<a class="btn sm discord" href="/api/auth/discord/link">${discordIcon} Discord verknüpfen</a>` : `<span class="muted">nicht eingerichtet</span>`}</dd>
        </dl></div></div>
        <div class="card"><div class="card-head"><h2>${me.has_password ? "Passwort ändern" : "Passwort festlegen"}</h2></div><div class="card-body"><form id="pw-form">
          ${me.has_password ? `<label class="field"><span>Aktuelles Passwort</span><input type="password" name="current" autocomplete="current-password" required></label>` : `<p class="muted" style="margin-top:0">Mit einem Passwort kannst du dich auch ohne Discord anmelden.</p>`}
          <label class="field"><span>Neues Passwort (min. 10 Zeichen)</span><input type="password" name="new" autocomplete="new-password" minlength="10" required></label>
          <button class="btn primary" type="submit">Speichern</button></form></div></div>
      </div>
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Kontingent</h2></div><div class="card-body">${quotaMeters(me.quota, me.usage)}</div></div>
        <div class="card"><div class="card-head"><h2>SSH-Schlüssel</h2></div><div class="card-body"><form id="keys-form">
          <p class="muted" style="margin-top:0">Werden beim Erstellen neuer Server automatisch für <code>root</code> hinterlegt.</p>
          <textarea name="ssh_keys" rows="5" placeholder="ssh-ed25519 AAAA… name@pc">${esc(me.ssh_keys || "")}</textarea>
          <button class="btn primary" type="submit" style="margin-top:10px">Speichern</button></form></div></div>
      </div>
    </div>`);
  $("#pw-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    await api("/api/me/password", { method: "POST", body: { current: f.current ? f.current.value : "", new: f.new.value } })
      .then(() => { toast("Passwort gespeichert"); f.reset(); route(); }).catch(fail);
  };
  $("#keys-form").onsubmit = async (e) => {
    e.preventDefault();
    await api("/api/me", { method: "PATCH", body: { ssh_keys: e.target.ssh_keys.value } }).then(() => toast("Gespeichert")).catch(fail);
  };
}

// ------------------------------------------------------------------ Start

if (!location.hash) location.hash = "#/dashboard";
route();
