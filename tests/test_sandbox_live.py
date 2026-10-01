"""The live half: a real container, a real Chromium, a real page, a real click.

**Excluded from the default suite** (`-m live`, pytest.ini `addopts`). Nothing
in the ordinary run starts Docker.

    docker build -t agent-deck/desk-computer:1 docker/desk-computer
    .venv/bin/python -m pytest -m live tests/test_sandbox_live.py

`tests/test_sandbox.py` proves what the deck *asks for*. Everything here is a
claim about what Docker, Chromium and Xvfb actually *do*, and each one has
already been wrong at least once somewhere:

  * **Chromium keeps its own sandbox.** The pure test only proves we never
    emit `--no-sandbox`. Measured, Chromium under Docker's default seccomp
    profile prints "No usable sandbox!" and exits, because the profile blocks
    `clone(CLONE_NEWUSER)` — so `create_argv` carries
    `--security-opt seccomp=unconfined` and this test is what proves that
    bought a real sandbox rather than merely a browser that starts. The
    signal is per-process: renderers in `Seccomp: 2` and in a user namespace
    that is not the container's own.
  * **Nothing on the Mac can dial the browser.** The DevTools port has no
    authentication of any kind, so reachability *is* the access control. The
    argv test proves no `--publish`; this proves the port is unreachable.
  * **A click from outside the container lands in the page.** The whole
    take-over rests on it, and `xdotool` needs XTEST on the display Xvfb
    provides.
  * **The profile is on the Mac when the container is gone.** That is what
    makes a sign-in during a take-over worth anything tomorrow.

Every test cleans up its own container. The desk is named `livetest` and is
not on anyone's roster.
"""

import os
import shutil
import socket
import subprocess

import pytest

from server import sandbox

pytestmark = pytest.mark.live

DESK = "livetest"

needs_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="needs Docker")


@pytest.fixture
def computer(tmp_path):
    """A real container for `DESK`, torn down whatever happens."""
    home = tmp_path / "home"
    try:
        yield sandbox.start(DESK, home=home)
    finally:
        sandbox.stop(DESK)


@pytest.fixture
def browsing(computer):
    """...with a real Chromium on a real page, PAINTED.

    Waiting for renderer processes is not enough and cost a red test to find
    out: they exist several seconds before anything is drawn, so a click sent
    then lands on a blank window. The frame settling -- two grabs the same
    size -- is the only signal available here that the page is actually up,
    since there is no CDP client on this side.
    """
    subprocess.Popen(sandbox.browser_argv(DESK, url="https://example.com"),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _wait_for(_renderers, "chromium renderers", timeout=45)
    _wait_for(_settled, "the page to finish painting", timeout=45)
    return computer


def _settled(tolerance: int = 500) -> bytes | None:
    """The frame, once two grabs a second apart are the same size."""
    import time
    first = sandbox.frame(DESK)
    time.sleep(1.0)
    second = sandbox.frame(DESK)
    if len(first) < 5_000:
        return None  # a blank window, not a page
    return second if abs(len(second) - len(first)) <= tolerance else None


def _wait_for(probe, what, timeout=30.0, every=0.5):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(every)
    raise AssertionError(f"{what} never appeared within {timeout}s")


def _renderers() -> list[str]:
    """Chromium's renderer pids, read from inside the container."""
    proc = subprocess.run(sandbox.exec_argv(DESK, [
        "sh", "-c",
        'for p in $(pgrep chromium); do '
        'tr "\\0" " " < /proc/$p/cmdline | grep -q -- "--type=renderer" && '
        'echo "$p $(grep Seccomp: /proc/$p/status | tr -d "\\t") '
        '$(readlink /proc/$p/ns/user)"; done']),
        capture_output=True, text=True)
    return [line for line in proc.stdout.splitlines() if line.strip()]


@needs_docker
def test_the_computer_comes_up_and_photographs_itself(computer):
    """The floor: a container, a display, and a JPEG of it."""
    assert sandbox.is_up(DESK)
    blob = sandbox.frame(DESK)
    assert blob[:2] == b"\xff\xd8" and blob[-2:] == b"\xff\xd9", "not a JPEG"
    assert len(blob) > 1000, f"a {len(blob)}-byte frame is not a screen"


@needs_docker
def test_chromium_keeps_its_own_sandbox(browsing):
    """The measurement that decided `--security-opt seccomp=unconfined`.

    Asserted per renderer, not on the absence of an error line: a Chromium
    started with `--no-sandbox` also logs nothing.
    """
    own = subprocess.run(
        sandbox.exec_argv(DESK, ["readlink", "/proc/1/ns/user"]),
        capture_output=True, text=True).stdout.strip()
    assert own, "could not read the container's own user namespace"

    renderers = _renderers()
    assert renderers, "no renderer processes at all"
    for line in renderers:
        assert "Seccomp:2" in line.replace(" ", ""), \
            f"renderer without a seccomp filter: {line}"
        assert own not in line, \
            f"renderer shares the container's user namespace: {line}"


@needs_docker
def test_nothing_on_this_mac_can_dial_the_agents_browser(browsing):
    """CDP has no authentication; reachability is the access control."""
    published = subprocess.run(
        ["docker", "port", sandbox.container_name(DESK)],
        capture_output=True, text=True).stdout.strip()
    assert published == "", f"the desk's computer published {published!r}"

    with socket.socket() as probe:
        probe.settimeout(2.0)
        with pytest.raises(OSError):
            probe.connect(("127.0.0.1", sandbox.browser.CDP_PORT_BASE))


@needs_docker
def test_a_click_from_outside_the_container_lands_in_the_page(browsing):
    """The take-over. `https://example.com` has one link, at (301, 301) on a
    1280x800 display, and following it changes the page enough that the JPEG
    changes size by more than any cursor blink."""
    before = sandbox.frame(DESK)
    sandbox.send_input(DESK, {"action": "click", "x": 301, "y": 301})
    after = _wait_for(
        lambda: (lambda f: f if abs(len(f) - len(before)) > 20_000 else None)(
            sandbox.frame(DESK)),
        "the page to change after the click", timeout=20)
    assert len(after) != len(before)


@needs_docker
def test_a_typed_backtick_is_a_character_and_not_a_command(browsing):
    """The owner types passwords through this. If `type` ever went through a
    shell, this string would run `id` inside the container."""
    sandbox.send_input(DESK, {"action": "key", "key": "ctrl+l"})
    sandbox.send_input(DESK, {"action": "type", "text": "example.com`id`"})
    assert sandbox.frame(DESK), "the display died mid-take-over"
    # Nothing to assert about the container's filesystem: the proof is that
    # xdotool got one argument. Read tests/test_sandbox.py for that assertion
    # and this frame for the omnibox showing the backtick as text.


@needs_docker
def test_the_profile_is_on_the_mac_after_the_container_is_gone(tmp_path):
    """A sign-in during a take-over has to be there tomorrow, and the
    container is not tomorrow."""
    home = tmp_path / "home"
    sandbox.start(DESK, home=home)
    try:
        subprocess.run(sandbox.browser_argv(DESK, url="about:blank"),
                       capture_output=True, timeout=25)
    except subprocess.TimeoutExpired:
        pass
    sandbox.stop(DESK)

    assert not sandbox.is_up(DESK)
    assert (home / "chrome-profile").is_dir(), \
        "the profile went away with the container"


@needs_docker
def test_stop_leaves_no_container_behind(tmp_path):
    sandbox.start(DESK, home=tmp_path / "home")
    sandbox.stop(DESK)
    names = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True, text=True).stdout.split()
    assert sandbox.container_name(DESK) not in names


@needs_docker
def test_a_value_the_browser_wrote_survives_stop_and_start(tmp_path):
    """THE Route A requirement: the owner signs into a service in the desk's
    own Chromium, and it is still signed in tomorrow.

    The existing test above proves the profile *directory* is still on disk,
    which turned out to be much weaker than it reads. MEASURED on the Linux box
    -- the machine this actually ships to -- the directory survived while the
    session did not, twice over: the home was root-owned so Chromium never
    wrote a profile at all, and the ProcessSingleton the force-killed browser
    left behind made the next container refuse to open it. Both were invisible
    on the Mac, where Docker Desktop remaps bind-mount ownership.

    So this asserts the thing the feature is actually for: a value written by
    the browser BEFORE the restart is read back by the browser AFTER it. The
    page is served `file://` from the desk's own home, so the measurement does
    not depend on the box's firewall or on any network at all.

    `stop` is `docker rm --force` and `start` is a fresh `docker run`, so this
    really is a different container over the same directory -- which is what
    made both faults possible.
    """
    home = tmp_path / "home"
    mark = f"deckmark{os.getpid()}"
    sandbox.start(DESK, home=home)
    try:
        (home / "set.html").write_text(
            f'<html><body><script>localStorage.setItem("k","{mark}")'
            f'</script>SET</body></html>')
        (home / "get.html").write_text(
            '<html><body><script>document.write('
            '"READBACK="+(localStorage.getItem("k")||"MISSING"))'
            '</script></body></html>')

        _dump(sandbox.DESK_HOME + "/set.html")

        # `stop` then `start`: a DIFFERENT container, the same home.
        sandbox.stop(DESK)
        sandbox.start(DESK, home=home)

        assert f"READBACK={mark}" in _dump(sandbox.DESK_HOME + "/get.html"), (
            "the browser could not read back what it wrote before the restart")
    finally:
        sandbox.stop(DESK)


def _dump(path: str) -> str:
    """One headless Chromium against a file:// page in the desk's home, DOM on
    stdout. `--allow-file-access-from-files` is what gives a `file://` page a
    real, storable origin instead of an opaque one."""
    argv = sandbox.exec_argv(DESK, [
        sandbox.CHROMIUM,
        f"--user-data-dir={sandbox.DESK_PROFILE}",
        "--headless=new", "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--allow-file-access-from-files",
        "--virtual-time-budget=4000", "--dump-dom", f"file://{path}",
    ])
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=120).stdout
