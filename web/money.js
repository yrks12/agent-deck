/* Shaliach - the Money view. One page of answers for the founder: what came
   in, what went out, what is left, per company. Reads /v1/money. Every string
   from the server goes through esc() before it touches innerHTML. */

const MONEY_URL = "/api/money";
const moneyEl = document.getElementById("money");
const SYMBOL = { GBP: "£", USD: "$", EUR: "€" };
let moneyCurrency = "GBP";

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Template tag: plain strings are joined as-is, so every interpolation must
// already be a safe fragment (esc/money/pct/raw).
function html(parts, ...values) {
  return parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ""), "");
}
function raw(fragment) { return fragment; }

function money(amount) {
  const n = Number(amount) || 0;
  const sym = SYMBOL[moneyCurrency] || esc(moneyCurrency) + " ";
  const body = Math.abs(n).toLocaleString("en-GB", { maximumFractionDigits: 0 });
  return esc((n < 0 ? "-" : "") + sym + body);
}

function pct(roi) {
  return esc((roi > 0 ? "+" : "") + Math.round(roi * 100) + "%");
}

function roiBadge(roi) {
  if (roi == null) return raw("");
  const cls = roi >= 0 ? "money__roi--up" : "money__roi--down";
  return html`<span class="money__roi ${cls}">ROI ${pct(roi)}</span>`;
}

function netClass(n) { return Number(n) < 0 ? "money__neg" : "money__pos"; }

function bigNumber(label, amount, signed) {
  const cls = signed ? netClass(amount) : "";
  return html`<div class="money__big"><span>${esc(label)}</span><b class="${cls}">${money(amount)}</b></div>`;
}

function companyCard(c) {
  const claude = c.claude ? html`<div class="money__claude">Claude work ≈ ${money(c.claude.api_usd)}</div>` : "";
  const dim = c.overhead ? " money__card--dim" : "";
  return html`<article class="money__card${dim}">
    <header><h3>${esc(c.name)}</h3>${roiBadge(c.roi)}</header>
    <dl>
      <div><dt>In</dt><dd>${money(c.revenue)}</dd></div>
      <div><dt>Out</dt><dd>${money(c.cost_total)}</dd></div>
      <div><dt>Net</dt><dd class="${netClass(c.net)}">${money(c.net)}</dd></div>
    </dl>${claude}</article>`;
}

function flagged(list, days) {
  const rows = (list || []).filter((e) => e.flag);
  if (!rows.length) return "";
  const items = rows.map((e) => html`<li><b>${esc(e.desk)}</b> <span>${esc(e.company)}</span>
    <em>${esc(e.days)} days</em></li>`).join("");
  return html`<section class="money__flags"><h3>No sales after ${esc(days)} days</h3><ul>${items}</ul></section>`;
}

function connectCard(c) {
  return html`<article class="money__connect money__connect--${esc(c.state)}">
    <h3>${esc(c.title)}</h3><p>${esc(c.detail)}</p></article>`;
}

function paintMoney(data) {
  if (!data || data.state !== "ready") {
    moneyEl.innerHTML = html`<div class="money__head"><h2>Money</h2></div>
      <p class="money__warm">Warming up. The numbers appear in a moment.</p>${refreshButton()}`;
    return;
  }
  moneyCurrency = data.currency || "GBP";
  const t = data.totals || {};
  const companies = (data.companies || []).slice().sort((a, b) => Number(!!a.overhead) - Number(!!b.overhead));
  const connect = (data.connect || []).filter((c) => c.state !== "connected");
  const notes = ["Costs include Claude work at API prices"];
  if (data.fx_assumed) notes.push("Exchange rates approximate");
  moneyEl.innerHTML = html`<div class="money__head"><h2>Money</h2>${refreshButton()}</div>
    <div class="money__totals">${bigNumber("Money in", t.revenue)}${bigNumber("Money out", t.costs)}${bigNumber("Net", t.net, true)}</div>
    <div class="money__sub">since start</div>
    <div class="money__cards">${companies.map(companyCard).join("")}</div>
    ${flagged(data.experiments, data.flag_days || 14)}
    <div class="money__connects">${connect.map(connectCard).join("")}</div>
    <footer class="money__notes">${notes.map((n) => `<p>${esc(n)}</p>`).join("")}</footer>`;
}

function refreshButton() {
  return html`<button type="button" class="money__refresh" data-refresh="1">Refresh</button>`;
}

async function loadMoney(refresh) {
  try {
    const res = await fetch(refresh ? MONEY_URL + "?refresh=1" : MONEY_URL, { credentials: "same-origin" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    paintMoney(await res.json());
  } catch (err) {
    moneyEl.innerHTML = html`<div class="money__head"><h2>Money</h2>${refreshButton()}</div>
      <p class="money__warm">Could not load the numbers (${esc(err.message)}).</p>`;
  }
}

function showView() {
  const on = location.hash === "#money";
  for (const id of ["floor", "desks", "deck"]) {
    const el = document.getElementById(id);
    if (el) el.style.display = on ? "none" : "";
  }
  moneyEl.hidden = !on;
  for (const tab of document.querySelectorAll(".tabs .tab")) {
    const mine = tab.getAttribute("href") === "/#money";
    tab.classList.toggle("tab--on", on ? mine : tab.getAttribute("href") === "/");
  }
  if (on) loadMoney(false);
}

moneyEl.addEventListener("click", (ev) => {
  if (ev.target.closest("[data-refresh]")) loadMoney(true);
});
window.addEventListener("hashchange", showView);
showView();
