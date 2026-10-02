"""`bin/deploy-box`: the one way code reaches the box.

MEASURED 2026-09-30: "another session overwrote the box with older code about a
minute after my deploy." Several agents rsynced their checkouts to the box by
hand, so whoever finished last won -- including an older tree.

The script is exercised end to end against a local directory standing in for the
box: `ssh`, `rsync` and `flock` are tiny fakes on PATH that run the remote side
locally, so the script's own logic (clean tree, lock, descendant check, backup,
restore on a failed health check, DEPLOYED) runs unmodified.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "deploy-box"

FAKE_SSH = """#!/usr/bin/env python3
import subprocess, sys
args = sys.argv[1:]
while args and args[0].startswith("-"):
    flag = args.pop(0)
    if flag in ("-o", "-p", "-i", "-l"):
        args.pop(0)
args.pop(0)  # the host
sys.exit(subprocess.call(["bash", "-c", " ".join(args)]))
"""

FAKE_RSYNC = """#!/usr/bin/env python3
import re, subprocess, sys
args = [re.sub(r"^[A-Za-z0-9_.@-]+:(?=/)", "", a) for a in sys.argv[1:]]
sys.exit(subprocess.call([REAL] + args))
"""

FAKE_FLOCK = """#!/usr/bin/env python3
import fcntl, subprocess, sys
args = sys.argv[1:]
nb = "-n" in args
args = [a for a in args if a != "-n"]
path, cmd = args[0], args[args.index("-c") + 1]
fh = open(path, "a")
try:
    fcntl.flock(fh, fcntl.LOCK_EX | (fcntl.LOCK_NB if nb else 0))
except OSError:
    sys.exit(1)
sys.exit(subprocess.call(["bash", "-c", cmd]))
"""


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def git(repo: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *argv], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    _exe(fakes / "ssh", FAKE_SSH)
    _exe(fakes / "flock", FAKE_FLOCK)
    real = shutil.which("rsync")
    assert real, "rsync is needed for this test"
    _exe(fakes / "rsync", FAKE_RSYNC.replace("REAL", repr(real)))

    repo = tmp_path / "repo"
    (repo / "server").mkdir(parents=True)
    (repo / "hooks").mkdir()
    (repo / "bin").mkdir()
    (repo / "docs").mkdir()
    shutil.copy(ROOT / "server" / "deploy_guard.py", repo / "server" / "deploy_guard.py")
    (repo / "server" / "__init__.py").write_text("")
    (repo / "hooks" / "cc-bus.js").write_text("// hook\n")
    (repo / "bin" / "deck").write_text("#!/bin/sh\n")
    (repo / "docs" / "x.md").write_text("doc\n")
    (repo / "server" / "app.py").write_text("V = 1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "tester")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c1")
    c1 = git(repo, "rev-parse", "HEAD")
    (repo / "server" / "app.py").write_text("V = 2\n")
    git(repo, "commit", "-q", "-am", "c2")
    c2 = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "side", c1)
    (repo / "server" / "app.py").write_text("V = 'side'\n")
    git(repo, "commit", "-q", "-am", "c3")
    c3 = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")

    box = tmp_path / "box"
    box.mkdir()
    env = dict(os.environ)
    env.update({
        "PATH": f"{fakes}:{env['PATH']}",
        "DECK_BOX": "tester@box",
        "DECK_BOX_PATH": str(box),
        "DEPLOY_BOX_RESTART": f"touch {box}/.restarted",
        "DEPLOY_BOX_HEALTH": "true",
        "DEPLOY_BOX_PYTHON": sys.executable,
        "DEPLOY_BOX_WHO": "pytest",
    })
    return {"repo": repo, "box": box, "env": env, "c1": c1, "c2": c2, "c3": c3}


def deploy(w, *args, **env):
    e = dict(w["env"], **env)
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=w["repo"], env=e,
                          capture_output=True, text=True, timeout=120)


def deployed_sha(w):
    text = (w["box"] / "DEPLOYED").read_text()
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)["sha"]


def test_first_deploy_ships_the_tree_restarts_and_records_it(world):
    r = deploy(world, "--commit", world["c1"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert (world["box"] / "server" / "app.py").read_text() == "V = 1\n"
    assert (world["box"] / ".restarted").exists()
    assert deployed_sha(world) == world["c1"]
    rec = (world["box"] / "DEPLOYED").read_text()
    assert "who=pytest" in rec and "time=" in rec and "fingerprint=" in rec
    sys.path.insert(0, str(ROOT))
    from server import deploy_guard
    assert deploy_guard.check(world["box"])[0] is True


def test_a_descendant_deploys(world):
    assert deploy(world, "--commit", world["c1"]).returncode == 0
    r = deploy(world, "--commit", world["c2"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert deployed_sha(world) == world["c2"]
    assert (world["box"] / "server" / "app.py").read_text() == "V = 2\n"


@pytest.mark.parametrize("older", ["c1", "c3"])
def test_an_older_or_diverged_commit_is_refused_and_the_box_untouched(world, older):
    assert deploy(world, "--commit", world["c2"]).returncode == 0
    before = (world["box"] / "server" / "app.py").read_text()
    r = deploy(world, "--commit", world[older])
    assert r.returncode != 0
    assert "descendant" in (r.stdout + r.stderr)
    assert "--force-rollback" in (r.stdout + r.stderr)
    assert deployed_sha(world) == world["c2"]
    assert (world["box"] / "server" / "app.py").read_text() == before


def test_force_rollback_is_the_only_way_back(world):
    assert deploy(world, "--commit", world["c2"]).returncode == 0
    r = deploy(world, "--commit", world["c1"], "--force-rollback")
    assert r.returncode == 0, r.stdout + r.stderr
    assert deployed_sha(world) == world["c1"]
    assert (world["box"] / "server" / "app.py").read_text() == "V = 1\n"


def test_a_dirty_tree_is_refused(world):
    (world["repo"] / "server" / "app.py").write_text("V = 'uncommitted'\n")
    r = deploy(world, "--commit", "HEAD")
    assert r.returncode != 0 and "dirty" in (r.stdout + r.stderr).lower()
    assert not (world["box"] / "DEPLOYED").exists()


def test_a_held_lock_refuses_a_second_deploy(world):
    lock = world["box"] / ".deploy.lock"
    fh = open(lock, "a")
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        r = deploy(world, "--commit", world["c1"])
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    assert r.returncode != 0 and "lock" in (r.stdout + r.stderr).lower()
    assert not (world["box"] / "DEPLOYED").exists()


def test_the_lock_is_held_for_the_whole_run(world):
    # The restart step tries to take the lock itself: it must find it held.
    box = world["box"]
    probe = box.parent / "probe.py"
    probe.write_text(textwrap.dedent(f"""
        import fcntl
        fh = open({str(box / '.deploy.lock')!r}, "a")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            open({str(box / '.lock-was-free')!r}, "w")
        except OSError:
            open({str(box / '.lock-was-held')!r}, "w")
    """))
    r = deploy(world, "--commit", world["c1"], DEPLOY_BOX_RESTART=f"{sys.executable} {probe}")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (world["box"] / ".lock-was-held").exists()
    assert not (world["box"] / ".lock-was-free").exists()


def test_a_failed_health_check_restores_the_previous_tree(world):
    assert deploy(world, "--commit", world["c1"]).returncode == 0
    r = deploy(world, "--commit", world["c2"], DEPLOY_BOX_HEALTH="false")
    assert r.returncode != 0 and "healthz" in (r.stdout + r.stderr)
    assert (world["box"] / "server" / "app.py").read_text() == "V = 1\n"
    assert deployed_sha(world) == world["c1"]


def test_redeploying_the_same_commit_is_a_no_op(world):
    assert deploy(world, "--commit", world["c2"]).returncode == 0
    os.remove(world["box"] / ".restarted")
    r = deploy(world, "--commit", world["c2"])
    assert r.returncode == 0 and "already" in r.stdout
    assert not (world["box"] / ".restarted").exists()


def test_the_script_carries_no_owner_box_address():
    text = SCRIPT.read_text()
    assert "10.8" "8." not in text and "ya" "iros" not in text


@pytest.mark.parametrize("doc", ["AGENTS.md", "docs/the-box.md"])
def test_the_docs_agents_read_say_deploy_box_and_never_by_hand(doc):
    text = (ROOT / doc).read_text()
    assert "bin/deploy-box" in text
    assert "never" in text.lower() and "rsync" in text and "by hand" in text
