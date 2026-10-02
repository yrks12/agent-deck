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
        self.code_tree = (True, "code matches DEPLOYED")
        self.renders = (True, "deck.toml renders")
        self.mem_fraction = 0.40
        self.mem_low_since = None

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
            "code_tree": lambda: self.code_tree,
            "renders": lambda: self.renders,
            "memory": lambda: (self.mem_fraction, self.mem_low_since),
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
        "auth", "disk_pct", "daemon", "stuck_messages", "desks_asleep", "code_tree", "renders",
        "memory", "logins", "oom_kills"}
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
    assert text.startswith("[Shaliach] deckdoctor RED -- disk 91% used (red at 85%). ")
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



# -- the code on the box was changed outside bin/deploy-box -------------------
# MEASURED 2026-09-30: "another session overwrote the box with older code about a
# minute after my deploy." The deck logs it at startup; the alarm says it aloud.


def test_a_hand_rsync_over_a_deploy_turns_the_doctor_red_and_speaks(box):
    box.code_tree = (False, "the code no longer matches DEPLOYED abc (by x): ROLLBACK")
    code, report = run(box)
    assert code == 1
    assert report["checks"]["code_tree"]["ok"] is False
    assert "ROLLBACK" in report["checks"]["code_tree"]["detail"]
    assert box.sent and "deploy-box" in box.sent[0]["text"]


def test_the_real_code_tree_check_reads_deploy_guard(box, tmp_path):
    mod = load()
    cfg = mod.config({"DECK_ROOT": str(tmp_path), "DECKDOCTOR_STATE": str(tmp_path / "s.json")})
    ok, detail = cfg["code_tree"]()
    assert ok is True and "DEPLOYED" in detail


# -- memory: the box that thrashed (2026-10-01) ------------------------------
#
# MEASURED: 13 desk browsers, swap full, kswapd on a core, load 48, a no-op
# `docker exec` taking 5-10 s. Nobody was told until the owner said "everything
# is slow". The daemon's reaper records since when RAM has been low; the
# doctor turns five minutes of it into an alarm.


def test_low_ram_for_five_minutes_is_red(box):
    box.mem_fraction = 0.05
    box.mem_low_since = box.now - 301
    code, report = run(box)
    assert code == 1
    assert report["checks"]["memory"]["ok"] is False
    assert "RAM" in box.sent[0]["text"]


def test_a_short_dip_is_not_an_alarm(box):
    box.mem_fraction = 0.05
    box.mem_low_since = box.now - 60
    assert run(box)[0] == 0


def test_low_ram_with_no_daemon_record_is_timed_by_the_doctor_itself(box):
    """The daemon may be the thing that is down. The doctor remembers the
    first low reading and goes red once it has lasted five minutes."""
    box.mem_fraction = 0.05
    assert run(box)[0] == 0
    box.now += 301
    code, report = run(box)
    assert code == 1 and report["checks"]["memory"]["ok"] is False


def test_recovered_ram_is_green_again(box):
    box.mem_fraction = 0.05
    box.mem_low_since = box.now - 900
    assert run(box)[0] == 1
    box.mem_fraction = 0.30
    box.mem_low_since = None
    assert run(box)[0] == 0


# -- out-of-memory kills ------------------------------------------------------
#
# MEASURED on the box, 2026-10-01: 14 kernel OOM kills in six hours, every one
# a desk browser's chromium hitting its container's 1 GiB cap, and one
# box-wide kill at 04:19 that nobody heard about. The lines are copied from
# `journalctl -k -o short-unix` on the box, timestamps moved to the test clock.


def _kernel(ts):
    k = f"{ts:.6f} localhost kernel: "
    late = f"{ts + 60:.6f} localhost kernel: "
    return (
        k + "ServiceWorker t invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_score_adj=200\n"
        + k + "oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=docker-d403ea3132aa.scope,mems_allowed=0,oom_memcg=/system.slice/docker-d403ea3132aa.scope,task_memcg=/system.slice/docker-d403ea3132aa.scope,task=chromium,pid=3937365,uid=1000\n"
        + k + "Memory cgroup out of memory: Killed process 3937365 (chromium) total-vm:1522514820kB, anon-rss:532496kB, file-rss:28928kB, shmem-rss:1660kB, UID:1000 pgtables:4608kB oom_score_adj:300\n"
        + late + "studio invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE|__GFP_COMP), order=0, oom_score_adj=0\n"
        + late + "oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=user.slice,mems_allowed=0,global_oom,task_memcg=/system.slice/agentdeck.service,task=claude,pid=3026110,uid=1000\n"
        + late + "Out of memory: Killed process 3026110 (claude) total-vm:1518552396kB, anon-rss:263308kB, file-rss:128kB, shmem-rss:4200kB, UID:1000 pgtables:1740kB oom_score_adj:300\n")


def test_the_oom_parser_reads_the_kernel_lines():
    kills = load().parse_oom(_kernel(NOW - 300))
    assert [(k["process"], k["pid"], k["box_wide"], k["cgroup"], k["trigger"])
            for k in kills] == [
        ("chromium", 3937365, False, "docker-d403ea3132aa.scope", "ServiceWorker t"),
        ("claude", 3026110, True, "agentdeck.service", "studio")]
    assert kills[0]["ts"] == pytest.approx(NOW - 300)


def test_a_new_oom_kill_names_the_process_to_the_chief_and_not_whatsapp(box):
    code, report = run(box, kernel_log=lambda since: _kernel(NOW - 300))
    oom = report["checks"]["oom_kills"]
    assert code == 1 and oom["ok"] is False and oom["count"] == 2
    (note,) = box.sent
    assert note["to"] == "atlas" and note["from"] == "deck"
    assert "chromium" in note["text"] and "claude" in note["text"]
    assert "studio" in note["text"], "what triggered the box-wide kill"
    assert box.wa == [], "an OOM kill is a note to the chief, not WhatsApp"


def test_an_oom_kill_is_reported_once(box):
    log = {"kernel_log": lambda since: _kernel(NOW - 300)}
    run(box, **log)
    box.now += 600
    code, report = run(box, **log)
    assert report["checks"]["oom_kills"]["ok"] is True
    assert len(box.sent) == 1 and code == 0


def test_the_first_run_does_not_replay_old_kills(box):
    code, report = run(box, kernel_log=lambda since: _kernel(NOW - 5 * HOUR))
    assert report["checks"]["oom_kills"]["count"] == 0 and box.sent == []



# -- deck.toml must still render the box's units ------------------------------
# MEASURED 2026-10-01: the owner box's deck.toml had no [network] section, so it
# defaulted to tls=sslip with an empty hostname and the next install-deck.sh
# could not render the units. Nothing said so until a builder ran the installer.

WIREGUARD_BOX = """
[deck]
user = "deckop"
home = "/home/deckop"
app_dir = "/opt/agent-deck"
bind = "10.99.0.1"
install_source = "rsync"

[network]
mode = "wireguard"
tls = "none"
"""


def test_a_config_that_no_longer_renders_turns_the_doctor_red_and_speaks(box):
    box.renders = (False, "network.hostname is empty, but network.tls = 'sslip' ...")
    code, report = run(box)
    assert code == 1
    assert report["checks"]["renders"]["ok"] is False
    assert "hostname" in report["checks"]["renders"]["detail"]
    assert box.sent and "deck.toml" in box.sent[0]["text"]


def test_the_real_render_check_fails_on_a_deck_toml_without_network(tmp_path):
    mod = load()
    toml = tmp_path / "deck.toml"
    # The box's file as measured: no bind, no [network] -> tls=sslip, no hostname.
    toml.write_text(WIREGUARD_BOX.split("[network]")[0].replace('bind = "10.99.0.1"\n', ""))
    cfg = mod.config({"DECK_CONFIG": str(toml), "DECKDOCTOR_STATE": str(tmp_path / "s.json")})
    ok, detail = cfg["renders"]()
    assert ok is False and "hostname" in detail


def test_the_real_render_check_passes_on_a_wireguard_box(tmp_path):
    mod = load()
    toml = tmp_path / "deck.toml"
    toml.write_text(WIREGUARD_BOX)
    cfg = mod.config({"DECK_CONFIG": str(toml), "DECKDOCTOR_STATE": str(tmp_path / "s.json")})
    ok, detail = cfg["renders"]()
    assert ok is True, detail


def test_no_deck_toml_is_not_an_alarm(tmp_path):
    mod = load()
    cfg = mod.config({"DECK_CONFIG": str(tmp_path / "absent.toml"),
                      "DECKDOCTOR_STATE": str(tmp_path / "s.json")})
    ok, _ = cfg["renders"]()
    assert ok is True


def test_oom_kills_in_the_last_hour_are_a_number_that_outlives_the_alarm(box):
    """The alarm speaks once per kill; a regression has to show as a rate.
    MEASURED 2026-10-01: 23 kills in 24 h, 7 of them in the 17:00 hour."""
    log = {"kernel_log": lambda since: _kernel(NOW - 300)}
    assert run(box, **log)[1]["checks"]["oom_kills"]["last_hour"] == 2
    box.now += 600
    oom = run(box, **log)[1]["checks"]["oom_kills"]
    assert oom["ok"] is True and oom["count"] == 0 and oom["last_hour"] == 2
    box.now += 3600
    assert run(box, **log)[1]["checks"]["oom_kills"]["last_hour"] == 0


# -- informational deck notes ride the next turn; they are not stuck ----------
# Owner ruling 2026-10-01: a Mac-permission revoke and a connector note are
# queued WITHOUT a wake on purpose. They carry `notice: true`, and only that
# explicit mark excuses a deck message: a new deck reply type that should wake
# a desk and does not still turns the check red.


def test_a_marked_deck_notice_is_not_stuck(box):
    box.say(to="atlas", **{"from": "deck"}, text="Mac access revoked", notice=True,
            ts=NOW - 3600)
    assert run(box)[1]["checks"]["stuck_messages"] == {"ok": True, "count": 0}


def test_an_unmarked_deck_message_is_still_stuck(box):
    box.say(to="atlas", **{"from": "deck"}, text="a new kind of receipt", ts=NOW - 3600)
    check = run(box)[1]["checks"]["stuck_messages"]
    assert check["ok"] is False and check["count"] == 1


def test_only_the_deck_can_excuse_a_message(box):
    box.say(to="atlas", **{"from": "owner"}, text="hi", notice=True, ts=NOW - 3600)
    assert run(box)[1]["checks"]["stuck_messages"]["count"] == 1


def test_the_two_queue_only_doors_mark_their_notes():
    root = Path(__file__).resolve().parents[1]
    app_src = (root / "server" / "app.py").read_text()
    line = [ln for ln in app_src.splitlines() if ln.strip().startswith("queue=lambda")]
    assert line and 'extra={"notice": True}' in line[0], line
    conn = (root / "server" / "connectors.py").read_text()
    assert 'office.send(name, note, sender="deck", extra={"notice": True})' in conn
