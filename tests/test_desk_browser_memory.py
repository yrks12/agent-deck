"""Desk browsers stay under the live cap and inside their memory.

MEASURED on the box (kernel log, 2026-10-01): 19 OOM kills in 24 h, every one
a desk browser's chromium; 18 hit the container's 1 GiB cgroup. At 19:24 nine
desk browsers started within ~20 s although the reaper caps live ones at 5:
the owner clicked through nine desks in the Mac app, each desk page polled
`GET /v1/agents/{desk}/screen` once or twice, and every poll of a stopped
desk called `browser_reaper.wake` at once. Each `ensure` then saw "under the
cap" before any of the others had started -- a check-then-act race.

Detectors here:
  * N parallel `ensure` calls never leave more than the cap running;
  * a screen poll wakes a browser only after the owner has stayed on it;
  * login sync never starts a browser (a guard: it already did not);
  * the launch line carries the memory flags;
  * the container limit comes from config;
  * a desk near its limit loses background tabs, not the browser.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import browser_reaper as reaper
from server import desk_computer, login_vault, sandbox

NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def _bus(tmp_path, monkeypatch):
    monkeypatch.setattr(reaper, "LAST_USE_DIR", tmp_path / "use")
    monkeypatch.setattr(reaper, "STATES_PATH", tmp_path / "states.json")
    monkeypatch.setattr(reaper, "MEMORY_PATH", tmp_path / "memory.json")
    monkeypatch.setattr(reaper, "ADMISSION_LOCK", tmp_path / "admission.lock",
                        raising=False)


class _Box:
    """Docker as the reaper sees it: a set of running desk containers, with
    a start slow enough that a racing caller sees the old count."""

    def __init__(self, running=()):
        self.running = set(running)
        self.lock = threading.Lock()
        self.peak = len(self.running)

    def is_up(self, desk):
        return desk in self.running

    def start(self, desk, **_):
        time.sleep(0.05)
        with self.lock:
            self.running.add(desk)
            self.peak = max(self.peak, len(self.running))

    def run(self, argv, *, timeout=None):
        if argv[:2] == ["docker", "ps"]:
            out = "\n".join(f"deck-desk-{d}" for d in sorted(self.running))
            return SimpleNamespace(returncode=0, stdout=out.encode(), stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


def _burst(monkeypatch, box, desks):
    monkeypatch.setattr(sandbox, "is_up", box.is_up)
    monkeypatch.setattr(sandbox, "start", box.start)
    monkeypatch.setattr(sandbox, "_run", box.run)
    monkeypatch.setattr(desk_computer, "ContainerCDP", lambda desk: SimpleNamespace(
        page_target=lambda: {"id": "p"}, wait_for_page=lambda timeout=0: {}))
    errors = []

    def one(desk):
        try:
            desk_computer.ensure(desk)
        except sandbox.SandboxError as exc:
            errors.append(exc.reason)

    threads = [threading.Thread(target=one, args=(d,)) for d in desks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return errors


def test_a_burst_of_parallel_ensures_never_passes_the_cap(monkeypatch):
    monkeypatch.setenv("DECK_BROWSER_MAX_LIVE", "3")
    desks = [f"d{i}" for i in range(9)]
    reaper.write_states({d: "IDLE" for d in desks}, reaper.STATES_PATH)
    box = _Box()
    errors = _burst(monkeypatch, box, desks)
    assert box.peak <= 3, f"{box.peak} browsers live under a burst of 9"
    assert len(box.running) == 3
    assert errors == ["browsers_full"] * 6


def test_at_the_cap_the_lru_idle_browser_is_stopped_before_the_launch(
        monkeypatch):
    monkeypatch.setenv("DECK_BROWSER_MAX_LIVE", "2")
    box = _Box(["old", "older"])
    reaper.touch("old", now=time.time() - 600)
    reaper.touch("older", now=time.time() - 900)
    reaper.write_states({"old": "IDLE", "older": "IDLE", "new": "IDLE"},
                        reaper.STATES_PATH)
    monkeypatch.setattr(reaper, "retire", lambda d: box.running.discard(d))
    assert _burst(monkeypatch, box, ["new"]) == []
    assert box.running == {"old", "new"}
    assert box.peak <= 2


# ── the trigger: a screen poll is not a request for a browser ──────────────


def test_flicking_past_a_desk_does_not_wake_its_browser(monkeypatch):
    woke = []
    monkeypatch.setattr(reaper, "wake", lambda d: woke.append(d) or True)
    monkeypatch.setattr(reaper, "_asked", {}, raising=False)
    for i, desk in enumerate(["a", "b", "c", "d"]):  # 19:24: ~2 s per desk
        assert reaper.ask(desk, now=NOW + 2 * i) is True  # "waking" to him
        reaper.ask(desk, now=NOW + 2 * i + 1.3)
    assert woke == []


def test_staying_on_a_desk_wakes_its_browser(monkeypatch):
    woke = []
    monkeypatch.setattr(reaper, "wake", lambda d: woke.append(d) or True)
    monkeypatch.setattr(reaper, "_asked", {}, raising=False)
    for t in range(0, 7):
        reaper.ask("atlas", now=NOW + t)
    assert woke and set(woke) == {"atlas"}


def test_a_desk_he_left_starts_its_wait_over(monkeypatch):
    woke = []
    monkeypatch.setattr(reaper, "wake", lambda d: woke.append(d) or True)
    monkeypatch.setattr(reaper, "_asked", {}, raising=False)
    reaper.ask("atlas", now=NOW)
    reaper.ask("atlas", now=NOW + 60)   # came back a minute later
    assert woke == []


# ── login sync reads running browsers and starts none ──────────────────────


def test_login_sync_never_starts_a_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(login_vault, "VAULT_PATH", tmp_path / "v.json")
    monkeypatch.setattr(login_vault, "LS_PATH", tmp_path / "ls.json")
    monkeypatch.setattr(login_vault, "enabled", lambda: True)
    monkeypatch.setattr(login_vault, "running_desks", lambda: ["up", "nobrowser"])
    started = []
    for mod, name in ((sandbox, "start"), (desk_computer, "ensure"),
                      (reaper, "wake"), (reaper, "admit")):
        monkeypatch.setattr(mod, name, lambda *a, _n=name, **k: started.append(_n),
                            raising=False)
    monkeypatch.setattr(sandbox, "_run", lambda argv, **k: started.append(argv))

    class Driver:
        def __init__(self, desk):
            self.desk = desk

        def page_target(self):
            return None if self.desk == "nobrowser" else {"id": "p"}

        def page_state(self):
            return {"url": ""}

        def send(self, method, params=None):
            return {"cookies": []} if method == "Network.getAllCookies" else {}

    monkeypatch.setattr(login_vault, "driver_for", Driver)
    login_vault.sync_running_desks()
    login_vault.share_in([{"name": "sessionid", "value": "v", "path": "/",
                           "domain": ".example.com",
                           "expires": time.time() + 3600}])
    assert started == []


# ── chromium's own memory ──────────────────────────────────────────────────


def test_the_launch_line_bounds_chromiums_memory():
    argv = desk_computer.launch_argv("atlas")
    for flag in ("--renderer-process-limit=2",
                 "--js-flags=--max-old-space-size=512",
                 "--disable-dev-shm-usage", "--disable-extensions",
                 "--disable-sync",
                 "--disable-component-extensions-with-background-pages"):
        assert flag in argv, flag
    assert argv[-1] == "about:blank", "flags go before the url"


def test_the_container_limit_comes_from_config():
    home = Path("/tmp/deck-test-home")

    def limit(env):
        argv = sandbox.create_argv("acme", home=home, env=env)
        return argv[argv.index("--memory") + 1], argv[argv.index("--memory-swap") + 1]

    assert limit({}) == ("1280m", "1280m")
    assert limit({"DECK_DESK_MEMORY": "1536m"}) == ("1536m", "1536m")
    assert limit({"DECK_DESK_MEMORY": "2g"}) == ("2g", "2g")
    assert limit({"DECK_DESK_MEMORY": "lots; rm -rf /"}) == ("1280m", "1280m")
    assert limit({"DECK_DESK_MEMORY": "64m"}) == ("1280m", "1280m"), "too small"


def test_the_cap_and_the_limit_fit_the_box_together():
    """3 x 1.25 GiB of browsers next to ~3.8 GB of claude sessions (measured
    2026-10-01) stays under the box's 7.9 GB; 5 x 1 GiB did not."""
    _, cap = reaper.settings({})
    assert cap == 3
    assert cap * sandbox.memory_limit_bytes({}) <= 4 * 1024 ** 3


# ── near the limit, background tabs go first ───────────────────────────────


def test_a_desk_near_its_limit_closes_background_tabs_not_the_browser(
        monkeypatch):
    closed = []

    class Driver:
        def __init__(self, desk):
            pass

        def targets(self):
            return [{"type": "page", "id": "FRONT"},
                    {"type": "service_worker", "id": "SW"},
                    {"type": "page", "id": "BACK1"},
                    {"type": "page", "id": "../evil"},
                    {"type": "page", "id": "BACK2"}]

        def _get(self, path):
            closed.append(path)
            raise desk_computer.browser.BrowserError("cdp_unreachable", "text")

    monkeypatch.setattr(desk_computer, "ContainerCDP", Driver)
    assert reaper.trim("atlas") == 2
    assert closed == ["/json/close/BACK1", "/json/close/BACK2"]


def test_pressure_is_read_from_the_containers_own_cgroup(monkeypatch):
    seen = []

    def run(argv, *, timeout=None):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"900000000\n1000000000\n",
                               stderr=b"")

    monkeypatch.setattr(sandbox, "_run", run)
    assert reaper.pressure("atlas") == pytest.approx(0.9)
    assert "/sys/fs/cgroup/memory.current" in seen[0]
