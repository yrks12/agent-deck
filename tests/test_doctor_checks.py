"""K7: the server doctor. Every check is (id, ok, says, fix): one plain sentence
and, when it is not plain PASS, one plain fix. Hermetic: a fake host answers every
question the doctor can ask, so nothing here touches a network, docker or systemd.
"""
import importlib
import re

import pytest

from server import deckconfig

NOW = 1_800_000_000.0
HOUR = 3600.0


def mod():
    return importlib.import_module("server.doctor_checks")


class FakeHost:
    """A healthy public-mode box; each test breaks exactly one thing."""

    def __init__(self):
        self.units = {"agentdeck": True, "caddy": True}
        self.answers = {"http://127.0.0.1:7789/healthz": 200,
                        "https://203-0-113-7.sslip.io/healthz": 200}
        self.exposed = []                      # non-loopback listeners on the deck port
        self.cert = 60.0
        self.dns = ["203.0.113.7"]
        self.ip = "203.0.113.7"
        self.ufw = {22, 443}
        self.cmds = {"claude", "docker", "qrencode"}
        self.login = (0, "ok")
        self.login_calls = 0
        self.docker = True
        self.images = {"agent-deck/desk-computer"}
        self.disk = 40
        self.mem = 8.0
        self.synced = True
        self.key = None
        self.key_ok = True
        self.latest = None
        self.ssh_pw = False
        self.now_ = NOW
        self.ver = "0.9.0"
        self.config_blocked = False

    def config_folder_blocked(self): return self.config_blocked
    def now(self): return self.now_
    def today(self): return "2026-09-30"
    def version(self): return self.ver
    def active(self, unit): return self.units.get(unit, False)
    def get(self, url, timeout=5): return self.answers.get(url)
    def listeners(self, port): return list(self.exposed)
    def cert_days(self, host): return self.cert
    def resolve(self, host): return list(self.dns)
    def public_ip(self): return self.ip
    def ufw_ports(self): return None if self.ufw is None else set(self.ufw)
    def have(self, cmd): return cmd in self.cmds
    def claude_login(self):
        self.login_calls += 1
        return self.login
    def docker_ok(self): return self.docker
    def docker_image(self, name): return name in self.images
    def disk_pct(self): return self.disk
    def mem_gb(self): return self.mem
    def clock_synced(self): return self.synced
    def openai_key(self): return self.key
    def openai_ok(self, key): return self.key_ok
    def latest_version(self): return self.latest
    def ssh_password(self): return self.ssh_pw


def cfg(tmp_path, body='[network]\nhostname = "203-0-113-7.sslip.io"\n'):
    path = tmp_path / "deck.toml"
    path.write_text(body)
    return deckconfig.load(path, env={})


def run(tmp_path, host=None, body=None, state=None):
    host = host or FakeHost()
    conf = cfg(tmp_path) if body is None else cfg(tmp_path, body)
    return {c.id: c for c in mod().run_checks(conf, host, {} if state is None else state)}, host


def test_a_healthy_public_box_is_all_pass(tmp_path):
    checks, _ = run(tmp_path)
    bad = {i: c for i, c in checks.items() if not c.ok or c.fix}
    assert not bad, bad
    assert list(checks)[:2] == ["service", "local"]


def test_the_checks_run_in_the_documented_order(tmp_path):
    host = FakeHost()
    host.key = "sk-x"
    host.latest = "0.9.0"
    ids = list(run(tmp_path, host)[0])
    order = ["service", "local", "config_folder", "exposure", "caddy", "cert", "dns", "hairpin", "firewall",
             "claude_cli", "claude_login", "token_age", "docker", "disk", "ram", "clock", "openai",
             "update", "ssh_password"]
    assert [i for i in order if i in ids] == ids


def _break(host, what):
    {
        "service": lambda: host.units.update(agentdeck=False),
        "local": lambda: host.answers.pop("http://127.0.0.1:7789/healthz"),
        "exposure": lambda: host.exposed.append("0.0.0.0"),
        "caddy": lambda: host.units.update(caddy=False),
        "cert": lambda: setattr(host, "cert", None),
        "dns": lambda: setattr(host, "dns", ["198.51.100.9"]),
        "hairpin": lambda: host.answers.pop("https://203-0-113-7.sslip.io/healthz"),
        "firewall": lambda: setattr(host, "ufw", {22, 443, 7789}),
        "claude_cli": lambda: host.cmds.discard("claude"),
        "claude_login": lambda: setattr(host, "login", (1, "Invalid API key")),
        "docker": lambda: setattr(host, "docker", False),
        "disk": lambda: setattr(host, "disk", 92),
        "ram": lambda: setattr(host, "mem", 2.0),
        "clock": lambda: setattr(host, "synced", False),
    }[what]()


FAILS = ["service", "local", "exposure", "caddy", "cert", "dns", "hairpin", "firewall",
         "claude_cli", "claude_login", "docker", "disk", "ram", "clock"]


@pytest.mark.parametrize("which", FAILS)
def test_each_check_fails_for_its_own_reason_with_one_sentence_and_a_fix(tmp_path, which):
    host = FakeHost()
    _break(host, which)
    checks, _ = run(tmp_path, host)
    c = checks[which]
    assert c.ok is False
    for text in (c.says, c.fix):
        assert text.strip() and "\n" not in text
        assert "Traceback" not in text and "Exception" not in text
    assert c.says.rstrip().endswith((".", "?"))
    assert c.fix.startswith("Fix:") or len(c.fix) > 5
    others = {i for i, x in checks.items() if not x.ok} - {which}
    # a dead service legitimately also means nothing answers locally or over https
    assert others <= {"local", "hairpin"}, others


def test_the_login_fix_names_deckctl_login(tmp_path):
    host = FakeHost()
    _break(host, "claude_login")
    c = run(tmp_path, host)[0]["claude_login"]
    assert "deckctl login" in c.fix
    assert "expired" in c.says.lower() or "no agent" in c.says.lower()


def test_the_login_probe_is_cached_six_hours(tmp_path):
    host, state = FakeHost(), {}
    run(tmp_path, host, state=state)
    run(tmp_path, host, state=state)
    assert host.login_calls == 1
    host.now_ += 6 * HOUR + 1
    run(tmp_path, host, state=state)
    assert host.login_calls == 2


def test_a_failed_login_probe_is_never_cached_as_good(tmp_path):
    host, state = FakeHost(), {}
    host.login = (1, "nope")
    run(tmp_path, host, state=state)
    run(tmp_path, host, state=state)
    assert host.login_calls == 2


def test_token_age_warns_at_330_days_and_is_silent_when_unknown(tmp_path):
    body = '[network]\nhostname = "203-0-113-7.sslip.io"\n[claude]\ntoken_issued = "%s"\n'
    checks, _ = run(tmp_path, body=body % "2025-10-30")      # 335 days before 2026-09-30
    age = checks["token_age"]
    assert age.ok is True and age.fix and "deckctl login" in age.fix
    fresh, _ = run(tmp_path, body=body % "2026-09-01")
    assert fresh["token_age"].ok and not fresh["token_age"].fix
    none, _ = run(tmp_path)
    assert "token_age" not in none


def test_certificate_under_14_days_is_a_warning_not_a_failure(tmp_path):
    host = FakeHost()
    host.cert = 9.0
    c = run(tmp_path, host)[0]["cert"]
    assert c.ok is True and c.fix and "9" in c.says


def test_an_expired_certificate_fails(tmp_path):
    host = FakeHost()
    host.cert = -1.0
    assert run(tmp_path, host)[0]["cert"].ok is False


def test_dns_naming_another_machine_says_which_address_it_should_be(tmp_path):
    host = FakeHost()
    _break(host, "dns")
    c = run(tmp_path, host)[0]["dns"]
    assert "203.0.113.7" in c.says or "203.0.113.7" in c.fix


def test_wireguard_mode_skips_the_public_checks_and_allows_a_vpn_listener(tmp_path):
    host = FakeHost()
    host.exposed = ["10.0.0.1"]
    body = '[network]\nmode = "wireguard"\ntls = "none"\n[deck]\nbind = "10.0.0.1"\n'
    checks, _ = run(tmp_path, host, body=body)
    for skipped in ("exposure", "caddy", "cert", "dns", "hairpin", "firewall"):
        assert skipped not in checks
    assert checks["service"].ok


def test_openai_is_only_checked_when_a_key_is_set(tmp_path):
    assert "openai" not in run(tmp_path)[0]
    host = FakeHost()
    host.key = "sk-test"
    assert run(tmp_path, host)[0]["openai"].ok
    host.key_ok = False
    bad = run(tmp_path, host)[0]["openai"]
    assert bad.ok is False and "set-openai-key" in bad.fix


def test_update_available_is_info_not_a_failure(tmp_path):
    host = FakeHost()
    host.latest = "0.9.1"
    c = run(tmp_path, host)[0]["update"]
    assert c.ok is True and "0.9.1" in c.says and "deckctl update" in c.fix


def test_update_is_silent_when_current_or_no_channel(tmp_path):
    host = FakeHost()
    host.latest = "0.9.0"
    assert run(tmp_path, host)[0]["update"].fix == ""
    host.latest = None
    assert "update" not in run(tmp_path, host)[0]


def test_ssh_password_login_is_a_warning_only(tmp_path):
    host = FakeHost()
    host.ssh_pw = True
    c = run(tmp_path, host)[0]["ssh_password"]
    assert c.ok is True and c.fix


def test_a_host_that_raises_becomes_a_plain_failure_not_a_crash(tmp_path):
    host = FakeHost()

    def boom(*a, **k):
        raise RuntimeError("secret internals at /x/y.py line 9")

    host.cert_days = boom
    c = run(tmp_path, host)[0]["cert"]
    assert c.ok is False
    assert "internals" not in c.says + c.fix and "line 9" not in c.says + c.fix


def test_core_checks_are_just_the_service_and_the_local_answer(tmp_path):
    host = FakeHost()
    host.disk = 99                      # irrelevant to "did the update break it"
    core = mod().core_checks(cfg(tmp_path), host)
    assert [c.id for c in core] == ["service", "local"]
    assert all(c.ok for c in core)


def test_render_is_pass_fail_warn_lines_with_the_fix_indented():
    Check = mod().Check
    text = mod().render([Check("a", True, "Agent Deck is running.", ""),
                         Check("b", False, "Your Claude login has expired.", "Fix: run x"),
                         Check("c", True, "Certificate has 9 days left.", "Fix: renew")])
    lines = text.splitlines()
    assert lines[0].startswith("PASS  ")
    assert lines[1].startswith("FAIL  ") and lines[2] == "      Fix: run x"
    assert lines[3].startswith("WARN  ")


def test_versions_compare_numerically_not_as_text():
    assert mod().newer("0.10.0", "0.9.0") and not mod().newer("0.9.0", "0.9.0")
    assert not mod().newer("garbage", "0.9.0")


def test_module_carries_no_owner_value():
    src = importlib.import_module("server.doctor_checks").__file__
    assert not re.search(r"10\.99\.|deckop|initech", open(src).read(), re.I)


def test_config_folder_the_deck_user_cannot_open_is_a_plain_sentence(tmp_path):
    host = FakeHost()
    host.config_blocked = True
    c = run(tmp_path, host)[0]["config_folder"]
    assert not c.ok
    assert c.says == "The config folder is not readable by the deck user."
    assert "chmod 755 /etc/agent-deck" in c.fix


def test_config_folder_ok_is_silent_pass(tmp_path):
    c = run(tmp_path, FakeHost())[0]["config_folder"]
    assert c.ok and not c.fix


# -- the service user's claude is not on root's (or sudo's) PATH --------------
# MEASURED on a fresh Ubuntu 24.04 box (2026-09-30): the installer puts claude at
# <home>/.local/bin/claude; sudo's secure_path does not include it, so
# `shutil.which("claude")` as root and `sudo -u agentdeck sh -c 'claude ...'`
# both said "not found" -- the doctor reported "not installed" and "expired"
# on a box whose claude was installed and working.


def _cfg(tmp_path, home):
    p = tmp_path / "deck.toml"
    p.write_text(f'[deck]\nhome = "{home}"\n\n[claude]\noauth_env = "{home}/.claude/oauth.env"\n')
    return deckconfig.load(p, env={})


def test_the_doctor_finds_claude_in_the_service_users_home_not_on_roots_path(tmp_path, monkeypatch):
    home = tmp_path / "home" / "agentdeck"
    (home / ".local" / "bin").mkdir(parents=True)
    exe = home / ".local" / "bin" / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    host = mod().Host(_cfg(tmp_path, home))
    assert host.have("claude")


def test_the_login_probe_calls_claude_by_its_installed_path(tmp_path):
    home = tmp_path / "home" / "agentdeck"
    seen = []
    host = mod().Host(_cfg(tmp_path, home), run=lambda argv, timeout=15: (seen.append(argv) or (0, "ok")))
    host.claude_login()
    assert seen and f"{home}/.local/bin/claude" in " ".join(seen[0])
