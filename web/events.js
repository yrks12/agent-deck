// The Events view (Feature C): recent events, where each went, and the
// routing table. Every value is set with textContent / .value -- an event's
// summary is outside data (an email subject, a customer's name) and must
// never become markup.
"use strict";

const routesBody = document.getElementById("routes");
const eventsBody = document.getElementById("events");
const said = document.getElementById("routes-said");
const FIELDS = ["source", "account", "kind", "desk"];

function cell(row, text, cls) {
  const td = document.createElement("td");
  td.textContent = text == null ? "" : String(text);
  if (cls) td.className = cls;
  row.appendChild(td);
  return td;
}

function routeRow(route) {
  const tr = document.createElement("tr");
  for (const field of FIELDS) {
    const td = document.createElement("td");
    const input = document.createElement("input");
    input.name = field;
    input.value = route[field] || (field === "account" || field === "kind" ? "*" : "");
    td.appendChild(input);
    tr.appendChild(td);
  }
  const td = document.createElement("td");
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Remove";
  remove.addEventListener("click", () => tr.remove());
  td.appendChild(remove);
  tr.appendChild(td);
  routesBody.appendChild(tr);
}

function drawEvents(rows) {
  eventsBody.textContent = "";
  for (const ev of rows) {
    const tr = document.createElement("tr");
    const when = ev.ts ? new Date(ev.ts * 1000).toLocaleString() : "";
    cell(tr, when);
    cell(tr, [ev.source, ev.account, ev.kind].filter(Boolean).join(" / "));
    cell(tr, `${ev.summary || ""} (ref ${ev.ref || "?"})`, "sum");
    cell(tr, ev.desk || "—");
    const state = String(ev.state || "");
    cell(tr, state, "ev-state--" + state.split(":")[0]);
    eventsBody.appendChild(tr);
  }
}

async function load(withRoutes) {
  const res = await fetch("/api/events");
  if (!res.ok) {
    said.textContent = `Could not load events (${res.status}).`;
    return;
  }
  const body = await res.json();
  drawEvents(body.events || []);
  if (withRoutes) {
    routesBody.textContent = "";
    (body.routes || []).forEach(routeRow);
  }
}

async function save() {
  const routes = [...routesBody.querySelectorAll("tr")].map((tr) => {
    const out = {};
    for (const input of tr.querySelectorAll("input")) out[input.name] = input.value.trim();
    return out;
  });
  const res = await fetch("/api/events/routes", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ routes }),
  });
  const body = await res.json().catch(() => ({}));
  said.textContent = res.ok ? "Saved." : `Not saved: ${body.detail || res.status}`;
}

document.getElementById("add-route").addEventListener("click", () => routeRow({}));
document.getElementById("save-routes").addEventListener("click", save);
load(true);
setInterval(() => load(false), 10000);
