/* Agent Deck — live workspace client.
   SSE-driven, keyed in-place DOM updates (never a full re-render, so card
   animations and hover state survive the 1 Hz feed). No build step. */

const deck = document.getElementById("deck");
const linkEl = document.getElementById("link");
const clockEl = document.getElementById("clock");
const toastEl = document.getElementById("toast");
const metersEl = document.getElementById("meters");
const floorEl = document.getElementById("floor");
const floorCanvas = document.getElementById("floor-canvas");
const floorMates = window.Mates && floorCanvas ? window.Mates.floor(floorCanvas) : null;
const desksEl = document.getElementById("desks");
const desksRow = document.getElementById("desks-row");

const counts = {
  alert: document.getElementById("count-alert"),
  work: document.getElementById("count-work"),
  sessions: document.getElementById("count-sessions"),
  agents: document.getElementById("count-agents"),
};

const STATE_LABEL = {
  NEEDS_YOU: "needs you",
  WORKING: "working",
  DONE: "done",
  SHELL: "shell",
  IDLE: "idle",
  DEAD: "gone",
  OFFLINE: "offline",
};

const cards = new Map(); // session_id -> {el, refs}
const deskRows = new Map(); // desk name -> el
const sections = new Map(); // key -> {el, grid}
const meters = new Map(); // window key -> {el, refs}
let latest = { sessions: [], totals: {}, plan: {} };
let previousStates = new Map();
let armed = false; // notifications + audio arm on the first user gesture

/* ── formatting ──────────────────────────────────────────────────────── */

function setText(node, value) {
  const next = value == null ? "" : String(value);
  if (node.textContent !== next) node.textContent = next;
}

function humanTokens(n) {
  if (!n) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e3) return Math.round(n / 1e3) + "k";
  return String(n);
}

function elapsed(sinceSeconds) {
  if (!sinceSeconds) return "";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - sinceSeconds));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h " + String(m % 60).padStart(2, "0") + "m";
  return Math.floor(h / 24) + "d";
}

function untilReset(iso) {
  if (!iso) return "";
  const ms = Date.parse(iso) - Date.now();
  if (!Number.isFinite(ms)) return "";
  if (ms <= 0) return "resetting";
  const mins = Math.round(ms / 60000);
  if (mins < 60) return "resets in " + mins + "m";
  const hours = Math.floor(mins / 60);
  if (hours < 24) return "resets in " + hours + "h " + String(mins % 60).padStart(2, "0") + "m";
  return "resets in " + Math.round(hours / 24) + "d";
}

// A stale reading is still a true reading — say how old it is rather than
// blanking the meter, which is what the first version did on a single 429.
function resetLine(w, plan) {
  const reset = untilReset(w.resets_at);
  if (!plan.stale) return reset;
  const age = plan.fetched_at ? elapsed(plan.fetched_at) : "";
  return reset + (age ? ` · as of ${age} ago` : " · cached");
}

/* ── plan usage meters ───────────────────────────────────────────────── */

function paintMeters(plan) {
  if (!plan || !plan.available) {
    if (!metersEl.dataset.off) {
      metersEl.dataset.off = "1";
      metersEl.innerHTML = `<div class="meter" data-off="1">
        <div class="meter__top"><span class="meter__pct">—</span><span class="meter__label">plan usage</span></div>
        <div class="meter__bar"><div class="meter__fill"></div></div>
        <div class="meter__reset"></div></div>`;
    }
    setText(metersEl.querySelector(".meter__reset"), (plan && plan.reason) || "unavailable");
    return;
  }
  delete metersEl.dataset.off;

  for (const w of plan.windows || []) {
    let m = meters.get(w.key);
    if (!m) {
      const el = document.createElement("div");
      el.className = "meter";
      el.innerHTML = `<div class="meter__top"><span class="meter__pct"></span><span class="meter__label"></span></div>
        <div class="meter__bar"><div class="meter__fill"></div></div>
        <div class="meter__reset"></div>`;
      m = {
        el,
        pct: el.querySelector(".meter__pct"),
        label: el.querySelector(".meter__label"),
        fill: el.querySelector(".meter__fill"),
        reset: el.querySelector(".meter__reset"),
      };
      meters.set(w.key, m);
      metersEl.appendChild(el);
    }
    const pct = Math.max(0, Math.min(100, w.percent));
    setText(m.pct, Math.round(pct) + "%");
    setText(m.label, w.label);
    m.fill.style.width = pct + "%";
    const heat = pct >= 90 ? "hot" : pct >= 70 ? "warn" : "ok";
    if (m.el.dataset.heat !== heat) m.el.dataset.heat = heat;
    setText(m.reset, resetLine(w, plan));
  }
}

/* ── cards ───────────────────────────────────────────────────────────── */

function buildCard(session) {
  const el = document.createElement("article");
  el.className = "card";
  el.tabIndex = 0;
  el.setAttribute("role", "button");
  // Which session this card is, readable from outside — the crown button acts
  // on it, so it has to be assertable.
  el.dataset.session = session.session_id;
  el.innerHTML = `
    <canvas class="card__scene"></canvas>
    <div class="card__top"><h3 class="card__name"></h3><span class="card__source"></span><span class="card__state"></span></div>
    <div class="card__meta"></div>
    <div class="card__alert" hidden></div>
    <div class="card__act" hidden><span class="card__tool"></span><span class="card__detail"></span></div>
    <div class="card__agents"></div>
    <div class="card__say" hidden></div>
    <div class="card__foot">
      <span class="f-tokens"></span><span class="spacer"></span>
      <span class="f-age"></span>
      <button class="crown-btn" type="button" title="make this session the manager">♔</button>
      <span class="jump">focus ↗</span>
    </div>`;

  const refs = {
    name: el.querySelector(".card__name"),
    source: el.querySelector(".card__source"),
    state: el.querySelector(".card__state"),
    meta: el.querySelector(".card__meta"),
    alert: el.querySelector(".card__alert"),
    act: el.querySelector(".card__act"),
    tool: el.querySelector(".card__tool"),
    detail: el.querySelector(".card__detail"),
    agents: el.querySelector(".card__agents"),
    say: el.querySelector(".card__say"),
    tokens: el.querySelector(".f-tokens"),
    age: el.querySelector(".f-age"),
  };

  // The mate in the cubicle acts out this session's state for as long as the
  // card lives; the handle is released in render() when the card is dropped.
  const mate = window.Mates ? window.Mates.cubicle(el.querySelector(".card__scene")) : null;

  // Crowning must not also focus the Terminal window, hence stopPropagation.
  el.querySelector(".crown-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    crownSession(session.session_id, session.name);
  });

  const activate = () => focusSession(session.pid, session.name);
  el.addEventListener("click", activate);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate();
    }
  });

  return { el, refs, mate };
}

function paintCard(entry, s) {
  const { el, refs } = entry;
  if (el.dataset.state !== s.state) el.dataset.state = s.state;
  const source = s.source || "claude";
  if (el.dataset.source !== source) el.dataset.source = source;
  if (entry.mate) entry.mate.set(s);

  setText(refs.name, s.name);
  setText(refs.source, source === "opencode" ? "OpenCode" : "Claude");
  setText(refs.state, STATE_LABEL[s.state] || s.state.toLowerCase());

  // One dim line rather than four competing chips.
  setText(
    refs.meta,
    [s.project, s.git_branch, s.model].filter(Boolean).join("  ·  ")
  );

  if (s.attention) {
    setText(refs.alert, s.attention.message || s.attention.kind.replace(/_/g, " "));
    refs.alert.hidden = false;
  } else refs.alert.hidden = true;

  // Activity only matters while something is actually running.
  if (s.activity.tool && (s.state === "WORKING" || s.state === "SHELL")) {
    refs.act.hidden = false;
    const running = s.activity.running ? "1" : "0";
    if (refs.act.dataset.running !== running) refs.act.dataset.running = running;
    setText(refs.tool, s.activity.tool);
    setText(refs.detail, s.activity.detail || "");
  } else refs.act.hidden = true;

  paintAgents(refs.agents, s.subagents);

  const say =
    s.state === "WORKING" || s.state === "SHELL"
      ? s.last_prompt
      : s.last_assistant || s.last_prompt;
  if (say) {
    setText(refs.say, say);
    refs.say.hidden = false;
  } else refs.say.hidden = true;

  setText(refs.tokens, humanTokens(s.usage.output) + " out");
  setText(refs.age, elapsed(s.state_since));
}

function paintAgents(host, agents) {
  const shown = agents.slice(0, 5);
  if (host.childElementCount !== shown.length) {
    host.replaceChildren(
      ...shown.map(() => {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.innerHTML = "<s></s><b></b><u></u>";
        return chip;
      })
    );
  }
  shown.forEach((agent, i) => {
    const chip = host.children[i];
    const flag = agent.running ? "1" : "0";
    if (chip.dataset.running !== flag) chip.dataset.running = flag;
    setText(chip.querySelector("b"), agent.agent_type);
    setText(chip.querySelector("u"), agent.description || "");
  });
}

/* ── layout ──────────────────────────────────────────────────────────── */

function ensureSection(key, title, alert) {
  let section = sections.get(key);
  if (!section) {
    const el = document.createElement("section");
    el.className = "section" + (alert ? " section--alert" : "");
    el.innerHTML = `<div class="section__head"><h2></h2></div><div class="grid"></div>`;
    el.querySelector("h2").textContent = title;
    section = { el, grid: el.querySelector(".grid") };
    sections.set(key, section);
  }
  return section;
}

function render(data) {
  latest = data;
  const list = data.sessions || [];

  const buckets = new Map();
  const alerts = list.filter((s) => s.state === "NEEDS_YOU");
  if (alerts.length) buckets.set(" alert", { title: "Awaiting you", items: alerts, alert: true });

  for (const s of list) {
    if (s.state === "NEEDS_YOU") continue;
    const key = s.project || s.cwd;
    if (!buckets.has(key)) buckets.set(key, { title: key, items: [], alert: false });
    buckets.get(key).items.push(s);
  }

  const seen = new Set();

  for (const [key, bucket] of buckets) {
    const section = ensureSection(key, bucket.title, bucket.alert);

    bucket.items.forEach((s, index) => {
      seen.add(s.session_id);
      let entry = cards.get(s.session_id);
      if (!entry) {
        entry = buildCard(s);
        entry.el.style.animationDelay = Math.min(index * 40, 280) + "ms";
        cards.set(s.session_id, entry);
      }
      paintCard(entry, s);
      // Only touch the DOM when the position changed — re-inserting a node
      // restarts its CSS animations.
      if (section.grid.children[index] !== entry.el) {
        section.grid.insertBefore(entry.el, section.grid.children[index] || null);
      }
    });

    if (section.el.parentNode !== deck) deck.appendChild(section.el);
  }

  for (const [key, section] of sections) {
    if (!buckets.has(key)) {
      section.el.remove();
      sections.delete(key);
    }
  }
  for (const [id, entry] of cards) {
    if (!seen.has(id)) {
      entry.mate?.destroy();
      entry.el.remove();
      cards.delete(id);
    }
  }

  paintDesks(data.desks);
  paintFloor(list);

  let cursor = 0;
  for (const key of buckets.keys()) {
    const section = sections.get(key);
    if (deck.children[cursor] !== section.el) {
      deck.insertBefore(section.el, deck.children[cursor] || null);
    }
    cursor += 1;
  }

  if (!list.length && !deck.querySelector(".empty")) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "no live sessions";
    deck.appendChild(empty);
  } else if (list.length) {
    deck.querySelector(".empty")?.remove();
  }

  paintCounts(data.totals || {});
  paintMeters(data.plan);
  announce(list);
}

/* ── desks ───────────────────────────────────────────────────────────── */

// A desk is a name on the roster, not a process. It stays on the board when
// nobody is sitting at it -- OFFLINE -- which is exactly what a session card
// cannot do. Clicking a seated desk focuses it; clicking an empty one starts it.
function paintDesks(desks) {
  if (!desksEl || !desksRow) return;
  const rows = (desks || []).filter((d) => d.desk);
  desksEl.hidden = rows.length === 0;

  const seen = new Set();
  rows.forEach((d, index) => {
    seen.add(d.name);
    let el = deskRows.get(d.name);
    if (!el) {
      el = document.createElement("button");
      el.type = "button";
      el.className = "desk";
      el.innerHTML = `<b class="desk__name"></b><i class="desk__state"></i><u class="desk__mission"></u>`;
      el.addEventListener("click", () => {
        const row = (latest.desks || []).find((x) => x.name === el.dataset.name);
        if (!row) return;
        if (row.pid) focusSession(row.pid, row.name);
        else startDesk(row.name);
      });
      deskRows.set(d.name, el);
    }
    el.dataset.name = d.name;
    if (el.dataset.state !== d.state) el.dataset.state = d.state;
    setText(el.querySelector(".desk__name"), d.name);
    setText(el.querySelector(".desk__state"), STATE_LABEL[d.state] || String(d.state || "").toLowerCase());
    setText(el.querySelector(".desk__mission"), d.mission || d.cwd || "");
    el.title = d.pid ? "focus this desk" : "start this desk";
    if (desksRow.children[index] !== el) {
      desksRow.insertBefore(el, desksRow.children[index] || null);
    }
  });

  for (const [name, el] of deskRows) {
    if (!seen.has(name)) {
      el.remove();
      deskRows.delete(name);
    }
  }
}

async function startDesk(name) {
  try {
    const res = await fetch(`/api/roster/${encodeURIComponent(name)}/start`, {
      method: "POST",
    });
    const body = await res.json();
    toast(
      body.ok ? `starting ${name}` : body.detail || body.reason || "could not start",
      !body.ok
    );
  } catch (err) {
    toast("start failed: " + err.message, true);
  }
}

/* ── the floor ───────────────────────────────────────────────────────── */

// One shared room holding every live session. Seating order is by state then
// name, so a mate only changes desk when what they're doing changes.
const STATE_ORDER = ["NEEDS_YOU", "WORKING", "SHELL", "DONE", "IDLE", "DEAD"];

function paintFloor(list) {
  if (!floorMates) return;
  const seated = list
    .slice()
    .sort(
      (a, b) =>
        STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state) ||
        String(a.name).localeCompare(String(b.name))
    );
  floorEl.hidden = seated.length === 0;
  floorMates.set(seated);
}

function paintCounts(t) {
  setText(counts.alert.querySelector("b"), t.needs_you ?? 0);
  counts.alert.dataset.hot = t.needs_you ? "1" : "0";
  setText(counts.work.querySelector("b"), t.working ?? 0);
  setText(counts.sessions.querySelector("b"), t.sessions ?? 0);
  setText(counts.agents.querySelector("b"), t.agents_running ?? 0);
  document.title = t.needs_you ? `(${t.needs_you}) Agent Deck` : "Agent Deck";
}

/* ── alerts ──────────────────────────────────────────────────────────── */

function announce(list) {
  const next = new Map(list.map((s) => [s.session_id, s.state]));
  for (const [id, state] of next) {
    if (state === "NEEDS_YOU" && previousStates.get(id) !== "NEEDS_YOU" && previousStates.size) {
      const s = list.find((x) => x.session_id === id);
      notify(`${s.name} needs you`, s.attention?.message || s.project);
      blip();
    }
  }
  previousStates = next;
}

function notify(title, body) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    new Notification(title, { body, tag: title, silent: true });
  } catch (_) { /* Safari throws on some constructor paths */ }
}

let audio = null;
function blip() {
  if (!audio) return;
  try {
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.type = "triangle";
    osc.frequency.setValueAtTime(660, audio.currentTime);
    osc.frequency.exponentialRampToValueAtTime(990, audio.currentTime + 0.09);
    gain.gain.setValueAtTime(0.0001, audio.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.1, audio.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.22);
    osc.connect(gain).connect(audio.destination);
    osc.start();
    osc.stop(audio.currentTime + 0.24);
  } catch (_) { /* audio is a nicety, never a failure path */ }
}

window.addEventListener(
  "pointerdown",
  () => {
    if (armed) return;
    armed = true;
    try {
      audio = new (window.AudioContext || window.webkitAudioContext)();
    } catch (_) { /* no audio available */ }
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
  },
  { once: true }
);

/* ── actions ─────────────────────────────────────────────────────────── */

let toastTimer = null;
function toast(message, bad) {
  setText(toastEl, message);
  toastEl.dataset.show = "1";
  toastEl.dataset.bad = bad ? "1" : "0";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (toastEl.dataset.show = "0"), 2600);
}

async function focusSession(pid, name) {
  try {
    const res = await fetch(`/api/focus/${pid}`, { method: "POST" });
    const body = await res.json();
    toast(body.ok ? `focused ${name}` : body.detail || "could not focus", !body.ok);
  } catch (err) {
    toast("focus failed: " + err.message, true);
  }
}

async function crownSession(sessionId, name) {
  try {
    const res = await fetch("/api/manager/crown", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    });
    const body = await res.json();
    toast(body.ok ? `${name} is the manager` : body.reason || "could not crown", !body.ok);
  } catch (err) {
    toast("crown failed: " + err.message, true);
  }
}

/* ── transport ───────────────────────────────────────────────────────── */

function setLink(up, label) {
  linkEl.dataset.up = up ? "1" : "0";
  setText(linkEl.querySelector("span"), label);
}

function connect() {
  const source = new EventSource("/api/stream");
  source.onopen = () => setLink(true, "live");
  source.onmessage = (event) => {
    try {
      render(JSON.parse(event.data));
    } catch (err) {
      console.error("[deck] bad payload", err);
    }
  };
  source.onerror = () => {
    setLink(false, "reconnecting");
    source.close();
    setTimeout(connect, 2000);
  };
}

// Local tick keeps elapsed counters and reset countdowns moving between frames.
setInterval(() => {
  clockEl.textContent = new Date().toLocaleTimeString("en-GB");
  for (const [id, entry] of cards) {
    const s = latest.sessions.find((x) => x.session_id === id);
    if (s) setText(entry.refs.age, elapsed(s.state_since));
  }
  for (const w of latest.plan?.windows || []) {
    const m = meters.get(w.key);
    if (m) setText(m.reset, resetLine(w, latest.plan));
  }
}, 1000);

connect();
