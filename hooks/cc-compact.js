#!/usr/bin/env node
/**
 * PostCompact hook — tell the deck that a session just forgot most of itself.
 *
 * A compaction replaces the early turns with a summary. If the desk's opening
 * brief was only ever a message, the line naming its boss goes with them, and
 * the desk carries on reporting to the owner instead. Nothing errors. Nothing
 * logs. That silence is the bug.
 *
 * The real fix is upstream: server/spawn.py puts the full brief in
 * --append-system-prompt, which is re-sent on every request and cannot be
 * summarised away. This hook is the second layer -- it makes the event VISIBLE
 * on the board, so if the first layer ever stops holding, you find out from a
 * row on a screen rather than from an agent that has quietly gone freelance.
 *
 * Contract, shared with hooks/cc-bus.js: always exit 0, never write to stdout.
 * A hook that prints or fails breaks the session it exists to protect. The
 * deck being down must never take a real session with it.
 */
'use strict';

const http = require('node:http');

const DECK_URL = process.env.DECK_URL || 'http://127.0.0.1:7788';
const TIMEOUT_MS = 2000;

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


function readStdin() {
  return new Promise((resolve) => {
    let buf = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (c) => { buf += c; });
    process.stdin.on('end', () => resolve(buf));
    process.stdin.on('error', () => resolve(''));
  });
}

function post(url, body) {
  return new Promise((resolve) => {
    let target;
    try {
      target = new URL('/api/compact', url);
    } catch {
      return resolve();           // a malformed DECK_URL is not the session's problem
    }
    const payload = Buffer.from(JSON.stringify(body), 'utf8');
    const req = http.request(
      {
        hostname: target.hostname,
        port: target.port || 80,
        path: target.pathname,
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'content-length': payload.length,
          authorization: 'Bearer ' + deckToken(),
        },
        timeout: TIMEOUT_MS,
      },
      (res) => { res.resume(); res.on('end', resolve); }
    );
    req.on('error', resolve);     // deck down: stay quiet, stay out of the way
    req.on('timeout', () => { req.destroy(); resolve(); });
    req.write(payload);
    req.end();
  });
}

(async () => {
  try {
    const raw = await readStdin();
    let payload = {};
    try { payload = JSON.parse(raw || '{}'); } catch { payload = {}; }
    await post(DECK_URL, {
      session_id: payload.session_id || '',
      trigger: payload.trigger || 'auto',
      cwd: payload.cwd || '',
    });
  } catch {
    // Deliberately swallowed. See the contract above.
  }
  process.exit(0);
})();
