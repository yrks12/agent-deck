"""The other two thirds of the agent's computer: a terminal and a file tree.

Written before the routes exist.

`docs/client-api.md` §15 ends by saying the screen route will never grow a
`run` action, and that "the terminal and the file manager in the observed dock
are a separate surface". This is that surface, and these are the five ways
serving it would be worse than not serving it:

* **It becomes a shell on the Mac.** A route called "terminal" that reaches
  `subprocess.run` on the host is the owner's laptop, his keychain and his
  ssh agent, handed to whatever an agent pasted. Every command here is
  asserted to leave through `sandbox.exec_argv` -- `docker`, `exec`, the desk's
  own container name -- and `server/api.py` is asserted to contain no
  docker-shaped argv of its own.
* **A path becomes a command.** `path=` is attacker-adjacent: an agent's own
  prose reaches it, and prose contains `;`, backticks, `$(...)`, `|`, `&&` and
  newlines. The sweep below is over that *class*, not over one example, and
  what it asserts is the good signal -- the path arrives as exactly one argv
  element, with no shell anywhere in the argv to interpret it.
* **A stopped container answers with a traceback.** "That agent's computer is
  not running" is a sentence the panel can render and a button it can offer.
  A 500 is a spinner forever.
* **A hung command wedges the daemon.** `sleep 999` must come back as a
  timeout with a slug, on `sandbox.DOCKER_TIMEOUT`, not hold a worker thread.
* **A binary file renders as mojibake.** A JPEG decoded with `errors=replace`
  is a screenful of U+FFFD that looks like a corrupt text file. It is refused
  with its own slug instead.

Hermetic: the seam is `sandbox._run` -- the one function that actually
executes `docker` -- so nothing here starts a container, and every argv the
routes *would* have run is captured and asserted on.
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api as api_mod
from server import office, sandbox
from server.sources import comms as comms_mod

TOKEN = "t-secret-not-a-real-credential"
ACME = {"name": "acme", "cwd": "/tmp/p", "engine": "claude", "mission": "sell",
        "label": "Closer", "charter": "Own the deal.", "reports_to": None}

TERMINAL = "/v1/agents/acme/terminal"
FILES = "/v1/agents/acme/files"
READ = "/v1/agents/acme/files/read"

#: The class, not the instance. Every one of these is a legal byte in a Linux
#: filename *and* a shell operator, which is the whole point: they must reach
#: the container verbatim and must never be interpreted on the way.
SHELL_METACHARACTERS = [
    "/home/agent/a;id",
    "/home/agent/a`id`",
    "/home/agent/a$(id)",
    "/home/agent/a|id",
    "/home/agent/a&&id",
    "/home/agent/a&id",
    "/home/agent/a>out",
    "/home/agent/a<in",
    "/home/agent/a\nid",
    "/home/agent/a$HOME",
    "/home/agent/a'id'",
    '/home/agent/a"id"',
    "/home/agent/a*",
    "/home/agent/a\\id",
]


@pytest.fixture
def bus(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "MESSAGES_FILE", tmp_path / "messages.jsonl")
    monkeypatch.setattr(office, "BUS_DIR", tmp_path)
    (tmp_path / "messages.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def surface(bus):
    path = bus / "roster.json"
    path.write_text(json.dumps({"version": 1, "agents": [ACME]}))
    return api_mod.Surface(
        snapshot=lambda: {"generated_at": 0.0, "sessions": []},
        comms=comms_mod.CommsIndex(), roster_path=path,
        prefs_path=bus / "agent_prefs.json", asks_path=bus / "asks.json",
        rules_path=bus / "autoreview.json", routines_path=bus / "routines.json",
    )


@pytest.fixture
def client(surface, monkeypatch):
    monkeypatch.setenv(api_mod.TOKEN_ENV, TOKEN)
    app = FastAPI()
    api_mod.register(app, surface=surface, background=False)
    return TestClient(app)


class _Box:
    """A container that did not run, and every argv it would have been asked.

    Replies are chosen by the tool being exec'd, because one request makes two
    calls -- a `stat` to find out what the path *is*, then the `find` or the
    `head` -- and a queue of replies would silently pass if the order flipped.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.replies: dict[str, SimpleNamespace] = {}

    def reply(self, tool: str, *, code: int = 0, out: bytes = b"",
              err: bytes = b"") -> None:
        self.replies[tool] = SimpleNamespace(returncode=code, stdout=out,
                                             stderr=err)

    def __call__(self, argv, *, timeout=None):
        self.calls.append(list(argv))
        for tool in ("stat", "find", "head", sandbox.SHELL):
            if tool in argv:
                return self.replies.get(
                    tool, SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def argv_for(self, tool: str) -> list[str]:
        for argv in reversed(self.calls):
            if tool in argv:
                return argv
        raise AssertionError(f"nothing ran {tool}: {self.calls}")


@pytest.fixture
def computer_up(monkeypatch):
    """A desk whose computer is running. The seam is `_run`, not the module's
    own functions: stubbing `list_dir` would take the path check out with it,
    and the path check is what most of this file is about."""
    box = _Box()
    monkeypatch.setattr(sandbox, "_run", box)
    monkeypatch.setattr(sandbox, "is_up", lambda desk: True)
    box.reply("stat", out=b"directory\t4096\t1756820000\n")
    return box


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def dirent(kind: str, size: int, mtime: str, name: str) -> bytes:
    """One `find -printf` record, NUL-terminated, exactly as find writes it."""
    return f"{kind}\t{size}\t{mtime}\t{name}\0".encode()


# ── the token, first ───────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", [
    ("post", TERMINAL),
    ("get", FILES),
    ("get", READ + "?path=/home/agent/x.txt"),
])
def test_no_terminal_or_files_route_answers_without_the_bearer_token(
        client, method, path):
    """A shell and a file reader on the agent's machine must be no weaker than
    the route that merely lists desks."""
    response = client.request(method.upper(), path, json={"command": "id"})
    assert response.status_code == 401
    assert response.json()["reason"] == "unauthorized"


@pytest.mark.parametrize("method,path", [
    ("post", TERMINAL),
    ("get", FILES),
    ("get", READ + "?path=/home/agent/x.txt"),
])
def test_with_no_token_configured_the_terminal_is_closed_not_open(
        client, monkeypatch, method, path):
    """Fail closed, exactly as the rest of /v1 does. An unset env var must not
    be what decides whether a stranger gets a shell."""
    monkeypatch.delenv(api_mod.TOKEN_ENV, raising=False)
    response = client.request(method.upper(), path, headers=auth(),
                              json={"command": "id"})
    assert response.status_code == 503
    assert response.json()["reason"] == "auth_not_configured"


# ── the terminal ───────────────────────────────────────────────────────────


def test_a_command_he_types_runs_inside_that_desks_container(client, computer_up):
    """The single most important assertion in the file: `docker exec`, that
    desk's container, and the command as one argument to a shell *in there*."""
    computer_up.reply(sandbox.SHELL, out=b"uid=1000(agent)\n")
    response = client.post(TERMINAL, headers=auth(), json={"command": "id"})
    assert response.status_code == 200

    argv = computer_up.argv_for(sandbox.SHELL)
    assert argv[0] == "docker" and argv[1] == "exec"
    assert sandbox.container_name("acme") in argv
    assert argv[-3:] == [sandbox.SHELL, "-lc", "id"]
    assert "--user" in argv and f"{sandbox.DESK_UID}:{sandbox.DESK_GID}" in argv


def test_the_terminal_hands_back_the_exit_code_and_both_streams(
        client, computer_up):
    """A shell whose stderr is thrown away is a shell that says nothing went
    wrong. Both streams and the code, always."""
    computer_up.reply(sandbox.SHELL, code=2, out=b"partial\n", err=b"boom\n")
    body = client.post(TERMINAL, headers=auth(),
                       json={"command": "ls /nope"}).json()
    assert body["exit"] == 2
    assert body["stdout"] == "partial\n"
    assert body["stderr"] == "boom\n"
    assert body["truncated"] is False


def test_a_working_directory_travels_as_dockers_own_flag(client, computer_up):
    """`cwd` must not be spliced into the shell line as `cd <cwd> && ...`.
    That is the one place a directory name would become a command, so it goes
    to `docker exec --workdir` as its own argv element instead."""
    computer_up.reply(sandbox.SHELL, out=b"")
    response = client.post(TERMINAL, headers=auth(),
                           json={"command": "pwd", "cwd": "/home/agent/work"})
    assert response.status_code == 200
    argv = computer_up.argv_for(sandbox.SHELL)
    assert argv[argv.index("--workdir") + 1] == "/home/agent/work"
    assert argv[-1] == "pwd", "the shell line grew something that was not typed"


@pytest.mark.parametrize("hostile", SHELL_METACHARACTERS)
def test_a_working_directory_full_of_shell_operators_stays_one_argument(
        client, computer_up, hostile):
    """The class, swept. Each of these is a legal directory name and a shell
    operator; each must arrive as exactly one element and must never appear
    inside the string the shell is handed."""
    computer_up.reply(sandbox.SHELL, out=b"")
    response = client.post(TERMINAL, headers=auth(),
                           json={"command": "pwd", "cwd": hostile})
    assert response.status_code == 200
    argv = computer_up.argv_for(sandbox.SHELL)
    assert argv[argv.index("--workdir") + 1] == hostile
    assert argv[-1] == "pwd"


def test_an_empty_command_is_refused_and_nothing_runs(client, computer_up):
    response = client.post(TERMINAL, headers=auth(), json={"command": "   "})
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_input"
    assert computer_up.calls == [], "a refused command still ran something"


def test_a_relative_working_directory_is_refused_and_nothing_runs(
        client, computer_up):
    """An absolute path is what stops a value that starts with `-` being read
    as a flag by whatever it is handed to."""
    response = client.post(TERMINAL, headers=auth(),
                           json={"command": "pwd", "cwd": "--rm"})
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_path"
    assert computer_up.calls == []


def test_a_flood_of_output_is_cut_and_the_cut_says_so_in_the_text(
        client, computer_up):
    """Same ceiling and the same visible mark as `office.fit`: a client that
    never learns to read `truncated` must still see that something was cut."""
    computer_up.reply(sandbox.SHELL, out=b"x" * (office.RECORD_MAX + 5000))
    body = client.post(TERMINAL, headers=auth(),
                       json={"command": "yes"}).json()
    assert body["truncated"] is True
    assert office.CUT_MARK in body["stdout"]
    assert body["stdout"].startswith("x" * 100)


# ── the file tree ──────────────────────────────────────────────────────────


def test_the_listing_is_directories_first_then_by_name(client, computer_up):
    """Navigation order, not disk order. `find` returns whatever the directory
    happens to hold; a file browser that reorders on every refresh is unusable."""
    computer_up.reply("find", out=(
        dirent("f", 12, "1756820001.5", "zebra.txt")
        + dirent("d", 4096, "1756820002.0", "work")
        + dirent("f", 3, "1756820003.0", "alpha.md")
        + dirent("d", 4096, "1756820004.0", "archive")
    ))
    body = client.get(FILES, headers=auth(),
                      params={"path": "/home/agent"}).json()
    assert body["path"] == "/home/agent"
    assert [e["name"] for e in body["entries"]] == [
        "archive", "work", "alpha.md", "zebra.txt"]
    assert [e["kind"] for e in body["entries"]] == ["dir", "dir", "file", "file"]
    assert body["entries"][-1]["size"] == 12
    assert body["entries"][-1]["modified"] == pytest.approx(1756820001.5)


def test_the_listing_defaults_to_the_agents_own_home(client, computer_up):
    """No `path` is the panel's first paint. It must open somewhere real."""
    computer_up.reply("find", out=dirent("f", 1, "1756820000.0", "notes.md"))
    body = client.get(FILES, headers=auth()).json()
    assert body["path"] == sandbox.DESK_HOME
    assert sandbox.DESK_HOME in computer_up.argv_for("find")


def test_a_file_where_a_directory_was_asked_for_says_which(client, computer_up):
    computer_up.reply("stat", out=b"regular file\t12\t1756820000\n")
    response = client.get(FILES, headers=auth(),
                          params={"path": "/home/agent/x.txt"})
    assert response.status_code == 409
    assert response.json()["reason"] == "not_a_directory"


def test_a_path_that_is_not_there_is_a_404_not_an_empty_directory(
        client, computer_up):
    """An empty list and a missing directory look identical in a panel, and
    they are opposite facts about whether the agent did any work."""
    computer_up.reply("stat", code=1, err=b"stat: cannot statx '/nope'\n")
    response = client.get(FILES, headers=auth(), params={"path": "/nope"})
    assert response.status_code == 404
    assert response.json()["reason"] == "no_such_path"


@pytest.mark.parametrize("hostile", SHELL_METACHARACTERS)
def test_a_path_full_of_shell_operators_reaches_find_as_one_argument(
        client, computer_up, hostile):
    """The sweep. `path` is reachable from an agent's own prose, so every
    shell operator is tried, and the good signal is asserted: the path is one
    element of an argv, and there is no shell in that argv to read it."""
    computer_up.reply("find", out=b"")
    response = client.get(FILES, headers=auth(), params={"path": hostile})
    assert response.status_code == 200

    for argv in computer_up.calls:
        assert argv.count(hostile) == 1, f"{hostile!r} was split or repeated"
        assert sandbox.SHELL not in argv and "sh" not in argv
        assert "-c" not in argv, "a listing went through a shell"


@pytest.mark.parametrize("bad", ["home/agent", "--rm", "", "-name"])
def test_a_path_that_is_not_absolute_is_refused_before_anything_runs(
        client, computer_up, bad):
    """`find -name ...` is an option wearing a path. Absolute-or-refused is
    what makes the leading `-` impossible rather than merely unlikely."""
    response = client.get(FILES, headers=auth(), params={"path": bad})
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_path"
    assert computer_up.calls == []


# ── reading a file ─────────────────────────────────────────────────────────


def test_a_text_file_comes_back_whole(client, computer_up):
    computer_up.reply("stat", out=b"regular file\t6\t1756820000\n")
    computer_up.reply("head", out="héllo\n".encode())
    body = client.get(READ, headers=auth(),
                      params={"path": "/home/agent/x.txt"}).json()
    assert body["path"] == "/home/agent/x.txt"
    assert body["text"] == "héllo\n"
    assert body["truncated"] is False
    assert "--" in computer_up.argv_for("head"), "head can read a file called -n"


def test_a_long_file_is_cut_at_the_ceiling_with_a_visible_mark(
        client, computer_up):
    """64 000 bytes, the same number and the same mark as `office.RECORD_MAX`,
    so there is one answer on this machine to "how much is too much"."""
    computer_up.reply("stat", out=b"regular file\t999999\t1756820000\n")
    computer_up.reply("head", out=b"a" * (sandbox.READ_MAX + 1))
    body = client.get(READ, headers=auth(),
                      params={"path": "/home/agent/big.log"}).json()
    assert body["truncated"] is True
    assert body["text"].startswith("a" * sandbox.READ_MAX)
    assert office.CUT_MARK in body["text"]


def test_the_read_asks_for_one_byte_past_the_ceiling(client, computer_up):
    """How it knows it was cut. Asking for exactly the ceiling cannot tell a
    64 000-byte file from a 2 MB one, so it reads one more and looks."""
    computer_up.reply("stat", out=b"regular file\t10\t1756820000\n")
    computer_up.reply("head", out=b"ok")
    client.get(READ, headers=auth(), params={"path": "/home/agent/x.txt"})
    argv = computer_up.argv_for("head")
    assert str(sandbox.READ_MAX + 1) in argv


def test_a_binary_file_is_refused_cleanly_rather_than_shown_as_mojibake(
        client, computer_up):
    """A JPEG through `errors='replace'` is a screen of U+FFFD that reads like
    a corrupt text file. Its own slug, and a status a client can branch on."""
    computer_up.reply("stat", out=b"regular file\t900\t1756820000\n")
    computer_up.reply("head", out=b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x00\x01")
    response = client.get(READ, headers=auth(),
                          params={"path": "/home/agent/shot.jpg"})
    assert response.status_code == 415
    assert response.json()["reason"] == "not_text"


def test_a_file_of_nul_bytes_is_binary_even_though_it_decodes(
        client, computer_up):
    """Sweeping the class rather than the instance: "invalid UTF-8" is only
    one way to be binary. A NUL byte decodes fine and is still not text."""
    computer_up.reply("stat", out=b"regular file\t900\t1756820000\n")
    computer_up.reply("head", out=b"MZ\x00\x00\x90\x00hello")
    response = client.get(READ, headers=auth(),
                          params={"path": "/home/agent/a.exe"})
    assert response.status_code == 415
    assert response.json()["reason"] == "not_text"


def test_a_multibyte_character_split_by_the_ceiling_is_not_called_binary(
        client, computer_up):
    """The false positive the NUL/UTF-8 rule invites: cutting at a byte count
    lands mid-character, and a truncated UTF-8 file must not be reported as a
    JPEG. The tail is trimmed, not the file rejected.

    A three-byte character, deliberately: 64 000 is a whole number of two-byte
    characters, so `é` would never land mid-character and this test would pass
    against code that has no trim in it at all.
    """
    computer_up.reply("stat", out=b"regular file\t999999\t1756820000\n")
    blob = ("€" * sandbox.READ_MAX).encode()[:sandbox.READ_MAX + 1]
    assert sandbox.READ_MAX % 3, "pick a character the ceiling actually splits"
    computer_up.reply("head", out=blob)
    response = client.get(READ, headers=auth(),
                          params={"path": "/home/agent/notes.md"})
    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is True
    assert body["text"].startswith("€")


def test_a_directory_asked_for_as_a_file_says_which(client, computer_up):
    computer_up.reply("stat", out=b"directory\t4096\t1756820000\n")
    response = client.get(READ, headers=auth(), params={"path": "/home/agent"})
    assert response.status_code == 409
    assert response.json()["reason"] == "not_a_file"


# ── when the machine is not there ──────────────────────────────────────────


@pytest.mark.parametrize("method,path", [
    ("post", TERMINAL), ("get", FILES), ("get", READ + "?path=/home/agent/x")])
def test_a_stopped_computer_is_a_sentence_the_app_can_render(
        client, monkeypatch, method, path):
    """Not a 500. "That agent's computer is not running" is a slug the panel
    turns into a line of text and an offer to start it."""
    box = _Box()
    box.reply("stat", code=1, err=b"Error response from daemon: not running")
    box.reply(sandbox.SHELL, code=1,
              err=b"Error response from daemon: not running")
    monkeypatch.setattr(sandbox, "_run", box)
    monkeypatch.setattr(sandbox, "is_up", lambda desk: False)

    response = client.request(method.upper(), path, headers=auth(),
                              json={"command": "id"})
    assert response.status_code == 409
    assert response.json()["reason"] == "computer_not_running"


@pytest.mark.parametrize("method,path", [
    ("post", TERMINAL), ("get", FILES), ("get", READ + "?path=/home/agent/x")])
def test_docker_being_off_is_its_own_answer_not_a_dead_panel(
        client, monkeypatch, method, path):
    def _no_docker(argv, *, timeout=None):
        raise sandbox.SandboxError("docker_unavailable", "not on PATH")

    monkeypatch.setattr(sandbox, "_run", _no_docker)
    response = client.request(method.upper(), path, headers=auth(),
                              json={"command": "id"})
    assert response.status_code == 503
    assert response.json()["reason"] == "docker_unavailable"


def test_a_command_that_hangs_comes_back_as_a_timeout(client, monkeypatch):
    """`sleep 999` must not hold the request open. `sandbox.DOCKER_TIMEOUT` is
    the one clock, so a future change to it moves this too."""
    seen: list[float] = []

    def _hang(argv, *, timeout=None):
        seen.append(timeout)
        raise sandbox.SandboxError("computer_not_responding",
                                   f"docker did not answer in {timeout}s")

    monkeypatch.setattr(sandbox, "_run", _hang)
    response = client.post(TERMINAL, headers=auth(),
                           json={"command": "sleep 999"})
    assert response.status_code == 504
    assert response.json()["reason"] == "computer_not_responding"
    assert seen and seen[0] == sandbox.DOCKER_TIMEOUT


@pytest.mark.parametrize("method,path", [
    ("post", "/v1/agents/ghost/terminal"),
    ("get", "/v1/agents/ghost/files"),
    ("get", "/v1/agents/ghost/files/read?path=/home/agent/x")])
def test_a_desk_that_is_not_on_the_roster_never_names_a_container(
        client, computer_up, method, path):
    response = client.request(method.upper(), path, headers=auth(),
                              json={"command": "id"})
    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_agent"
    assert computer_up.calls == []


# ── one door to Docker, structurally ───────────────────────────────────────


def _docker_words(source: str) -> set[str]:
    """Every function in `source` that writes the word `docker` into a list.

    Parsed rather than grepped, so a second `docker` argv added anywhere shows
    up here without anyone remembering to update a list.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and child.value == "docker":
                found.add(node.name)
    return found


def test_the_api_layer_builds_no_docker_command_of_its_own():
    """The requirement, asserted structurally rather than trusted. `api.py` is
    the wire; the machine is `sandbox.py`, and a second `docker exec` built in
    a route handler is how the uid and the display stop being stated."""
    names = _docker_words(Path(api_mod.__file__).read_text())
    assert names == set(), f"api.py builds docker argv in {sorted(names)}"


def test_only_the_known_functions_in_sandbox_speak_docker():
    """`exec_argv` stays the portability seam. Everything new -- the terminal,
    the listing, the read -- must go through it, so swapping six words for
    `ssh box` still moves the whole module."""
    names = _docker_words(Path(sandbox.__file__).read_text())
    assert names == {"create_argv", "exec_argv", "is_up", "start", "stop"}, (
        f"a new door to Docker: {sorted(names)}")


def test_every_slug_this_surface_can_return_is_in_the_client_doc():
    """A client that meets an undocumented slug shows its generic fallback for
    a specific cause, forever."""
    doc = (Path(__file__).resolve().parent.parent
           / "docs" / "client-api.md").read_text()
    for slug in ("bad_path", "no_such_path", "not_a_directory", "not_a_file",
                 "not_text", "read_failed", "list_failed"):
        assert f"`{slug}`" in doc, f"undocumented slug: {slug}"
    for route in ("/v1/agents/{name}/terminal", "/v1/agents/{name}/files",
                  "/v1/agents/{name}/files/read"):
        assert route in doc, f"undocumented route: {route}"
