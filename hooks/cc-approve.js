#!/usr/bin/env node
/*
 * Agent Deck Auto Review hook (PreToolUse).
 *
 * Claude Code hands this process a tool call as JSON on stdin and reads a
 * permission decision back on stdout. That is the only point where the deck can
 * decide anything: the session socket has no frame that answers an open
 * permission prompt, so a prompt that has already been drawn can never be
 * dismissed remotely. We prevent it instead.
 *
 * This asks the daemon (POST /api/approve) and prints what it says. It decides
 * nothing itself -- the rule engine lives in server/autoreview.py, so one rule
 * set governs every engine rather than being reimplemented per hook.
 *
 * This runs before EVERY tool call in EVERY session under it, so the contract
 * is strict:
 *   - always exit 0
 *   - always print exactly one decision object, even when everything failed
 *   - every failure prints "ask", never "allow": if the deck is down, hung or
 *     talking nonsense, the human gets the prompt they would have got anyway.
 *     Failing the other way would silently auto-approve on an outage.
 *   - never exceed the timeout budget; a hung daemon must not hang the session
 *   - no dependencies, no stderr on the happy path
 */

"use strict";

const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const TIMEOUT_MS = 2000;
const DEFAULT_URL = "http://127.0.0.1:7788";
const DECISIONS = { allow: 1, deny: 1, ask: 1 };

// The deck's fourth answer, and the only one that is not an opinion: "I hold no
// rule about this call." It is emitted as a PreToolUse output with NO
// `permissionDecision` field, which is schema-valid (measured on 2.1.263:
// the field is optional) and leaves Claude Code's own engine -- its rules, and
// in a hired desk its auto-mode classifier -- to decide.
//
// This exists because "ask" is not neutral. A PreToolUse result is returned AS
// the decision before any rule or classifier runs, and a hook "ask" outranks
// even a classifier allow. So answering "ask" for every call the deck had no
// rule about did not observe the owner's thirteen pending prompts, it created
// them.
//
// NOT the CLI's own "defer": that value is print-mode only and is ignored in an
// interactive session. Saying nothing is the portable way to say nothing.
//
// The failure floor below is unchanged. Every failure path still prints "ask",
// and an unrecognised decision still degrades to "ask" -- only this exact
// literal, arriving in a 200 from the deck, produces silence.
const ABSTAIN = "abstain";

// `/api` requires the deck's token now: a Docker Desktop container reached the
// unauthenticated board through the VM gateway with both hostname aliases
// blackholed, and this door is the one worth stealing -- it decides whether a
// tool call in a real session runs.
//
// A file, not an env var carrying the secret. `server/approval.py` names this
// path in the generated --settings so a hired desk does not have to guess it,
// but the token itself never enters that file: it lives beside world-readable
// siblings in the bus, and a rotation would otherwise strand every desk hired
// before it. Read here instead, fresh, once per tool call.
//
// The budget. This runs before every Bash/Read/Write/Edit in every hired
// session with a 2s guard, so nothing here may block on anything but the
// filesystem. MEASURED on this Mac: one readFileSync of the 44-byte token file
// costs 0.0075 ms mean over 1000 reads, against the ~10 ms node already spends
// starting. That is 0.07% of the process this hook already is, and 0.4% of the
// guard. No round trip, no second process, no daemon call to fetch the
// credential for a daemon call.
const DEFAULT_TOKEN_FILE = path.join(
  process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"),
  "agent-bus",
  "deck-token.txt"
);

function deckToken() {
  try {
    return fs
      .readFileSync(process.env.DECK_TOKEN_FILE || DEFAULT_TOKEN_FILE, "utf8")
      .trim();
  } catch (_) {
    // No readable token is the same outage as no reachable daemon: the request
    // goes out anyway, gets a 401, and the existing failure path prints "ask".
    // Never a throw -- an exception here is a tool call that never returns.
    return "";
  }
}

let answered = false;
let request = null;

// Whatever happens -- unparseable stdin, refused connection, a daemon that
// accepts and never replies -- this timer guarantees a decision inside budget.
const guard = setTimeout(function () {
  emit("ask", "auto review timed out");
}, TIMEOUT_MS);

function emit(decision, reason) {
  if (answered) return;
  answered = true;
  try {
    clearTimeout(guard);
  } catch (_) {}
  try {
    if (request) request.destroy();
  } catch (_) {}

  const output = {
    hookEventName: "PreToolUse",
    permissionDecisionReason: String(reason || ""),
  };
  if (decision !== ABSTAIN) {
    output.permissionDecision = DECISIONS[decision] ? decision : "ask";
  }
  const line = JSON.stringify({ hookSpecificOutput: output }) + "\n";

  // Exit from the write callback: stdout is a pipe here, so the write is async
  // and a bare process.exit() can truncate the decision into invalid JSON.
  try {
    process.stdout.write(line, function () {
      process.exit(0);
    });
  } catch (_) {
    process.exit(0);
  }
  // Belt and braces: if that callback never fires, still leave inside budget.
  const bail = setTimeout(function () {
    process.exit(0);
  }, 500);
  if (bail.unref) bail.unref();
}

function ask(payload) {
  let target;
  try {
    target = new URL("/api/approve", process.env.DECK_URL || DEFAULT_URL);
  } catch (_) {
    return emit("ask", "bad DECK_URL");
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
        if (res.statusCode !== 200) {
          return emit("ask", "deck http " + res.statusCode);
        }
        let answer;
        try {
          answer = JSON.parse(raw);
        } catch (_) {
          return emit("ask", "unparseable deck reply");
        }
        if (!answer || typeof answer !== "object") {
          return emit("ask", "unparseable deck reply");
        }
        const reason =
          answer.reason ||
          (answer.rule_id ? "rule " + answer.rule_id : "auto review");
        emit(answer.decision, reason);
      });
      res.on("error", function () {
        emit("ask", "deck read failed");
      });
    });
  } catch (_) {
    return emit("ask", "deck unreachable");
  }

  request.on("error", function () {
    emit("ask", "deck unreachable");
  });
  request.on("timeout", function () {
    emit("ask", "deck timed out");
  });

  try {
    request.end(body);
  } catch (_) {
    emit("ask", "deck unreachable");
  }
}

function main() {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", function (chunk) {
    raw += chunk;
  });
  process.stdin.on("error", function () {
    emit("ask", "no hook payload");
  });
  process.stdin.on("end", function () {
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (_) {
      return emit("ask", "unparseable hook payload");
    }
    if (!payload || typeof payload !== "object") {
      return emit("ask", "unparseable hook payload");
    }
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
  emit("ask", "auto review failed");
}
