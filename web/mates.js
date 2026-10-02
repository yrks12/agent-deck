/* mates.js — the office.

   Every session gets a pixel-art Claude who acts out its state: typing at the
   desk while working, hand up when it needs you, leaning back when it's done,
   dozing when idle. Canvas, not DOM — a hundred animated <div>s would fight the
   1 Hz SSE repaint; one <canvas> per card, all repainted from a single rAF.

   Units, not pixels. Every shape sits on an integer grid and is multiplied by a
   unit size at draw time, so the same sprite is crisp at 2px units inside a card
   and at 3px units on the floor band. */

(() => {
  "use strict";

  /* ── palette ───────────────────────────────────────────────────────── */

  const PAL = {
    body: "#c9805e",
    dark: "#a6644a",
    ink: "#141414",
    gone: "#3b444a",
    goneDark: "#2d363b",

    bot: "#5a8e9e",
    botDark: "#3a5e6e",

    wall: "#0d1114",
    wallTrim: "#161e23",
    floor: "#101619",
    floorLine: "#1d262c",

    deskTop: "#4a382e",
    deskEdge: "#2a1f1a",
    deskLeg: "#33261f",

    bezel: "#1b2226",
    screenOff: "#0f1417",

    pot: "#6b4a3a",
    leaf: "#3f6b4f",
    leafDark: "#2f5340",

    metal: "#8e9aa0",
    water: "#4a7f92",
    steam: "#bed6dc",
    shadow: "rgba(0, 0, 0, 0.38)",
  };

  const ACCENT = {
    WORKING: "#4fd6cf",
    SHELL: "#f0b45a",
    NEEDS_YOU: "#ff5f70",
    DONE: "#5fd98a",
    IDLE: "#38454d",
    DEAD: "#2b3439",
  };

  const accentOf = (state) => ACCENT[state] || ACCENT.IDLE;
  const isBusy = (state) => state === "WORKING" || state === "SHELL";

  const REDUCED =
    !!window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── primitives ────────────────────────────────────────────────────── */

  function rect(g, u, x, y, w, h, fill) {
    if (h <= 0 || w <= 0) return;
    g.fillStyle = fill;
    g.fillRect(Math.round(x * u), Math.round(y * u), Math.round(w * u), Math.round(h * u));
  }

  // Deterministic per-session phase offset, so nobody blinks in lockstep.
  function hash(str) {
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  /* ── the character ─────────────────────────────────────────────────── */

  /* Box is 24 wide x 18 tall and the feet land on oy + 18, so a caller places a
     mate by their baseline and never has to know the sprite's height. */
  const MATE_W = 24;
  const MATE_H = 18;

  function drawClaude(g, u, ox, oy, p) {
    const dead = p.state === "DEAD";
    const skin = dead ? PAL.gone : PAL.body;
    const dark = dead ? PAL.goneDark : PAL.dark;

    const lift = p.lift || 0; // whole body leaves the floor — feet included
    const sink = p.sink || 0; // shoulders drop; feet stay put
    const bx = ox + 2; // body left edge
    const by = oy + 1 - lift + sink; // body top edge

    // The shadow stays on the ground while they hop. That's what sells it.
    rect(g, u, ox + 3 + lift, oy + 17, 18 - lift * 2, 1, PAL.shadow);

    // Four legs in two pairs, exactly like the real thing.
    for (const dx of [0, 3, 11, 14]) rect(g, u, bx + dx, oy + 14 - lift, 2, 4, dark);

    // A slab of a body with a grounded bottom edge.
    rect(g, u, bx, by, 20, 13, skin);
    rect(g, u, bx, by + 12, 20, 1, dark);

    // Eyes: two tall bars, or two flat lines when shut.
    const eyeY = by + 4;
    for (const dx of [6, 12]) {
      if (p.blink) rect(g, u, bx + dx, eyeY + 2, 2, 1, PAL.ink);
      else rect(g, u, bx + dx, eyeY, 2, 4, PAL.ink);
    }

    // The nubs on each flank are the arms — all the acting happens here.
    const armY = by + 5;
    if (p.arm === "wave") {
      // Arm stays welded to the flank; only the hand tips side to side. An arm
      // that swings as one block just reads as a floating stick.
      rect(g, u, bx - 2, armY, 2, 4, dark);
      rect(g, u, bx + 20, by - 2, 2, 7, dark);
      rect(g, u, bx + 20 + (p.wavePhase ? 1 : -1), by - 4, 2, 2, dark);
    } else if (p.arm === "stretch") {
      // Up and out, so it reads as a stretch rather than a pair of ears.
      rect(g, u, bx - 2, by + 1, 2, 4, dark);
      rect(g, u, bx - 4, by - 3, 2, 5, dark);
      rect(g, u, bx + 20, by + 1, 2, 4, dark);
      rect(g, u, bx + 22, by - 3, 2, 5, dark);
    } else if (p.arm === "type") {
      rect(g, u, bx - 2, armY + (p.typePhase ? 0 : 1), 2, 4, dark);
      rect(g, u, bx + 20, armY + (p.typePhase ? 1 : 0), 2, 4, dark);
    } else {
      rect(g, u, bx - 2, armY, 2, 4, dark);
      rect(g, u, bx + 20, armY, 2, 4, dark);
    }
  }

  /* The OpenCode robot: same 24x18 box, but a mechanical sprite with a visor,
     antennae, and a signal arm. The poses are the same as the Claude mate,
     only re-interpreted for the new shape. */
  function drawRobot(g, u, ox, oy, p) {
    const dead = p.state === "DEAD";
    const idle = p.state === "IDLE";
    const bot = dead ? PAL.gone : PAL.bot;
    const botDark = dead ? PAL.goneDark : PAL.botDark;
    const accent = dead ? PAL.goneDark : accentOf(p.state);

    const lift = p.lift || 0;
    const sink = p.sink || 0;
    const bx = ox + 2;
    const by = oy + 2 - lift + sink;

    // Shadow stays on the floor while the body hops.
    rect(g, u, ox + 3 + lift, oy + 17, 18 - lift * 2, 1, PAL.shadow);

    // Two legs — simpler than the humanoid's four.
    rect(g, u, bx + 4, oy + 14 - lift, 3, 4, botDark);
    rect(g, u, bx + 15, oy + 14 - lift, 3, 4, botDark);

    // Body and a narrower head on top.
    rect(g, u, bx + 2, by + 5, 16, 9, bot);
    rect(g, u, bx + 2, by + 14, 16, 1, botDark);
    rect(g, u, bx + 4, by, 12, 5, bot);
    rect(g, u, bx + 4, by + 5, 12, 1, botDark);

    // Visor glow: bright while active, dim when idle, off when dead.
    const visorGlow = dead ? 0 : idle ? 0.25 : 0.75;
    if (visorGlow) {
      g.globalAlpha = visorGlow;
      rect(g, u, bx + 5, by + 1, 10, 3, accent);
      g.globalAlpha = 1;
    }

    // Eyes: two bright pixels, or a flat line when shut.
    const eyeColor = dead ? PAL.goneDark : idle ? PAL.metal : PAL.steam;
    if (p.blink) {
      rect(g, u, bx + 7, by + 2, 2, 1, eyeColor);
      rect(g, u, bx + 11, by + 2, 2, 1, eyeColor);
    } else {
      rect(g, u, bx + 7, by + 1, 2, 2, eyeColor);
      rect(g, u, bx + 11, by + 1, 2, 2, eyeColor);
    }

    // Antennae.
    rect(g, u, bx + 7, by - 2, 1, 2, botDark);
    rect(g, u, bx + 12, by - 2, 1, 2, botDark);
    if (!dead && (p.state === "WORKING" || p.state === "SHELL")) {
      // Tips pulse with the accent while active.
      g.globalAlpha = p.typePhase ? 0.4 : 0.9;
      rect(g, u, bx + 7, by - 3, 1, 1, accent);
      rect(g, u, bx + 12, by - 3, 1, 1, accent);
      g.globalAlpha = 1;
    }

    // Mechanical arms.
    const armY = by + 6;
    if (p.arm === "wave") {
      // Right arm raised as a signal mast with a light on top.
      rect(g, u, bx - 2, armY, 2, 4, botDark);
      rect(g, u, bx + 18, by - 2, 2, 6, botDark);
      rect(g, u, bx + 18, by - 3, 2, 1, ACCENT.NEEDS_YOU);
    } else if (p.arm === "stretch") {
      // Arms up and out in a relaxed stretch.
      rect(g, u, bx - 2, by + 1, 2, 4, botDark);
      rect(g, u, bx - 4, by - 3, 2, 5, botDark);
      rect(g, u, bx + 18, by + 1, 2, 4, botDark);
      rect(g, u, bx + 20, by - 3, 2, 5, botDark);
    } else if (p.arm === "type") {
      rect(g, u, bx - 2, armY + (p.typePhase ? 0 : 1), 2, 4, botDark);
      rect(g, u, bx + 18, armY + (p.typePhase ? 1 : 0), 2, 4, botDark);
    } else {
      rect(g, u, bx - 2, armY, 2, 4, botDark);
      rect(g, u, bx + 18, armY, 2, 4, botDark);
    }
  }

  function drawMate(g, u, ox, oy, p, source) {
    if (source === "opencode") drawRobot(g, u, ox, oy, p);
    else drawClaude(g, u, ox, oy, p);
  }

  /* A subagent: the same creature, pocket sized, standing by the desk. */
  const MINI_W = 14;
  const MINI_H = 11;

  function drawMini(g, u, ox, oy, p) {
    const skin = p.busy ? PAL.body : PAL.gone;
    const dark = p.busy ? PAL.dark : PAL.goneDark;
    const lift = p.lift || 0;
    const by = oy - lift;

    rect(g, u, ox + 2 + lift, oy + 10, 10 - lift * 2, 1, PAL.shadow);
    rect(g, u, ox + 3, oy + 8 - lift, 2, 3, dark);
    rect(g, u, ox + 9, oy + 8 - lift, 2, 3, dark);
    rect(g, u, ox + 1, by + 1, 12, 8, skin);
    rect(g, u, ox + 1, by + 8, 12, 1, dark);
    if (p.blink) {
      rect(g, u, ox + 4, by + 4, 2, 1, PAL.ink);
      rect(g, u, ox + 9, by + 4, 2, 1, PAL.ink);
    } else {
      rect(g, u, ox + 4, by + 3, 2, 3, PAL.ink);
      rect(g, u, ox + 9, by + 3, 2, 3, PAL.ink);
    }
  }

  /* ── props ─────────────────────────────────────────────────────────── */

  // Desk top sits at `y`; legs run down to the floor line.
  function drawDesk(g, u, x, y, w, floorY) {
    rect(g, u, x, y, w, 2, PAL.deskTop);
    rect(g, u, x, y + 2, w, 1, PAL.deskEdge);
    rect(g, u, x + 1, y + 3, 2, floorY - y - 3, PAL.deskLeg);
    rect(g, u, x + w - 3, y + 3, 2, floorY - y - 3, PAL.deskLeg);
  }

  // 10 wide, 10 tall including the stand — bottom edge lands on y + 10.
  function drawMonitor(g, u, x, y, accent, live, t) {
    rect(g, u, x, y, 10, 8, PAL.bezel);
    rect(g, u, x + 4, y + 8, 2, 2, PAL.bezel);
    if (!live) {
      // Not asleep, just nothing running — leave a couple of dim lines up.
      rect(g, u, x + 1, y + 1, 8, 6, PAL.screenOff);
      g.globalAlpha = 0.35;
      rect(g, u, x + 2, y + 2, 5, 1, "#4a565c");
      rect(g, u, x + 2, y + 4, 3, 1, "#4a565c");
      g.globalAlpha = 1;
      return;
    }
    g.globalAlpha = 0.16;
    rect(g, u, x + 1, y + 1, 8, 6, accent);
    g.globalAlpha = 1;

    // Code scrolling past, abstracted to a few pixels of line length. The gaps
    // matter as much as the lines — a full screen just reads as a solid block.
    const step = Math.floor(t * 4);
    for (let row = 0; row < 6; row++) {
      if ((step + row * 3) % 4 === 0) continue;
      g.globalAlpha = row === step % 6 ? 0.95 : 0.45;
      rect(g, u, x + 2, y + 1 + row, 2 + ((step + row * 7) % 5), 1, accent);
    }
    g.globalAlpha = 1;
  }

  // Mug bottom lands on y + 3. Pass a time to make it steam.
  function drawMug(g, u, x, y, steamT) {
    rect(g, u, x, y, 3, 3, PAL.metal);
    rect(g, u, x + 3, y + 1, 1, 1, PAL.metal);
    if (steamT == null) return;
    for (let i = 0; i < 3; i++) {
      const phase = (steamT * 0.9 + i * 0.33) % 1;
      g.globalAlpha = 0.5 * (1 - phase);
      rect(g, u, x + (i % 2), y - 1 - phase * 5, 1, 1, PAL.steam);
      g.globalAlpha = 1;
    }
  }

  function drawPlant(g, u, x, floorY) {
    rect(g, u, x + 1, floorY - 4, 6, 4, PAL.pot);
    rect(g, u, x + 1, floorY - 4, 6, 1, "#845a46");
    rect(g, u, x + 3, floorY - 8, 2, 4, PAL.leafDark);
    rect(g, u, x, floorY - 9, 3, 2, PAL.leaf);
    rect(g, u, x + 5, floorY - 11, 3, 3, PAL.leaf);
    rect(g, u, x + 2, floorY - 12, 3, 3, PAL.leaf);
  }

  function drawCooler(g, u, x, floorY) {
    rect(g, u, x + 1, floorY - 7, 5, 7, "#e8eef0");
    rect(g, u, x + 1, floorY - 7, 5, 1, PAL.metal);
    rect(g, u, x, floorY - 13, 7, 6, "#243036");
    rect(g, u, x + 1, floorY - 12, 5, 4, PAL.water);
    rect(g, u, x + 2, floorY - 4, 3, 1, PAL.metal);
  }

  function drawClock(g, u, x, y, now) {
    rect(g, u, x, y, 8, 8, PAL.bezel);
    rect(g, u, x + 1, y + 1, 6, 6, "#0b0f11");
    const d = new Date(now);
    const hands = [
      { turn: ((d.getHours() % 12) + d.getMinutes() / 60) / 12, len: 2 },
      { turn: d.getMinutes() / 60, len: 3 },
    ];
    for (const hand of hands) {
      const rad = (hand.turn - 0.25) * Math.PI * 2;
      for (let i = 1; i <= hand.len; i++) {
        rect(g, u, x + 3.5 + Math.cos(rad) * i, y + 3.5 + Math.sin(rad) * i, 1, 1, PAL.metal);
      }
    }
  }

  function drawPoster(g, u, x, y, accent) {
    rect(g, u, x, y, 9, 7, PAL.wallTrim);
    g.globalAlpha = 0.55;
    rect(g, u, x + 1, y + 1, 7, 5, accent);
    g.globalAlpha = 1;
    rect(g, u, x + 2, y + 3, 5, 1, PAL.wall);
  }

  // A raised hand needs a shout to go with it. 7 wide, 8 tall with the tail.
  function drawBang(g, u, x, y, accent, bob) {
    const yy = y + bob;
    rect(g, u, x, yy, 7, 7, accent);
    rect(g, u, x + 2, yy + 7, 2, 1, accent);
    rect(g, u, x + 3, yy + 1, 1, 3, "#1a0e10");
    rect(g, u, x + 3, yy + 5, 1, 1, "#1a0e10");
  }

  function drawZ(g, u, x, y, size, alpha) {
    g.globalAlpha = Math.max(0, alpha);
    rect(g, u, x, y, size, 1, "#7f8f97");
    rect(g, u, x, y + size - 1, size, 1, "#7f8f97");
    for (let i = 0; i < size - 1; i++) rect(g, u, x + size - 2 - i, y + 1 + i, 1, 1, "#7f8f97");
    g.globalAlpha = 1;
  }

  /* ── poses ─────────────────────────────────────────────────────────── */

  /* One place decides what a state looks like, so a card and the floor band can
     never disagree about what "working" means. */
  function poseFor(state, seed, now) {
    const t = REDUCED ? 0 : now / 1000;
    const off = (seed % 997) / 997;
    const cycle = (period) => ((t + off * period) % period) / period;
    const p = { state, blink: cycle(4.6 + (seed % 40) / 10) > 0.96 };

    switch (state) {
      case "WORKING":
        p.arm = "type";
        p.typePhase = Math.floor(t * 7 + off * 9) % 2 === 0;
        p.sink = cycle(2.7) > 0.86 ? 1 : 0; // glances down at the keyboard
        break;
      case "SHELL":
        p.arm = "type";
        p.typePhase = Math.floor(t * 12 + off * 9) % 2 === 0;
        break;
      case "NEEDS_YOU":
        p.arm = "wave";
        p.wavePhase = cycle(0.66) < 0.5;
        p.lift = cycle(1.15) < 0.14 ? 1 : 0; // a little hop to be seen
        p.blink = false; // eyes wide — they want you
        break;
      case "DONE":
        p.arm = "stretch";
        p.sink = cycle(3.4) > 0.5 ? 1 : 0;
        break;
      case "IDLE":
        p.arm = "rest";
        p.sink = 1;
        p.blink = cycle(7) > 0.12; // dozing, cracks an eye now and then
        break;
      case "DEAD":
        p.arm = "rest";
        p.sink = 2;
        p.blink = true;
        break;
      default:
        p.arm = "rest";
    }
    return p;
  }

  /* ── canvas plumbing ───────────────────────────────────────────────── */

  class Stage {
    /* fluid: the canvas takes its width from CSS (a card). Otherwise the stage
       sets an explicit width and may overflow its scroll container. */
    constructor(canvas, unit, unitsHigh, fluid) {
      this.canvas = canvas;
      this.u = unit;
      this.H = unitsHigh;
      this.fluid = !!fluid;
      this.g = canvas.getContext("2d");
      this.cssW = 0;
      this.W = 0;
      this.dpr = 0;
      canvas.style.height = unitsHigh * unit + "px";
    }

    resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 3);
      let cssW;
      if (this.fluid) {
        cssW = Math.round(this.canvas.clientWidth);
      } else {
        cssW = Math.max(1, Math.round(this.wantWidth()));
        this.canvas.style.width = cssW + "px";
      }
      if (cssW < 8) return false; // hidden or not laid out yet
      if (cssW === this.cssW && dpr === this.dpr) return false;

      this.cssW = cssW;
      this.dpr = dpr;
      this.W = Math.floor(cssW / this.u);
      this.canvas.width = Math.round(cssW * dpr);
      this.canvas.height = Math.round(this.H * this.u * dpr);
      this.g.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.g.imageSmoothingEnabled = false;
      return true;
    }

    wantWidth() {
      return this.canvas.parentElement ? this.canvas.parentElement.clientWidth : 240;
    }

    // Wall above, floor below. Painted in real pixels so no seam is left on the
    // right when the width isn't a whole number of units.
    room(floorY) {
      const g = this.g;
      const h = this.H * this.u;
      g.clearRect(0, 0, this.cssW, h);
      g.fillStyle = PAL.wall;
      g.fillRect(0, 0, this.cssW, floorY * this.u);
      g.fillStyle = PAL.floor;
      g.fillRect(0, floorY * this.u, this.cssW, h - floorY * this.u);
      g.fillStyle = PAL.floorLine;
      g.fillRect(0, floorY * this.u, this.cssW, this.u);
    }
  }

  /* ── a card's cubicle ──────────────────────────────────────────────── */

  const CARD_U = 2;
  const CARD_H = 30; // units → 60 css px
  const CARD_FLOOR = 25;
  const CARD_DESK = 19; // desk top: clears the arms, hides the legs

  class Cubicle extends Stage {
    constructor(canvas) {
      super(canvas, CARD_U, CARD_H, true);
      this.session = null;
      this.seed = 1;
    }

    set(session) {
      this.session = session;
      this.seed = hash(session.session_id || session.name || "mate");
      this.dirty = true;
    }

    draw(now) {
      const s = this.session;
      if (!s || !this.cssW) return;
      const g = this.g;
      const u = this.u;
      const t = REDUCED ? 0 : now / 1000;
      const accent = accentOf(s.state);
      const dead = s.state === "DEAD";

      this.room(CARD_FLOOR);
      if (dead) g.globalAlpha = 0.45;

      // Wall and floor dressing, anchored right so the room fills any width.
      if (this.W > 118) drawClock(g, u, this.W - 40, 4, now);
      if (this.W > 104) drawCooler(g, u, this.W - 26, CARD_FLOOR);
      if (this.W > 86) drawPlant(g, u, this.W - 12, CARD_FLOOR);
      if (this.W > 66) drawPoster(g, u, 52, 5, accent);

      drawMate(g, u, 8, CARD_FLOOR - MATE_H, poseFor(s.state, this.seed, now), s.source);

      // Desk paints over the mate: they sit behind it, not on it.
      drawDesk(g, u, 4, CARD_DESK, 44, CARD_FLOOR);
      drawMonitor(g, u, 34, CARD_DESK - 10, accent, isBusy(s.state) && !dead, t);
      drawMug(g, u, 4, CARD_DESK - 3, s.state === "DONE" || s.state === "IDLE" ? t : null);

      // The shout hangs beside the raised hand and never lands on the head.
      if (s.state === "NEEDS_YOU") drawBang(g, u, 22, 1, accent, Math.sin(t * 4) > 0 ? 0 : -1);
      if (s.state === "IDLE") {
        for (let i = 0; i < 3; i++) {
          const phase = (t * 0.35 + i * 0.34) % 1;
          drawZ(g, u, 26 + phase * 8, 6 - phase * 6, 3, 0.7 * (1 - phase));
        }
      }

      // With no subagents to stand there, the next desk over is empty — which
      // is what an office looks like, rather than one mate in a void.
      const helpers = (s.subagents || []).slice(0, 4);
      if (!helpers.length && this.W > 112) {
        drawDesk(g, u, 56, CARD_DESK, 40, CARD_FLOOR);
        drawMonitor(g, u, 60, CARD_DESK - 10, accent, false, t);
      }

      // Subagents line up beside the desk while they run.
      helpers.forEach((agent, i) => {
        const x = 52 + i * 17;
        if (x + MINI_W > this.W - 34) return;
        const bounce = agent.running && !REDUCED && Math.floor(t * 3 + i) % 2 === 0 ? 1 : 0;
        drawMini(g, u, x, CARD_FLOOR - MINI_H, {
          busy: !!agent.running,
          lift: bounce,
          blink: !agent.running,
        });
      });

      g.globalAlpha = 1;
    }
  }

  /* ── the open-plan floor ───────────────────────────────────────────── */

  const FLOOR_U = 3;
  const FLOOR_H = 38; // units → 114 css px, nameplate included
  const FLOOR_LINE = 30;
  const FLOOR_DESK = 23;
  const BAY = 62; // units per workstation: desk, mate, and room for one helper
  const BAY_0 = 30; // room for the cooler and plant by the door

  class Floor extends Stage {
    constructor(canvas) {
      super(canvas, FLOOR_U, FLOOR_H, false);
      this.sessions = [];
    }

    wantWidth() {
      const host = this.canvas.parentElement;
      const avail = host ? host.clientWidth : 640;
      return Math.max(avail, (BAY_0 + this.sessions.length * BAY + 8) * this.u);
    }

    set(sessions) {
      this.sessions = sessions;
      this.dirty = true;
      this.resize();
    }

    draw(now) {
      if (!this.cssW) return;
      const g = this.g;
      const u = this.u;
      const t = REDUCED ? 0 : now / 1000;

      this.room(FLOOR_LINE);
      drawCooler(g, u, 4, FLOOR_LINE);
      drawPlant(g, u, 15, FLOOR_LINE);
      drawClock(g, u, 5, 4, now);

      this.sessions.forEach((s, i) => {
        const x0 = BAY_0 + i * BAY;
        const accent = accentOf(s.state);
        const dead = s.state === "DEAD";
        const seed = hash(s.session_id || s.name || String(i));
        if (dead) g.globalAlpha = 0.42;

        if (i % 3 === 1) drawPoster(g, u, x0 + 14, 4, accent);

        drawMate(g, u, x0 + 6, FLOOR_LINE - MATE_H, poseFor(s.state, seed, now), s.source);
        drawDesk(g, u, x0, FLOOR_DESK, 44, FLOOR_LINE);
        drawMonitor(g, u, x0 + 33, FLOOR_DESK - 10, accent, isBusy(s.state) && !dead, t + i);
        drawMug(g, u, x0 + 2, FLOOR_DESK - 3, s.state === "DONE" || s.state === "IDLE" ? t + i : null);

        if (s.state === "NEEDS_YOU") {
          drawBang(g, u, x0 + 20, 2, accent, Math.sin(t * 4 + i) > 0 ? 0 : -1);
        }
        if (s.state === "IDLE") {
          for (let k = 0; k < 3; k++) {
            const phase = (t * 0.32 + k * 0.34 + i * 0.2) % 1;
            drawZ(g, u, x0 + 22 + phase * 8, 10 - phase * 8, 3, 0.7 * (1 - phase));
          }
        }

        // One helper fits beside each desk; the card shows the rest.
        const helper = (s.subagents || [])[0];
        if (helper) {
          const bounce = helper.running && !REDUCED && Math.floor(t * 3 + i) % 2 === 0 ? 1 : 0;
          drawMini(g, u, x0 + 46, FLOOR_LINE - MINI_H, {
            busy: !!helper.running,
            lift: bounce,
            blink: !helper.running,
          });
        }

        g.globalAlpha = 1;

        // Nameplate on the floor in front of the desk.
        g.font = '9px "SF Mono", Menlo, Monaco, ui-monospace, monospace';
        g.textBaseline = "top";
        g.fillStyle = s.state === "NEEDS_YOU" ? accent : "#61727a";
        const name = String(s.name || "").toUpperCase();
        g.fillText(name.length > 13 ? name.slice(0, 12) + "…" : name, x0 * u, (FLOOR_LINE + 2) * u);
      });
    }
  }

  /* ── the manager's office ──────────────────────────────────────────── */

  /* The manager sits at the centre desk with a crown; the reports take the
     desks either side. A message is a note that flies along the arc between
     two desks, so the traffic is something you watch rather than read. */

  const OFF_U = 4;
  const OFF_H = 54;          // units: arcs overhead, desks, nameplates
  const OFF_LINE = 42;       // floor line
  const OFF_DESK = 35;
  const OFF_SEAT = 50;       // desk plus the mate beside it
  const OFF_BAY_MIN = 56;
  const OFF_BAY_MAX = 84;
  const NOTE_SECONDS = 2.4;  // how long a note takes to cross
  const NOTE_MAX = 14;

  class Office extends Stage {
    constructor(canvas) {
      super(canvas, OFF_U, OFF_H, false);
      this.seats = [];        // [{session_id, name, state, manager}]
      this.links = [];        // [{a, b}] peer links between reports
      this.notes = [];        // in flight
      this.seen = new Set();  // message ids already flown
      this.primed = false;    // first load must not launch 128 notes at once
      this.hot = null;        // focused session_id
    }

    set(scene) {
      const seats = [];
      if (scene.manager) seats.push({ ...scene.manager, manager: true });
      for (const report of scene.reports || []) seats.push({ ...report, manager: false });
      // Manager in the middle: it is the only seat everything connects to.
      const mid = Math.floor(seats.length / 2);
      if (seats.length > 2) seats.splice(mid, 0, seats.shift());
      this.seats = seats;
      this.links = scene.links || [];
      this.hot = scene.hot || null;

      for (const msg of scene.messages || []) {
        if (this.seen.has(msg.id)) continue;
        this.seen.add(msg.id);
        if (this.primed && this.notes.length < NOTE_MAX) {
          const from = this.index(msg.from);
          const to = this.index(msg.to);
          if (from >= 0 && to >= 0 && from !== to) {
            this.notes.push({ from, to, p: 0, born: performance.now() });
          }
        }
      }
      this.primed = true;
      this.dirty = true;
    }

    index(sessionId) {
      return this.seats.findIndex((s) => s.session_id === sessionId);
    }

    bay() {
      const count = Math.max(this.seats.length, 1);
      return Math.max(OFF_BAY_MIN, Math.min(OFF_BAY_MAX, (this.W - 8) / count));
    }

    /* Desks sit centred in the room rather than pinned left, so a two-person
       office does not look like an eight-person one with six people missing. */
    left() {
      const span = this.seats.length * this.bay();
      return Math.max(4, Math.round((this.W - span) / 2));
    }

    seatX(i) {
      return this.left() + i * this.bay();
    }

    /* Where a note leaves and lands: just above the monitor. */
    anchor(i) {
      return { x: this.seatX(i) + 20, y: OFF_DESK - 13 };
    }

    hitTest(cssX) {
      const i = Math.floor((cssX / this.u - this.left()) / this.bay());
      const seat = this.seats[i];
      return seat ? seat.session_id : null;
    }

    arc(a, b) {
      const from = this.anchor(a);
      const to = this.anchor(b);
      const span = Math.abs(to.x - from.x);
      return { from, to, lift: Math.min(30, 8 + span * 0.34) };
    }

    point(arc, p) {
      const q = 1 - p;
      const cx = (arc.from.x + arc.to.x) / 2;
      const cy = Math.min(arc.from.y, arc.to.y) - arc.lift;
      return {
        x: q * q * arc.from.x + 2 * q * p * cx + p * p * arc.to.x,
        y: q * q * arc.from.y + 2 * q * p * cy + p * p * arc.to.y,
      };
    }

    strokeArc(arc, color, dashed) {
      const g = this.g;
      const u = this.u;
      g.save();
      g.strokeStyle = color;
      g.lineWidth = Math.max(1, u / 2);
      if (dashed) g.setLineDash([u, u * 2]);
      g.beginPath();
      g.moveTo(arc.from.x * u, arc.from.y * u);
      g.quadraticCurveTo(
        ((arc.from.x + arc.to.x) / 2) * u,
        (Math.min(arc.from.y, arc.to.y) - arc.lift) * u,
        arc.to.x * u,
        arc.to.y * u,
      );
      g.stroke();
      g.restore();
    }

    draw(now) {
      if (!this.cssW || !this.seats.length) return;
      const g = this.g;
      const u = this.u;
      const t = REDUCED ? 0 : now / 1000;

      this.room(OFF_LINE);

      const managerIndex = this.seats.findIndex((s) => s.manager);

      // Every report is joined to the manager; reports joined to each other get
      // the dashed gold arc, so a side-channel reads as a side-channel.
      this.seats.forEach((seat, i) => {
        if (i === managerIndex || managerIndex < 0) return;
        this.strokeArc(this.arc(managerIndex, i), "#2f3f47", false);
      });
      for (const link of this.links) {
        const a = this.index(link.a);
        const b = this.index(link.b);
        if (a >= 0 && b >= 0) this.strokeArc(this.arc(a, b), "#5a4d33", true);
      }

      this.seats.forEach((seat, i) => {
        const x0 = this.seatX(i);
        const accent = accentOf(seat.state);
        const seed = hash(seat.session_id || seat.name || String(i));
        const focused = this.hot && this.hot === seat.session_id;

        if (this.hot && !focused) g.globalAlpha = 0.45;

        drawMate(g, u, x0 + 6, OFF_LINE - MATE_H, poseFor(seat.state, seed, now), seat.source);
        drawDesk(g, u, x0, OFF_DESK, 44, OFF_LINE);
        drawMonitor(g, u, x0 + 33, OFF_DESK - 10, accent, isBusy(seat.state), t + i);
        drawMug(g, u, x0 + 2, OFF_DESK - 3,
          seat.state === "DONE" || seat.state === "IDLE" ? t + i : null);

        if (seat.manager) drawCrown(g, u, x0 + 10, OFF_LINE - MATE_H - 7, t);
        // The bang belongs over its own mate's head, not up under the ceiling.
        if (seat.state === "NEEDS_YOU" || seat.waiting) {
          drawBang(g, u, x0 + 19, OFF_LINE - MATE_H - 11, ACCENT.NEEDS_YOU,
            Math.sin(t * 4 + i) > 0 ? 0 : -1);
        }

        g.globalAlpha = 1;

        g.font = '9px "SF Mono", Menlo, Monaco, ui-monospace, monospace';
        g.textBaseline = "top";
        g.fillStyle = focused ? "#e2ebee" : seat.waiting ? "#ff5f70" : "#61727a";
        const name = String(seat.name || "").toUpperCase();
        g.fillText(name.length > 13 ? name.slice(0, 12) + "…" : name,
          x0 * u, (OFF_LINE + 2) * u);

        if (seat.msg_count) {
          g.fillStyle = "#44545b";
          g.fillText(`${seat.msg_count} msgs`, x0 * u, (OFF_LINE + 7) * u);
        }
      });

      // Notes last, so they ride over the desks.
      const alive = [];
      for (const note of this.notes) {
        const p = REDUCED ? 1 : (now - note.born) / (NOTE_SECONDS * 1000);
        if (p < 1) alive.push(note);
        if (p >= 1 || p < 0) continue;
        const arc = this.arc(note.from, note.to);
        const at = this.point(arc, p);
        rect(g, u, Math.round(at.x) - 2, Math.round(at.y) - 1, 5, 4, "#e8eef0");
        rect(g, u, Math.round(at.x) - 2, Math.round(at.y) - 1, 5, 1, "#cbb27e");
      }
      this.notes = alive;
      if (this.notes.length) this.dirty = true;
    }
  }

  /* A small crown above the manager's head — the one thing that says who. */
  function drawCrown(g, u, x, y, t) {
    const lift = REDUCED ? 0 : Math.sin(t * 2) > 0 ? 0 : 1;
    const top = y - lift;
    rect(g, u, x, top + 3, 7, 2, "#cbb27e");
    rect(g, u, x, top, 2, 3, "#cbb27e");
    rect(g, u, x + 3, top + 1, 1, 2, "#cbb27e");
    rect(g, u, x + 5, top, 2, 3, "#cbb27e");
  }

  /* ── one loop for everything on screen ─────────────────────────────── */

  const stages = new Set();
  let ticking = false;
  let last = 0;

  function tick(now) {
    if (!stages.size) {
      ticking = false;
      return;
    }
    requestAnimationFrame(tick);
    if (document.hidden) return;
    if (now - last < 62) return; // ~16 fps — it's pixel art, not a shooter
    last = now;
    for (const stage of stages) {
      const resized = stage.resize();
      // Nothing moves under reduced motion, so only repaint on real change.
      if (REDUCED && !resized && !stage.dirty) continue;
      stage.dirty = false;
      stage.draw(now);
    }
  }

  function add(stage) {
    stages.add(stage);
    if (!ticking) {
      ticking = true;
      requestAnimationFrame(tick);
    }
    return stage;
  }

  window.addEventListener("resize", () => {
    for (const stage of stages) stage.cssW = 0; // force a re-measure next frame
  });

  window.Mates = {
    cubicle(canvas) {
      const stage = add(new Cubicle(canvas));
      return {
        set: (session) => stage.set(session),
        destroy: () => stages.delete(stage),
      };
    },
    floor(canvas) {
      const stage = add(new Floor(canvas));
      return {
        set: (sessions) => stage.set(sessions),
        destroy: () => stages.delete(stage),
      };
    },
    office(canvas) {
      const stage = add(new Office(canvas));
      return {
        set: (scene) => stage.set(scene),
        hitTest: (cssX) => stage.hitTest(cssX),
        destroy: () => stages.delete(stage),
      };
    },
  };
})();
