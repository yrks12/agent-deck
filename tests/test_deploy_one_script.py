"""One command has to leave the box able to *hire*, not merely able to answer.

`tests/test_deploy_artefacts.py` pins that `deploy/install-deck.sh` is
idempotent and that it does not fight yair_os for :7788. Both still hold. What
neither pins is the thing measured on 10.99.0.1 on 2026-09-02, with the deck
already live and green on :7789:

    $ printf %s "$PAYLOAD" | sudo -u deckop \
        env DECK_URL=http://127.0.0.1:7789 node /opt/agent-deck/hooks/cc-approve.js
    {"hookSpecificOutput":{...,"permissionDecisionReason":"deck unreachable"}}

    $ printf %s "$PAYLOAD" | sudo -u deckop \
        env DECK_URL=http://10.99.0.1:7789 node /opt/agent-deck/hooks/cc-approve.js
    {"hookSpecificOutput":{...,"permissionDecisionReason":"no rule"}}

`http://127.0.0.1:7789` is exactly what `approval.deck_url()` returns inside the
service, because the unit's argv carries `--port 7789` (which it reads) and
`--host 10.99.0.1` (which it does not). The deck binds the tunnel address ONLY,
so loopback is closed -- `curl http://127.0.0.1:7789/api/state` on the box is
`Failed to connect`. Every approval, permission request, elicitation and
compaction from every desk the box hires therefore posts into a closed socket,
and the desk sits on a modal nobody can clear. The board stays green throughout.

That is the class these tests cover: a component the box needs that the one
command does not install, does not point at the right place, or does not prove.
Nothing here runs the installer -- this Mac is not the box -- so every claim is
made by parsing the artefacts, the same way bash and systemd read them.
"""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


class _OwnerUnit:
    """agentdeck.service as the owner's box receives it: rendered from the
    owner profile (tests/fixtures/owner-box/deck.toml). The static file is gone."""

    def read_text(self) -> str:
        spec = importlib.util.spec_from_file_location("deck_render", REPO / "deploy" / "render.py")
        render = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(render)
        cfg = render.load_config(REPO / "tests" / "fixtures" / "owner-box" / "deck.toml")
        return render.render_all(cfg)["agentdeck.service"]


UNIT = _OwnerUnit()
INSTALLER = REPO / "deploy" / "install-deck.sh"
BOX_DOC = REPO / "docs" / "the-box.md"

DECK_PORT = "7789"
WG_ADDRESS = "10.99.0.1"
WG_INTERFACE = "wg0"
#: The service user. `claude` and its credentials belong to it, not to root.
DECK_USER = "deckop"


def script() -> str:
    return INSTALLER.read_text()


def code_lines() -> list[str]:
    """The installer with comments and blanks dropped.

    Every assertion below is about what the script *does*. A promise in a
    comment is how the previous version claimed the hooks were wired.
    """
    return [ln for ln in INSTALLER.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def code() -> str:
    return "\n".join(code_lines())


# ── 1. the address a hired desk reports back to ──────────────────────────────


def test_deck_url_uses_the_address_the_deck_actually_bound(monkeypatch):
    """The measured defect. argv says `--host 10.99.0.1`; the hooks got loopback.

    `deck_url()` already reads `--port` off argv -- that half works, and 7789 is
    derived correctly on the box. The host half was never read, and the
    docstring's "always loopback" was true only of the Mac's LaunchAgent.
    """
    from server import approval

    monkeypatch.delenv("DECK_URL", raising=False)
    monkeypatch.delenv("AGENT_DECK_PORT", raising=False)
    monkeypatch.setattr(approval.sys, "argv", [
        "/opt/agent-deck/.venv/bin/uvicorn", "server.app:app",
        "--host", WG_ADDRESS, "--port", DECK_PORT,
    ])
    assert approval.deck_url() == f"http://{WG_ADDRESS}:{DECK_PORT}"


def test_deck_url_is_unchanged_for_the_loopback_deck_on_the_mac(monkeypatch):
    """The regression pin for the fix above.

    `bin/cdash` writes `--host 127.0.0.1 --port 7788` into the LaunchAgent's
    ProgramArguments. Reading the host must not move the Mac's answer by a byte,
    or every hire on the laptop starts posting somewhere new.
    """
    from server import approval

    monkeypatch.delenv("DECK_URL", raising=False)
    monkeypatch.delenv("AGENT_DECK_PORT", raising=False)
    monkeypatch.setattr(approval.sys, "argv", [
        "uvicorn", "server.app:app", "--host", "127.0.0.1", "--port", "7788",
    ])
    assert approval.deck_url() == "http://127.0.0.1:7788"


def test_deck_url_refuses_a_bind_that_is_not_an_address_on_this_box(monkeypatch):
    """`--host 0.0.0.0` is "every interface", not a place to post to.

    A hook handed `http://0.0.0.0:7789` connects to the local host by accident
    on Linux and not at all on some stacks. Fall back to loopback, which is at
    least always the machine the hook is running on.
    """
    from server import approval

    monkeypatch.delenv("DECK_URL", raising=False)
    monkeypatch.delenv("AGENT_DECK_PORT", raising=False)
    monkeypatch.setattr(approval.sys, "argv", [
        "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", DECK_PORT,
    ])
    assert approval.deck_url() == f"http://127.0.0.1:{DECK_PORT}"


def test_the_unit_states_the_decks_own_address_rather_than_leaving_it_derived():
    """Belt to the braces above: the unit says it outright.

    Deriving it from argv fixes the service. Stating it in the unit also fixes
    anyone who starts uvicorn by hand on that box, which is what an operator
    debugging at 2am actually does.
    """
    text = UNIT.read_text()
    stated = [ln.strip() for ln in text.splitlines()
              if ln.strip().startswith("Environment=DECK_URL=")]
    assert stated, "the unit never states DECK_URL; hired desks fall back to loopback"
    assert stated == [f"Environment=DECK_URL=http://{WG_ADDRESS}:{DECK_PORT}"], stated


# ── 2. the CLI the deck hires with ───────────────────────────────────────────


def test_the_installer_installs_the_agent_cli_when_the_box_has_none():
    """A deck with no `claude` is a board with no players.

    Preflight already *refuses* a box without it, which turns a missing CLI into
    a failed install and a human reading a blocker. One command means the
    command fixes it.
    """
    installs = [ln for ln in code_lines()
                if re.search(r"claude\.ai/install|install\.claude"
                             r"|@anthropic-ai/claude-code", ln)]
    assert installs, (
        "nothing in the installer installs `claude`; a fresh box preflights "
        "clean on everything else and can still hire nobody")


def test_the_agent_cli_is_installed_as_the_service_user_and_never_as_root():
    """`claude` keeps its credentials in the installing user's home.

    Installed as root it lands in /root, where the service user cannot read it,
    and the box looks equipped while every spawn dies at the login prompt.
    """
    for line in code_lines():
        if not re.search(r"claude\.ai/install|install\.claude"
                         r"|@anthropic-ai/claude-code", line):
            continue
        assert "DECK_USER" in line or f"-u {DECK_USER}" in line, \
            f"this installs the CLI without dropping to the service user: {line!r}"


def test_the_installer_never_installs_an_engine_the_deck_cannot_gate():
    """codex and opencode are deliberately absent, and this is the pin.

    `server/pretrust.py` returns `engine_not_covered` for both -- the deck cannot
    write either one's trust gate, so a desk opens and stalls on a prompt nothing
    outside the process can answer. `server/spawn.py::spawn_background` raises
    `background_unsupported` for any engine but claude, and `spawn_terminal`
    needs `osascript`, which does not exist on Linux. There is no code path on
    this box that can start one. A binary on PATH the board can offer and never
    drive is worse than an absent one.
    """
    for line in code_lines():
        assert not re.search(r"\b(install|get)\b.*\b(codex|opencode)\b", line), line
        assert not re.search(r"\b(codex|opencode)\b.*\binstall\b", line), line


def test_the_installer_prints_the_login_command_it_cannot_run_itself():
    """`claude setup-token` needs the owner. The script must stop, not fake it.

    The failure to prevent is a half-install: a deck that answers /api/state,
    shows green, and dies at the login prompt on the first hire.
    """
    assert "setup-token" in script(), (
        "the installer never names the one command a script cannot run")
    for line in code_lines():
        if not re.search(r"claude[\"']?\s+(setup-token|login)", line):
            continue
        assert re.search(r"\b(say|echo|printf|die)\b", line), \
            f"the installer tries to log in unattended: {line!r}"


# ── 3. the firewall ──────────────────────────────────────────────────────────


def test_the_installer_opens_the_deck_port_on_the_tunnel_and_nowhere_else():
    """Added by hand on 2026-09-02; before it the Mac got nothing at all.

    A rule made by hand is a rule the next box does not have.
    """
    rules = [ln for ln in code_lines() if re.search(r"ufw\s+(--\S+\s+)*allow", ln)
             and "DECK_PORT" in ln]
    assert rules, "the installer adds no firewall rule; the Mac cannot reach 7789"
    for rule in rules:
        # The interface is deck.toml's network.wireguard_iface (wg0 on the
        # owner's box), validated by render.py as an interface name.
        assert re.search(r"allow in on \"?\$\{?WG_IFACE", rule), \
            f"this rule is not scoped to the tunnel: {rule!r}"
        assert "proto tcp" in rule, f"a bare port rule also opens UDP: {rule!r}"
    idx = code().index(rules[0])
    guard = code()[max(0, idx - 400):idx]
    assert re.search(r'NET_MODE"?\s*=\s*"?wireguard', guard), \
        "the tunnel rule is added on a box that has no tunnel"


def test_no_rule_the_installer_adds_is_reachable_from_the_public_internet():
    """The board carries every transcript and the spawn controls.

    The box already publishes 80 and 443 for another site. One `ufw allow 7789`
    without `in on wg0` puts the deck beside them.
    """
    for line in code_lines():
        if not re.search(r"ufw\s+(--\S+\s+)*allow", line):
            continue
        assert not re.search(r"allow\s+\$?\{?\w+\}?(/(tcp|udp))?\s*(#.*)?$", line), \
            f"this opens the port on every interface: {line!r}"
        assert "0.0.0.0" not in line, line


def test_the_installer_never_removes_or_resets_a_firewall_rule_it_did_not_make():
    """Four other services depend on that rule list, two of them public."""
    for line in code_lines():
        assert not re.search(r"ufw\s+(--\S+\s+)*(delete|reset|disable)", line), line
        assert not re.search(r"iptables\s+(-F|--flush|-X)", line), line


# ── 4. docker, and the blast radius of installing it ─────────────────────────


def test_docker_is_never_installed_unless_the_operator_asks_for_it():
    """dockerd rewrites the host packet filter, and this box is live.

    Docker inserts its own chains into iptables ahead of ufw's, so a published
    container port is reachable on every interface *regardless* of the deny
    default -- on a box whose 80 and 443 already face the internet. It also
    enables ip_forward and adds a 172.17.0.0/16 bridge to a machine that is a
    WireGuard endpoint. That is the owner's call, so it is a flag, off by
    default, and the script says what it would do.
    """
    docker_installs = [ln for ln in code_lines()
                       if re.search(r"apt-get\s+(-\S+\s+)*install.*docker", ln)]
    if not docker_installs:
        pytest.skip("the installer does not offer docker at all")
    assert "WITH_DOCKER" in code(), "docker is installed with no flag guarding it"


def test_docker_when_installed_does_not_publish_container_ports_to_the_world():
    """`-p 5900:5900` binds 0.0.0.0 by default. On this box that is the internet.

    The agents' own-computer containers publish a browser and a terminal. The
    daemon default has to be loopback so a container written later cannot expose
    a desk by omission.
    """
    text = code()
    if "docker" not in text:
        pytest.skip("the installer does not offer docker at all")
    assert "/etc/docker/daemon.json" in text, (
        "docker is installed without a daemon default, so every published "
        "container port lands on 0.0.0.0 and bypasses ufw")
    assert '"ip"' in text and "127.0.0.1" in text, (
        "daemon.json does not pin the default publish address to loopback")


# ── 5. blast radius of the package installs themselves ───────────────────────


def test_the_installer_names_every_package_it_installs():
    """A live box. `apt-get install $SOMETHING` is not a reviewable line."""
    for line in code_lines():
        if not re.search(r"apt(-get)?\s+(-\S+\s+)*install", line):
            continue
        args = line.split("install", 1)[1]
        assert "$" not in args, \
            f"the package list is not visible in the source: {line!r}"


def test_the_installer_never_upgrades_packages_it_was_not_asked_about():
    """`apt-get upgrade` on a box running two other people's services is a deploy
    of everything at once, with no rollback and no changelog anybody read."""
    for line in code_lines():
        assert not re.search(r"apt-get\s+(-\S+\s+)*(upgrade|dist-upgrade|full-upgrade)",
                             line), line


def test_the_installer_pins_the_python_packages_it_puts_in_the_venv():
    """`pip install --upgrade fastapi uvicorn` makes run five different from run
    one, which is the definition of not idempotent. A float is also how a box
    that worked in September stops importing in October."""
    for line in code_lines():
        if not re.search(r"pip\s+install", line):
            continue
        if "--upgrade pip" in line:
            continue
        assert re.search(r"[a-z0-9_.-]+==\d", line), \
            f"unpinned package install: {line!r}"


# ── 6. the verification pass: proof, not assumption ──────────────────────────


@pytest.mark.parametrize("needle", [
    pytest.param("/api/state", id="the-service-answers"),
    pytest.param("/v1/agents", id="the-token-authenticates"),
    pytest.param("cc-approve.js", id="a-hook-reaches-the-deck"),
    pytest.param("--version", id="the-cli-runs"),
])
def test_the_installer_proves_each_piece_rather_than_assuming_it(needle):
    """A step whose proof you did not see is a step that did not happen."""
    assert needle in code(), f"nothing in the installer exercises {needle}"


def test_the_verification_checks_the_hook_actually_reached_the_deck():
    """`cc-approve.js` exits 0 and prints a decision even when it reached nothing.

    That is deliberate -- the hook must never hang a session -- and it means
    "the hook ran" proves nothing. The discriminator is the reason string it
    prints: `deck unreachable` when the socket refused, a rule verdict when the
    deck answered. Both were observed on the box.
    """
    assert "deck unreachable" in code(), (
        "the installer runs the hook but never reads what it said, so a hook "
        "posting into a closed socket passes verification")


def test_the_verification_proves_the_token_opens_the_client_surface():
    """Without a token every /v1 route answers 503 and the box looks healthy.

    Curling /v1 anonymously and calling 503 a pass is the exact shape of that
    failure, so the check must send the token and must reject a 503.
    """
    text = code()
    assert re.search(r"Authorization:\s*Bearer", text), \
        "nothing ever presents the token to /v1"
    assert "503" in text, \
        "the /v1 check does not distinguish an authenticated 200 from a 503"


def test_the_verification_runs_after_the_service_is_up_not_before():
    """Proof before the thing exists is not proof."""
    lines = code_lines()
    restarts = [i for i, ln in enumerate(lines)
                if re.search(r"systemctl\s+(restart|reload-or-restart)", ln)]
    checks = [i for i, ln in enumerate(lines) if "/v1/agents" in ln]
    assert restarts and checks, (restarts, checks)
    assert max(checks) > max(restarts), \
        "the client-surface check runs before the unit is restarted"


# ── 7. the sweep: every component a running deck actually touches ────────────
#
# Derived by grepping what the code shells out to, not from a given list:
#   server/office.py         -> git rev-parse
#   server/spawn.py          -> claude (and osascript, which Linux has not)
#   approval.py + the hooks  -> node
#   install-deck.sh itself   -> ss (iproute2), openssl, curl, python3-venv
#   the unit                 -> systemd, plus ufw to be reachable at all


@pytest.mark.parametrize("component", [
    pytest.param("git", id="git-office-reads-the-branch"),
    pytest.param("node", id="node-every-hook-runs-under-it"),
    pytest.param("claude", id="claude-the-thing-it-hires"),
    pytest.param("openssl", id="openssl-the-token"),
    pytest.param("curl", id="curl-the-verification"),
    pytest.param("ufw", id="ufw-reachability-from-the-mac"),
    pytest.param("venv", id="python3-venv"),
    pytest.param("iproute2", id="iproute2-the-port-guard"),
])
def test_the_installer_accounts_for_every_component_the_box_needs(component):
    """Named, so a missing one is a failing test and not a surprise on the box."""
    assert component in code(), f"the installer never mentions {component!r}"


def test_the_installer_says_out_loud_which_component_it_cannot_supply():
    """`server/notify.py` shells out to the WhatsApp bridge:

        DEFAULT_SCRIPT = "~/Projects/comunicate_with_me/bin/wa-send.js"

    MEASURED: that path does not exist on the box. Every routine that delivers
    over WhatsApp fails there, and it fails at delivery time rather than at
    install time -- the quietest possible place.

    A script cannot fix this: the bridge is a separate repo whose WhatsApp
    session only the owner can authenticate, the same class of thing as
    `claude setup-token`. So it is not a FAIL -- the deck runs perfectly well
    without it -- but it must be *named*. An unnamed missing component is the
    failure this whole verification phase exists to prevent.
    """
    assert "wa-send.js" in code(), (
        "the installer never mentions the WhatsApp bridge, so a box that "
        "cannot deliver a routine looks identical to one that can")


def test_the_installer_checks_the_tools_it_shells_out_to_exist():
    """`set -euo pipefail` turns a missing `ss` into a bare exit 127 halfway
    through, with the box half-configured and nothing saying which line."""
    assert re.search(r"command\s+-v", code()), \
        "nothing verifies the base tooling before using it"


def test_the_one_command_is_still_one_command():
    """The entry point does not grow a second step he has to remember."""
    assert INSTALLER.stat().st_mode & 0o111, "install-deck.sh is not executable"
    done = subprocess.run(["bash", "-n", str(INSTALLER)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "sudo /opt/agent-deck/deploy/install-deck.sh" in script(), \
        "the script no longer documents its own single entry point"


def test_the_runbook_names_the_pieces_the_one_command_now_installs():
    """A runbook that describes the old script is trusted and wrong."""
    text = BOX_DOC.read_text().lower()
    for needle in ("ufw", "setup-token", "deck_url", "docker"):
        assert needle in text, f"the runbook never mentions {needle}"


# ── 8. who holds the port: the re-run refusal, found by running it ───────────
#
# MEASURED on the box on 2026-09-02, on the SECOND run of the installer with the
# deck already listening:
#
#   install-deck: this box is not ready:
#     port 7789 is already in use by python: the deck will not fight it for the
#     port -- stop that service or pick another port
#   Nothing was enabled. Fix the above and run me again.
#
# The first install passed because 7789 was free. Every run after it refuses,
# which breaks the one property the whole design rests on. `ss` reports the
# kernel's comm string, and for every python service on that box that string is
# the word `python`:
#
#   LISTEN 0 2048 10.99.0.1:7789 0.0.0.0:* users:(("python",pid=2221429,fd=12))
#   LISTEN 0 2048 10.99.0.1:7788 0.0.0.0:* users:(("python",pid=3626971,fd=6))
#
# `deploy_check._is_our_own` looks for "agentdeck" in that string and cannot
# find it, so the deck refuses to recognise itself. The judge is right -- a bare
# `python` really could be any of four services on that box. The MEASUREMENT is
# what is wrong, and systemd knows the answer: `systemctl status 2221429` starts
# `● agentdeck.service - Agent Deck ...`.


def port_holder_function() -> str:
    """`port_holder()` lifted out of the installer, to be run against stubs.

    Extracted rather than described: this is the one piece of the script whose
    *behaviour* decides whether a re-run works, and a grep for the word
    `systemctl` would pass on a script that called it and ignored the answer.
    """
    text = INSTALLER.read_text()
    start = text.index("port_holder() {")
    depth, i = 0, start
    while True:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1


def run_port_holder(tmp_path, ss_output: str, systemctl_output: str = "") -> str:
    """Run the real function with fake `ss` and `systemctl` on PATH."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name, output in (("ss", ss_output), ("systemctl", systemctl_output)):
        stub = stub_dir / name
        stub.write_text("#!/bin/sh\ncat <<'STUBOUT'\n" + output + "\nSTUBOUT\n")
        stub.chmod(0o755)
    done = subprocess.run(
        ["bash", "-c", f'PATH="{stub_dir}:$PATH"\n{port_holder_function()}\n'
                       'port_holder 7789\n'],
        capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


#: Both strings copied off the box rather than invented.
SS_LINE = ('LISTEN 0      2048   10.99.0.1:7789 0.0.0.0:* '
           'users:(("python",pid=2221429,fd=12))')
SYSTEMCTL_STATUS = (
    "* agentdeck.service - Agent Deck - the board across Sam's Claude sessions\n"
    "     Loaded: loaded (/etc/systemd/system/agentdeck.service; enabled)")


def test_the_port_holder_is_named_by_systemd_not_by_the_kernels_comm_string(tmp_path):
    """The re-run bug, at its source."""
    assert run_port_holder(tmp_path, SS_LINE, SYSTEMCTL_STATUS) == "agentdeck.service"


def test_a_holder_systemd_does_not_recognise_still_gets_named(tmp_path):
    """A stranger squatting on the port is the case the refusal exists for.

    Falling back to the comm string is worse than nothing only if it is silent.
    `python` is a poor answer; no answer at all reads as "the port is free" and
    the installer would move in.
    """
    assert run_port_holder(tmp_path, SS_LINE, "") == "python"


def test_no_listener_reports_no_holder(tmp_path):
    """An empty answer must mean free, and nothing else."""
    assert run_port_holder(tmp_path, "", "") == ""


def test_the_deck_recognises_its_own_service_as_the_holder_of_its_own_port():
    """The other half of the same contract, in the tested judge.

    `preflight` must let a re-run through when the deck already owns the port,
    and must still refuse a bare `python` -- the honest verdict on a name that
    could be any of the four python services on that box.
    """
    from server.deploy_check import preflight

    facts = {
        "python_version": "3.12.3", "node_present": True,
        "claude_present": True, "claude_authenticated": True,
        "deck_port": 7789, "bus_dir_writable": True,
        "token_configured": True, "disk_free_mb": 120000,
    }
    ours = preflight({**facts, "ports_in_use": {"7788": "deckop.service",
                                               "7789": "agentdeck.service"}})
    assert ours == [], ours

    unnamed = preflight({**facts, "ports_in_use": {"7789": "python"}})
    assert any("7789" in blocker for blocker in unnamed), unnamed


# ---------------------------------------------------------------- HOME

def test_running_the_cli_as_the_deck_user_carries_that_user_s_home():
    """`sudo -u deckop claude` runs with ROOT's HOME, and the CLI reads it.

    Measured on 10.99.0.1 on 2026-09-02, from the owner's own screen:

        /root/.claude/settings.local.json
          └ Settings file could not be read: EACCES: permission denied,
            stat '/root/.claude/settings.local.json'

    `sudo -u` changes the user and leaves `HOME=/root`. Claude Code resolves
    its config, its settings and its credentials from `$HOME`, so as the deck
    user it looks in root's home -- a directory that user cannot even stat.
    The script's own login hint told him to run exactly that.

    This is the whole class, not the one line: `as_deck()` is the helper every
    step goes through, so any command it runs that cares about `$HOME` is
    wrong in the same way. Pinning the helper pins the class.

    Asserts the GOOD signal -- HOME present and pointing at the deck user --
    rather than the absence of an error, because a script that ran nothing at
    all would also produce no error.
    """
    lines = script().splitlines()

    helper = [ln for ln in lines if ln.strip().startswith("as_deck()")]
    assert helper, "as_deck() is gone; this test no longer guards anything"
    assert "HOME" in helper[0], (
        "as_deck() runs as the deck user with root's HOME: " + helper[0])

    # Every hint the script prints for a human must carry it too -- that is the
    # line he actually pasted.
    for ln in lines:
        if "${CLAUDE_BIN}" in ln and "sudo -u" in ln:
            assert "HOME" in ln, f"printed command loses HOME: {ln.strip()}"


def test_the_unit_carries_the_cli_login_so_hired_desks_inherit_it():
    """A deck that cannot log its desks in can hire nobody.

    Measured on 10.99.0.1 on 2026-09-02: the box's browser credentials from
    31 August had expired -- `claude -p` answered `Failed to authenticate:
    OAuth session expired and could not be refreshed`, while every automated
    check in the installer passed, because a credentials FILE existed. A green
    check that proves a file is present rather than that it works is the
    failure mode this project keeps hitting.

    A long-lived token (`claude setup-token`) replaces it, and the CLI reads it
    from `CLAUDE_CODE_OAUTH_TOKEN` -- confirmed by reading the env names out of
    the 2.1.258 binary, not from documentation.

    It has to reach the *spawned* session, not just a shell: the daemon starts
    desks, so the unit is where it belongs. `EnvironmentFile=-` with the
    leading dash so a box that has not been given a token still boots -- a deck
    that refuses to start is worse than one that cannot yet hire, and the
    installer's verification already reports the login separately.
    """
    text = UNIT.read_text()
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip().startswith("EnvironmentFile")]
    assert lines, "the unit passes no environment file; a hired desk has no login"
    oauth = [ln for ln in lines if "oauth" in ln.lower()]
    assert oauth, f"no login env file in the unit: {lines}"
    assert oauth[0].split("=", 1)[1].startswith("-"), (
        "EnvironmentFile must be optional (leading '-') or a box with no token "
        f"fails to boot: {oauth[0]}")


def test_the_health_check_presents_a_credential_now_that_api_is_gated():
    """`/api` authenticates, so an unauthenticated health check fails closed.

    A container reached the owner's live board through the Docker gateway and
    pulled 114,727 bytes of it, so `/api/*` now takes a bearer or a cookie.
    This script's first verification step curls `/api/state` and treats
    anything but 200 as a failure -- so on a *healthy* box it would now see 401
    and refuse the install.

    The failure mode that matters is the second-order one: an installer that
    cries wolf on a working box teaches its reader to ignore it, which is
    exactly what the verification phase exists to prevent.

    Asserts the GOOD signal -- the check sends a credential -- rather than the
    absence of a 401, because a check that stopped running at all would also
    produce no 401.
    """
    # Anchor on the curl target itself. The script narrates "/api/state" in a
    # comment and inside a die message, and this file's own docstring warns
    # that a promise in prose is how the previous version claimed the hooks
    # were wired -- so match the shell expansion the request actually uses.
    lines = script().splitlines()
    idx = [i for i, ln in enumerate(lines)
           if '"${DECK_BASE}/api/state"' in ln]
    assert idx, "the health check is gone; this test no longer guards anything"

    for i in idx:
        window = "\n".join(lines[max(0, i - 4):i + 1])
        assert "Authorization" in window, (
            "the /api/state health check sends no credential and will 401 on a "
            f"healthy box:\n{window}")

    # And the token must be read before it is used, not after.
    read_at = next((i for i, ln in enumerate(lines)
                    if "AGENT_DECK_TOKEN=" in ln and "sed -n" in ln), None)
    assert read_at is not None, "the script no longer reads the token"
    assert read_at < min(idx), (
        "the token is read after the check that needs it "
        f"(read at {read_at}, used at {min(idx)})")


# ---------------------------------------------------------------- the venv

def test_every_third_party_module_the_server_imports_is_installed_pinned():
    """MEASURED 2026-09-30 in a systemd container (install-deck.sh on a fresh
    Ubuntu 24.04): the deck died at start with `ModuleNotFoundError: No module
    named 'httpx'` (server/realtime.py). The owner's box has httpx==0.28.1 in
    its venv -- put there by hand and never folded back into the installer, so
    the next box would never have started.

    The class: every top-level module server/ imports that is neither stdlib
    nor the server package itself must be pip-installed, pinned, by the script.
    """
    import ast
    import sys as _sys

    stdlib = set(_sys.stdlib_module_names)
    imported = set()
    for path in (REPO / "server").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in tree.body:  # module level only: lazy imports are optional deps
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    third_party = sorted(imported - stdlib - {"server", "__future__"})
    pins = " ".join(ln for ln in code_lines() if re.search(r"pip\s+install", ln))
    for mod in third_party:
        assert re.search(rf"\b{mod}==\d", pins), \
            f"server/ imports {mod!r} at module level but the installer never installs it pinned"
