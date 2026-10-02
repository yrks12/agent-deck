/* Shaliach - the Standing approvals screen. The owner's "always, up to a
   limit" policies: what is proposed, what is live, how much of today's limit is
   used. Talks to the board's /api mirror of the standing routes
   (docs/client-api.md section 23), which the board's cookie already
   authorises. Every string from the server goes through esc() before it
   touches innerHTML. */

const NOUN = {
  send_email: "emails", spend_money: "purchases", post_comment: "comments",
  run_command: "commands", call_api: "calls",
};

const REFUSALS = {
  bad_kind: "That is not a kind of action a standing approval can cover.",
  missing_field: "Choose which desk this is for.",
  no_limit: "Set a daily limit: a count, or a dollar amount. Spending always needs a dollar limit.",
  bad_limit: "Limits and the expiry must be numbers above zero.",
  too_wide: "A command policy needs a command pattern, such as gh pr*.",
  bad_policy: "That is not a standing approval.",
  unknown_policy: "That policy no longer exists.",
  unknown_ask: "That approval card no longer exists.",
  never_coverable: "Money to others, deleting data, credentials and account security always ask you. A standing approval cannot cover them.",
  not_proposed: "That policy is not waiting for approval.",
  revoked: "That policy was revoked, so it cannot change.",
  no_desk: "This card's desk cannot be named, so a policy cannot be scoped to it.",
  unauthorized: "The board session expired. Reopen the board with bin/cdash.",
};

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function html(parts, ...values) {
  return parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ""), "");
}
function raw(fragment) { return fragment; }

function usd(n) { return "$" + (Number(n) || 0).toFixed(2); }

// "43/80 emails today", "$1.20/$5.00 today". Nothing for a limit that is not set.
function usageLines(policy) {
  const limits = policy.limits || {};
  const usage = policy.usage || {};
  const lines = [];
  if (limits.count_per_day != null) {
    lines.push(`${usage.count || 0}/${limits.count_per_day} ${NOUN[policy.kind] || "actions"} today`);
  }
  if (limits.usd_per_day != null) {
    lines.push(`${usd(usage.usd)}/${usd(limits.usd_per_day)} today`);
  }
  return lines;
}

const RANK = { proposed: 0, active: 1, revoked: 2 };
function ordered(rows) {
  return rows.map((r, i) => [r, i])
    .sort((a, b) => (RANK[a[0].status] ?? 3) - (RANK[b[0].status] ?? 3) || a[1] - b[1])
    .map((pair) => pair[0]);
}

function proposedCount(rows) {
  return rows.filter((r) => r.status === "proposed").length;
}

function refusalText(body) {
  const b = body || {};
  return REFUSALS[b.reason] || b.detail || b.reason || "That did not work.";
}

function num(text) {
  const t = String(text == null ? "" : text).trim();
  return t === "" ? null : Number(t);
}

// The form's strings as the contract body. Blank stays null so the server, not
// this file, decides what is too wide or has no limit.
function policyBody(f) {
  const day = String(f.expires || "").trim();
  return {
    desk: String(f.desk || "").trim(),
    kind: f.kind,
    tool: String(f.tool || "").trim() || "*",
    pattern: String(f.pattern || "").trim() || "*",
    limits: {
      count_per_day: num(f.count),
      usd_per_day: num(f.usd),
      recipients: String(f.recipients || "").split(",").map((x) => x.trim()).filter(Boolean),
      account: String(f.account || "").trim(),
    },
    expires_at: day ? Math.floor(Date.parse(day + "T00:00:00Z") / 1000) : null,
    note: String(f.note || ""),
  };
}

function standingCards(rows) {
  return (rows || []).filter((r) => r.standing_option && r.standing_option.available);
}

// What POST standing_option.route takes: the limits, an expiry and a note.
function limitBody(f) {
  const day = String(f.expires || "").trim();
  return {
    limits: { count_per_day: num(f.count), usd_per_day: num(f.usd) },
    expires_at: day ? Math.floor(Date.parse(day + "T00:00:00Z") / 1000) : null,
    note: String(f.note || ""),
  };
}

function auditText(line) {
  const when = new Date(line.ts * 1000).toLocaleString("en-GB");
  const cost = line.cost_usd ? ` (${usd(line.cost_usd)})` : "";
  return `${when} · ${line.desk} · ${line.kind} · ${line.action}${cost} · ${line.count_after} today`;
}

if (typeof module !== "undefined") {
  module.exports = { esc, usageLines, ordered, proposedCount, refusalText, policyBody, auditText, standingCards, limitBody };
}

/* ── the screen ──────────────────────────────────────────────────────── */

if (typeof document !== "undefined" && document.getElementById("standing")) {
  const root = document.getElementById("standing");
  const badge = document.getElementById("standing-badge");
  const BASE = "/api/standing-approvals";
  let policies = [];
  let said = "";
  let editing = null; // null | "new" | a policy id
  let audit = [];
  let asks = [];
  let limiting = null; // the approval id whose limit form is open
  const KINDS = Object.keys(NOUN);

  async function api(method, path, body) {
    return call(method, BASE + path, body);
  }

  async function call(method, url, body) {
    const headers = {};
    if (body !== undefined) headers["content-type"] = "application/json";
    const res = await fetch(url, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(refusalText(data));
    return data;
  }

  function paintBadge() {
    const n = proposedCount(policies);
    badge.textContent = n ? String(n) : "";
    badge.hidden = n === 0;
  }

  function who(p) {
    return p.desk === "*" ? "every desk" : p.desk;
  }

  function what(p) {
    const bits = [p.kind.replace(/_/g, " ")];
    if (p.pattern && p.pattern !== "*") bits.push(p.pattern);
    return bits.join(" · ");
  }

  function policyCard(p) {
    const usage = usageLines(p).map((l) => html`<span class="standing__use">${esc(l)}</span>`).join("");
    const meta = [];
    if (p.status === "proposed") meta.push(`proposed by ${p.created_by}`);
    if (p.expired) meta.push("expired");
    else if (p.expires_at) meta.push("until " + new Date(p.expires_at * 1000).toLocaleDateString("en-GB"));
    if (p.note) meta.push(p.note);
    const acts = p.status === "proposed"
      ? html`<button type="button" data-act="approve" data-id="${esc(p.id)}">Approve</button>
             <button type="button" data-act="revoke" data-id="${esc(p.id)}">Dismiss</button>`
      : p.status === "active"
        ? html`<button type="button" data-act="edit" data-id="${esc(p.id)}">Edit</button>
               <button type="button" data-act="revoke" data-id="${esc(p.id)}">Revoke</button>` : "";
    const liveClass = p.live ? " standing--live" : "";
    return html`<article class="standing__card standing--${esc(p.status)}${raw(liveClass)}">
      <header><b>${esc(who(p))}</b><span>${esc(what(p))}</span><em>${esc(p.status)}</em></header>
      <div class="standing__usage">${raw(usage)}</div>
      <div class="standing__meta">${esc(meta.join(" · "))}</div>
      <div class="standing__acts">${raw(acts)}</div></article>`;
  }

  function formFor(p) {
    const v = p || { desk: "", kind: "send_email", tool: "*", pattern: "*", limits: {}, note: "" };
    const l = v.limits || {};
    const chosen = v.kind;
    const kinds = KINDS.map((k) =>
      html`<option value="${esc(k)}"${raw(k === chosen ? " selected" : "")}>${esc(k.replace(/_/g, " "))}</option>`).join("");
    const day = v.expires_at ? new Date(v.expires_at * 1000).toISOString().slice(0, 10) : "";
    const field = (label, name, value, hint) =>
      html`<label>${esc(label)}<input name="${esc(name)}" value="${esc(value)}" placeholder="${esc(hint || "")}"></label>`;
    return html`<form class="standing__form" data-form="policy" data-id="${esc(p ? p.id : "")}">
      ${raw(field("Desk (* = every desk)", "desk", v.desk))}
      <label>Kind<select name="kind"${p ? " disabled" : ""}>${raw(kinds)}</select></label>
      ${raw(field("Tool", "tool", v.tool))}
      ${raw(field("Command pattern", "pattern", v.pattern, "gh pr*"))}
      ${raw(field("Max per day", "count", l.count_per_day, "80"))}
      ${raw(field("Max dollars per day", "usd", l.usd_per_day, "5"))}
      ${raw(field("Recipients, comma separated", "recipients", (l.recipients || []).join(", "), "@acme.com"))}
      ${raw(field("Account", "account", l.account))}
      <label>Expires<input name="expires" type="date" value="${esc(day)}"></label>
      ${raw(field("Note", "note", v.note))}
      <div class="standing__acts"><button type="submit">Save</button>
      <button type="button" data-act="cancel">Cancel</button></div></form>`;
  }

  function askCard(a) {
    const route = a.standing_option.route;
    const open = limiting === a.id;
    const form = open ? html`<form class="standing__form" data-form="always" data-route="${esc(route)}">
      <label>Max per day<input name="count" placeholder="20"></label>
      <label>Max dollars per day<input name="usd" placeholder="5"></label>
      <label>Expires<input name="expires" type="date"></label>
      <label>Note<input name="note"></label>
      <div class="standing__acts"><button type="submit">Allow up to this limit</button>
      <button type="button" data-act="cancel">Cancel</button></div></form>` : "";
    return html`<article class="standing__card standing--proposed">
      <header><b>${esc(a.agent)}</b><span>${esc(a.tool)}</span></header>
      <div class="standing__meta">${esc(a.subject)}</div>
      <div class="standing__meta">${esc(a.standing_option.summary)}</div>
      <div class="standing__acts"><button type="button" data-act="always" data-id="${esc(a.id)}">Always, up to a limit…</button></div>
      ${raw(form)}</article>`;
  }

  function askList() {
    const rows = standingCards(asks);
    if (!rows.length) return "";
    return html`<section class="standing__asks"><h3>Waiting for you</h3>
      <div class="standing__cards">${raw(rows.map(askCard).join(""))}</div></section>`;
  }

  function auditList() {
    if (!audit.length) return "";
    const items = audit.slice(0, 50).map((l) => html`<li>${esc(auditText(l))}</li>`).join("");
    return html`<section class="standing__audit"><h3>Audit log</h3><ul>${raw(items)}</ul></section>`;
  }

  function paint() {
    const rows = ordered(policies);
    const body = rows.length ? rows.map(policyCard).join("")
      : html`<p class="standing__empty">No standing approvals yet.</p>`;
    root.innerHTML = html`<div class="standing__head"><h2>Standing approvals</h2>
      <span><button type="button" data-act="add">Add</button>
      <button type="button" data-act="reload">Refresh</button></span></div>
      <p class="standing__said" role="status">${esc(said)}</p>
      ${raw(editing ? formFor(editing === "new" ? null : policies.find((x) => x.id === editing)) : "")}
      ${raw(askList())}
      <div class="standing__cards">${raw(body)}</div>${raw(auditList())}`;
    paintBadge();
  }

  function paintFailed(message) {
    root.innerHTML = html`<div class="standing__head"><h2>Standing approvals</h2>
      <button type="button" data-act="reload">Refresh</button></div>
      <p class="standing__said">${esc(message)}</p>`;
  }

  async function load() {
    try {
      policies = (await api("GET", "")).policies || [];
      audit = (await api("GET", "/audit?limit=50")).audit || [];
      asks = await call("GET", "/api/approvals").then((r) => r.approvals || [], () => []);
      paint();
    } catch (err) {
      paintFailed(err.message);
    }
  }

  async function act(name, id) {
    try {
      if (name === "approve") await api("POST", `/${encodeURIComponent(id)}/approve`);
      else if (name === "revoke") await api("DELETE", `/${encodeURIComponent(id)}`);
      said = "";
    } catch (err) { said = err.message; }
    await load();
  }

  async function allow(form) {
    const body = limitBody(Object.fromEntries(new FormData(form).entries()));
    try {
      await call("POST", form.dataset.route.replace(/^\/v\d+/, "/api"), body);
      said = "Allowed up to that limit, and this one went through.";
      limiting = null;
    } catch (err) { said = err.message; }
    await load();
  }

  async function save(form) {
    const f = Object.fromEntries(new FormData(form).entries());
    const id = form.dataset.id;
    if (id) f.kind = (policies.find((x) => x.id === id) || {}).kind;
    const body = policyBody(f);
    try {
      if (id) { delete body.kind; await api("PATCH", `/${encodeURIComponent(id)}`, body); }
      else await api("POST", "", body);
      said = "";
      editing = null;
    } catch (err) { said = err.message; }
    await load();
  }

  root.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-act]");
    if (!btn) return;
    const name = btn.dataset.act;
    if (name === "reload") load();
    else if (name === "add" || name === "edit") { editing = name === "add" ? "new" : btn.dataset.id; paint(); }
    else if (name === "cancel") { editing = null; limiting = null; paint(); }
    else if (name === "always") { limiting = btn.dataset.id; paint(); }
    else act(btn.dataset.act, btn.dataset.id);
  });
  root.addEventListener("submit", (ev) => {
    const always = ev.target.closest("[data-form=always]");
    if (always) { ev.preventDefault(); allow(always); return; }
    const policy = ev.target.closest("[data-form=policy]");
    if (policy) { ev.preventDefault(); save(policy); return; }
  });

  function showView() {
    const on = location.hash === "#standing";
    for (const id of ["floor", "desks", "deck", "money"]) {
      const el = document.getElementById(id);
      if (!el) continue;
      if (on) el.style.display = "none";
      else if (id !== "money") el.style.display = "";
    }
    if (on) document.getElementById("money").hidden = true;
    root.hidden = !on;
    for (const tab of document.querySelectorAll(".tabs .tab")) {
      if (on) tab.classList.toggle("tab--on", tab.getAttribute("href") === "/#standing");
    }
    if (on) load();
  }

  window.addEventListener("hashchange", showView);
  showView();
  load();
  setInterval(() => { if (!editing && !limiting) load(); }, 30000);
}
