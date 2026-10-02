# The agent's computer. One image, one container per desk.
#
#   docker build -t agent-deck/desk-computer:2 docker/desk-computer
#
# `server/sandbox.py` is what runs it, and its docstring is the argument for
# the shape. This file only has to justify what is INSIDE.
#
# Pinned to `debian:12` by digest below rather than to a floating tag: the
# security claim in sandbox.py is about a specific image's contents, and
# `debian:12` moves on every point release.
#
# Nothing is installed that the four surfaces do not need:
#   chromium  - the browser the agent drives and the owner takes over
#   xvfb      - the display both of them look at
#   ffmpeg    - one JPEG of that display, per "<Agent>'s screen"
#   xdotool   - the take-over: the owner's click and keystroke, injected
#   jwm       - the desktop: windows, a taskbar, a clock. One process, ~5 MB;
#               measured against openbox+tint2 (see docs/the-agents-computer.md)
#   xterm     - a terminal window, opened from the taskbar on demand
#   tmux      - the owner's shell that survives a dropped socket, shared by the
#               xterm, the Mac and the phone (session `deck`), and the read-only
#               mirror of the agent's own commands (session `agent`)
#   procps, vim, less, openssh-client, curl, git - what a person types in a
#               terminal. OWNER ASK 2026-10-01: "the terminal should look and
#               feel like a terminal"; the container stays the boundary.
#
# Deliberately NOT here:
#   chromium-sandbox - the setuid sandbox. `--security-opt no-new-privileges`
#                      makes a setuid binary useless, and Chromium's namespace
#                      sandbox is the better one anyway; see sandbox.py.
#   x11vnc / noVNC   - a second listening service is a second way in, and the
#                      take-over is done with frames and xdotool instead.
FROM debian:12@sha256:813017f3d62be4b5891a7acca6a01bdcd4b8513daa81b1ab99d3a50385b26931

# `--no-install-recommends` keeps this to what is listed: with recommends,
# `chromium` alone pulls in a desktop's worth of fonts and an update daemon.
# fonts-liberation is the one real recommend -- without any font, every page
# the owner is asked to look at renders as boxes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium \
      xvfb \
      ffmpeg \
      xdotool \
      ca-certificates \
      fonts-liberation \
      jwm \
      xterm \
      tmux \
      procps \
      vim \
      less \
      openssh-client \
      curl \
      git \
 && rm -rf /var/lib/apt/lists/*

# uid 1000 is stated on `docker run` too. Both, on purpose: the run flag is
# the control, and this is what makes the home directory belong to it.
RUN useradd --uid 1000 --create-home --home-dir /home/agent --shell /usr/sbin/nologin agent

# Xvfb binds its socket here and, running unprivileged, cannot create the
# directory itself -- measured: "_XSERVTransmkdir: ERROR: euid != 0". Sticky
# like /tmp so one container's X server is the only thing in its own.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# The desktop and the terminal (started by server/sandbox.py DESKTOP_SCRIPT).
# Config lives outside /home/agent on purpose:
# home is the desk's bind mount, so anything put there by the image would be
# hidden by it.
COPY tmux.conf /etc/tmux.conf
COPY jwmrc /etc/jwm/system.jwmrc
COPY bashrc /tmp/deck-bashrc
RUN cat /tmp/deck-bashrc >> /etc/bash.bashrc && rm /tmp/deck-bashrc

USER 1000:1000
WORKDIR /home/agent
ENV HOME=/home/agent DISPLAY=:99

# The container's main process is the X server itself, so "the computer is up"
# means "the display exists" -- a supervisor would keep the container alive
# with a dead screen behind it, and the panel would show black and call it an
# idle agent. `sandbox.create_argv` passes `browser.xvfb_argv`, which carries
# `-nolisten tcp`; this default is the same line for a manual `docker run`.
CMD ["Xvfb", ":99", "-screen", "0", "1600x1000x24", "-nolisten", "tcp"]

# No HEALTHCHECK on purpose. `docker inspect` would report healthy for a box
# with an X server and no browser, which is exactly the state the panel must
# not describe as working. `sandbox.frame()` is the health check: a JPEG came
# back, or it did not.
