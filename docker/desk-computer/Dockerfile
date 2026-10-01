# The agent's computer. One image, one container per desk.
#
#   docker build -t agent-deck/desk-computer:1 docker/desk-computer
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
#
# Deliberately NOT here:
#   chromium-sandbox - the setuid sandbox. `--security-opt no-new-privileges`
#                      makes a setuid binary useless, and Chromium's namespace
#                      sandbox is the better one anyway; see sandbox.py.
#   x11vnc / noVNC   - a second listening service is a second way in, and the
#                      take-over is done with frames and xdotool instead.
#   curl / ssh / git - an agent's browser box is not a shell box. Less in the
#                      image is less for an escaped renderer to pick up.
FROM debian:13@sha256:9cc080028c43b27d2074d63a5f9caf7166d731494965616c1a6d2827a004585c

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
 && rm -rf /var/lib/apt/lists/*

# uid 1000 is stated on `docker run` too. Both, on purpose: the run flag is
# the control, and this is what makes the home directory belong to it.
RUN useradd --uid 1000 --create-home --home-dir /home/agent --shell /usr/sbin/nologin agent

# Xvfb binds its socket here and, running unprivileged, cannot create the
# directory itself -- measured: "_XSERVTransmkdir: ERROR: euid != 0". Sticky
# like /tmp so one container's X server is the only thing in its own.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

USER 1000:1000
WORKDIR /home/agent
ENV HOME=/home/agent DISPLAY=:99

# The container's main process is the X server itself, so "the computer is up"
# means "the display exists" -- a supervisor would keep the container alive
# with a dead screen behind it, and the panel would show black and call it an
# idle agent. `sandbox.create_argv` passes `browser.xvfb_argv`, which carries
# `-nolisten tcp`; this default is the same line for a manual `docker run`.
CMD ["Xvfb", ":99", "-screen", "0", "1280x800x24", "-nolisten", "tcp"]

# No HEALTHCHECK on purpose. `docker inspect` would report healthy for a box
# with an X server and no browser, which is exactly the state the panel must
# not describe as working. `sandbox.frame()` is the health check: a JPEG came
# back, or it did not.
