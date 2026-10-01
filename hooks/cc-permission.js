#!/usr/bin/env node
/*
 * Agent Deck prompt-time hook (PermissionRequest).
 *
 * `cc-approve.js` runs at PreToolUse and may answer allow / deny / ask. That
 * "ask" is the hole this file closes. MEASURED against Claude Code 2.1.252 in
 * an interactive pty: a PreToolUse "ask" makes the CLI draw its own modal --
 *
 *     │Hook PreToolUse:Bash requires confirmation for this command:
 *      Do you want to proceed?   ❯ 1. Yes   2. No
 *
 * -- and the session sits on it. Nothing outside the process can answer it: the
 * deck's authed socket injection reported ok/authed/145 bytes and the text
 * landed in the composer UNDERNEATH the modal. `--permission-mode dontAsk` did
 * not suppress it either.
 *
 * `PermissionRequest` fires immediately before that prompt resolves, and its
 * vocabulary is only {"behavior":"allow"} / {"behavior":"deny","message":...}.
 * There is no "ask", so this hook structurally cannot fall through to a modal.
 * Measured: a deny dismisses the prompt, the transcript records "Denied by
 * PermissionRequest hook", and the model reads the message and carries on.
 *
 * Contract, and note the ONE line where it is the opposite of cc-approve.js:
 *   - always exit 0
 *   - always print exactly one decision object, even when everything failed
 *   - every failure prints "deny", never "allow". cc-approve fails to "ask"
 *     because there the fall-through is the prompt the human would have got
 *     anyway. Here the fall-through IS the unanswerable modal, so deny is the
 *     only safe floor -- it never widens permission, and unlike a stall it is
 *     something the agent can read out loud and route around.
 *   - never exceed the timeout budget; this runs with a prompt half-drawn
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
  process.env.CLAUDE_CONFIG_DIR ||
    require("node:path").join(require("node:os").homedir(), ".claude"),
  "agent-bus",
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


// What the agent is told when the deck could not be reached at all. It has to
// be a sentence, not a slug: this string is the only thing the model sees, and
// a denial it cannot explain to its boss is as opaque as the modal was.
const OFFLINE =
  "Blocked: the Agent Deck could not be reached, so this action could not be " +
  "put to the owner. Nothing was run. Say what you are blocked on in your " +
  "reply rather than retrying.";

let answered = false;
let request = null;

const guard = setTimeout(function () {
  emit("deny", "Blocked: the Agent Deck did not answer in time. " + OFFLINE);
}, TIMEOUT_MS);

function emit(behavior, message) {
  if (answered) return;
  answered = true;
  try {
    clearTimeout(guard);
  } catch (_) {}
  try {
    if (request) request.destroy();
  } catch (_) {}

  const decision =
    behavior === "allow"
      ? { behavior: "allow" }
      : { behavior: "deny", message: String(message || OFFLINE) };

  const line =
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PermissionRequest",
        decision: decision,
      },
    }) + "\n";

  // Exit from the write callback: stdout is a pipe, the write is async, and a
  // bare process.exit() can truncate the decision into invalid JSON.
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
    target = new URL("/api/permission", process.env.DECK_URL || DEFAULT_URL);
  } catch (_) {
    return emit("deny", OFFLINE);
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
        if (res.statusCode !== 200) return emit("deny", OFFLINE);
        let answer;
        try {
          answer = JSON.parse(raw);
        } catch (_) {
          return emit("deny", OFFLINE);
        }
        if (!answer || typeof answer !== "object") return emit("deny", OFFLINE);
        // Anything that is not literally "allow" is a denial. An unknown word
        // from a future deck must not become permission.
        if (answer.behavior === "allow") return emit("allow");
        emit("deny", answer.message);
      });
      res.on("error", function () {
        emit("deny", OFFLINE);
      });
    });
  } catch (_) {
    return emit("deny", OFFLINE);
  }

  request.on("error", function () {
    emit("deny", OFFLINE);
  });
  request.on("timeout", function () {
    emit("deny", OFFLINE);
  });

  try {
    request.end(body);
  } catch (_) {
    emit("deny", OFFLINE);
  }
}

function main() {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", function (chunk) {
    raw += chunk;
  });
  process.stdin.on("error", function () {
    emit("deny", OFFLINE);
  });
  process.stdin.on("end", function () {
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (_) {
      return emit("deny", OFFLINE);
    }
    if (!payload || typeof payload !== "object") return emit("deny", OFFLINE);
    ask({
      tool_name: payload.tool_name || "",
      tool_input: payload.tool_input || {},
      session_id: payload.session_id || "",
      cwd: payload.cwd || "",
    });
  });
}

try {
  main();
} catch (_) {
  emit("deny", OFFLINE);
}
