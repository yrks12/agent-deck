"""H1 -- the box tells the chief, and Sam, BEFORE it dies. Hermetic.

Why this exists. The box's disk fills (an unrelated writer grew 63 GB in three
days), the OAuth login expires, the watchdog spams 0.8 GB/day into syslog --
and every one of those ends the same way: every desk dies and the board says
nothing. `bin/deckdoctor` is the smoke alarm. These tests pin what it must
say and, as much, what it must NOT do: repeat itself every ten minutes, or
print the deck token.

Nothing here touches a network, a real `claude`, or the real bus: the script's
seams (`disk`, `run`, `fetch`, the message file, the alert sink) are injected.
"""

import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "deckdoctor"
DEPLOY = ROOT / "deploy"
NOW = 1_800_000_000.0
HOUR = 3600.0


def load():
    loader = importlib.machinery.SourceFileLoader("deckdoctor", str(SCRIPT))
    spec = importlib.util.spec_from_loader("deckdoctor", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Box:
    """A fake box: every seam deckdoctor reads, healthy unless a test breaks it."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.disk_pct = 40
        self.logged_in = True
        self.probe_rc = 0
        self.probes = 0
        self.deck_up = True
        self.agents = [{"name": "atlas", "state": "WORKING"},
                       {"name": "sales", "state": "ASLEEP"}]
        self.messages = tmp_path / "messages.jsonl"
        self.messages.write_text("")
        self.roster = tmp_path / "roster.json"
        self.roster.write_text(json.dumps({"version": 1, "agents": [
            {"name": "atlas", "cwd": "/x", "engine": "claude", "mission": "m",
             "reports_to": None},
            {"name": "sales", "cwd": "/x", "engine": "claude", "mission": "m",
             "reports_to": "atlas"}]}))
        self.sent: list[dict] = []
        self.wa: list[str] = []
        self.now = NOW

    def cfg(self, **over):
        cfg = {
            "disk_red_pct": 85.0,
            "disk_path": "/",
            "deck_url": "http://10.99.0.1:7789",
            "token": "TOKEN-MUST-NEVER-APPEAR",
            "messages": self.messages,
            "roster": self.roster,
            "state": self.tmp / "state.json",
            "alert": "send",
            "auth_probe": True,
            "claude": "claude",
            "stuck_seconds": 300.0,
            "stuck_lookback": 24 * HOUR,
            "dedupe_seconds": 6 * HOUR,
            "disk": lambda path: self.disk_pct,
            "run": self._run,
            "fetch": self._fetch,
            "post": lambda to, text: self.sent.append({"to": to, "text": text,
                                                        "from": "deck"}),
            "whatsapp": lambda text: (self.wa.append(text) or "sent"),
            "now": lambda: self.now,
        }
        cfg.update(over)
        return cfg

    def _run(self, argv, timeout):
        if argv[1:3] == ["auth", "status"]:
            return 0, json.dumps({"loggedIn": self.logged_in})
        self.probes += 1
        return self.probe_rc, "ok" if self.probe_rc == 0 else "Invalid API key"

    def _fetch(self, url, token):
        self.fetched = getattr(self, "fetched", []) + [(url, token)]
        if not self.deck_up:
            raise OSError("connection refused")
        return {"agents": self.agents} if url.endswith("/v1/agents") else {}

    def say(self, **rec):
        rec.setdefault("ts", self.now - 3600)
        rec.setdefault("id", f"m{len(self.messages.read_text().splitlines())}")
        with self.messages.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def run(box, **over):
    mod = load()
    code, report = mod.run(box.cfg(**over))
    return code, report


# -- the healthy box ---------------------------------------------------------


def test_a_healthy_box_is_green_and_silent(box):
    code, report = run(box)
    assert code == 0
    assert report["ok"] is True
    assert set(report["checks"]) == {
        "auth", "disk_pct", "daemon", "stuck_messages", "desks_asleep"}
    assert box.sent == [] and box.wa == []


def test_the_report_is_one_json_line_and_never_holds_the_token(box, capsys):
    mod = load()
    code = mod.main([], env={}, cfg=box.cfg(disk=lambda p: 99))
    out = capsys.readouterr().out
    assert code == 1
    assert len(out.strip().splitlines()) == 1
    assert json.loads(out)["ok"] is False
    assert "TOKEN-MUST-NEVER-APPEAR" not in out
    assert all("TOKEN-MUST-NEVER-APPEAR" not in m["text"] for m in box.sent)


# -- each check goes red for its own reason ---------------------------------


def test_disk_at_the_threshold_is_red_and_below_it_is_green(box):
    box.disk_pct = 84
    assert run(box)[0] == 0
    box.disk_pct = 85
    code, report = run(box)
    assert code == 1
    assert report["checks"]["disk_pct"]["ok"] is False
    assert report["checks"]["disk_pct"]["value"] == 85


def test_disk_red_pct_one_turns_any_box_red(box):
    """A-7's dry run: DISK_RED_PCT=1 must make a healthy 40% disk alarm."""
    mod = load()
    cfg = mod.config({"DISK_RED_PCT": "1"})
    assert cfg["disk_red_pct"] == 1.0
    assert run(box, disk_red_pct=1.0)[0] == 1


def test_a_logged_out_cli_is_red(box):
    box.logged_in = False
    code, report = run(box)
    assert code == 1 and report["checks"]["auth"]["ok"] is False


def test_the_login_probe_catches_a_token_that_looks_present_but_is_dead(box):
    """`claude auth status` reads the file; only a real call proves the login."""
    box.probe_rc = 1
    code, report = run(box)
    assert code == 1
    assert report["checks"]["auth"]["ok"] is False
    assert "Invalid API key" in report["checks"]["auth"]["detail"]


def test_the_login_probe_runs_once_per_six_hours_not_every_tick(box):
    run(box)
    run(box)
    assert box.probes == 1
    box.now += 6 * HOUR + 1
    run(box)
    assert box.probes == 2


def test_the_probe_can_be_switched_off_for_a_dry_run(box):
    run(box, auth_probe=False)
    assert box.probes == 0


def test_an_unreachable_deck_is_red(box):
    box.deck_up = False
    code, report = run(box)
    assert code == 1 and report["checks"]["daemon"]["ok"] is False


def test_the_daemon_probe_authenticates_because_the_box_answers_401_anonymously(box):
    """Measured on the box: an anonymous GET /api/state is an HTTPError, so an
    unauthenticated probe reports a healthy deck as down, forever."""
    run(box)
    sent = dict(box.fetched)
    assert sent["http://10.99.0.1:7789/api/state"] == "TOKEN-MUST-NEVER-APPEAR"


def test_an_owner_message_unacked_past_five_minutes_is_stuck(box):
    box.say(to="atlas", **{"from": "owner"}, text="hello", ts=NOW - 600)
    code, report = run(box)
    assert code == 1
    assert report["checks"]["stuck_messages"]["count"] == 1


def test_a_routine_and_a_deck_message_count_as_system_messages(box):
    box.say(to="atlas", **{"from": "routine"}, text="a", ts=NOW - 600)
    box.say(to="sales", **{"from": "deck"}, text="b", ts=NOW - 600)
    assert run(box)[1]["checks"]["stuck_messages"]["count"] == 2


def test_fresh_acked_desk_to_desk_and_owner_inbox_messages_are_not_stuck(box):
    box.say(to="atlas", **{"from": "owner"}, text="fresh", ts=NOW - 60)
    box.say(to="atlas", **{"from": "owner"}, text="taken", ts=NOW - 900, id="t1")
    box.say(ack="t1", ts=NOW - 800)
    box.say(to="atlas", **{"from": "sales"}, text="peer", ts=NOW - 900)
    box.say(to="owner", **{"from": "atlas"}, text="reply", ts=NOW - 900)
    assert run(box)[0] == 0


def test_a_message_older_than_the_lookback_is_history_not_an_alarm(box):
    box.say(to="atlas", **{"from": "owner"}, text="ancient", ts=NOW - 30 * HOUR)
    assert run(box)[0] == 0


def test_the_doctors_own_alert_never_counts_as_stuck(box):
    """Otherwise an alert to a dead chief keeps the box red forever."""
    box.say(to="atlas", **{"from": "deck"}, text="alert", ts=NOW - 900,
            doctor=True)
    assert run(box)[0] == 0


def test_sleeping_desks_are_counted_but_do_not_turn_the_box_red(box):
    code, report = run(box)
    assert code == 0
    assert report["checks"]["desks_asleep"]["count"] == 1


# -- the alert: once, to the chief, de-duplicated, honest --------------------


def test_red_posts_exactly_one_line_to_the_chief_as_the_deck(box):
    box.disk_pct = 91
    run(box)
    assert len(box.sent) == 1
    msg = box.sent[0]
    assert msg["to"] == "atlas" and msg["from"] == "deck"
    assert "91" in msg["text"] and "/opt/studio" in msg["text"]
    assert "\n" not in msg["text"].strip()
    assert len(box.wa) == 1


def test_a_second_run_inside_six_hours_says_nothing_more(box):
    box.disk_pct = 91
    run(box)
    box.now += 10 * 60
    run(box)
    box.now += 5 * HOUR
    run(box)
    assert len(box.sent) == 1 and len(box.wa) == 1


def test_after_six_hours_a_still_red_check_speaks_again(box):
    box.disk_pct = 91
    run(box)
    box.now += 6 * HOUR + 1
    run(box)
    assert len(box.sent) == 2


def test_a_check_that_recovers_and_fails_again_alerts_again(box):
    box.disk_pct = 91
    run(box)
    box.disk_pct = 40
    box.now += 600
    run(box)
    box.disk_pct = 91
    box.now += 600
    run(box)
    assert len(box.sent) == 2


def test_a_different_check_going_red_is_not_swallowed_by_the_first(box):
    box.disk_pct = 91
    run(box)
    box.deck_up = False
    box.now += 600
    run(box)
    assert len(box.sent) == 2
    assert "disk" not in box.sent[1]["text"].lower()


def test_alert_off_sends_nothing_and_still_reports_red(box):
    box.disk_pct = 91
    code, report = run(box, alert="off")
    assert code == 1 and box.sent == [] and box.wa == []
    assert report["alert"]["cos"] == "off"


def test_a_dead_whatsapp_bridge_does_not_lose_the_thread_line(box):
    box.disk_pct = 91

    def broken(text):
        raise RuntimeError("bridge down")

    code, report = run(box, whatsapp=broken)
    assert code == 1 and len(box.sent) == 1
    assert report["alert"]["whatsapp"].startswith("failed")


def test_a_stray_root_desk_is_not_mistaken_for_the_chief(box):
    """Measured: the roster holds several desks with no boss (a stale hire, an
    engineer's probe). The chief is the root with the most reports."""
    box.roster.write_text(json.dumps({"version": 1, "agents": [
        {"name": "stray", "cwd": "/x", "engine": "claude", "mission": "m"},
        {"name": "atlas", "cwd": "/x", "engine": "claude", "mission": "m"},
        {"name": "a", "cwd": "/x", "engine": "claude", "mission": "m",
         "reports_to": "atlas"},
        {"name": "b", "cwd": "/x", "engine": "claude", "mission": "m",
         "reports_to": "atlas"}]}))
    box.disk_pct = 91
    run(box)
    assert box.sent[0]["to"] == "atlas"


def test_no_chief_on_the_roster_is_reported_not_crashed(box):
    box.roster.write_text(json.dumps({"version": 1, "agents": []}))
    box.disk_pct = 91
    code, report = run(box)
    assert code == 1 and box.sent == []
    assert report["alert"]["cos"] == "no_chief"


def test_a_file_alert_target_redirects_the_alert_for_a_dry_run(box, tmp_path):
    """DECKDOCTOR_ALERT=file:<path> is how the box is dry-run with nothing posted."""
    mod = load()
    cfg = mod.config({"DECKDOCTOR_ALERT": f"file:{tmp_path}/out.jsonl"})
    assert cfg["alert"] == f"file:{tmp_path}/out.jsonl"
    box.disk_pct = 91
    code, report = mod.run(box.cfg(alert=cfg["alert"]))
    lines = (tmp_path / "out.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["to"] == "atlas"
    assert box.sent == [] and box.wa == []
    assert report["alert"]["cos"] == "file"


# -- the units and configs that ship with it --------------------------------


def test_the_script_is_executable_stdlib_python():
    assert SCRIPT.stat().st_mode & 0o111
    assert SCRIPT.read_text().startswith("#!/usr/bin/env python3")


def _owner_rendered(name):
    """The file as the owner's box receives it: rendered from deck.toml by
    deploy/render.py (the static deploy/ copies are gone -- easy-setup K1)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("deck_render", DEPLOY / "render.py")
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)
    cfg = render.load_config(ROOT / "tests" / "fixtures" / "owner-box" / "deck.toml")
    return render.render_all(cfg)[name]


def _shipped(name):
    path = DEPLOY / name
    return path.read_text() if path.is_file() else _owner_rendered(name)


def test_the_service_runs_the_script_as_the_deck_user_not_root():
    unit = _owner_rendered("deckdoctor.service")
    assert re.search(r"^User=deckop$", unit, re.M)
    assert "ExecStart=/opt/agent-deck/.venv/bin/python /opt/agent-deck/bin/deckdoctor" in unit
    assert re.search(r"^Type=oneshot$", unit, re.M)
    assert "oauth.env" in unit
    # MEASURED 2026-09-30: without the deck's env the doctor never saw
    # DECK_WA_URL / DECK_WA_TOKEN and every red alert to his phone read
    # "skipped". systemd reads the 0600 file as root before dropping to deckop.
    assert re.search(r"^EnvironmentFile=-/etc/agent-deck/agentdeck.env$", unit, re.M)


def test_no_unit_or_config_carries_a_credential():
    for name in ("deckdoctor.service", "deckdoctor.timer",
                 "journald-cap.conf", "agent-deck-log.logrotate"):
        text = _shipped(name)
        assert not re.search(r"(TOKEN|SECRET|KEY)=\S", text), name


def test_the_timer_fires_every_ten_minutes_and_survives_reboot():
    timer = (DEPLOY / "deckdoctor.timer").read_text()
    assert re.search(r"^OnUnitActiveSec=10min$", timer, re.M)
    assert re.search(r"^OnBootSec=\S+$", timer, re.M)
    assert re.search(r"^WantedBy=timers.target$", timer, re.M)
    assert re.search(r"^Unit=deckdoctor.service$", timer, re.M)


def test_journald_is_capped_at_one_gigabyte():
    conf = (DEPLOY / "journald-cap.conf").read_text()
    assert "[Journal]" in conf
    assert re.search(r"^SystemMaxUse=1G$", conf, re.M)


def test_the_deck_log_is_rotated_with_a_size_bound():
    conf = _owner_rendered("agent-deck-log.logrotate")
    assert "/opt/agent-deck/.agent-deck.log" in conf
    assert "copytruncate" in conf
    assert re.search(r"^\s*(maxsize|size) \d+[MG]$", conf, re.M)
    assert re.search(r"^\s*rotate \d+$", conf, re.M)


def test_the_installer_installs_the_three_units_idempotently():
    text = (DEPLOY / "install-deck.sh").read_text()
    for needle in ("deckdoctor.service", "deckdoctor.timer",
                   "journald-cap.conf", "agent-deck-log.logrotate"):
        assert needle in text, needle
    assert "systemctl enable --now deckdoctor.timer" in text
    assert re.search(r"cmp -s|install -m", text)


def test_the_installer_still_parses_as_bash():
    r = subprocess.run(["bash", "-n", str(DEPLOY / "install-deck.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_box_doc_has_a_health_section_naming_the_runbook():
    doc = (ROOT / "docs" / "the-box.md").read_text()
    assert "## Health" in doc
    for needle in ("deckdoctor", "DISK_RED_PCT", "/opt/studio", "Rollback"):
        assert needle in doc, needle


# -- D3: no owner values baked in; the owner's box behaves exactly as before --


def _toml(tmp_path, body):
    path = tmp_path / "deck.toml"
    path.write_text(body)
    return str(path)


def test_the_deck_url_env_still_wins_so_the_owners_box_is_unchanged():
    """deckdoctor.service sets DECK_URL; that must be what the doctor probes."""
    cfg = load().config({"DECK_URL": "http://10.99.0.1:7789/"})
    assert cfg["deck_url"] == "http://10.99.0.1:7789"


def test_without_the_env_the_deck_url_comes_from_deck_toml(tmp_path):
    path = _toml(tmp_path, '[deck]\nbind = "127.0.0.1"\nport = 7790\n')
    cfg = load().config({"DECK_CONFIG": path})
    assert cfg["deck_url"] == "http://127.0.0.1:7790"


def test_a_wildcard_bind_is_probed_on_loopback(tmp_path):
    path = _toml(tmp_path, '[network]\nmode = "local"\n[deck]\nbind = "0.0.0.0"\n')
    assert load().config({"DECK_CONFIG": path})["deck_url"] == "http://127.0.0.1:7789"


def test_no_env_and_no_toml_probes_the_local_default_never_an_owner_address(tmp_path):
    cfg = load().config({"DECK_CONFIG": str(tmp_path / "missing.toml")})
    assert cfg["deck_url"] == "http://127.0.0.1:7789"


def test_a_broken_deck_toml_does_not_stop_the_alarm(tmp_path):
    path = _toml(tmp_path, "this is not toml [[[")
    assert load().config({"DECK_CONFIG": path})["deck_url"] == "http://127.0.0.1:7789"


def test_the_runbook_names_the_admin_command_not_a_person_or_a_user():
    mod = load()
    text = " ".join(mod.RUNBOOK.values())
    assert "deckctl login" in mod.RUNBOOK["auth"]
    assert "deckop" not in text.lower()


def test_the_auth_alert_tells_the_reader_to_run_deckctl_login(box):
    box.logged_in = False
    run(box)
    assert len(box.sent) == 1
    assert "deckctl login" in box.sent[0]["text"]


def test_the_report_contract_is_pinned_key_for_key(box):
    """The live 10-minute timer parses this. Do not rename, add or drop keys."""
    box.disk_pct = 91
    code, report = run(box)
    assert code == 1
    assert list(report) == ["ok", "checks", "alert"]
    assert set(report["alert"]) == {"cos", "whatsapp"}
    assert report["checks"]["disk_pct"] == {"ok": False, "value": 91, "limit": 85.0}
    assert report["checks"]["daemon"] == {"ok": True}
    assert report["checks"]["stuck_messages"] == {"ok": True, "count": 0}
    assert report["checks"]["desks_asleep"] == {"ok": True, "count": 1}
    assert report["checks"]["auth"]["ok"] is True


def test_the_alert_line_keeps_its_shape(box):
    box.disk_pct = 91
    run(box)
    text = box.sent[0]["text"]
    assert text.startswith("[Agent Deck] deckdoctor RED -- disk 91% used (red at 85%). ")
    assert "/opt/studio" in text


def _script(name, env, *args):
    base = {k: v for k, v in __import__("os").environ.items()
            if k not in ("DECK_URL", "DECK_TOKEN", "DECK_TOKEN_FILE", "YOS_SCREEN_IS_FREE")}
    base.update(env)
    return subprocess.run([str(ROOT / name), *args], capture_output=True, text=True,
                          env=base, timeout=30)


def test_overhaul_acceptance_requires_deck_url_and_names_the_variable():
    done = _script("bin/overhaul-acceptance.sh", {"DECK_TOKEN": "x"})
    assert done.returncode == 2
    assert "DECK_URL" in done.stderr
    assert "10.88" not in done.stderr


def test_overhaul_acceptance_still_requires_a_token():
    done = _script("bin/overhaul-acceptance.sh", {"DECK_URL": "http://127.0.0.1:7789/v1"})
    assert done.returncode == 2
    assert "DECK_TOKEN" in done.stderr


def test_idle_cpu_requires_deck_url_in_the_environment():
    done = _script("macos/Scripts/idle-cpu.sh", {"YOS_SCREEN_IS_FREE": "1"})
    assert done.returncode == 64
    assert "DECK_URL" in done.stderr


def test_idle_cpu_still_refuses_to_take_the_screen_by_default():
    done = _script("macos/Scripts/idle-cpu.sh", {"DECK_URL": "http://127.0.0.1:7788"})
    assert done.returncode == 78
