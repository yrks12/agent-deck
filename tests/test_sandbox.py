"""The agent's computer is a container, and a container is a way in.

Written before `server/sandbox.py` exists.

`server/browser.py` and `server/screen.py` build argv for a Linux box: Xvfb,
`google-chrome`, `ffmpeg -f x11grab`. None of those exist on this Mac, and an
agent driving Chrome on the owner's own desktop would fight him for his
keyboard. So the desk gets its own machine -- a container -- and this module
is the only thing that puts a command inside it.

That makes `sandbox.py` the security boundary of the whole feature, in the
same way `chrome_argv` is for the browser. Everything pinned here is a way
the boundary fails **open** rather than a way the feature fails:

  * **A published port is a remote shell.** The container runs a browser with
    an unauthenticated DevTools port and a display with no X authority. Put
    one `-p 9222:9222` in that argv and every process on the LAN can
    `Page.navigate` to `file:///Users/samcarter/.ssh/id_ed25519`. The only
    sanctioned way in is `docker exec` over the root-owned Docker socket, and
    the only HTTP surface is `/v1`, behind the bearer token. So the sweep is
    for *publishing at all*, not for a particular port.
  * **`docker exec` defaults to the image's user.** An image rebuilt with
    `USER root`, or a future `--user` dropped in an edit, silently turns every
    frame grab and every keystroke into a root command in a container that
    bind-mounts a directory of the owner's. The uid is stated on the exec too,
    never inherited -- the same rule as `--remote-debugging-address`.
  * **The Docker socket is root on the Mac.** Mounted into a container it is
    not a privilege escalation, it is the whole machine. Swept by name.
  * **A keystroke is an argument, not a sentence.** The take-over path types
    what the owner sends into a real browser. Built through a shell, a
    password containing a backtick runs a command in the container; built as
    argv after `--`, a leading `-` is text and not an option. `xdotool` is
    the tool being handed the string, and `xdotool key --file` reads a file.
  * **A fired desk must not come back.** `--restart=unless-stopped` would have
    Chrome for a desk nobody works at anymore start itself on the next Mac
    reboot, signed into whatever the owner signed it into.

The reuse assertions are the other half. `frame_argv` and `browser_argv` must
be `screen.snapshot_argv` and `browser.chrome_argv` *placed inside* the
container, not retyped -- a second copy of the ffmpeg line is a second answer
to "which display do we grab", and one of them will read `:0`.

Nothing here runs Docker. Every test is an argv or a tmp_path; the live proof
is `-m live` in tests/test_sandbox_live.py.
"""

import pytest

from server import browser, sandbox, screen


# ── names ──────────────────────────────────────────────────────────────────


def test_a_hostile_desk_name_never_becomes_a_container_name():
    """Refused, not sanitised -- `browser`'s rule, reused rather than
    restated. `--name ../../etc` is a name Docker would take."""
    for bad in ("../../etc", "a/b", "/absolute", "", "-flag", "a" * 200):
        with pytest.raises(ValueError):
            sandbox.container_name(bad)
    assert sandbox.container_name("acme") == f"{sandbox.NAME_PREFIX}acme"


# ── the way in ─────────────────────────────────────────────────────────────


def test_the_desks_computer_publishes_no_port(tmp_path):
    """THE back-door detector.

    Presence, not a value: an argv that published 9222 on a *different* host
    port would pass a `"9222:9222" not in argv` test and be just as open.
    """
    argv = sandbox.create_argv("acme", home=tmp_path / "acme")
    for flag in ("-p", "--publish", "-P", "--publish-all",
                 "--network=host", "--net=host", "--privileged",
                 "--pid=host", "--ipc=host"):
        assert flag not in argv, f"{flag} is in the desk's docker run"
    assert not any(a.startswith("--publish") for a in argv)


def test_the_docker_socket_is_never_mounted_into_the_desk(tmp_path):
    """A container with the Docker socket is root on the Mac, not a sandbox."""
    argv = sandbox.create_argv("acme", home=tmp_path / "acme")
    joined = " ".join(argv)
    assert "docker.sock" not in joined
    assert "/var/run/docker" not in joined


def test_the_container_never_runs_as_root(tmp_path):
    """Stated on the run AND on every exec, never inherited from the image."""
    run = sandbox.create_argv("acme", home=tmp_path / "acme")
    assert "--user" in run
    uid = run[run.index("--user") + 1]
    assert uid == f"{sandbox.DESK_UID}:{sandbox.DESK_GID}"
    assert not uid.startswith("0:"), "uid 0 is root"

    ex = sandbox.exec_argv("acme", ["true"])
    assert "--user" in ex and ex[ex.index("--user") + 1] == uid


def test_the_container_keeps_no_capabilities_and_gains_no_privileges(tmp_path):
    argv = sandbox.create_argv("acme", home=tmp_path / "acme")
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in " ".join(argv)


def test_the_host_alias_is_pointed_at_the_containers_own_loopback(tmp_path):
    """Measured on this Mac: a default-bridge container resolves
    `host.docker.internal` and can GET the daemon's `/api/state`, which binds
    127.0.0.1 and is not authenticated. The name is blackholed here. The raw
    gateway address still works and that is written down in
    docs/the-agents-computer.md rather than papered over."""
    argv = sandbox.create_argv("acme", home=tmp_path / "acme")
    joined = " ".join(argv)
    assert "host.docker.internal:127.0.0.1" in joined
    assert "gateway.docker.internal:127.0.0.1" in joined


def test_the_container_does_not_come_back_by_itself(tmp_path):
    """A fired desk's browser must not restart on the next Mac reboot, signed
    into whatever the owner signed it into during a take-over."""
    argv = sandbox.create_argv("acme", home=tmp_path / "acme")
    assert "--restart" in argv
    assert argv[argv.index("--restart") + 1] == "no"


def test_the_image_is_pinned_and_never_latest(tmp_path):
    argv = sandbox.create_argv("acme", home=tmp_path / "acme")
    assert sandbox.IMAGE in argv
    assert not sandbox.IMAGE.endswith(":latest")
    assert ":" in sandbox.IMAGE, "an untagged image resolves to :latest"


# ── the home directory ─────────────────────────────────────────────────────


def test_the_home_directory_is_a_bind_mount_and_is_absolute(tmp_path):
    home = tmp_path / "acme"
    argv = sandbox.create_argv("acme", home=home)
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a in ("-v", "--volume")]
    assert mounts == [f"{home.resolve()}:{sandbox.DESK_HOME}"]
    assert mounts[0].startswith("/"), "a relative bind mount is Docker's cwd"


def test_a_home_inside_a_git_checkout_is_refused(tmp_path):
    """`browser.start` already refuses this for the profile: a branch switch
    deletes the cookies that are the point of persistence, and `git add -A`
    commits the session tokens in them. The container's whole home is the same
    hazard, one level up."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(browser.BrowserError) as exc:
        sandbox.create_argv("acme", home=repo / "home")
    assert exc.value.reason == "home_in_repo"


# ── input: the take-over path ──────────────────────────────────────────────


def test_typed_text_is_one_argument_and_never_a_shell_string():
    """The owner types a password into the panel during a take-over. Through
    a shell, a backtick in it runs a command inside the container."""
    nasty = "`id`; rm -rf / $(whoami) --window 1"
    argv = sandbox.type_argv("acme", nasty)
    assert nasty in argv, "the text was mangled or split"
    assert argv.index("--") < argv.index(nasty), "no -- before the text"
    assert "sh" not in argv and "bash" not in argv and "-c" not in argv


def test_a_key_that_is_really_an_option_is_refused():
    """`xdotool key --file /etc/passwd` is a file read wearing a keystroke."""
    for bad in ("--file", "-f", "--window", "a b", "", ";id", "x" * 80):
        with pytest.raises(ValueError):
            sandbox.key_argv("acme", bad)
    assert "Return" in sandbox.key_argv("acme", "Return")
    assert "ctrl+l" in sandbox.key_argv("acme", "ctrl+l")


def test_a_click_outside_the_screen_is_refused():
    """Coordinates arrive from a phone scaling a JPEG. Off-screen is a bug in
    the client; negative is a string that parsed as one."""
    for x, y in ((-1, 10), (10, -1), (99999, 10), (10, 99999)):
        with pytest.raises(ValueError):
            sandbox.click_argv("acme", x, y)
    argv = sandbox.click_argv("acme", 301, 301)
    assert "mousemove" in argv and "301" in argv and "click" in argv


def test_input_never_reaches_the_owners_own_display():
    """Every exec states DISPLAY. Inherited, a daemon started from a desktop
    session would type the owner's password into his own screen."""
    for argv in (sandbox.click_argv("acme", 5, 5), sandbox.type_argv("acme", "x"),
                 sandbox.key_argv("acme", "Return")):
        assert f"DISPLAY={sandbox.DISPLAY}" in argv
        assert "DISPLAY=:0" not in " ".join(argv)


# ── reuse, not restatement ─────────────────────────────────────────────────


def test_the_frame_command_is_the_screen_modules_own_argv():
    """A second copy of the x11grab line is a second answer to "which display
    do we grab", and one of them will read `:0`."""
    inner = screen.snapshot_argv(sandbox.DISPLAY, "pipe:1")
    argv = sandbox.frame_argv("acme")
    assert argv[-len(inner):] == inner
    assert argv[0] == "docker"


def test_the_browser_command_is_the_browser_modules_own_argv(tmp_path):
    """Including its banned-flag sweep: `--no-sandbox` must not appear because
    someone found it easier than fixing seccomp."""
    argv = sandbox.browser_argv("acme", url="https://example.com")
    joined = " ".join(argv)
    for flag in browser.BANNED_CHROME_FLAGS:
        assert flag not in joined
    assert f"--display={sandbox.DISPLAY}" in argv
    assert f"--user-data-dir={sandbox.DESK_PROFILE}" in argv
    assert "https://example.com" in argv


def test_the_browser_binary_is_the_one_in_the_image_not_the_debian_wrapper():
    """Measured: Debian's `/usr/bin/chromium` is a shell wrapper that appends
    its own flags (`--enable-remote-extensions`, `--load-extension=`). A
    security boundary asserted on our argv is not a boundary if a wrapper
    edits it, so the real binary is named."""
    argv = sandbox.browser_argv("acme", url="about:blank")
    assert sandbox.CHROMIUM in argv
    assert sandbox.CHROMIUM.startswith("/usr/lib/"), sandbox.CHROMIUM
