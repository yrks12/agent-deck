"""K8: `deckctl update` -- download, verify, unpack, build, repoint, restart, prove it
answers within 60 s, and otherwise put the previous release back. Hermetic: the
manifest and tarball come from a dict, systemd and pip are a recording fake, and
time is a counter the fake `sleep` advances."""
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "deckctl"
BASE = "https://releases.example.test/agent-deck"


def load():
    loader = importlib.machinery.SourceFileLoader("deckctl", str(SCRIPT))
    spec = importlib.util.spec_from_loader("deckctl", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def tarball(version, extra=None, top=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        files = {"VERSION": version + "\n", "requirements.txt": "",
                 "deploy/render.py": "# renderer\n", **(extra or {})}
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo((top + "/" if top else "") + name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Box:
    def __init__(self, tmp_path, current="0.9.0", old=(), source="release", base=BASE):
        self.tmp = tmp_path
        self.opt = tmp_path / "opt" / "agent-deck"
        self.releases = self.opt / "releases"
        for v in (*old, current):
            (self.releases / v).mkdir(parents=True)
            (self.releases / v / "VERSION").write_text(v + "\n")
        self.current = self.opt / "current"
        self.current.symlink_to(self.releases / current)
        self.state = tmp_path / "state"
        self.toml = tmp_path / "deck.toml"
        self.toml.write_text(
            '[deck]\napp_dir = "%s"\nstate_dir = "%s"\ninstall_source = "%s"\n'
            '[update]\nbase_url = "%s"\n' % (self.current, self.state, source, base))
        self.served = {}
        self.fetched, self.ran = [], []
        self.t = 0.0
        self.broken = set()           # versions whose deck never answers
        self.healthy_after = 0.0      # seconds after a restart before it answers
        self.restarted_at = 0.0
        self.fail_run = None          # substring of a command that should fail
        self.publish("0.9.1")

    def publish(self, version, **tb):
        data = tarball(version, **tb)
        self.served[f"{BASE}/manifest.json"] = json.dumps({
            "version": version, "released": "2026-10-05", "notes": "a note",
            "server": {"url": f"{BASE}/agent-deck-server-{version}.tar.gz",
                       "sha256": hashlib.sha256(data).hexdigest()}}).encode()
        self.served[f"{BASE}/agent-deck-server-{version}.tar.gz"] = data
        return data

    def live(self):
        return os.path.realpath(self.current).rsplit("/", 1)[-1]

    # -- seams
    def fetch(self, url):
        self.fetched.append(url)
        if url not in self.served:
            raise OSError("404")
        return self.served[url]

    def run(self, argv, timeout=60):
        self.ran.append(list(argv))
        if self.fail_run and self.fail_run in " ".join(argv):
            return 1, "boom"
        if argv[:2] == ["systemctl", "restart"]:
            self.restarted_at = self.t
        return 0, ""

    def sleep(self, secs):
        self.t += secs

    def answering(self):
        return self.live() not in self.broken and self.t - self.restarted_at >= self.healthy_after

    def main(self, *argv):
        mod = load()
        box = self

        class Host:
            def active(_, unit): return box.answering()
            def get(_, url, timeout=5): return 200 if box.answering() else None
            def version(_):
                return (box.current / "VERSION").read_text().strip()
            def latest_version(_): return None

        ctl = mod.Ctl(config_path=self.toml, euid=0, run=self.run, fetch=self.fetch,
                      sleep=self.sleep, clock=lambda: self.t,
                      host_factory=lambda cfg: Host())
        return mod.main(list(argv), ctl)

    def restarts(self):
        return [a for a in self.ran if a[:2] == ["systemctl", "restart"]]


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def test_the_owners_rsync_box_refuses_every_form_and_touches_nothing(tmp_path, capsys):
    b = Box(tmp_path, source="rsync")
    for argv in (["update"], ["update", "--check"], ["update", "--to", "0.9.1"]):
        assert b.main(*argv) == 1
    err = capsys.readouterr().err
    assert "deployed from source" in err and "deploy procedure" in err
    assert b.fetched == [] and b.ran == [] and b.live() == "0.9.0"


def test_no_release_channel_says_how_to_set_one(tmp_path, capsys):
    b = Box(tmp_path, base="")
    assert b.main("update") == 1
    assert "update.base_url" in capsys.readouterr().err
    assert b.fetched == []


def test_check_reports_without_downloading_or_changing_anything(box, capsys):
    assert box.main("update", "--check") == 0
    text = capsys.readouterr().out
    assert "0.9.1" in text and "0.9.0" in text
    assert box.fetched == [f"{BASE}/manifest.json"] and box.ran == []
    assert box.live() == "0.9.0"


def test_check_json(box, capsys):
    box.main("update", "--check", "--json")
    data = json.loads(capsys.readouterr().out)
    assert data == {"current": "0.9.0", "latest": "0.9.1", "available": True,
                    "notes": "a note"}


def test_up_to_date_changes_nothing(box, capsys):
    box.publish("0.9.0")
    assert box.main("update") == 0
    assert "up to date" in capsys.readouterr().out.lower()
    assert box.ran == [] and box.live() == "0.9.0"


def test_an_older_manifest_is_never_a_downgrade(box, capsys):
    box.publish("0.8.0")
    assert box.main("update") == 0
    assert box.live() == "0.9.0" and box.ran == []


def test_a_good_update_unpacks_builds_repoints_restarts_and_proves_it(box, capsys):
    assert box.main("update") == 0
    assert box.live() == "0.9.1"
    assert (box.releases / "0.9.1" / "VERSION").read_text().strip() == "0.9.1"
    assert (box.releases / "0.9.0").is_dir()                      # the way back
    assert any("venv" in " ".join(a) for a in box.ran)
    assert len(box.restarts()) == 1
    assert "0.9.0" in capsys.readouterr().out and box.current.is_symlink()


def test_the_download_is_verified_against_the_manifest_before_anything_changes(box, capsys):
    box.served[f"{BASE}/agent-deck-server-0.9.1.tar.gz"] += b"tampered"
    assert box.main("update") == 1
    assert "checksum" in capsys.readouterr().err.lower()
    assert box.live() == "0.9.0" and not (box.releases / "0.9.1").exists()
    assert box.restarts() == []


def test_a_tarball_that_writes_outside_its_folder_is_refused(box, capsys):
    box.publish("0.9.1", extra={"../../escape.txt": "x"})
    assert box.main("update") == 1
    assert not (box.tmp / "opt" / "escape.txt").exists()
    assert box.live() == "0.9.0" and not (box.releases / "0.9.1").exists()


def test_a_tarball_with_one_top_level_folder_is_unwrapped(box, capsys):
    box.publish("0.9.1", top="agent-deck-0.9.1")
    assert box.main("update") == 0
    assert (box.releases / "0.9.1" / "VERSION").exists()


def test_a_failed_venv_build_removes_the_half_release_and_changes_nothing(box, capsys):
    box.fail_run = "venv"
    assert box.main("update") == 1
    assert box.live() == "0.9.0" and not (box.releases / "0.9.1").exists()
    assert box.restarts() == []


def test_a_failed_render_stops_before_the_switch(box, capsys):
    box.fail_run = "render.py"
    assert box.main("update") == 1
    assert box.live() == "0.9.0" and box.restarts() == []


def test_a_release_that_never_answers_is_rolled_back_and_the_reason_printed(box, capsys):
    box.broken = {"0.9.1"}
    assert box.main("update") == 1
    err = capsys.readouterr().err
    assert box.live() == "0.9.0"
    assert len(box.restarts()) == 2                      # new, then back
    assert "not answering" in err.lower() and "rolled back" in err.lower()
    assert not (box.releases / "0.9.1").exists()         # a retry downloads afresh
    assert box.t <= 70                                   # waited ~60 s, not forever


def test_a_slow_start_inside_60_seconds_is_not_a_failure(box, capsys):
    box.healthy_after = 25
    assert box.main("update") == 0 and box.live() == "0.9.1"
    assert len(box.restarts()) == 1


def test_only_the_current_and_the_previous_release_are_kept(tmp_path, capsys):
    b = Box(tmp_path, current="0.9.0", old=("0.8.0", "0.8.5"))
    assert b.main("update") == 0
    assert sorted(p.name for p in b.releases.iterdir()) == ["0.9.0", "0.9.1"]


def test_versions_are_ordered_numerically_when_pruning(tmp_path, capsys):
    b = Box(tmp_path, current="0.10.0", old=("0.9.0",))
    b.publish("0.11.0")
    assert b.main("update") == 0
    assert sorted(p.name for p in b.releases.iterdir()) == ["0.10.0", "0.11.0"]


def test_to_an_installed_release_switches_without_downloading(tmp_path, capsys):
    b = Box(tmp_path, current="0.9.1", old=("0.9.0",))
    assert b.main("update", "--to", "0.9.0") == 0
    assert b.live() == "0.9.0" and b.fetched == []
    assert len(b.restarts()) == 1


def test_a_switch_to_an_installed_release_that_fails_goes_back(tmp_path, capsys):
    b = Box(tmp_path, current="0.9.1", old=("0.9.0",))
    b.broken = {"0.9.0"}
    assert b.main("update", "--to", "0.9.0") == 1
    assert b.live() == "0.9.1" and (b.releases / "0.9.0").is_dir()


def test_to_a_version_nobody_published_is_refused(box, capsys):
    assert box.main("update", "--to", "0.7.7") == 1
    assert "0.7.7" in capsys.readouterr().err and box.live() == "0.9.0"


def test_to_the_published_version_installs_it(box, capsys):
    assert box.main("update", "--to", "0.9.1") == 0 and box.live() == "0.9.1"


def test_an_app_dir_that_is_a_real_folder_is_refused_in_plain_words(box, capsys):
    box.current.unlink()
    box.current.mkdir()
    assert box.main("update") == 1
    assert "releases" in capsys.readouterr().err and box.ran == []


def test_an_unreachable_channel_changes_nothing_and_says_so(box, capsys):
    box.served.clear()
    assert box.main("update") == 1
    assert "release channel" in capsys.readouterr().err.lower() and box.live() == "0.9.0"
