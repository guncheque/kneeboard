// SPDX-License-Identifier: MIT · Copyright (c) 2026 GracelessDev
"use strict";

const $ = (id) => document.getElementById(id);
const img = $("page");

let docs = [];
let state = { doc: 0, page: 0, night: false, fit: "page" };
let ws = null;
let retry = 500;
const preloaded = new Map(); // url -> Image, keeps a few neighbours warm

// ---------- connection ----------
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { retry = 500; $("conn").classList.add("ok"); };
  ws.onclose = () => {
    $("conn").classList.remove("ok");
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 5000);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "library") { docs = msg.docs; renderList(); render(); }
    else if (msg.type === "state") {
      const flipped = msg.doc !== state.doc || msg.page !== state.page;
      state = msg;
      render();
      if (flipped) toast();
    }
  };
}

function send(action, extra = {}) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ action, ...extra }));
}

// ---------- rendering ----------
function pageUrl(docIdx, pageIdx) {
  const d = docs[docIdx];
  if (!d) return "";
  const w = Math.ceil((window.innerWidth * (window.devicePixelRatio || 1)) / 200) * 200;
  return `/page/${d.id}/${pageIdx}?w=${w}&v=${d.v}`;
}

function preload(docIdx, pageIdx) {
  const d = docs[docIdx];
  if (!d || pageIdx < 0 || pageIdx >= d.pages) return;
  const url = pageUrl(docIdx, pageIdx);
  if (preloaded.has(url)) return;
  const im = new Image();
  im.src = url;
  preloaded.set(url, im);
  if (preloaded.size > 12) preloaded.delete(preloaded.keys().next().value);
}

function render() {
  document.body.classList.toggle("night", state.night);
  document.body.classList.toggle("fit-width", state.fit === "width");
  const d = docs[state.doc];
  $("empty").hidden = !!d;
  img.hidden = !d;
  if (!d) { $("doc-name").textContent = "—"; $("page-num").textContent = ""; return; }

  const url = pageUrl(state.doc, state.page);
  if (img.getAttribute("src") !== url) {
    img.src = url;
    $("viewer").scrollTop = 0;
  }
  $("doc-name").textContent = d.name.split("/").pop();
  $("page-num").textContent = d.pages > 1 ? `${state.page + 1} / ${d.pages}` : "";
  document.querySelectorAll("#doc-list li").forEach((li) =>
    li.classList.toggle("active", Number(li.dataset.id) === state.doc));

  preload(state.doc, state.page + 1);
  preload(state.doc, state.page - 1);
}

function renderList() {
  const ul = $("doc-list");
  ul.replaceChildren();
  for (const d of docs) {
    const li = document.createElement("li");
    li.dataset.id = d.id;
    const name = document.createElement("span");
    const parts = d.name.split("/");
    if (parts.length > 1) {
      const f = document.createElement("span");
      f.className = "folder";
      f.textContent = parts.slice(0, -1).join(" / ") + " / ";
      name.append(f);
    }
    name.append(parts.at(-1));
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = d.pages > 1 ? `${d.pages} pp` : "";
    li.append(name, count);
    li.onclick = () => { send("goto", { doc: d.id }); toggleDrawer(false); };
    ul.append(li);
  }
}

// ---------- UI ----------
let toastTimer;
function toast() {
  const d = docs[state.doc];
  if (!d || document.body.classList.contains("ui")) return;
  const t = $("toast");
  t.textContent = d.pages > 1 ? `${d.name.split("/").pop()} · ${state.page + 1}/${d.pages}`
                              : d.name.split("/").pop();
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 1200);
}

let uiTimer;
function toggleUi(show = !document.body.classList.contains("ui")) {
  document.body.classList.toggle("ui", show);
  clearTimeout(uiTimer);
  if (!show) toggleDrawer(false);
  else uiTimer = setTimeout(() => { if ($("drawer").hidden) toggleUi(false); }, 4000);
}

function toggleDrawer(show = $("drawer").hidden) {
  $("drawer").hidden = !show;
  clearTimeout(uiTimer);
  if (!show && document.body.classList.contains("ui")) uiTimer = setTimeout(() => toggleUi(false), 4000);
}

$("zone-prev").onclick = () => send("prev_page");
$("zone-next").onclick = () => send("next_page");
$("zone-mid").onclick = () => toggleUi();
$("btn-docs").onclick = () => toggleDrawer();
$("btn-night").onclick = () => send("toggle_night");
$("btn-fit").onclick = () => send("toggle_fit");
$("btn-full").onclick = () => {
  const el = document.documentElement;
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  } else if (el.requestFullscreen) {
    el.requestFullscreen({ navigationUI: "hide" }).catch(() => {});
  } else if (el.webkitRequestFullscreen) {
    el.webkitRequestFullscreen(); // older Safari / iPadOS
  }
};

// keep the tablet awake (needs a secure context; silently skipped over plain http)
async function keepAwake() {
  try { await navigator.wakeLock?.request("screen"); } catch (_) {}
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) keepAwake(); });
keepAwake();

// re-request at the new resolution after rotating the tablet
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(render, 250);
});

connect();
