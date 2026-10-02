#!/usr/bin/env node
/*
 * Agent Deck MCP-elicitation hook (Elicitation).
 *
 * Same fault, same shape, different event. `cc-permission.js` closed the case
 * where Claude Code itself asks a hired desk for permission. This closes the
 * case where an *MCP server* asks it for input -- Claude Code's own words for
 * the event are "Fired when an MCP server requests user input. Hooks can
 * auto-respond (accept/decline) instead of showing the dialog." The deck
 * registered nothing for it, so those dialogs were drawn on desks with nobody
 * sitting at them.
 *
 * MEASURED off the 2.1.252 dispatcher, not read off documentation, because a
 * wrong key here fails SILENTLY -- the hook runs, prints, is ignored, and the
 * dialog appears exactly as before:
 *
 *     case "Elicitation":
 *       if (e.hookSpecificOutput.action) {
 *         M.elicitationResponse = {action: e.hookSpecificOutput.action,
 *                                  content: e.hookSpecificOutput.content};
 *         if (e.hookSpecificOutput.action === "decline")
 *           M.blockingError = {blockingError: e.reason || "Elicitation denied by hook"}
 *       }
 *
 * So: `action` and `content` go DIRECTLY on `hookSpecificOutput`, and the
 * top-level `reason` is the sentence the agent is left holding.
 * `elicitationResponse` is the CLI's internal name for the parsed result and is
 * not a key a hook writes.
 *
 * The fail-closed floor, from the same code:
 *
 *     if (blockingError) return {action: "decline"};
 *     if (response) return {action: response.action, content: response.content};
 *     return;                    // <-- undefined: the dialog IS shown
 *
 * A hook that prints nothing, crashes, or prints a shape the CLI does not
 * recognise therefore falls through to the dialog. That is the stall. So the
 * contract is the same as cc-permission.js's, with "decline" where that one
 * says "deny":
 *
 *   - always exit 0
 *   - always print exactly one response object, even when everything failed
 *   - every failure prints "decline", never "accept". An accept would hand an
 *     MCP server a value nobody chose; a missing print hands the desk a dialog.
 *   - never exceed the timeout budget; this runs with a dialog half-drawn
 *   - no dependencies, no stderr on the happy path
 */

"use strict";

const http = require("node:http");

const TIMEOUT_MS = 2000;
const DEFAULT_URL = "http://127.0.0.1:7788";

// `/api` requires the deck's token now. Read from a file, not handed in as an
// env var: `server/approval.py` names this path in the generated --settings so
// a hired desk does not have to guess it, but the secret itself never enters
// that JSON, and a rotation therefore does not strand desks hired before it.
//
// Duplicated from `cc-approve.js` rather than shared. Every hook here is
// dependency-free by design and always answers; a `require` of a sibling file
// adds a load-time throw -- a hook that crashes before its own try/catch -- to
// the hot path of every session, to save fifteen lines.
const DEFAULT_TOKEN_FILE = require("node:path").join(
  process.env.DECK_BUS_DIR ||
    require("node:path").join(
      process.env.CLAUDE_CONFIG_DIR ||
        require("node:path").join(require("node:os").homedir(), ".claude"),
      "agent-bus"
    ),
  "deck-token.txt"
);

function deckToken() {
  try {
    return require("node:fs")
      .readFileSync(process.env.DECK_TOKEN_FILE || DEFAULT_TOKEN_FILE, "utf8")
      .trim();
  } catch (_) {
    // No readable token is the same outage as no reachable daemon: the request
    // goes out, gets a 401, and the existing failure path below handles it.
    return "";
  }
}


// The only thing the model sees when the deck could not be reached. A sentence,
// not a slug: a refusal the agent cannot explain to its boss is as opaque as
// the dialog was.
const OFFLINE =
  "Declined: an MCP server asked for input, and the Agent Deck could not be " +
  "reached to put that to the owner. Nothing was supplied. Say what you are " +
  "blocked on in your reply rather than retrying.";

let answered = false;
let request = null;

const guard = setTimeout(function () {
  emit("The Agent Deck did not answer in time. " + OFFLINE);
}, TIMEOUT_MS);

// `content` is deliberately never sent. This hook has exactly one word in its
// vocabulary, so there is no path on which it fabricates a value for a form it
// never showed anyone.
function emit(reason) {
  if (answered) return;
  answered = true;
  try {
    clearTimeout(guard);
  } catch (_) {}
  try {
    if (request) request.destroy();
  } catch (_) {}

  const line =
    JSON.stringify({
      reason: String(reason || OFFLINE),
      hookSpecificOutput: {
        hookEventName: "Elicitation",
        action: "decline",
      },
    }) + "\n";

  // Exit from the write callback: stdout is a pipe, the write is async, and a
  // bare process.exit() can truncate the response into invalid JSON -- which
  // the CLI would read as "no response" and draw the dialog.
  try {
    process.stdout.write(line, function () {
      process.exit(0);
    });
  } catch (_) {
    process.exit(0);
  }
  const bail = setTimeout(function () {
    process.exit(0);
  }, 500);
  if (bail.unref) bail.unref();
}

function ask(payload) {
  let target;
  try {
    target = new URL("/api/elicitation", process.env.DECK_URL || DEFAULT_URL);
  } catch (_) {
    return emit(OFFLINE);
  }

  const body = Buffer.from(JSON.stringify(payload));
  const options = {
    hostname: target.hostname,
    port: target.port || 80,
    path: target.pathname + target.search,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Content-Length": body.length,
      Authorization: "Bearer " + deckToken(),
    },
    timeout: TIMEOUT_MS,
  };

  try {
    request = http.request(options, function (res) {
      let raw = "";
      res.setEncoding("utf8");
      res.on("data", function (chunk) {
        raw += chunk;
      });
      res.on("end", function () {
        if (res.statusCode !== 200) return emit(OFFLINE);
        let answer;
        try {
          answer = JSON.parse(raw);
        } catch (_) {
          return emit(OFFLINE);
        }
        if (!answer || typeof answer !== "object") return emit(OFFLINE);
        // The deck's reason when it gave one -- it names the ask id the owner
        // will answer against. Anything else about the response is discarded:
        // this hook declines, and a future deck that learns to say "accept"
        // must ship its own hook rather than widening this one by accident.
        emit(answer.reason || OFFLINE);
      });
      res.on("error", function () {
        emit(OFFLINE);
      });
    });
  } catch (_) {
    return emit(OFFLINE);
  }

  request.on("error", function () {
    emit(OFFLINE);
  });
  request.on("timeout", function () {
    emit(OFFLINE);
  });

  try {
    request.end(body);
  } catch (_) {
    emit(OFFLINE);
  }
}

function main() {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", function (chunk) {
    raw += chunk;
  });
  process.stdin.on("error", function () {
    emit(OFFLINE);
  });
  process.stdin.on("end", function () {
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (_) {
      return emit(OFFLINE);
    }
    if (!payload || typeof payload !== "object") return emit(OFFLINE);
    ask({
      mcp_server_name: payload.mcp_server_name || "",
      message: payload.message || "",
      mode: payload.mode || "",
      url: payload.url || "",
      elicitation_id: payload.elicitation_id || "",
      requested_schema: payload.requested_schema || {},
      session_id: payload.session_id || "",
      cwd: payload.cwd || "",
    });
  });
}

try {
  main();
} catch (_) {
  emit(OFFLINE);
}
