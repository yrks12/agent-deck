#!/usr/bin/env node
/*
 * Agent Deck event bus hook.
 *
 * Reads a Claude Code hook payload on stdin and appends one JSON line to
 * ~/.claude/agent-bus/events.jsonl. That log is the only reliable source for
 * "this session is blocked waiting on you" -- `claude agents --json` reports
 * idle|busy|shell and nothing finer.
 *
 * This runs inside EVERY Claude Code session, so the contract is strict:
 *   - always exit 0, whatever happens
 *   - never write to stdout (Claude Code parses stdout as hook output)
 *   - one appendFileSync of a single short line; no read-modify-write
 */

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

const MAX_BYTES = 5 * 1024 * 1024;

function main() {
  let raw = "";
  try {
    raw = fs.readFileSync(0, "utf8");
  } catch (_) {
    return;
  }
  if (!raw.trim()) return;

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch (_) {
    return;
  }
  if (!payload || typeof payload !== "object") return;

  const event = payload.hook_event_name;
  const sessionId = payload.session_id;
  if (!event || !sessionId) return;

  // Debug aid: `touch ~/.claude/agent-bus/.debug` to capture raw payloads.
  try {
    const dbgDir = path.join(
      process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"),
      "agent-bus"
    );
    if (fs.existsSync(path.join(dbgDir, ".debug"))) {
      fs.appendFileSync(path.join(dbgDir, "debug.jsonl"), raw.trim() + "\n");
    }
  } catch (_) {}

  const line = { ts: Date.now() / 1000, event, session_id: sessionId };

  if (event === "Notification") {
    line.notification_type = payload.notification_type || "";
    // Truncated: the deck shows one line, and the log stays small.
    line.message = String(payload.message || "").slice(0, 300);
  }
  if (payload.agent_id) line.agent_id = payload.agent_id;
  if (payload.agent_type) line.agent_type = payload.agent_type;
  if (event === "PostToolUse" && payload.tool_name) line.tool = payload.tool_name;
  if (event === "SessionEnd" && payload.source) line.source = payload.source;

  const dir = path.join(
    process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"),
    "agent-bus"
  );
  const file = path.join(dir, "events.jsonl");

  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch (_) {
    return;
  }

  // Rotate before appending. The reader detects the inode change and resets
  // its byte offset, so truncation is safe mid-stream.
  try {
    if (fs.statSync(file).size > MAX_BYTES) fs.truncateSync(file, 0);
  } catch (_) {
    /* missing file is the normal first-run case */
  }

  try {
    fs.appendFileSync(file, JSON.stringify(line) + "\n");
  } catch (_) {
    /* a full or read-only disk must not break the session */
  }

  // Separate ledger of file writes -- this is what the collision guard and the
  // "who touched what" briefing read. Kept apart from events.jsonl so the guard
  // never has to scan the much noisier event log.
  const EDIT_TOOLS = { Edit: 1, Write: 1, NotebookEdit: 1, MultiEdit: 1 };
  if (event === "PostToolUse" && EDIT_TOOLS[payload.tool_name]) {
    const target =
      payload.tool_input &&
      (payload.tool_input.file_path || payload.tool_input.notebook_path);
    if (typeof target === "string" && target) {
      const edits = path.join(dir, "edits.jsonl");
      try {
        if (fs.statSync(edits).size > MAX_BYTES) fs.truncateSync(edits, 0);
      } catch (_) {}
      try {
        fs.appendFileSync(
          edits,
          JSON.stringify({
            ts: line.ts,
            session_id: sessionId,
            cwd: payload.cwd || "",
            file: target,
            tool: payload.tool_name,
          }) + "\n"
        );
      } catch (_) {}
    }
  }
}

try {
  main();
} catch (err) {
  try {
    process.stderr.write("[cc-bus] " + (err && err.message) + "\n");
  } catch (_) {}
}
process.exit(0);
