"""The sign-in has to survive the desk being stopped and started.

The owner's ruling is that an agent reaches an outside service either through a
skill somebody already wrote, or through **its own browser, which he signs into
himself** during a take-over. That second route has exactly one hard
requirement: if he signs into Gmail in a desk's Chromium and has to do it again
after the container restarts, the design is worthless.

`sandbox.stop` is `docker rm --force` and `sandbox.start` is a fresh
`docker run` over the same bind-mounted home, so "restart" always means *the
container is destroyed and a different one is created over the same directory*.
Two things broke that, both MEASURED on the Linux box (`10.99.0.1`, Docker
29.1.3, `agent-deck/desk-computer:1`) rather than read off the Dockerfile.

**Fault 1 — the desk could not write to its own home at all.** `start` created
the home with `path.mkdir()`, so it belonged to whoever runs the daemon. On the
box that is root, and the container runs as uid 1000:

    $ ls -ldn .../computers/persistproof
    drwxr-xr-x 2 0 0 4096 .../persistproof
    $ docker exec --user 1000:1000 ... touch /home/agent/canary.txt
    touch: cannot touch '/home/agent/canary.txt': Permission denied
    $ docker exec --user 1000:1000 ... chromium --user-data-dir=/home/agent/chrome-profile ...
    exit=133   profile-exists=no

Nothing persisted because nothing was ever written. This never showed on the
Mac: Docker Desktop's file sharing remaps ownership, so the bind mount is
writable there whatever the host directory says — which is precisely why the
existing live test passed while the product was broken on the machine it ships
to.

**Fault 2 — Chromium refused to reopen the profile.** `ProcessSingleton` is a
symlink holding `<hostname>-<pid>`, and Docker gives every new container a new
hostname. `sandbox.stop` force-kills Chromium mid-run, so the lock is left
behind naming a host that no longer exists. Measured, on the exact Route A
sequence — sign in, leave the browser open, stop the desk, start it again:

    ERROR:chrome/browser/process_singleton_posix.cc:365] The profile appears to
    be in use by another Chromium process (10) on another computer
    (ba3f31d33f29). Chromium has locked the profile so that it doesn't get
    corrupted.

The browser does not start, and the session is unreachable even though it is
sitting right there on disk.

**With both fixed, measured on the same box, same sequence:**

    browser still running: 11
    singleton before stop: 3
    AFTER RESTART: READBACK=deckmark1788356962
    expected     : READBACK=deckmark1788356962

The live round-trip is in `tests/test_sandbox_live.py`. What is here is the
part that runs in every suite: the preparation `start` does to the home before
Docker ever sees it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from server import sandbox


def _profile(home: Path) -> Path:
    """The profile directory as it appears on the HOST: `DESK_PROFILE` is the
    container's path, under the bind-mounted home, so the last segment is the
    one that shows up here. Derived rather than typed, so renaming the profile
    in `sandbox` cannot leave this file quietly testing a stale directory."""
    return home / Path(sandbox.DESK_PROFILE).name


# ── fault 1: the home belongs to the uid the container runs as ─────────────

def test_the_home_is_handed_to_the_uid_the_container_actually_runs_as(
        tmp_path, monkeypatch):
    """GOOD signal: `chown` is called with the very ids `create_argv` passes to
    `--user`. Read off the module, not repeated as literals -- a desk whose
    container runs as 1000 and whose home is handed to 1001 is the same bug
    with better paperwork."""
    seen: list[tuple] = []
    monkeypatch.setattr(os, "chown",
                        lambda p, u, g: seen.append((Path(p), u, g)))

    home = tmp_path / "home"
    sandbox.prepare_home(home)

    assert seen == [(home, sandbox.DESK_UID, sandbox.DESK_GID)]


def test_the_home_exists_afterwards_even_when_it_did_not_before(tmp_path):
    """Docker would otherwise create it as a side effect of the bind mount,
    owned by root, which is fault 1 arriving by a different door."""
    home = tmp_path / "nested" / "home"

    sandbox.prepare_home(home)

    assert home.is_dir()


def test_a_chown_this_process_is_not_allowed_to_do_never_stops_a_desk(
        tmp_path, monkeypatch):
    """On the Mac the daemon is not root and `chown` to uid 1000 is refused --
    and it does not matter there, because Docker Desktop remaps the mount. So
    the refusal must cost the desk nothing. Fails open, like `_vouch` and
    `_approval_settings` already do."""
    def refuse(*_args):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chown", refuse)
    home = tmp_path / "home"

    result = sandbox.prepare_home(home)

    assert result == home
    assert home.is_dir()


# ── fault 2: the locks a destroyed container left behind ───────────────────

@pytest.mark.parametrize("lock", sandbox.SINGLETONS)
def test_every_lock_a_destroyed_container_left_behind_is_cleared(tmp_path,
                                                                 lock):
    """Swept over the whole class, not just `SingletonLock`: Chromium writes
    three of these and consults more than one. The parametrisation is driven by
    the module's own tuple, so a fourth name added to it without being cleared
    fails here."""
    home = tmp_path / "home"
    profile = _profile(home)
    profile.mkdir(parents=True)
    (profile / lock).symlink_to("911ef588919a-9")

    sandbox.prepare_home(home)

    assert not (profile / lock).is_symlink(), f"{lock} survived"


def test_a_lock_is_cleared_even_though_it_points_at_nothing(tmp_path):
    """These are symlinks to a host and pid that no longer exist, so every
    check on them has to be link-aware. `Path.exists()` follows the link and
    answers False for exactly the file that is the problem -- a clear built on
    it would look like it worked and clear nothing."""
    home = tmp_path / "home"
    profile = _profile(home)
    profile.mkdir(parents=True)
    dangling = profile / "SingletonLock"
    dangling.symlink_to("/tmp/gone-with-the-container/nothing-here")
    assert not dangling.exists() and dangling.is_symlink()

    sandbox.prepare_home(home)

    assert not dangling.is_symlink()


def test_the_signed_in_session_itself_is_never_touched(tmp_path):
    """THE guard on the fix. Clearing locks is one `rm` away from clearing the
    profile, and a fix that made the browser start by throwing away the sign-in
    would satisfy every other test in this file while destroying the only thing
    the feature is for.

    The good signal is presence: every artefact that carries a session is still
    there, byte for byte, afterwards."""
    home = tmp_path / "home"
    default = _profile(home) / "Default"
    (default / "Local Storage" / "leveldb").mkdir(parents=True)
    kept = {
        default / "Cookies": b"sqlite-cookies",
        default / "Login Data": b"sqlite-logins",
        default / "Preferences": b'{"signed_in": true}',
        default / "Local Storage" / "leveldb" / "000003.log": b"deckmark",
    }
    for path, body in kept.items():
        path.write_bytes(body)
    (_profile(home) / "SingletonLock").symlink_to("host-9")

    sandbox.prepare_home(home)

    for path, body in kept.items():
        assert path.read_bytes() == body, f"{path.name} was disturbed"


def test_a_desk_that_has_never_run_a_browser_is_prepared_without_complaint(
        tmp_path):
    """There is no profile directory before the first sign-in. Clearing locks
    out of a directory that does not exist is the ordinary case on a brand-new
    desk, not an error."""
    home = tmp_path / "home"

    sandbox.prepare_home(home)

    assert home.is_dir()
    assert not _profile(home).exists()


# ── the preparation is on the path that starts a computer ──────────────────

def test_start_prepares_the_home_before_docker_is_told_anything(tmp_path,
                                                                monkeypatch):
    """A fix in a helper nothing calls is not a fix. Ordering is the assertion:
    Docker creates the bind mount itself if the directory is missing, and it
    creates it root-owned -- so preparation that ran after `docker run` would
    be preparing a directory the container had already been given."""
    order: list[str] = []
    home = tmp_path / "home"

    real_prepare = sandbox.prepare_home

    def spy(path):
        order.append("prepare")
        return real_prepare(path)

    def fake_run(argv, **_kw):
        order.append("docker " + argv[1])
        return type("P", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr(sandbox, "prepare_home", spy)
    monkeypatch.setattr(sandbox, "_run", fake_run)
    monkeypatch.setattr(sandbox, "is_up", lambda _desk: False)

    sandbox.start("livetest", home=home)

    assert order[0] == "prepare", order
    assert "docker run" in order, order
