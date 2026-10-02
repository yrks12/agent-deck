#!/usr/bin/env node
/*
 * Agent Deck mirror hook: the agent's commands, shown in its computer.
 *
 * A desk's Bash tool runs on the box (the desk is `claude --bg`, a host
 * process), not in the desk's container -- so the owner watching that
 * computer saw the browser and nothing of the shell work. This hook writes
 * each Bash command (PreToolUse) and its output (PostToolUse) to
 * `<bus>/browser/computers/<desk>/.deck/agent.log`, which is `~/.deck/
 * agent.log` inside the container (the home is a bind mount). The
 * container's tmux session `agent` tails it, attached read-only: the
 * terminal tab's "agent" window and the desktop's Agent button.
 *
 * Runs on every Bash call of every hired desk, so:
 *   - always exit 0, never write to stdout (Claude Code parses it)
 *   - a desk with no computer gets nothing created
 *   - output is capped, and escapes other than colours are stripped
 */

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

const BUS =
  process.env.DECK_BUS_DIR ||
  path.join(
    process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"),
    "agent-bus"
  );
const COMPUTERS = path.join(BUS, "browser", "computers");
const MAX_LOG = 1024 * 1024;
const MAX_LINES = 200;
const MAX_CHARS = 32 * 1024;

const DIM = "\x1b[2m";
const BOLD_GREEN = "\x1b[1;32m";
const BOLD_BLUE = "\x1b[1;34m";
const RED = "\x1b[31m";
const YELLOW = "\x1b[33m";
const RESET = "\x1b[0m";

function deskName(sessionId) {
  try {
    const data = JSON.parse(fs.readFileSync(path.join(BUS, "office.json"), "utf8"));
    const mine = (data.sessions || {})[sessionId];
    return mine && typeof mine.name === "string" ? mine.name : "";
  } catch (_) {
    return "";
  }
}

/* Colours (CSI ... m) survive; every other escape, BEL and stray CR do not:
 * an OSC 52 in a command's output would otherwise write the owner's
 * clipboard, and an OSC 0 retitle his window. */
function clean(text) {
  return String(text || "")
    .replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)?/g, "")
    .replace(/\x1b\[[0-9;?]*[A-Za-z]/g, (m) => (m.endsWith("m") ? m : ""))
    .replace(/\x1b[^[]/g, "")
    .replace(/\r\n/g, "\n")
    .replace(/[\x00-\x08\x0b-\x0c\x0d\x0e-\x1a\x1c-\x1f\x7f]/g, "");
}

function cap(text) {
  let body = text.length > MAX_CHARS ? text.slice(0, MAX_CHARS) : text;
  const lines = body.split("\n");
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  const total = text.split("\n").length - (text.endsWith("\n") ? 1 : 0);
  if (lines.length > MAX_LINES || text.length > MAX_CHARS) {
    const shown = lines.slice(0, MAX_LINES);
    return (
      shown.join("\n") +
      `\n${DIM}... ${Math.max(total - shown.length, 1)} more lines not shown${RESET}\n`
    );
  }
  return lines.length ? lines.join("\n") + "\n" : "";
}

function shortCwd(cwd) {
  const home = os.homedir();
  const value = String(cwd || "");
  return home && value.startsWith(home) ? "~" + value.slice(home.length) : value;
}

function stamp() {
  const d = new Date();
  const two = (n) => String(n).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

function formatPre(desk, payload) {
  const input = payload.tool_input || {};
  const lines = clean(input.command).split("\n");
  const command = lines[0] + lines.slice(1).map((l) => "\n> " + l).join("");
  let out = `\n${DIM}-- ${stamp()} --${RESET}\n`;
  if (input.description) out += `${DIM}# ${clean(input.description)}${RESET}\n`;
  out += `${BOLD_GREEN}${desk}${RESET}:${BOLD_BLUE}${shortCwd(payload.cwd)}${RESET}$ ${command}\n`;
  return out;
}

function formatPost(payload) {
  const r = payload.tool_response || {};
  if (typeof r === "string") return cap(clean(r));
  let out = cap(clean(r.stdout));
  const err = cap(clean(r.stderr));
  if (err) {
    out += err
      .split("\n")
      .map((l) => (l ? RED + l + RESET : l))
      .join("\n");
  }
  if (r.backgroundTaskId) out += `${YELLOW}(running in the background)${RESET}\n`;
  if (r.interrupted) out += `${YELLOW}(interrupted)${RESET}\n`;
  return out;
}

function main() {
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(0, "utf8"));
  } catch (_) {
    return;
  }
  if (!payload || payload.tool_name !== "Bash") return;
  const event = payload.hook_event_name;
  if (event !== "PreToolUse" && event !== "PostToolUse") return;

  const desk = deskName(payload.session_id);
  if (!desk || desk.includes("/") || desk.includes("\\") || desk.startsWith(".")) return;
  const home = path.join(COMPUTERS, desk);
  if (!fs.existsSync(home)) return; // no computer, nothing to show it on

  const text = event === "PreToolUse" ? formatPre(desk, payload) : formatPost(payload);
  if (!text) return;
  const dir = path.join(home, ".deck");
  const file = path.join(dir, "agent.log");
  try {
    fs.mkdirSync(dir, { recursive: true });
    try {
      if (fs.statSync(file).size > MAX_LOG) fs.truncateSync(file, 0);
    } catch (_) {}
    fs.appendFileSync(file, text);
  } catch (_) {}
}

try {
  main();
} catch (err) {
  try {
    process.stderr.write("[cc-mirror] " + (err && err.message) + "\n");
  } catch (_) {}
}
process.exit(0);
