"""K5: `deckctl`, the admin CLI. Hermetic: every machine-touching seam (systemctl,
sudo, docker, openssl, the network, chown, the terminal) is injected, so these run
as an ordinary user with nothing installed."""
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from server import pairing

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "deckctl"
TOKEN = "sk-ant-oat01-" + "A" * 40
NOW = 1_800_000_000.0


def load():
    loader = importlib.machinery.SourceFileLoader("deckctl", str(SCRIPT))
    spec = importlib.util.spec_from_loader("deckctl", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FakeHost:
    def __init__(self):
        self.up = True
        self.cert = 60.0
        self.latest = None
        self.login = (0, "ok")

    def now(self): return NOW
    def today(self): return "2026-09-30"
    def version(self): return "0.9.0"
    def active(self, unit): return self.up
    def get(self, url, timeout=5): return 200 if self.up else None
    def listeners(self, port): return []
    def cert_days(self, host): return self.cert
    def resolve(self, host): return ["203.0.113.7"]
    def public_ip(self): return "203.0.113.7"
    def ufw_ports(self): return {22, 443}
    def have(self, cmd): return True
    def claude_login(self): return self.login
    def docker_ok(self): return True
    def docker_image(self, name): return True
    def disk_pct(self): return 40
    def mem_gb(self): return 8.0
    def clock_synced(self): return True
    def openai_key(self): return None
    def openai_ok(self, key): return True
    def latest_version(self): return self.latest
    def ssh_password(self): return False


class Box:
    """A fake admin session: a tmp /etc/agent-deck, and a log of everything run."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.etc = tmp_path / "etc"
        self.etc.mkdir()
        self.state = tmp_path / "state"
        self.home = tmp_path / "home"
        self.toml = self.etc / "deck.toml"
        self.toml.write_text(
            '[deck]\nstate_dir = "%s"\nhome = "%s"\n\n[network]\nhostname = "203-0-113-7.sslip.io"\n\n'
            '[claude]\noauth_env = "%s"\n' % (self.state, self.home, self.home / "oauth.env"))
        self.env_file = self.etc / "agentdeck.env"
        self.env_file.write_text("AGENT_DECK_TOKEN=abc\n")
        self.env_file.chmod(0o600)
        self.ran, self.tty, self.chowned, self.execs = [], [], [], []
        #: argv substring -> what that command prints (default "ok").
        self.outputs: dict[str, str] = {}
        self.host = FakeHost()
        self.run_rc = 0
        self.http = {}
        self.secret_in = TOKEN
        self.euid = 0

    def ctl(self, **over):
        mod = load()
        kw = dict(
            config_path=self.toml, env_file=self.env_file, euid=self.euid,
            run=self._run, run_tty=lambda argv: (self.tty.append(argv) or 0),
            secret=lambda prompt: self.secret_in,
            http_status=lambda url, headers=None: self.http.get(url, 200),
            host_factory=lambda cfg: self.host,
            fix_owner=lambda paths, user: self.chowned.append((list(map(str, paths)), user)),
            exec_sudo=lambda argv: (self.execs.append(argv) or 0),
            clock=lambda: NOW, today=lambda: "2026-09-30",
            pin=lambda host: "sha256/" + "A" * 43 + "=",
        )
        kw.update(over)
        return mod, mod.Ctl(**kw)

    def _run(self, argv, timeout=60):
        self.ran.append(list(argv))
        line = " ".join(map(str, argv))
        return self.run_rc, next((o for k, o in self.outputs.items() if k in line), "ok")

    def main(self, *argv, **over):
        mod, ctl = self.ctl(**over)
        code = mod.main(list(argv), ctl)
        return code


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def out(capsys):
    cap = capsys.readouterr()
    return cap.out, cap.err


# -- shape ------------------------------------------------------------------


def test_the_script_is_executable_stdlib_python():
    assert SCRIPT.stat().st_mode & 0o111
    assert SCRIPT.read_text().startswith("#!/usr/bin/env python3")


def test_bad_usage_exits_2(box, capsys):
    assert box.main("frobnicate") == 2
    assert box.main() == 2


def test_not_root_reruns_under_sudo_and_says_why(box, capsys):
    box.euid = 1000
    assert box.main("status") == 0
    _, err = out(capsys)
    assert "sudo" in err and "root" in err
    assert box.execs and box.execs[0][0] == "sudo"
    assert box.execs[0][-1] == "status"


def test_help_needs_no_root(box, capsys):
    box.euid = 1000
    assert box.main("--help") == 0
    assert box.execs == []


# -- status -----------------------------------------------------------------


def test_status_is_one_screen_with_address_cert_devices_login_and_version(box, capsys):
    box.main("status")
    text, _ = out(capsys)
    assert "Shaliach is running" in text
    assert "https://203-0-113-7.sslip.io" in text
    assert "60 days" in text
    assert "0 devices" in text
    assert "0.9.0" in text
    assert len(text.strip().splitlines()) <= 12


def test_status_exits_1_when_the_deck_is_down(box, capsys):
    box.host.up = False
    assert box.main("status") == 1
    assert "not running" in out(capsys)[0]


def test_status_json(box, capsys):
    box.main("status", "--json")
    data = json.loads(out(capsys)[0])
    assert data["running"] is True and data["devices"] == 0
    assert data["address"] == "https://203-0-113-7.sslip.io"
    assert data["version"] == "0.9.0"


# -- pair / devices / revoke -------------------------------------------------


def test_pair_prints_a_parseable_code_its_expiry_and_a_qr(box, capsys):
    assert box.main("pair", "--name", "Dan's Mac") == 0
    text, _ = out(capsys)
    line = next(l for l in text.splitlines() if l.startswith("ADK1."))
    code = pairing.parse(line)
    assert code.url == "https://203-0-113-7.sslip.io"
    assert code.name == "Dan's Mac" and code.tls == "ca"
    assert code.exp == int(NOW) + pairing.CODE_TTL
    assert "15 minutes" in text
    assert any(a[0] == "qrencode" for a in box.ran)


def test_pair_without_a_qr_tool_still_prints_the_text(box, capsys):
    box.run_rc = 127
    assert box.main("pair") == 0
    assert "ADK1." in out(capsys)[0]


def test_pair_stores_only_a_hash_and_hands_the_files_to_the_service_user(box, capsys):
    box.main("pair", "--json")
    data = json.loads(out(capsys)[0])
    secret = pairing.parse(data["code"]).code
    raw = (box.state / "pairing.json").read_text()
    assert secret not in raw and pairing.sha256_hex(secret) in raw
    assert box.chowned and box.chowned[-1][1] == "agentdeck"


def test_pair_on_a_self_signed_box_pins_the_certificate(box, capsys):
    box.toml.write_text(box.toml.read_text().replace(
        '[network]', '[network]\ntls = "self-signed"'))
    box.main("pair", "--json")
    code = pairing.parse(json.loads(out(capsys)[0])["code"])
    assert code.tls == "pin" and code.pin.startswith("sha256/")


def test_pair_refuses_when_there_is_no_secure_address(box, capsys):
    box.toml.write_text('[deck]\nstate_dir = "%s"\n' % box.state)
    assert box.main("pair") == 1
    text = "".join(out(capsys))
    assert "network.hostname" in text and "ADK1." not in text


def test_devices_lists_and_revoke_removes_one(box, capsys):
    store = pairing.DeviceStore(box.state)
    _, dev = store.add("Dan's MacBook")
    assert box.main("devices") == 0
    text, _ = out(capsys)
    assert dev.id in text and "Dan's MacBook" in text
    assert box.main("revoke", dev.id) == 0
    assert store.list() == []


def test_revoke_of_an_unknown_id_says_so_and_fails(box, capsys):
    assert box.main("revoke", "d_00000000") == 1
    assert "deckctl devices" in "".join(out(capsys))


def test_devices_json_never_holds_a_token_hash(box, capsys):
    pairing.DeviceStore(box.state).add("x")
    box.main("devices", "--json")
    raw = out(capsys)[0]
    assert "sha256" not in raw and json.loads(raw)[0]["name"] == "x"


# -- login -------------------------------------------------------------------
#
# TERMS (claude-dashbaord-oss-terms.md): a developer "may not collect, store, or
# intermediate Claude.ai credentials or session tokens". So `deckctl login` runs
# the CLI's OWN `claude auth login` for the service user -- the CLI writes and
# refreshes its `.credentials.json` -- proves it with `claude auth status`, and
# never asks for, reads, writes or prints a token. The old setup-token in
# claude.oauth_env is retired (deleted) once the CLI's own login is proven.

LOGGED_IN = json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                        "subscriptionType": "max"})
LOGGED_OUT = json.dumps({"loggedIn": False, "authMethod": "none"})


def _files(root: Path) -> dict[str, bytes]:
    return {str(f): f.read_bytes() for f in root.rglob("*") if f.is_file()}


def test_login_runs_the_clis_own_login_for_the_service_user_by_path(box, capsys):
    # By path: sudo's secure_path has no ~/.local/bin (measured 2026-09-30).
    box.outputs["auth status"] = LOGGED_IN
    assert box.main("login") == 0
    claude = f"{box.home}/.local/bin/claude"
    assert box.tty == [["sudo", "-u", "agentdeck", "-H", claude, "auth", "login"]]
    assert ["sudo", "-u", "agentdeck", "-H", claude, "auth", "status", "--json"] in box.ran


def test_login_never_writes_reads_or_asks_for_a_token(box, capsys):
    box.outputs["auth status"] = LOGGED_IN
    before = _files(box.tmp)

    def no_secret(prompt):
        raise AssertionError(f"deckctl asked for a secret: {prompt!r}")

    assert box.main("login", secret=no_secret) == 0
    after = _files(box.tmp)
    written = {k: v for k, v in after.items() if before.get(k) != v}
    assert all(b"sk-ant" not in v for v in written.values()), sorted(written)
    assert not (box.home / "oauth.env").exists()
    assert "token" not in " ".join(" ".join(a) for a in box.ran + box.tty).replace(
        "--json", "")


def test_a_token_file_is_refused_not_stored(box, tmp_path, capsys):
    f = tmp_path / "t"
    f.write_text(TOKEN + "\n")
    assert box.main("login", "--token-file", str(f)) == 1
    assert "no longer stores" in "".join(out(capsys))
    assert box.tty == [] and not (box.home / "oauth.env").exists()


def test_a_login_the_cli_does_not_confirm_fails_and_says_so(box, capsys):
    box.outputs["auth status"] = LOGGED_OUT
    assert box.main("login") == 1
    assert "not signed in" in "".join(out(capsys))
    assert ["systemctl", "restart", "agentdeck"] not in box.ran


def test_a_cli_login_that_did_not_finish_stops_there(box, capsys):
    mod, ctl = box.ctl(run_tty=lambda argv: (box.tty.append(argv) or 1))
    assert mod.main(["login"], ctl) == 1
    assert not any("auth status" in " ".join(a) for a in box.ran)


def test_a_proven_login_retires_the_old_setup_token_and_restarts(box, capsys):
    box.outputs["auth status"] = LOGGED_IN
    box.home.mkdir(parents=True, exist_ok=True)
    (box.home / "oauth.env").write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
    assert box.main("login") == 0
    assert not (box.home / "oauth.env").exists()
    restart = ["systemctl", "restart", "agentdeck"]
    assert restart in box.ran
    status = next(i for i, a in enumerate(box.ran) if "status" in a)
    assert box.ran.index(restart) > status, "restart only after the login is proven"


def test_with_no_old_token_nothing_restarts(box, capsys):
    # The CLI's own login is read per call; the running deck needs no restart.
    box.outputs["auth status"] = LOGGED_IN
    assert box.main("login") == 0
    assert ["systemctl", "restart", "agentdeck"] not in box.ran


def test_the_doctor_probe_uses_the_clis_own_login():
    from server import deckconfig, doctor_checks
    ran = []
    host = doctor_checks.Host(deckconfig.DeckConfig())
    host._run = lambda argv, timeout=60: (ran.append(argv) or (0, "ok"))
    host.claude_login()
    line = " ".join(ran[-1])
    assert "oauth" not in line and "claude" in line and "-p" in line


# -- set-openai-key ----------------------------------------------------------


def test_set_openai_key_validates_then_writes_and_restarts(box, capsys):
    box.secret_in = "sk-test-123"
    assert box.main("set-openai-key") == 0
    env = box.env_file.read_text()
    assert "OPENAI_API_KEY=sk-test-123" in env and "AGENT_DECK_TOKEN=abc" in env
    assert stat.S_IMODE(box.env_file.stat().st_mode) == 0o600
    assert ["systemctl", "restart", "agentdeck"] in box.ran
    assert "sk-test-123" not in "".join(out(capsys))


def test_a_rejected_openai_key_changes_nothing(box, capsys):
    box.http["https://api.openai.com/v1/models"] = 401
    box.secret_in = "sk-bad"
    assert box.main("set-openai-key") == 1
    assert "OPENAI_API_KEY" not in box.env_file.read_text()
    assert ["systemctl", "restart", "agentdeck"] not in box.ran


def test_an_unreachable_openai_changes_nothing_and_says_why(box, capsys):
    box.http["https://api.openai.com/v1/models"] = None
    box.secret_in = "sk-x"
    assert box.main("set-openai-key") == 1
    assert "OPENAI_API_KEY" not in box.env_file.read_text()


def test_set_openai_key_replaces_an_existing_line(box, tmp_path, capsys):
    box.env_file.write_text("AGENT_DECK_TOKEN=abc\nOPENAI_API_KEY=old\n")
    f = tmp_path / "k"
    f.write_text("sk-new\n")
    assert box.main("set-openai-key", "--file", str(f)) == 0
    assert box.env_file.read_text().count("OPENAI_API_KEY") == 1
    assert "sk-new" in box.env_file.read_text()


# -- doctor ------------------------------------------------------------------


def test_doctor_prints_pass_lines_and_exits_0_when_healthy(box, capsys):
    assert box.main("doctor") == 0
    text, _ = out(capsys)
    assert text.startswith("PASS  Shaliach is running")
    assert "FAIL" not in text


def test_doctor_exits_1_and_prints_the_fix_under_a_failure(box, capsys):
    box.host.login = (1, "expired")
    assert box.main("doctor") == 1
    text, _ = out(capsys)
    assert "FAIL  Your Claude login has expired" in text
    assert "      Fix: run  sudo deckctl login" in text


def test_doctor_json_has_id_ok_says_fix(box, capsys):
    box.main("doctor", "--json")
    rows = json.loads(out(capsys)[0])
    assert {"id", "ok", "says", "fix"} <= set(rows[0])
    assert rows[0]["id"] == "service"


def test_doctor_caches_the_login_probe_between_runs(box, capsys):
    calls = []
    box.host.claude_login = lambda: (calls.append(1) or (0, "ok"))
    box.main("doctor")
    box.main("doctor")
    assert len(calls) == 1


# -- config ------------------------------------------------------------------


def test_config_get_prints_the_value(box, capsys):
    assert box.main("config", "get", "network.hostname") == 0
    assert out(capsys)[0].strip() == "203-0-113-7.sslip.io"


def test_config_get_of_a_default_works_without_the_key_in_the_file(box, capsys):
    box.main("config", "get", "deck.port")
    assert out(capsys)[0].strip() == "7789"


def test_config_set_edits_the_file_in_place_and_keeps_the_rest(box, capsys):
    before = box.toml.read_text()
    assert box.main("config", "set", "desks.docker", "false") == 0
    after = box.toml.read_text()
    assert "[desks]" in after and "docker = false" in after
    assert before.strip() in after
    assert stat.S_IMODE(box.toml.stat().st_mode) == 0o644


def test_config_set_replaces_an_existing_key_and_restarts_what_it_affects(box, capsys):
    assert box.main("config", "set", "deck.port", "7790") == 0
    assert "port = 7790" in box.toml.read_text()
    assert ["systemctl", "restart", "agentdeck"] in box.ran


def test_config_set_of_a_quiet_key_does_not_restart(box, capsys):
    assert box.main("config", "set", "doctor.public_probe_urls", '["https://x.example/"]') == 0
    assert 'public_probe_urls = ["https://x.example/"]' in box.toml.read_text()
    assert ["systemctl", "restart", "agentdeck"] not in box.ran


def test_config_set_a_list_from_plain_numbers(box, capsys):
    assert box.main("config", "set", "deck.reserved_ports", "7788,7790") == 0
    assert "reserved_ports = [7788, 7790]" in box.toml.read_text()


def test_a_bad_value_is_refused_with_a_sentence_and_the_file_is_untouched(box, capsys):
    before = box.toml.read_text()
    assert box.main("config", "set", "network.tls", "telepathy") == 1
    assert box.toml.read_text() == before
    assert "network.tls" in "".join(out(capsys))


def test_an_unknown_key_is_bad_usage(box, capsys):
    assert box.main("config", "set", "deck.nonsense", "1") == 2


def test_setting_the_same_value_changes_and_restarts_nothing(box, capsys):
    before = box.toml.read_text()
    assert box.main("config", "set", "network.hostname", "203-0-113-7.sslip.io") == 0
    assert box.toml.read_text() == before and box.ran == []


def test_config_set_re_renders_when_the_renderer_exists(box, tmp_path, capsys):
    app = tmp_path / "app"
    (app / "deploy").mkdir(parents=True)
    (app / "deploy" / "render.py").write_text("")
    box.toml.write_text(box.toml.read_text() + '\n[deck]\napp_dir = "%s"\n' % app)
    # duplicate [deck] table is invalid TOML; rewrite cleanly instead
    box.toml.write_text(
        '[deck]\nstate_dir = "%s"\napp_dir = "%s"\n\n[network]\nhostname = "h.example"\n'
        % (box.state, app))
    assert box.main("config", "set", "deck.port", "7791") == 0
    assert any("render.py" in " ".join(a) for a in box.ran)


# -- version -----------------------------------------------------------------


def test_version_shows_server_and_the_latest_when_a_channel_is_set(box, capsys):
    box.host.latest = "0.9.2"
    box.main("version")
    text, _ = out(capsys)
    assert "0.9.0" in text and "0.9.2" in text


def test_version_without_a_channel_just_says_the_server_version(box, capsys):
    box.main("version")
    text, _ = out(capsys)
    assert "0.9.0" in text and "latest" not in text.lower()


def test_no_owner_value_in_the_cli():
    import re
    assert not re.search(r"10\.99\.|deckop|initech", SCRIPT.read_text(), re.I)


# -- uninstall ---------------------------------------------------------------


class Layout:
    """Fake filesystem for uninstall: every path the command may delete."""

    def __init__(self, box):
        t = box.tmp
        self.units = t / "systemd"
        self.units.mkdir()
        for n in ("agentdeck.service", "deckdoctor.service", "deckdoctor.timer"):
            (self.units / n).write_text("x")
        self.logrotate = t / "agent-deck.logrotate"
        self.logrotate.write_text("x")
        self.caddy = t / "agent-deck.caddy"
        self.caddy.write_text("x")
        self.other_caddy = t / "someone-else.caddy"
        self.other_caddy.write_text("keep me")
        self.bin = t / "deckctl"
        self.bin.write_text("x")
        box.state.mkdir(exist_ok=True)
        (box.state / "devices.json").write_text("{}")
        box.home.mkdir(exist_ok=True)


def uninstall(box, *argv, typed="", **over):
    lay = Layout(box)
    over.setdefault("prompt", lambda msg: typed)
    mod, ctl = box.ctl(system_dir=lay.units, logrotate_path=lay.logrotate,
                       caddy_site=lay.caddy, bin_path=lay.bin, **over)
    return mod.main(["uninstall", *argv], ctl), lay


def test_uninstall_removes_ours_and_keeps_the_data(box, capsys):
    code, lay = uninstall(box, "--yes")
    assert code == 0
    assert not any(p.exists() for p in (lay.logrotate, lay.caddy, lay.bin))
    assert not any(lay.units.iterdir())
    assert lay.other_caddy.exists()                       # someone else's site
    assert (box.state / "devices.json").exists() and box.home.exists()
    assert box.etc.exists() and box.toml.exists()
    cmds = [" ".join(a) for a in box.ran]
    assert "systemctl disable --now agentdeck" in cmds
    assert any(c.startswith("ufw delete allow 443") for c in cmds)
    assert not any(c.startswith(("docker", "apt", "userdel")) for c in cmds)


def test_uninstall_asks_first_unless_told_yes(box, capsys):
    code, lay = uninstall(box, typed="n")
    assert code == 1 and lay.bin.exists() and box.ran == []


def test_purge_needs_the_hostname_typed_exactly(box, capsys):
    code, lay = uninstall(box, "--purge", "--yes", typed="wrong.example")
    assert code == 1 and box.state.exists() and lay.bin.exists()
    assert "hostname" in "".join(out(capsys))


def test_purge_then_removes_data_desks_images_config_and_the_user(box, capsys):
    code, lay = uninstall(box, "--purge", typed="203-0-113-7.sslip.io")
    assert code == 0
    assert not box.state.exists() and not box.home.exists() and not box.etc.exists()
    cmds = [" ".join(a) for a in box.ran]
    assert any(c.startswith("docker rm -f") or "ancestor=agent-deck/desk-computer" in c
               for c in cmds)
    assert "docker rmi agent-deck/desk-computer" in cmds
    assert "userdel agentdeck" in cmds
    assert not any(c.startswith(("apt", "docker system", "docker stop $")) for c in cmds)
    assert lay.other_caddy.exists()


def test_uninstall_refuses_on_a_box_deployed_from_source(box, capsys):
    box.toml.write_text(box.toml.read_text() + "\n[x]\n")  # noise must not matter
    box.toml.write_text(box.toml.read_text().replace("[deck]\n", '[deck]\ninstall_source = "rsync"\n', 1)
                        if "[deck]" in box.toml.read_text() else
                        '[deck]\ninstall_source = "rsync"\n' + box.toml.read_text())
    code, lay = uninstall(box, "--yes")
    assert code == 1 and lay.bin.exists() and box.ran == []
    assert "deployed from source" in "".join(out(capsys))


# -- a second Claude account (docs/plans/2026-10-01-two-accounts.md, S7) -----
#
# `deckctl login --account work` makes `<deck home>/.claude-accounts/work`
# (0700), runs the CLI's own `claude auth login` with CLAUDE_CONFIG_DIR set to
# it, proves it with `claude auth status`, and registers it. Still no token in
# deckctl's hands. MEASURED (probes P2/P3): sharing `projects/` by symlink is
# what lets a desk resume across accounts, and the link survives the session.


def _work(box):
    return box.home / ".claude-accounts" / "work"


def test_login_account_runs_the_cli_in_that_accounts_config_dir(box, capsys):
    box.outputs["auth status"] = LOGGED_IN
    assert box.main("login", "--account", "work", "--label", "Work") == 0
    claude = f"{box.home}/.local/bin/claude"
    env = f"CLAUDE_CONFIG_DIR={_work(box)}"
    assert box.tty == [["sudo", "-u", "agentdeck", "-H", "env", env, claude, "auth", "login"]]
    assert ["sudo", "-u", "agentdeck", "-H", "env", env, claude, "auth", "status",
            "--json"] in box.ran


def test_login_account_makes_a_private_dir_sharing_transcripts_and_the_bus(box, capsys):
    box.outputs["auth status"] = LOGGED_IN
    assert box.main("login", "--account", "work") == 0
    work = _work(box)
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    for shared in ("projects", "skills", "agent-bus"):
        assert (work / shared).is_symlink()
        assert (work / shared).resolve() == (box.home / ".claude" / shared).resolve()
    seeded = json.loads((work / ".claude.json").read_text())
    assert seeded["hasCompletedOnboarding"] is True
    assert seeded["bypassPermissionsModeAccepted"] is True
    assert stat.S_IMODE((work / ".claude.json").stat().st_mode) == 0o600
    assert any(str(work) in paths and user == "agentdeck" for paths, user in box.chowned)


def test_login_account_registers_it_without_a_credential(box, capsys):
    box.outputs["auth status"] = LOGGED_IN
    assert box.main("login", "--account", "work", "--label", "Work") == 0
    reg = box.home / ".claude" / "agent-bus" / "accounts.json"
    rows = json.loads(reg.read_text())
    assert rows == [{"id": "work", "label": "Work", "kind": "subscription",
                     "config_dir": str(_work(box)), "added_at": NOW}]
    assert stat.S_IMODE(reg.stat().st_mode) == 0o600
    assert "sk-ant" not in reg.read_text()


def test_console_login_registers_an_api_account(box, capsys):
    box.outputs["auth status"] = LOGGED_IN
    assert box.main("login", "--account", "api", "--console") == 0
    assert box.tty[-1][-3:] == ["auth", "login", "--console"]
    rows = json.loads((box.home / ".claude" / "agent-bus" / "accounts.json").read_text())
    assert rows[0]["kind"] == "api"


def test_a_failed_account_login_registers_nothing(box, capsys):
    box.outputs["auth status"] = LOGGED_OUT
    assert box.main("login", "--account", "work") == 1
    assert not (box.home / ".claude" / "agent-bus" / "accounts.json").exists()


@pytest.mark.parametrize("bad", ["main", "../x", "Work", ""])
def test_a_bad_account_id_is_refused(box, capsys, bad):
    assert box.main("login", "--account", bad) == 2
    assert box.tty == []


def test_relogin_keeps_one_row(box, capsys):
    box.outputs["auth status"] = LOGGED_IN
    box.main("login", "--account", "work", "--label", "Old")
    box.main("login", "--account", "work", "--label", "New")
    rows = json.loads((box.home / ".claude" / "agent-bus" / "accounts.json").read_text())
    assert [r["label"] for r in rows] == ["New"]


def test_accounts_lists_main_and_the_registered_ones_with_their_login(box, capsys):
    box.outputs["auth status"] = LOGGED_IN
    box.main("login", "--account", "work", "--label", "Work")
    capsys.readouterr()
    assert box.main("accounts", "--json") == 0
    listed = json.loads(out(capsys)[0])
    assert [(a["id"], a["signed_in"]) for a in listed] == [("main", True), ("work", True)]
    assert listed[1]["config_dir"] == str(_work(box))
