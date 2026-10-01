"""The agent's browser: one Chrome, on a display a human can walk into.

Written before `server/browser.py` exists.

The whole feature is a real Chrome running on a virtual X display. The agent
drives it over the DevTools Protocol; Sam watches the same display and, when
the agent hits a login or a card field, uses it himself. Because it is
literally one browser, "take over, sign in, hand back" needs no session
transfer -- the cookie is already in the profile.

That design puts three things one command line away from being a disaster, and
they are what these tests pin:

  * **An open DevTools port is remote code execution.** Anything that can speak
    CDP to that port can open `file:///Users/samcarter/.ssh/id_ed25519` and
    read it back. Bound to loopback it is reachable only from this machine;
    bound to `0.0.0.0` it is reachable from the LAN. So the bind address is
    asserted as *present*, not merely as "0.0.0.0 is absent" -- an argv that
    forgot the flag entirely would pass the absence test.
  * **`--no-sandbox` hands the same power to any page the agent visits.** It is
    the flag every "make Chrome work in Docker" answer on the internet tells
    you to add, which is exactly why it needs a test and not a comment.
  * **A profile directory inside a repo is a profile that gets committed or
    wiped.** `git checkout` on another branch would delete the cookies that are
    the entire point of a persistent browser, and `git add -A` would publish
    the session tokens in them.

Nothing here starts Xvfb, Chrome or ffmpeg, and nothing opens a socket. The
argv builders are pure, and the two functions that do spawn are tested only on
the guards that fire *before* they spawn.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from server import browser as B
from server import paths, vault

# Shaped like a Stripe live key. Is not one.
FAKE = "sk_live_" + "FAKE51H8sV2qNrPzXk9TdWmB4gY7cQaE0uLjR"

REPO = Path(__file__).resolve().parent.parent


def stocked_vault(tmp_path, grants=("acme",)):
    """A vault holding one fake key, granted to `grants`. Returns its path."""
    path = tmp_path / "vault.json"
    vault.put(path, name="STRIPE_LIVE_KEY", value=FAKE,
              description="Live key for the Acme checkout.", grants=grants)
    return path


# ── per-desk profile isolation ─────────────────────────────────────────────


def test_each_desk_gets_its_own_profile_directory(tmp_path):
    """The good signal: two desks, two real directories, both under the root.

    Shared profiles are the Grok Bot failure the spec calls out by name -- one
    VM, one browser session, one credential set for every agent. Here, signing
    the Acme desk into Stripe must not sign the Villas desk into anything.
    """
    acme = B.profile_for("acme", tmp_path)
    villas = B.profile_for("villas", tmp_path)

    assert acme != villas
    assert tmp_path in acme.parents
    assert tmp_path in villas.parents
    assert "acme" in acme.name


def test_the_same_desk_always_gets_the_same_profile(tmp_path):
    """Persistence is the point: a login survives to the agent's next run."""
    assert B.profile_for("acme", tmp_path) == B.profile_for("acme", tmp_path)


@pytest.mark.parametrize("hostile", [
    "../../../etc",
    "..",
    "acme/../../villas",
    "a/b",
    "/absolute",
    "",
    "   ",
    ".",
])
def test_a_hostile_desk_name_cannot_escape_the_profile_root(tmp_path, hostile):
    """A desk name reaches here from roster.json, which a human edits.

    `profile_for("../../..")` resolving to a real directory would let a desk
    name choose what Chrome's `--user-data-dir` points at -- and Chrome will
    happily take over a directory it is given.
    """
    with pytest.raises(ValueError):
        B.profile_for(hostile, tmp_path)


def test_the_default_profile_root_is_in_the_bus_dir_not_a_checkout():
    """Where the cookies live, and where they must never live.

    The bus dir is the deck's own state directory: no branch switch touches it,
    no `git add -A` reaches it. The repo checkout is the opposite of both.
    """
    root = B.DEFAULT_ROOT
    assert paths.BUS_DIR == root or paths.BUS_DIR in root.parents
    assert REPO not in root.parents and REPO != root

    profile = B.profile_for("acme", root)
    assert paths.BUS_DIR in profile.parents


def test_in_git_repo_sees_a_repo_and_does_not_invent_one(tmp_path):
    """Both halves: a directory under a `.git` is caught, a clean one is not."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "server" / "deep").mkdir(parents=True)
    assert B.in_git_repo(repo / "server" / "deep") is True
    assert B.in_git_repo(repo) is True

    clean = tmp_path / "state" / "browser"
    clean.mkdir(parents=True)
    assert B.in_git_repo(clean) is False


# ── chrome_argv: the command line that decides whether this is safe ────────


def _chrome(tmp_path, **over):
    kw = dict(display=":99", cdp_port=9222,
              profile_dir=str(tmp_path / "profiles" / "acme"),
              url="https://acme.initech.example/")
    kw.update(over)
    return B.chrome_argv(**kw)


def test_chrome_argv_binds_the_devtools_port_to_loopback(tmp_path):
    """The good signal: the loopback bind is actually in the argv.

    Asserted as presence. A build that dropped the flag would still pass a test
    that only checked `0.0.0.0` was absent, and would then inherit whatever
    Chrome's default happens to be on the day it is upgraded.
    """
    argv = _chrome(tmp_path)
    assert f"--remote-debugging-address={B.CDP_BIND}" in argv
    assert B.CDP_BIND == "127.0.0.1"
    assert "--remote-debugging-port=9222" in argv
    assert not any("0.0.0.0" in arg for arg in argv)


@pytest.mark.parametrize("banned", [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-web-security",
    "--remote-allow-origins",
])
def test_chrome_argv_never_disables_the_sandbox(tmp_path, banned):
    """Each of these turns the agent's browser into a shell on this Mac."""
    argv = _chrome(tmp_path)
    assert not any(arg == banned or arg.startswith(banned + "=")
                   for arg in argv), f"{banned} is in {argv}"


def test_the_banned_list_names_all_four_flags():
    """The sweep above is only as good as the list it is driven from."""
    assert {"--no-sandbox", "--disable-setuid-sandbox",
            "--disable-web-security",
            "--remote-allow-origins"} <= set(B.BANNED_CHROME_FLAGS)


def test_chrome_argv_carries_the_desks_own_profile_and_display(tmp_path):
    """Isolation is only real if the profile actually reaches the flag."""
    profile = str(tmp_path / "profiles" / "acme")
    argv = _chrome(tmp_path, profile_dir=profile)
    assert f"--user-data-dir={profile}" in argv
    assert "--display=:99" in argv
    assert argv[0] == B.CHROME_BINARY
    assert argv[-1] == "https://acme.initech.example/"


def test_chrome_argv_refuses_a_relative_profile_dir(tmp_path):
    """Chrome resolves a relative --user-data-dir against its own cwd, which
    is not a thing this module controls."""
    with pytest.raises(ValueError):
        _chrome(tmp_path, profile_dir="profiles/acme")


@pytest.mark.parametrize("port", [0, -1, 80, 65536, 1023])
def test_chrome_argv_refuses_a_port_that_is_not_a_free_high_one(tmp_path, port):
    """A privileged or nonsense port is a misconfiguration, not a preference."""
    with pytest.raises(ValueError):
        _chrome(tmp_path, cdp_port=port)


# ── xvfb_argv and capture_argv ─────────────────────────────────────────────


def test_xvfb_argv_is_the_display_at_size_and_refuses_tcp():
    """`-nolisten tcp` is the X-level twin of the loopback CDP bind.

    Without it the display itself is a network service, and anything that can
    reach it can read every pixel of a signed-in browser and inject keystrokes.
    """
    argv = B.xvfb_argv(":99", (1280, 800))
    assert argv == ["Xvfb", ":99", "-screen", "0", "1280x800x24",
                    "-nolisten", "tcp"]


def test_xvfb_argv_refuses_a_display_that_is_not_a_display():
    for bad in ("99", ":", "", ":abc", ":99;rm -rf /", ":-1"):
        with pytest.raises(ValueError):
            B.xvfb_argv(bad, (1280, 800))


def test_capture_argv_grabs_that_display_into_that_file(tmp_path):
    """The panel's video comes from x11grab on the same display Chrome is on."""
    out = str(tmp_path / "acme.mp4")
    argv = B.capture_argv(display=":99", out_path=out, fps=12)
    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-f") + 1] == "x11grab"
    assert argv[argv.index("-framerate") + 1] == "12"
    assert argv[argv.index("-i") + 1] == ":99"
    assert argv[-1] == out


def test_capture_argv_refuses_a_silly_frame_rate(tmp_path):
    out = str(tmp_path / "acme.mp4")
    for fps in (0, -1, 121):
        with pytest.raises(ValueError):
            B.capture_argv(display=":99", out_path=out, fps=fps)


# ── display allocation: two desks must not land on one screen ──────────────


def test_two_desks_starting_at_once_do_not_get_the_same_display(tmp_path):
    """Both on `:99` means Villas' Chrome draws over Acme's, and Sam takes
    over the wrong one. The reservation is a lock file, so it holds across
    processes, not just across calls in one interpreter."""
    xlocks = tmp_path / "xlocks"
    xlocks.mkdir()
    first = B.reserve_display(tmp_path, x_lock_dir=xlocks)
    second = B.reserve_display(tmp_path, x_lock_dir=xlocks)

    assert first != second
    assert first == ":99"
    assert B.display_lock(tmp_path, first).exists()
    assert B.display_lock(tmp_path, second).exists()


def test_a_display_already_used_by_a_real_x_server_is_skipped(tmp_path):
    """X itself records `:99` as taken in /tmp/.X99-lock. Our own lock files
    are not the only claim on a display number."""
    xlocks = tmp_path / "xlocks"
    xlocks.mkdir()
    (xlocks / ".X99-lock").write_text("12345\n")

    assert B.reserve_display(tmp_path, x_lock_dir=xlocks) == ":100"


def test_a_released_display_comes_back(tmp_path):
    """Otherwise a box that restarts browsers all day runs out of screens."""
    xlocks = tmp_path / "xlocks"
    xlocks.mkdir()
    first = B.reserve_display(tmp_path, x_lock_dir=xlocks)
    B.release_display(tmp_path, first)
    assert not B.display_lock(tmp_path, first).exists()
    assert B.reserve_display(tmp_path, x_lock_dir=xlocks) == first


# ── the environment Chrome is handed ───────────────────────────────────────


def test_the_browser_gets_its_desks_secrets_and_not_the_ambient_ones(
        tmp_path, monkeypatch):
    """Both halves. The granted key really is delivered -- an empty dict would
    pass an absence-only test -- and an unrelated credential sitting in this
    shell's environment is not carried into a browser that renders untrusted
    pages and runs untrusted JavaScript."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-and-not-palms")
    vault_path = stocked_vault(tmp_path)
    profile = tmp_path / "profiles" / "acme"

    env = B.chrome_env("acme", display=":99", profile_dir=profile,
                       vault_path=vault_path)

    assert env["STRIPE_LIVE_KEY"] == FAKE
    assert env["DISPLAY"] == ":99"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    # HOME points into the isolated profile so Chrome's stray dotfiles land
    # there rather than in ~, where the next desk would read them.
    assert env["HOME"] == str(profile)


def test_a_desk_with_no_grant_gets_no_secrets(tmp_path):
    """The default is nothing, exactly as vault.env_for promises."""
    vault_path = stocked_vault(tmp_path, grants=("acme",))
    env = B.chrome_env("villas", display=":100",
                       profile_dir=tmp_path / "profiles" / "villas",
                       vault_path=vault_path)
    assert "STRIPE_LIVE_KEY" not in env
    assert FAKE not in "".join(env.values())


# ── nothing leaves this module unredacted ──────────────────────────────────


def test_a_url_leaving_this_module_is_redacted(tmp_path):
    """A session token in a query string is the single easiest way for this
    layer to leak the thing it exists to protect: the URL goes on the board,
    into a handoff, and onto a phone."""
    vault_path = stocked_vault(tmp_path)
    dirty = f"https://acme.initech.example/checkout?key={FAKE}&plan=hand"
    clean = B.safe(dirty, vault_path=vault_path)

    assert FAKE not in clean
    assert "[redacted STRIPE_LIVE_KEY]" in clean
    # The half that makes it useful: he still knows where to go.
    assert "acme.initech.example/checkout" in clean


def test_redaction_also_catches_a_secret_the_vault_never_saw(tmp_path):
    """The vault only knows what it was told. The shape rules in
    `handoff.redact` are the complement, and both must run."""
    vault_path = stocked_vault(tmp_path)
    clean = B.safe("https://x.example/cb?code=417293", vault_path=vault_path)
    assert "417293" not in clean


# ── the origin: the only part of a URL a durable rule may be written on ────


def test_origin_of_keeps_scheme_and_host_and_drops_everything_after(tmp_path):
    """A rule says "this desk may act on `https://ads.google.com`". It must
    never say "...on `https://ads.google.com/x?session=abc123`", because the
    session id would then be stored, forever, in a rules file the whole deck
    reads."""
    assert B.origin_of(
        "https://ads.google.com/aw/campaigns?ocid=99&sig=deadbeefcafe#tab"
    ) == "https://ads.google.com"
    assert B.origin_of("https://acme.initech.example/checkout/step/2") == \
        "https://acme.initech.example"
    # A non-default port is part of the origin -- :8443 is a different server.
    assert B.origin_of("https://box.local:8443/admin") == "https://box.local:8443"
    assert B.origin_of("http://127.0.0.1:7788/") == "http://127.0.0.1:7788"


def test_an_origin_never_carries_a_query_string_or_credentials():
    """The two things that must not survive into anything rule-shaped."""
    origin = B.origin_of("https://user:hunter2@shop.example/pay?token=" + "abc123def456")
    assert "?" not in origin and "token" not in origin
    assert "hunter2" not in origin and "@" not in origin
    assert origin == "https://shop.example"


@pytest.mark.parametrize("bad", [
    "file:///Users/samcarter/.ssh/id_ed25519",
    "javascript:alert(1)",
    "data:text/html,<h1>hi",
    "about:blank",
    "chrome://settings",
    "not a url",
    "",
])
def test_origin_of_refuses_anything_that_is_not_a_web_origin(bad):
    """`file:` is the exact scheme an escaped agent would want a standing rule
    for, and it has no origin worth granting."""
    with pytest.raises(ValueError):
        B.origin_of(bad)


# ── start() and stop(): only the guards that fire before any spawn ─────────


def test_start_refuses_a_profile_root_inside_a_git_repo(tmp_path, monkeypatch):
    """A branch switch must never be able to delete a live browser session."""
    def explode(*a, **kw):  # pragma: no cover - the point is it is not called
        raise AssertionError("start() spawned something before checking")
    monkeypatch.setattr(subprocess, "Popen", explode)

    repo = tmp_path / "worktree"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(B.BrowserError) as exc:
        B.start("acme", root=repo / "browser")
    assert exc.value.reason == "profile_in_repo"


def test_start_refuses_a_hostile_desk_name_before_spawning(tmp_path, monkeypatch):
    def explode(*a, **kw):  # pragma: no cover
        raise AssertionError("start() spawned something before checking")
    monkeypatch.setattr(subprocess, "Popen", explode)

    with pytest.raises(ValueError):
        B.start("../../etc", root=tmp_path)


def test_is_running_tells_a_live_pid_from_a_dead_one(tmp_path):
    """Both halves, because `False` for everything is a passing absence test."""
    alive = B.BrowserSession(desk="acme", display=":99", cdp_port=9222,
                             profile_dir=str(tmp_path / "acme"),
                             pid=os.getpid(), started_at=0.0)
    assert B.is_running(alive) is True

    reaped = subprocess.Popen([sys.executable, "-c", ""])
    reaped.wait()
    dead = B.BrowserSession(desk="acme", display=":99", cdp_port=9222,
                            profile_dir=str(tmp_path / "acme"),
                            pid=reaped.pid, started_at=0.0)
    assert B.is_running(dead) is False

    never = B.BrowserSession(desk="acme", display=":99", cdp_port=9222,
                             profile_dir=str(tmp_path / "acme"),
                             pid=None, started_at=0.0)
    assert B.is_running(never) is False


def test_stop_is_safe_on_a_session_that_never_started(tmp_path):
    """`stop` runs from teardown paths and must not be the thing that raises."""
    sess = B.BrowserSession(desk="acme", display=":99", cdp_port=9222,
                            profile_dir=str(tmp_path / "profiles" / "acme"),
                            pid=None, started_at=0.0)
    B.stop(sess)  # no exception


def test_stop_releases_the_display_for_the_next_desk(tmp_path):
    """A display leaked on every stop is a box that runs out of screens."""
    xlocks = tmp_path / "xlocks"
    xlocks.mkdir()
    display = B.reserve_display(tmp_path, x_lock_dir=xlocks)
    sess = B.BrowserSession(desk="acme", display=display, cdp_port=9222,
                            profile_dir=str(B.profile_for("acme", tmp_path)),
                            pid=None, started_at=0.0)

    B.stop(sess)

    assert not B.display_lock(tmp_path, display).exists()
