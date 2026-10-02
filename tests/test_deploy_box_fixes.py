"""Four fixes applied BY HAND on 10.99.0.1, made durable, plus the box's phone.

`/opt/agent-deck` is a copied tree, not a git checkout, so re-running the
installer is the only upgrade path there is. Anything fixed by hand and not
folded back into `deploy/install-deck.sh` is undone by the next run -- which is
why these are tests and not a runbook.

Everything asserted here was MEASURED on the box on 2026-09-02 and is quoted in
the test that guards it. Nothing in this module runs the installer, ssh's
anywhere, or starts a container; this Mac is not the box, so every claim is made
by parsing the artefact the way bash reads it. That also means these tests prove
the SCRIPT says the right thing -- they cannot prove the box is in that state.

The class swept: a step whose proof you did not see is a step that did not
happen. Each check below rejects the cheap proxy (a file exists, a tag exists)
and demands the expensive proof (a login shell authenticates, a container runs).
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "deploy" / "install-deck.sh"

DECK_USER = "deckop"
WG_ADDRESS = "10.99.0.1"
#: This Mac's address on the same tunnel. Measured: `utun11 inet 10.99.0.4`,
#: and `ping 10.99.0.1` from here answers in ~70ms.
MAC_WG_ADDRESS = "10.99.0.4"


def script() -> str:
    return INSTALLER.read_text()


def code_lines() -> list[str]:
    """The installer with comments dropped. A promise in a comment is not a
    step -- this repo has been bitten by exactly that before."""
    return [ln for ln in script().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def code() -> str:
    return "\n".join(code_lines())


def index_of(pattern: str, *, where: str | None = None) -> int:
    """Line number of the first match, for ordering assertions."""
    lines = (where or script()).splitlines()
    for i, line in enumerate(lines):
        if re.search(pattern, line):
            return i
    return -1


# ── 1. a login shell must be able to authenticate ───────────────────────────
#
# MEASURED on the box: `su - deckop -c claude` failed with
#
#     Failed to authenticate: OAuth session expired and could not be refreshed
#
# while the systemd service worked perfectly. The token lives in
# /home/deckop/.claude/oauth.env, which the unit loads via EnvironmentFile and
# which no login shell ever reads. Every automated check passed anyway, because
# the check was "does a credentials file exist" -- the same false-pass shape as
# the hook that posted into a closed socket.


def test_the_installer_teaches_a_login_shell_to_read_the_token():
    """The good signal: the profile sources oauth.env, exported."""
    body = code()
    assert ".profile" in body, (
        "nothing writes a login-shell profile, so `su - deckop -c claude` "
        "still fails with 'OAuth session expired' on a box whose service is "
        "perfectly healthy")
    assert "oauth.env" in body
    # `set -a` is what makes it an EXPORT rather than a shell-local variable.
    # Sourced without it, `claude` (a child process) still sees nothing.
    assert re.search(r"set -a", body), (
        "oauth.env is sourced without `set -a`, so the variables stay local to "
        "the shell and the claude child process still sees nothing")


def test_the_profile_edit_is_idempotent():
    """Re-running the installer is the box's ONLY upgrade path, so an append
    that is not guarded writes the same block on every run."""
    lines = script().splitlines()
    idx = index_of(r"\.profile")
    assert idx >= 0
    window = "\n".join(lines[max(0, idx - 12):idx + 12])
    assert re.search(r"grep -q|grep -qs|grep -qF", window), (
        "the .profile block is written without first checking whether it is "
        f"already there:\n{window}")


def test_the_login_is_proved_by_running_claude_not_by_stat_ing_a_file():
    """The whole lesson of this box. A credentials FILE existing means somebody
    logged in once; it does not mean a login shell can authenticate today."""
    body = script()
    assert re.search(r"su - ?\"?\$\{?DECK_USER|su - " + DECK_USER, body), (
        "no check runs claude in a LOGIN shell, so the exact failure measured "
        "on the box -- service fine, `su - deckop -c claude` broken -- is "
        "still invisible to the installer")


def test_the_token_is_never_echoed():
    """It is a credential. It may be read from a file and used; it may not be
    printed, and the runbook line about it says exactly that."""
    for line in code_lines():
        if "oauth.env" not in line and "OAUTH" not in line.upper():
            continue
        assert not re.search(r"\b(echo|printf|cat)\b[^|]*\$\{?[A-Z_]*(TOKEN|OAUTH)",
                             line), f"a credential is printed: {line}"


# ── 2. docker: the config lands BEFORE the daemon ever starts ───────────────
#
# The box is INTERNET-FACING: nginx serves v.initech.example on public 80/443.
# dockerd's iptables chains land ahead of ufw's, so a published port is
# reachable from the internet regardless of the deny-incoming default. Writing
# /etc/docker/daemon.json AFTER installing means dockerd has already started
# once with the wrong default.


def test_the_docker_default_is_pinned_before_the_daemon_is_installed():
    """Ordering, measured as the thing that matters: daemon.json first, then
    the package, then the start. The reverse leaves a window in which dockerd
    runs with 0.0.0.0 as its publish default on a public box."""
    body = script()
    wrote_config = index_of(r"daemon\.json", where=body)
    installed = index_of(r"apt-get install.*docker", where=body)
    assert wrote_config >= 0 and installed >= 0
    assert wrote_config < installed, (
        f"daemon.json is written at line {wrote_config} but docker is "
        f"installed at line {installed} -- dockerd starts once with the wrong "
        "publish default on an internet-facing box")


def test_the_docker_default_binds_loopback_and_keeps_containers_through_restarts():
    body = code()
    assert '"ip"' in body and "127.0.0.1" in body, (
        "an accidental `-p 5900:5900` would bind 0.0.0.0 on a box serving a "
        "live public site")
    assert "live-restore" in body, (
        "without live-restore a dockerd restart kills every running desk "
        "container")


def test_the_public_site_is_checked_before_and_after_docker():
    """The control, and it must be a control: one reading with nothing to
    compare it to cannot tell 'docker broke the site' from 'the site was
    already 403'. Measured by hand as 403/403/403 and 301 both times.

    The sites are deck.toml's `doctor.public_probe_urls` (the owner's box lists
    v.initech.example over https and http); the script names none itself."""
    body = script()
    before = index_of(r"PUBLIC_BEFORE", where=body)
    after = index_of(r"PUBLIC_AFTER", where=body)
    assert before >= 0 and after >= 0, (
        "public reachability is sampled once or not at all; an install that "
        "silently breaks a neighbour's public site is the failure mode here")
    assert before < after
    assert "PROBE_URLS" in code(), "the probe does not read doctor.public_probe_urls"
    assert "initech" not in code()


def test_the_docker_group_risk_is_stated_where_the_owner_will_see_it():
    """`usermod -aG docker deckop` is what makes the deck able to run a desk
    container, and it makes deckop root-equivalent on a box serving a live
    public site. That is a trade the owner is entitled to be told about, not a
    line buried in a script."""
    body = script()
    assert "usermod -aG docker" in body
    idx = index_of(r"usermod -aG docker", where=body)
    # The comment that states the trade sits immediately above the guard that
    # performs it. A generous window, because the point is that a reader of
    # this line cannot miss it -- not that it is N lines away.
    window = "\n".join(body.splitlines()[max(0, idx - 32):idx + 8])
    assert re.search(r"root.?equivalent", window, re.I), (
        "the group grant is made without naming what it grants")
    assert "rootless" in window.lower(), (
        "the safer path is not named, so it will never be taken. Ubuntu "
        "24.04's archive has no docker-ce-rootless-extras (checked: "
        "apt-cache policy returns nothing), which is WHY this is a documented "
        "follow-up rather than a silent gap")


# ── 3. the desk image is proved by running it, not by tagging it ────────────


def test_the_desk_image_is_built_and_then_actually_exercised():
    """`docker images | grep` proves a tag exists. The box proved the real
    thing by hand: start -> is_up True -> frame() returned a 12,591-byte JPEG
    -> exec `uname -sr` inside the container -> stop -> is_up False."""
    body = script()
    assert "desk-computer" in body, "the installer never builds the desk image"
    assert re.search(r"docker build", body)
    for proof in ("sandbox", "frame", "exec"):
        assert proof in body.lower(), (
            f"the image is built but never exercised ({proof!r} appears "
            "nowhere): a tag that exists is not a computer that works")
    assert re.search(r"stop", body), (
        "nothing stops the container the verification started, so every "
        "install leaks one")


# ── 4. the box's own phone ──────────────────────────────────────────────────
#
# MEASURED 2026-09-02, both directions:
#   Mac -> box:  ping 10.99.0.1 -> 0% loss, ~70ms; /api/state -> 401 (auth on).
#   box -> Mac:  curl http://10.99.0.4:7799/status ->
#                "Failed to connect to 10.99.0.4 port 7799 after 73 ms"
#   and there is no bridge on the box:
#                ls /home/deckop/Projects/comunicate_with_me/bin/wa-send.js
#                -> No such file or directory
#
# The cause is not the tunnel and not the firewall: the Mac's bridge listens on
# 127.0.0.1:7799 ONLY (`lsof -nP -iTCP:7799` and BOT_HOST=127.0.0.1 in its
# .env). So the box cannot notify anybody today, and the existing check says
# only that a FILE is missing -- which reads as "optional extra" rather than
# "this box cannot tell him anything".


def test_the_box_proves_it_can_reach_a_bridge_rather_than_stat_ing_one():
    body = script()
    assert "DECK_WA_URL" in body, (
        "the box has no bridge of its own and cannot get one -- pairing is "
        "interactive and a second Baileys client on one number risks the "
        "number. Its only route is the Mac's bridge over the tunnel, which "
        "means an URL, not a local script path")
    # The check has to make a REQUEST. Asserted as a window rather than a
    # single line because the curl is wrapped over three; what matters is that
    # a request is issued against the configured bridge, not its layout.
    idx = index_of(r"WA_STATUS=")
    assert idx >= 0, (
        "the whatsapp check still only stats a path. A path check passes on a "
        "box that can reach nothing, and the deck then pages nobody, silently")
    window = "\n".join(script().splitlines()[idx:idx + 6])
    assert "curl" in window and "WA_URL" in window and "/status" in window, (
        f"the whatsapp check does not actually reach the bridge:\n{window}")


def test_the_unreachable_bridge_is_reported_as_the_box_being_unable_to_page_him():
    """Wording is the deliverable here. 'note: optional' is why this sat
    unnoticed; the operator has to read a consequence, not a component."""
    idx = index_of(r"DECK_WA_URL")
    assert idx >= 0
    window = "\n".join(script().splitlines()[max(0, idx - 6):idx + 30])
    assert re.search(r"cannot|can NOT|no way to (tell|reach|page)", window), (
        f"the unreachable case does not say what it costs him:\n{window}")


def test_the_mac_side_change_is_named_but_not_performed():
    """The bridge binds loopback and that is a deliberate security posture: it
    sends as his WhatsApp. Widening it to the tunnel is his call and it happens
    on the Mac, not from a script running as root on the box."""
    body = script()
    assert "BOT_HOST" in body, (
        "the one change that would make the box able to page him is not "
        "written down anywhere an operator would find it")
    # The Mac's tunnel address is his, not the script's: named as a
    # placeholder, never as the owner's 10.99.0.4.
    assert MAC_WG_ADDRESS not in code()
    # A command, not the word: the public-mode firewall text says "keep ssh open".
    assert not re.search(r"(^|[;&|(]|\$\()\s*(sudo\s+)?(ssh|scp)\s", code(), re.M), (
        "the installer reaches out to another machine; the Mac's bridge "
        "config is not root-on-the-box's to rewrite")
