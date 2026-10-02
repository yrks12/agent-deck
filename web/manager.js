// The manager's office: the crowned session at the centre desk, its reports
// either side, and every message flying between them as a note. Click a desk to
// read that thread; click it again to go back to everything.

const state = {
  edges: [],
  reports: [],
  peerLinks: [],
  sessions: {},
  manager: null,
  focus: null,
  expanded: new Set(),
};

const el = (id) => document.getElementById(id);
const office = window.Mates ? window.Mates.office(el("office-canvas")) : null;

function ago(ts) {
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

async function refresh() {
  try {
    const res = await fetch("/api/comms");
    if (!res.ok) return;
    const data = await res.json();
    state.edges = data.edges || [];
    state.reports = data.reports || [];
    state.peerLinks = data.peer_links || [];
    state.sessions = data.sessions || {};
    state.manager = data.manager || null;
    render();
  } catch (err) {
    // A dropped poll is not worth breaking the page over; the next is 2s away.
  }
}

/* ── the scene ─────────────────────────────────────────────────────────── */

function seatOf(sessionId, extra) {
  const live = state.sessions[sessionId] || {};
  return {
    session_id: sessionId,
    name: live.name || (extra && extra.name) || "?",
    state: live.state || "IDLE",
    ...extra,
  };
}

function paintOffice() {
  if (!office) return;
  const manager = state.manager ? seatOf(state.manager.session_id, { name: state.manager.name }) : null;
  const reports = state.reports.map((r) =>
    seatOf(r.session_id, { name: r.name, msg_count: r.msg_count, waiting: r.unanswered }));

  office.set({
    manager,
    reports,
    links: state.peerLinks,
    hot: state.focus,
    // Only the newest slice can fly: the office ignores everything it saw on a
    // previous poll, so a first load does not launch a hundred notes at once.
    messages: state.edges.slice(0, 40).map((e) => ({
      id: e.id, from: e.from.session_id, to: e.to.session_id,
    })),
  });

  el("hint").textContent = state.focus
    ? "click the same desk again to see everything"
    : manager
      ? "click a desk to read that thread"
      : "crown a session on the deck to seat this office";
}

/* ── the numbers ───────────────────────────────────────────────────────── */

function paintCounts() {
  const waiting = state.reports.filter((r) => r.unanswered).length;
  el("count-reports").querySelector("b").textContent = state.reports.length;
  el("count-msgs").querySelector("b").textContent = state.edges.length;
  const alert = el("count-waiting");
  alert.querySelector("b").textContent = waiting;
  alert.dataset.hot = waiting ? "1" : "0";

  const crown = el("crown");
  crown.textContent = state.manager ? `♔ ${state.manager.name}` : "no manager";
  crown.dataset.on = state.manager ? "1" : "0";

  // The composer is only live when there is somebody to talk to.
  const box = el("say");
  const send = el("send");
  box.disabled = !state.manager;
  send.disabled = !state.manager;
  box.placeholder = state.manager
    ? `talk to ${state.manager.name}…`
    : "crown a session on the deck to talk to it";
}

/* ── the thread ────────────────────────────────────────────────────────── */

function paintThread() {
  const box = el("messages");
  const shown = state.edges.filter((e) => !state.focus
    || e.from.session_id === state.focus || e.to.session_id === state.focus);

  const who = state.focus ? (state.sessions[state.focus] || {}).name || "thread" : "everything";
  el("thread-head").textContent = `Traffic · ${who} · ${shown.length}`;

  box.replaceChildren();
  for (const edge of shown.slice(0, 120)) {
    const row = document.createElement("article");
    row.className = "msg";
    if (state.manager && edge.from.session_id === state.manager.session_id) {
      row.classList.add("msg--mgr");
    }
    if (edge.status === "sent") row.classList.add("msg--pending");
    if (state.expanded.has(edge.id)) row.classList.add("is-open");

    const head = document.createElement("header");
    const route = document.createElement("span");
    route.className = "msg__route";
    route.textContent = `${edge.from.name} → ${edge.to.name}`;
    const when = document.createElement("span");
    when.className = "msg__when";
    when.textContent = edge.status === "sent" ? `${ago(edge.ts)} · unconfirmed` : ago(edge.ts);
    head.append(route, when);

    const body = document.createElement("p");
    body.className = "msg__body";
    body.textContent = edge.text;

    row.append(head, body);
    row.onclick = () => {
      if (state.expanded.has(edge.id)) state.expanded.delete(edge.id);
      else state.expanded.add(edge.id);
      paintThread();
    };
    box.append(row);
  }

  if (!shown.length) {
    const empty = document.createElement("p");
    empty.className = "msg__empty";
    empty.textContent = "no messages yet";
    box.append(empty);
  }
}

function render() {
  paintCounts();
  paintOffice();
  paintThread();
}

/* Clicking a desk focuses that session's thread; clicking it again clears. */
el("office-canvas").addEventListener("click", (event) => {
  if (!office) return;
  const box = event.currentTarget.getBoundingClientRect();
  const hit = office.hitTest(event.clientX - box.left);
  if (!hit) return;
  state.focus = state.focus === hit ? null : hit;
  render();
});

/* One write path: you to the manager. Nothing else on this page can send. */
el("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const box = el("say");
  const status = el("say-status");
  const text = box.value.trim();
  if (!text) return;

  box.value = "";
  status.textContent = "sending…";
  status.dataset.bad = "0";
  try {
    const res = await fetch("/api/manager/say", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, mute: el("mute").checked }),
    });
    const body = await res.json();
    if (body.ok) {
      status.textContent = el("mute").checked ? "sent · muted" : "sent · listening for the reply";
    } else {
      status.textContent = REFUSALS[body.reason] || body.reason || "refused";
      status.dataset.bad = "1";
      box.value = text;   // hand it back rather than losing what you typed
    }
  } catch (err) {
    status.textContent = "daemon not answering";
    status.dataset.bad = "1";
    box.value = text;
  }
  refresh();
});

const REFUSALS = {
  no_manager: "nobody is crowned",
  no_socket: "that session is gone — crown another",
  unreachable: "the session did not accept it",
  too_long: "too long — 2000 characters max",
  empty: "nothing to send",
  bad_pid: "that is not a session",
};

refresh();
setInterval(refresh, 2000);
