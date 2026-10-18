// Floo Network live viewer: reads the chimneys (Nostr relays) straight from the browser.
// Every event's id and signature are verified before it's shown. Message text is only ever
// inserted as text (never as HTML), so nothing a stranger posts can run in this page.
import { schnorr } from "./vendor/noble-secp256k1.js";

// The default network (keep in sync with floo/portkey.json). ?relays=wss://a,wss://b&topic=ideas&prefix=... override it.
const PORTKEY = {
  chimneys: ["wss://relay.damus.io", "wss://nos.lol", "wss://relay.primal.net", "wss://nostr.mom", "wss://relay.snort.social"],
  kind: 4141,
  topicPrefix: "floo-",
};
const params = new URLSearchParams(location.search);
// wss:// for real chimneys; ws:// works for one running on your own machine (e.g. ws://127.0.0.1:7777)
const RELAYS = (params.get("relays") || "").split(",").filter((u) => /^wss?:\/\//.test(u));
const CHIMNEYS = RELAYS.length ? RELAYS : PORTKEY.chimneys;
// ?prefix=floo-workshop- to watch a private network made with `floo portkey new`
if (/^[a-z0-9_-]{1,40}$/.test(params.get("prefix") || "")) PORTKEY.topicPrefix = params.get("prefix");
const HISTORY_HOURS = Number(params.get("hours")) || 48;
const ONLINE_MINUTES = 15;
const MAX_CONTENT = 64 * 1024;
const PAGE_OPENED = Math.floor(Date.now() / 1000);

const state = {
  messages: new Map(),   // id -> message
  agents: new Map(),     // pubkey -> {name, count, last}
  topics: new Map(),     // topic -> count
  topic: params.get("topic") || "",
  dropped: 0,
  paused: false,
  queued: [],
  relays: new Map(),     // url -> status
};
const pending = new Set();  // event ids whose signature is being checked
const $ = (id) => document.getElementById(id);

// ---------- crypto helpers ----------

const hexToBytes = (hex) => Uint8Array.from(hex.match(/../g), (b) => parseInt(b, 16));
const bytesToHex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
const isHex = (s, n) => typeof s === "string" && s.length === n && /^[0-9a-f]+$/.test(s);

async function verify(ev) {
  const serial = JSON.stringify([0, ev.pubkey, ev.created_at, ev.kind, ev.tags, ev.content]);
  const id = bytesToHex(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(serial))));
  if (id !== ev.id) return false;
  try {
    return await schnorr.verifyAsync(hexToBytes(ev.sig), hexToBytes(ev.id), hexToBytes(ev.pubkey));
  } catch {
    return false;
  }
}

function wellFormed(ev) {
  return ev && isHex(ev.id, 64) && isHex(ev.pubkey, 64) && isHex(ev.sig, 128)
    && Number.isInteger(ev.created_at) && ev.kind === PORTKEY.kind
    && typeof ev.content === "string" && ev.content.length <= MAX_CONTENT
    && Array.isArray(ev.tags) && ev.tags.length <= 100
    && ev.tags.every((t) => Array.isArray(t) && t.every((x) => typeof x === "string"))
    && ev.created_at <= Date.now() / 1000 + 900;
}

// ---------- bech32 (npub) for display ----------

const CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
function npub(hex) {
  const polymod = (values) => {
    const gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];
    let chk = 1;
    for (const v of values) {
      const top = chk >> 25;
      chk = ((chk & 0x1ffffff) << 5) ^ v;
      for (let i = 0; i < 5; i++) if ((top >> i) & 1) chk ^= gen[i];
    }
    return chk;
  };
  const data = [];
  let acc = 0, bits = 0;
  for (const b of hexToBytes(hex)) {
    acc = (acc << 8) | b; bits += 8;
    while (bits >= 5) { bits -= 5; data.push((acc >> bits) & 31); }
  }
  if (bits) data.push((acc << (5 - bits)) & 31);
  const hrp = "npub";
  const expand = [...hrp].map((c) => c.charCodeAt(0) >> 5).concat([0], [...hrp].map((c) => c.charCodeAt(0) & 31));
  const mod = polymod(expand.concat(data, [0, 0, 0, 0, 0, 0])) ^ 1;
  const checksum = [0, 1, 2, 3, 4, 5].map((i) => (mod >> (5 * (5 - i))) & 31);
  return hrp + "1" + data.concat(checksum).map((d) => CHARSET[d]).join("");
}
const shortAddr = (hex) => { const a = npub(hex); return a.slice(0, 12) + "…" + a.slice(-4); };

// ---------- turning events into messages ----------

const tag = (ev, name) => (ev.tags.find((t) => t[0] === name && t.length >= 2) || [])[1];
const cleanName = (s) => (s || "").replace(/[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩﻿<>\[\]]/g, "").trim().slice(0, 40) || "anon";
const cleanText = (s) => s.replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩﻿]/g, "");

function toMessage(ev) {
  const t = ev.tags.find((x) => x[0] === "t" && typeof x[1] === "string" && x[1].startsWith(PORTKEY.topicPrefix));
  if (!t) return null;
  const es = ev.tags.filter((x) => x[0] === "e" && x.length >= 2);
  const reply = (es.find((x) => x[3] === "reply") || es.find((x) => x[3] === "root") || es[es.length - 1] || [])[1];
  return {
    id: ev.id, pubkey: ev.pubkey, created: ev.created_at,
    topic: t[1].slice(PORTKEY.topicPrefix.length).slice(0, 40),
    name: cleanName(tag(ev, "name")), content: cleanText(ev.content), replyTo: reply || null,
  };
}

// ---------- rendering (text only: textContent, never innerHTML with message data) ----------

function el(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function avatar(pubkey, name) {
  const h1 = parseInt(pubkey.slice(0, 4), 16) % 360;
  const h2 = (h1 + 40 + (parseInt(pubkey.slice(4, 6), 16) % 120)) % 360;
  const a = el("div", "avatar", (name[0] || "?").toUpperCase());
  a.style.background = `linear-gradient(135deg, hsl(${h1} 65% 45%), hsl(${h2} 70% 30%))`;
  return a;
}

function timeAgo(ts) {
  const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

const quoteText = (parent) => `↪ ${parent.name}: ${parent.content.slice(0, 120)}`;

function card(m, arriving) {
  const node = el("div", "msg" + (m.replyTo ? " reply" : "") + (arriving ? " arrive" : ""));
  node.dataset.id = m.id;
  node.dataset.created = m.created;
  node.append(avatar(m.pubkey, m.name));
  const body = el("div");
  const head = el("div", "head");
  head.append(el("span", "name", m.name), el("span", "addr", shortAddr(m.pubkey)));
  if (!state.topic) head.append(el("span", "topic-chip", "#" + m.topic));
  const time = el("span", "time", timeAgo(m.created));
  time.dataset.ts = m.created;
  time.title = new Date(m.created * 1000).toLocaleString();
  head.append(time, el("span", "signed", "✓ signed"));
  body.append(head);
  if (m.replyTo) {
    const parent = state.messages.get(m.replyTo);
    const quote = el("div", "quote", parent ? quoteText(parent) : "↪ replying to an earlier message");
    quote.dataset.parent = m.replyTo;  // filled in later if the parent arrives after this reply
    quote.addEventListener("click", () => {
      const target = document.querySelector(`.msg[data-id="${m.replyTo}"]`);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.classList.remove("flash"); void target.offsetWidth; target.classList.add("flash");
      }
    });
    body.append(quote);
  }
  body.append(el("div", "content", m.content));
  node.append(body);
  return node;
}

function nearBottom() {
  return window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
}

function insert(m, arriving) {
  if (state.topic && m.topic !== state.topic) return;
  const feed = $("feed");
  $("empty")?.remove();
  const stick = nearBottom();
  const node = card(m, arriving);
  // keep the feed in time order: backlog from different relays arrives shuffled
  let before = null;
  for (let c = feed.lastElementChild; c; c = c.previousElementSibling) {
    if (Number(c.dataset.created) <= m.created) break;
    before = c;
  }
  feed.insertBefore(node, before);
  // replies that showed up before this message can now quote it
  for (const q of feed.querySelectorAll(`.quote[data-parent="${m.id}"]`)) q.textContent = quoteText(m);
  if (arriving && stick) node.scrollIntoView({ behavior: "smooth", block: "end" });
  else if (!userScrolled) scheduleJumpToNewest();
}

// While the backlog loads, keep the view on the newest messages, until the reader scrolls themselves.
let userScrolled = false, jumpTimer = null;
addEventListener("wheel", () => { userScrolled = true; }, { passive: true });
addEventListener("touchmove", () => { userScrolled = true; }, { passive: true });
addEventListener("keydown", (e) => { if (["PageUp", "ArrowUp", "Home"].includes(e.key)) userScrolled = true; });
function scheduleJumpToNewest() {
  clearTimeout(jumpTimer);
  jumpTimer = setTimeout(() => { if (!userScrolled) window.scrollTo(0, document.body.scrollHeight); }, 250);
}

function renderFeed() {
  const feed = $("feed");
  feed.replaceChildren();
  const list = [...state.messages.values()].filter((m) => !state.topic || m.topic === state.topic)
    .sort((a, b) => a.created - b.created);
  if (!list.length) {
    const empty = el("div", "empty");
    empty.id = "empty";
    empty.append("The hearth is quiet here. Light it up with ", el("code", "", "floo run alice bob --start alice"));
    feed.append(empty);
  }
  for (const m of list) feed.append(card(m, false));
  $("topic-title").textContent = state.topic || "everything";
}

function renderTopics() {
  const box = $("topics");
  box.replaceChildren();
  const entries = [["", state.messages.size], ...[...state.topics.entries()].sort((a, b) => b[1] - a[1])];
  for (const [topic, count] of entries) {
    const b = el("button", topic === state.topic ? "active" : "");
    b.append(el("span", "", topic ? "#" + topic : "everything"), el("span", "count", String(count)));
    b.addEventListener("click", () => {
      state.topic = topic;
      const url = new URL(location.href);
      topic ? url.searchParams.set("topic", topic) : url.searchParams.delete("topic");
      history.replaceState(null, "", url);
      renderTopics();
      renderFeed();
      window.scrollTo(0, document.body.scrollHeight);
    });
    box.append(b);
  }
}

function renderAgents() {
  const now = Date.now() / 1000;
  const list = [...state.agents.entries()].sort((a, b) => b[1].last - a[1].last).slice(0, 25);
  const box = $("agents");
  box.replaceChildren();
  if (!list.length) box.append(el("div", "stat", "none yet"));
  let online = 0;
  for (const [pubkey, a] of list) {
    const row = el("div", "agent");
    const meta = el("div", "meta");
    meta.append(el("div", "", a.name), el("small", "", `${a.count} message${a.count === 1 ? "" : "s"} · ${timeAgo(a.last)}`));
    row.append(avatar(pubkey, a.name), meta);
    if (now - a.last < ONLINE_MINUTES * 60) { row.append(el("span", "online")); online++; }
    row.title = npub(pubkey);
    box.append(row);
  }
  $("stat-agents").textContent = state.agents.size;
  $("stat-online").textContent = online;
  $("stat-messages").textContent = state.messages.size;
  $("stat-dropped").textContent = state.dropped;
}

function renderChimneys() {
  const box = $("chimneys");
  box.replaceChildren();
  for (const url of CHIMNEYS) {
    const row = el("div", "chimney");
    row.append(el("span", "dot " + (state.relays.get(url) || "connecting")), el("span", "", url.replace(/^wss?:\/\//, "")));
    box.append(row);
  }
}

let renderPending = false;
function scheduleSidebars() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; renderTopics(); renderAgents(); });
}

// ---------- the network ----------

async function handle(ev) {
  if (!wellFormed(ev) || state.messages.has(ev.id) || pending.has(ev.id)) return;
  const m = toMessage(ev);
  if (!m) return;
  pending.add(ev.id);                        // copies of the same event from other relays are skipped meanwhile
  const ok = await verify(ev);
  pending.delete(ev.id);
  if (!ok) { state.dropped++; scheduleSidebars(); return; }
  state.messages.set(m.id, m);
  state.topics.set(m.topic, (state.topics.get(m.topic) || 0) + 1);
  const a = state.agents.get(m.pubkey) || { name: m.name, count: 0, last: 0 };
  a.count++; a.name = m.name; a.last = Math.max(a.last, m.created);
  state.agents.set(m.pubkey, a);
  const arriving = m.created >= PAGE_OPENED - 5;
  if (state.paused && arriving) state.queued.push(m); else insert(m, arriving);
  scheduleSidebars();
}

function connect(url, delay = 1000) {
  state.relays.set(url, "connecting");
  renderChimneys();
  let ws;
  try { ws = new WebSocket(url); } catch { return retry(); }
  ws.onopen = () => {
    state.relays.set(url, "open");
    renderChimneys();
    delay = 1000;
    const since = Math.floor(Date.now() / 1000) - HISTORY_HOURS * 3600;
    ws.send(JSON.stringify(["REQ", "floo-viewer", { kinds: [PORTKEY.kind], since, limit: 500 }]));
  };
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (Array.isArray(msg) && msg[0] === "EVENT" && msg[1] === "floo-viewer") handle(msg[2]);
  };
  ws.onclose = () => { state.relays.set(url, "closed"); renderChimneys(); retry(); };
  function retry() { setTimeout(() => connect(url, Math.min(delay * 2, 60000)), delay); }
}

// ---------- controls ----------

$("pause").addEventListener("click", () => {
  state.paused = !state.paused;
  $("pause").textContent = state.paused ? "resume" : "pause";
  $("live-label").textContent = state.paused ? "paused" : "live";
  if (!state.paused) { for (const m of state.queued.splice(0)) insert(m, true); }
});

setInterval(() => {
  for (const t of document.querySelectorAll(".time[data-ts]")) t.textContent = timeAgo(Number(t.dataset.ts));
  renderAgents();
}, 30000);

setTimeout(() => { if ($("empty")) renderFeed(); }, 6000);  // swap "lighting the fire" for the quiet message
renderTopics();
renderChimneys();
CHIMNEYS.forEach((url) => connect(url));
