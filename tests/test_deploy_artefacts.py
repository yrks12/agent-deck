"""The deploy artefacts are read by systemd and by root. Nobody reviews them twice.

These are the two files that decide what the always-on box actually runs:
`deploy/agentdeck.service` and `deploy/install-deck.sh`. They are not executed
here -- this Mac is not the box, and the brief forbids touching it -- so every
claim below is made by *parsing* the artefacts, which is also how systemd and
bash themselves read them.

Two failure modes are worth this much test weight:

  * **a token in the unit file.** `/etc/systemd/system/*.service` is
    world-readable and `systemctl cat` prints it to anyone. A secret pasted
    there leaks to every process on the box and into every support paste.
  * **an installer that is not idempotent.** Sam will run it more than once --
    a re-run after a failure is the normal case. If a second run appends a
    second copy of anything, the box ends up with duplicate units, duplicate
    env lines, or a regenerated token that invalidates the Mac's saved one.
"""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "deploy" / "install-deck.sh"
RENDER = REPO / "deploy" / "render.py"
TEMPLATES = REPO / "deploy" / "templates"
OWNER_TOML = REPO / "tests" / "fixtures" / "owner-box" / "deck.toml"


def _render():
    spec = importlib.util.spec_from_file_location("deck_render", RENDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _RenderedUnit:
    """The unit as the owner's box receives it: the template rendered from the
    owner profile. The static deploy/agentdeck.service is gone (K1); what
    systemd reads is this render, and tests/test_render_owner_profile.py pins
    it line-for-line to the unit captured off the live box."""

    name = "agentdeck.service"

    def __init__(self, toml):
        self.toml = toml

    def read_text(self) -> str:
        render = _render()
        return render.render_all(render.load_config(self.toml))["agentdeck.service"]


UNIT = _RenderedUnit(OWNER_TOML)
BOX_DOC = REPO / "docs" / "the-box.md"

#: yair_os's engine already serves its dashboard here. It is not ours to take.
DECKOP_PORT = "7788"
#: The deck's port on the box: the next one up, so the two coexist.
DECK_PORT = "7789"
#: The WireGuard address of the box. The Mac is 10.99.0.4 on the same tunnel.
WG_ADDRESS = "10.99.0.1"


# ── helpers: read the artefacts the way their real readers do ────────────────


def unit_directives() -> list[tuple[str, str, str]]:
    """[(section, key, value)] from the unit, comments and blanks dropped.

    Hand-rolled rather than `configparser` because systemd allows a key to
    repeat (`Environment=`, `ExecStartPre=`) and configparser silently keeps
    only the last -- which would hide exactly the kind of duplicate a review
    needs to see.
    """
    out: list[tuple[str, str, str]] = []
    section = ""
    for raw in UNIT.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, _, value = line.partition("=")
        out.append((section, key.strip(), value.strip()))
    return out


def values_for(key: str) -> list[str]:
    return [v for _, k, v in unit_directives() if k == key]


def secret_shaped(text: str) -> list[str]:
    """Words that look like a credential rather than like configuration.

    Split on `=` as well as whitespace so `Environment=AGENT_DECK_PORT=7789`
    is read as three short words and not one long suspicious one. A real token
    (hex, base64url, a UUID without dashes) is a long run of letters-and-digits
    with no `/` or `.`, which no systemd path, hostname or unit name is.
    """
    words = re.split(r"[\s=,;:'\"()\[\]{}]+", text)
    return [
        w
        for w in words
        if len(w) >= 24
        and re.fullmatch(r"[A-Za-z0-9_+-]+", w)
        and any(c.isdigit() for c in w)
        and any(c.isalpha() for c in w)
    ]


def installer_lines() -> list[str]:
    return INSTALLER.read_text().splitlines()


def enclosing_conditions(index: int) -> list[str]:
    """The `if` conditions a given installer line sits inside.

    A crude but honest shell reader: `... ; then` opens a block, `fi` closes
    one. It is enough to prove that a mutating command is *guarded* rather than
    merely near a guard, which a substring search could not tell apart.
    """
    stack: list[str] = []
    for line in installer_lines()[:index]:
        stripped = line.strip()
        if stripped.startswith("fi"):
            if stack:
                stack.pop()
        elif stripped.startswith(("if ", "elif ")) or stripped.endswith("; then"):
            stack.append(stripped)
    return stack


# ── the artefacts exist at all ───────────────────────────────────────────────


def test_both_artefacts_are_present_and_the_installer_is_executable():
    assert (TEMPLATES / "agentdeck.service.in").is_file(), "missing the unit template"
    assert RENDER.is_file(), f"missing {RENDER}"
    assert INSTALLER.is_file(), f"missing {INSTALLER}"
    assert INSTALLER.stat().st_mode & 0o111, "install-deck.sh is not executable"


def test_the_static_owner_units_are_gone_so_nothing_can_install_them():
    """Two sources of truth drift. The units exist only as templates now."""
    for name in ("agentdeck.service", "deckdoctor.service", "agent-deck-log.logrotate"):
        assert not (REPO / "deploy" / name).exists(), f"deploy/{name} still ships"


def test_no_template_carries_a_secret_or_the_token_value():
    for tpl in TEMPLATES.glob("*.in"):
        assert secret_shaped(tpl.read_text()) == [], tpl.name
        assert not re.search(r"^Environment=AGENT_DECK_TOKEN", tpl.read_text(), re.M), tpl.name


def test_the_installer_is_valid_bash():
    """`bash -n` parses without running. A unit test for a shell script's syntax.

    Parsing is not execution: nothing on this machine or the box is touched.
    """
    done = subprocess.run(["bash", "-n", str(INSTALLER)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# ── 3. the unit file must not carry the token ────────────────────────────────


def test_the_unit_reads_the_token_from_an_environment_file():
    """The good signal: the secret has a declared home, outside the unit."""
    files = values_for("EnvironmentFile")
    assert files, "the unit declares no EnvironmentFile, so the token has nowhere to live"
    for path in files:
        bare = path.lstrip("-")  # systemd's "tolerate if missing" prefix
        assert bare.startswith("/"), f"EnvironmentFile must be absolute: {path}"
        assert not bare.startswith(str(REPO)), "the token file must not live in the repo"


def test_the_unit_never_assigns_the_token_itself():
    """`Environment=AGENT_DECK_TOKEN=...` would put the secret in `systemctl cat`."""
    for section, key, value in unit_directives():
        assert not (key == "Environment" and "AGENT_DECK_TOKEN" in value), \
            f"[{section}] {key}={value}"


def test_no_secret_shaped_string_appears_anywhere_in_the_unit():
    """Comments included -- a token pasted into a comment leaks just as well."""
    found = secret_shaped(UNIT.read_text())
    assert found == [], f"credential-shaped strings in the unit: {found}"


def test_the_unit_names_the_token_variable_so_the_operator_knows_what_to_put_there():
    """Naming the variable is not leaking it, and an unnamed one never gets set."""
    assert "AGENT_DECK_TOKEN" in UNIT.read_text()


# ── the unit runs the right thing, in the right place, on the right port ─────


def test_the_unit_runs_as_the_deckop_user_and_not_as_root():
    assert values_for("User") == ["deckop"], values_for("User")


def test_a_default_profile_runs_as_its_own_user_on_loopback(tmp_path):
    """A stranger's box: no deck.toml values of the owner's, deck on loopback."""
    toml = tmp_path / "stranger.toml"
    toml.write_text('[network]\nhostname = "5-161-10-20.sslip.io"\n')
    unit = _RenderedUnit(toml).read_text()
    assert re.search(r"^User=agentdeck$", unit, re.M)
    assert "--host 127.0.0.1 --port 7789" in unit
    assert "wg-quick" not in unit


def test_the_unit_binds_the_wireguard_address_and_not_every_interface():
    """Reachable from the Mac over the tunnel, and from nowhere else.

    0.0.0.0 would expose the whole board -- sessions, transcripts, the message
    surface -- to anything that can route to the box.
    """
    execs = values_for("ExecStart")
    assert execs, "the unit has no ExecStart"
    start = execs[0]
    assert WG_ADDRESS in start, start
    assert "0.0.0.0" not in start, start


def test_the_unit_binds_7789_and_no_directive_binds_yair_os_port():
    execs = values_for("ExecStart")
    assert DECK_PORT in execs[0], execs[0]
    for section, key, value in unit_directives():
        assert DECKOP_PORT not in value, \
            f"[{section}] {key}={value} would collide with yair_os"


def test_the_unit_waits_for_the_tunnel_it_binds_to():
    """Binding 10.99.0.1 before wg0 exists fails at boot with EADDRNOTAVAIL."""
    ordering = " ".join(values_for("After") + values_for("Wants"))
    assert "wg" in ordering, f"nothing orders the unit after WireGuard: {ordering!r}"


def test_the_unit_restarts_itself_and_starts_at_boot():
    """The whole point of the box: it keeps running with the laptop shut."""
    assert values_for("Restart"), "no Restart= -- one crash and routines stop"
    assert values_for("Restart")[0] in {"always", "on-failure"}
    assert values_for("WantedBy") == ["multi-user.target"], values_for("WantedBy")


# ── 4. the installer must be idempotent ──────────────────────────────────────


def test_the_installer_fails_fast_on_any_error():
    assert "set -euo pipefail" in INSTALLER.read_text()


def test_the_installer_never_appends_to_a_file():
    """`>>` is how a re-run duplicates a line. Every write here overwrites."""
    appends = [ln for ln in installer_lines()
               if ">>" in ln and not ln.strip().startswith("#")]
    assert appends == [], f"append redirections found: {appends}"


def test_every_directory_the_installer_makes_tolerates_already_existing():
    made = [ln for ln in installer_lines()
            if re.search(r"\bmkdir\b", ln) and not ln.strip().startswith("#")]
    assert made, "the installer creates no directories at all"
    for line in made:
        assert re.search(r"\bmkdir\s+(-\S+\s+)*-\S*p", line), line


def test_the_token_is_generated_only_when_the_env_file_does_not_exist():
    """The re-run pin. Regenerating the token would silently lock out the Mac.

    Proven structurally -- the generating line must sit *inside* an `if` whose
    condition tests the env file's absence -- not by finding a guard somewhere
    in the file.
    """
    generators = [
        i for i, ln in enumerate(installer_lines())
        if re.search(r"openssl\s+rand|/dev/urandom|uuidgen", ln)
        and not ln.strip().startswith("#")
    ]
    assert generators, "the installer never generates a token"
    for i in generators:
        conditions = " ".join(enclosing_conditions(i))
        assert "! -f" in conditions or "! -s" in conditions, \
            f"line {i + 1} generates a token unguarded: {installer_lines()[i]!r}"
        assert "ENV_FILE" in conditions, \
            f"line {i + 1} is guarded, but not by the env file's absence"


def test_the_env_file_is_written_private_to_its_owner():
    text = INSTALLER.read_text()
    assert re.search(r"(install|chmod)\s+(-\S+\s+)*0?600", text), \
        "nothing sets the env file to 0600; the token would be world-readable"


def test_the_installer_uses_idempotent_systemd_verbs():
    """`daemon-reload` + `enable` + `restart` all behave the same on run two."""
    text = INSTALLER.read_text()
    assert "daemon-reload" in text
    assert re.search(r"systemctl\s+enable", text)
    assert re.search(r"systemctl\s+(restart|reload-or-restart)", text)


def test_the_installer_copies_the_unit_rather_than_editing_one_in_place():
    """Overwrite, so run two produces byte-identical state, not an accretion.

    This first demanded the literal `agentdeck.service` on the `install` line,
    which forbade the perfectly good `"$UNIT_SRC"` the script actually uses --
    that pinned a spelling, not a property. What matters is that the unit
    arrives by a whole-file copy into /etc/systemd/system and that nothing ever
    edits the installed copy in place, so the two runs cannot diverge.
    """
    text = INSTALLER.read_text()
    copies = [ln for ln in installer_lines()
              if re.match(r"\s*(install|cp|put)\s", ln) and "UNIT_DST" in ln]
    assert copies, "the unit is never copied to its destination"
    assert re.search(r'UNIT_DST=.*/etc/systemd/system/', text), \
        "the destination is not under /etc/systemd/system"
    assert "agentdeck.service" in text
    for editor in (r"sed\s+-i", r"\bpatch\b", r"\bex\s+-s"):
        assert not re.search(editor, text), f"{editor} edits an installed unit in place"


# ── 2 (again): the installer refuses ports another service owns ────────────
#
# On the owner's box that is 7788 (deckop.service). It used to be a literal in
# the script; it is now `deck.reserved_ports` in deck.toml, so a stranger's box
# carries no knowledge of the owner's and the owner's box keeps the refusal.


def code_lines() -> list[str]:
    return [ln for ln in installer_lines() if ln.strip() and not ln.strip().startswith("#")]


def test_the_installer_refuses_a_deck_port_listed_as_reserved():
    code = "\n".join(code_lines())
    assert "RESERVED_PORTS" in code, "the installer never reads deck.reserved_ports"
    loop = [i for i, ln in enumerate(code_lines()) if re.search(r"for \w+ in \$\{?RESERVED_PORTS", ln)]
    assert loop, "reserved ports are read but never walked"
    window = "\n".join(code_lines()[loop[0]:loop[0] + 6])
    assert "DECK_PORT" in window and re.search(r"\bdie\b", window), window


def test_the_owner_profile_still_reserves_7788():
    render = _render()
    cfg = render.load_config(OWNER_TOML)
    assert int(DECKOP_PORT) in cfg.deck.reserved_ports


def test_the_installer_names_no_owner_port_service_or_site_in_code():
    for line in code_lines():
        assert not re.search(r"\b7788\b|deckop|initech|10\.99\.", line), line


def test_the_installer_never_stops_or_disables_any_service():
    """Coexistence is the design. Taking a neighbour down is not a deploy."""
    for line in code_lines():
        assert not re.search(r"systemctl\s+(stop|disable|mask)\b", line), line
        assert not re.search(r"\bkill(all)?\s", line), line


def test_reserved_ports_are_proved_untouched_after_the_install():
    """The control: who holds each reserved port before, and the same after."""
    code = "\n".join(code_lines())
    assert "RESERVED_BEFORE" in code and "RESERVED_AFTER" in code


# ── K1/K6: deck.toml is the input, and the owner's box migrates itself ──────


def test_a_box_with_a_unit_and_no_deck_toml_is_migrated_not_reset():
    code = "\n".join(code_lines())
    assert re.search(r"render\.py.*\binfer\b|\binfer\b.*--unit", code), \
        "the installer never infers deck.toml from the running unit"
    infer_at = [i for i, ln in enumerate(installer_lines()) if re.search(r"\binfer\b", ln)
                and not ln.strip().startswith("#")]
    conditions = " ".join(enclosing_conditions(infer_at[0]))
    assert "! -f" in conditions and "DECK_TOML" in conditions, \
        "deck.toml is inferred without checking it is absent -- a re-run would overwrite it"


def test_units_are_rendered_from_deck_toml_not_copied_from_static_files():
    code = "\n".join(code_lines())
    assert re.search(r"render\.py.*\brender\b|RENDER.*\brender\b", code)
    assert "SRC_DIR}/agentdeck.service" not in code


def test_deck_toml_is_readable_by_the_service_user_and_the_token_is_not():
    """deck.toml 0644 in a 0755 /etc/agent-deck; agentdeck.env stays 0600 root."""
    code = "\n".join(code_lines())
    assert re.search(r"chmod 0755 \"\$ETC_DIR\"", code), "deck.toml sits in a dir the service user cannot enter"
    assert re.search(r"install -m 0644 .*DECK_TOML", code)
    assert re.search(r'chmod 0600 "\$ENV_FILE"', code)


# ── the runbook has to agree with the artefacts ──────────────────────────────


@pytest.mark.parametrize(
    "needle",
    ["agentdeck.service", DECK_PORT, WG_ADDRESS, "systemctl", "rollback"],
    ids=["unit-name", "port", "wireguard-address", "the-command", "rollback"],
)
def test_the_runbook_matches_what_the_artefacts_actually_do(needle):
    """A runbook that drifts from the unit is worse than none -- it is trusted."""
    assert BOX_DOC.is_file(), f"missing {BOX_DOC}"
    assert needle.lower() in BOX_DOC.read_text().lower(), needle


def test_the_runbook_says_what_was_assumed_rather_than_measured():
    """Nobody touched the box to write this. The doc must admit which parts."""
    text = BOX_DOC.read_text().lower()
    assert "assum" in text, "the runbook claims measurement it never made"
