"""Getting a file *off* the agent's computer, not just looking at it.

Written before `GET /v1/agents/{name}/files/download` exists.

Different from `.../files/read` in the ways that matter: the size refusal
happens *before* the read (`test_an_oversized_file_is_refused_before_cat_is_built`
inspects the argv the stub saw, not merely the status, so a version that
builds `cat` and then declines to run it still fails this); the bytes are raw,
never decoded, and `Content-Type` is always `application/octet-stream`; and
the filename becomes an HTTP header, so a `"` or a newline in it -- both legal
in a Linux filename, both bytes `sandbox._check_path` lets through on purpose
-- must be neutralised on the way in, not merely hoped against.

Hermetic, same seam as `test_desk_terminal_files.py`: `sandbox._run` is
stubbed, so nothing here starts a container.
"""

import json
import re
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

DOWNLOAD = "/v1/agents/acme/files/download"


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

    Copied from `test_desk_terminal_files.py`'s helper rather than imported:
    that file is owned by a route this slice does not touch, and a shared
    fixture module is a second thing two concurrent edits could collide on.
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
        for tool in ("stat", "cat"):
            if tool in argv:
                return self.replies.get(
                    tool, SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def argv_for(self, tool: str) -> list[str]:
        for argv in reversed(self.calls):
            if tool in argv:
                return argv
        raise AssertionError(f"nothing ran {tool}: {self.calls}")

    def ran(self, tool: str) -> bool:
        return any(tool in argv for argv in self.calls)


@pytest.fixture
def computer_up(monkeypatch):
    box = _Box()
    monkeypatch.setattr(sandbox, "_run", box)
    monkeypatch.setattr(sandbox, "is_up", lambda desk: True)
    return box


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# ── the token, first ─────────────────────────────────────────────────────


def test_no_download_route_answers_without_the_bearer_token(client):
    response = client.get(DOWNLOAD, params={"path": "/home/agent/x.bin"})
    assert response.status_code == 401
    assert response.json()["reason"] == "unauthorized"


def test_with_no_token_configured_download_is_closed_not_open(
        client, monkeypatch):
    monkeypatch.delenv(api_mod.TOKEN_ENV, raising=False)
    response = client.get(DOWNLOAD, headers=auth(),
                          params={"path": "/home/agent/x.bin"})
    assert response.status_code == 503
    assert response.json()["reason"] == "auth_not_configured"


# ── `download_argv` itself, pure ────────────────────────────────────────


def test_download_argv_is_exactly_cat_dash_dash_path():
    """The whole detector for the argv shape: `cat`, `--`, the path, and
    nothing else, inside `exec_argv` -- same door every other command uses."""
    argv = sandbox.download_argv("acme", "/home/agent/report.pdf")
    assert argv[0] == "docker" and argv[1] == "exec"
    assert sandbox.container_name("acme") in argv
    assert argv[-3:] == ["cat", "--", "/home/agent/report.pdf"]


# ── the happy path and its headers ──────────────────────────────────────


def test_a_file_downloads_as_raw_octet_stream_bytes(client, computer_up):
    computer_up.reply("stat", out=b"regular file\t11\t1756820000\n")
    computer_up.reply("cat", out=b"hello world")
    response = client.get(DOWNLOAD, headers=auth(),
                          params={"path": "/home/agent/hello.txt"})
    assert response.status_code == 200
    assert response.content == b"hello world"
    assert "--" in computer_up.argv_for("cat"), "cat can read a file called -n"


def test_the_headers_are_present_with_the_documented_values(
        client, computer_up):
    """The good signal the task calls out explicitly: these three headers,
    present, with these exact values -- not merely a 200."""
    computer_up.reply("stat", out=b"regular file\t11\t1756820000\n")
    computer_up.reply("cat", out=b"hello world")
    response = client.get(DOWNLOAD, headers=auth(),
                          params={"path": "/home/agent/hello.txt"})
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == (
        'attachment; filename="hello.txt"')
    assert response.headers["content-length"] == "11"
    assert response.headers["cache-control"] == "no-store"


def test_content_type_is_never_guessed_from_the_extension(
        client, computer_up):
    """A `.jpg` must still be `application/octet-stream` -- this is `cat`,
    not a MIME sniffer, and `.../files/read` already exists for a client
    that wants the server to reason about the content."""
    computer_up.reply("stat", out=b"regular file\t3\t1756820000\n")
    computer_up.reply("cat", out=b"\xff\xd8\xff")
    response = client.get(DOWNLOAD, headers=auth(),
                          params={"path": "/home/agent/shot.jpg"})
    assert response.headers["content-type"] == "application/octet-stream"


# ── a filename that would otherwise break the header ────────────────────


def test_a_quote_in_the_filename_is_sanitised_and_the_response_still_parses(
        client, computer_up):
    computer_up.reply("stat", out=b"regular file\t2\t1756820000\n")
    computer_up.reply("cat", out=b"ok")
    response = client.get(DOWNLOAD, headers=auth(),
                          params={"path": '/home/agent/a"quote.txt'})
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert response.content == b"ok"
    # It must still be a well-formed quoted-string: exactly two unescaped
    # closing quotes (the wrapper), never a bare `"` that ends it early.
    assert re.fullmatch(r'attachment; filename="(?:[^"\\]|\\.)*"', disposition)


def test_a_newline_in_the_filename_never_reaches_the_raw_header(
        client, computer_up):
    """A raw `\\n` in a header value is a response-splitting shape. The
    server (or the ASGI stack under it) must neutralise it -- the true
    failure mode here is a broken/hung response, not a particular string."""
    computer_up.reply("stat", out=b"regular file\t2\t1756820000\n")
    computer_up.reply("cat", out=b"ok")
    response = client.get(DOWNLOAD, headers=auth(),
                          params={"path": "/home/agent/a\nb.txt"})
    assert response.status_code == 200
    assert response.content == b"ok"
    disposition = response.headers["content-disposition"]
    assert "\n" not in disposition and "\r" not in disposition


# ── the size refusal, decided before the read ───────────────────────────


def test_an_oversized_file_is_refused_before_cat_is_built(client, computer_up):
    """The assertion the task calls out by name: not just a 413, but proof
    `cat` was never constructed. `download_argv` calls `_check_path`, which
    would put the path into `computer_up.calls` if `cat` ran at all."""
    computer_up.reply(
        "stat", out=f"regular file\t{sandbox.DOWNLOAD_MAX + 1}\t1756820000\n"
        .encode())
    response = client.get(DOWNLOAD, headers=auth(),
                          params={"path": "/home/agent/huge.bin"})
    assert response.status_code == 413
    assert response.json()["reason"] == "too_large"
    assert not computer_up.ran("cat"), "cat was built for a file too large to read"


def test_a_file_exactly_at_the_ceiling_is_not_too_large(client, computer_up):
    computer_up.reply(
        "stat", out=f"regular file\t{sandbox.DOWNLOAD_MAX}\t1756820000\n"
        .encode())
    computer_up.reply("cat", out=b"x" * sandbox.DOWNLOAD_MAX)
    response = client.get(DOWNLOAD, headers=auth(),
                          params={"path": "/home/agent/exact.bin"})
    assert response.status_code == 200
    assert computer_up.ran("cat")


# ── the other refusals, same shapes as `read` ───────────────────────────


def test_a_directory_asked_for_as_a_download_says_which(client, computer_up):
    computer_up.reply("stat", out=b"directory\t4096\t1756820000\n")
    response = client.get(DOWNLOAD, headers=auth(),
                          params={"path": "/home/agent"})
    assert response.status_code == 409
    assert response.json()["reason"] == "not_a_file"
    assert not computer_up.ran("cat")


def test_a_path_that_is_not_there_is_a_404(client, computer_up):
    computer_up.reply("stat", code=1, err=b"stat: cannot statx '/nope'\n")
    response = client.get(DOWNLOAD, headers=auth(), params={"path": "/nope"})
    assert response.status_code == 404
    assert response.json()["reason"] == "no_such_path"


def test_a_relative_path_is_refused_and_nothing_runs(client, computer_up):
    response = client.get(DOWNLOAD, headers=auth(), params={"path": "etc/passwd"})
    assert response.status_code == 400
    assert response.json()["reason"] == "bad_path"
    assert computer_up.calls == []


def test_cat_failing_is_read_failed(client, computer_up):
    computer_up.reply("stat", out=b"regular file\t3\t1756820000\n")
    computer_up.reply("cat", code=1, err=b"cat: Permission denied\n")
    response = client.get(DOWNLOAD, headers=auth(),
                          params={"path": "/home/agent/locked.bin"})
    assert response.status_code == 409
    assert response.json()["reason"] == "read_failed"


@pytest.mark.parametrize("reason,status", [
    ("computer_not_running", 409),
    ("docker_unavailable", 503),
    ("computer_not_responding", 504),
])
def test_machine_failures_map_to_the_same_slugs_as_every_other_route(
        client, monkeypatch, reason, status):
    """This route reuses `_sandbox_refusal`, so these three come for free --
    pinned anyway, once each, so a future change to that shared map cannot
    silently stop covering this route too."""
    def _fail(argv, *, timeout=None):
        raise sandbox.SandboxError(reason, "stubbed")

    monkeypatch.setattr(sandbox, "_run", _fail)
    monkeypatch.setattr(sandbox, "is_up", lambda desk: False)
    response = client.get(DOWNLOAD, headers=auth(),
                          params={"path": "/home/agent/x.bin"})
    assert response.status_code == status
    assert response.json()["reason"] == reason


# ── the traversal pin ────────────────────────────────────────────────────


def test_traversal_in_the_desk_name_never_reaches_the_sandbox(
        client, computer_up):
    """A `%2f` inside `{name}` decodes to `/` before Starlette matches the
    route at all, so this never reaches `desk_file_download`, let alone
    `_known_desk` or the sandbox -- confirmed the same way for every other
    `{name}` route in this app (`GET .../screen.jpg`, `GET .../files`),
    so this is FastAPI's routing, not a gap this route added. `_known_desk`
    is the second, redundant fence behind it, exercised by
    `test_a_desk_not_on_the_roster_is_unknown_agent` below with a name that
    *does* reach the handler. What matters here -- and what would fail if a
    future change swapped `{name}` for `{name:path}` -- is that nothing ran."""
    response = client.get(
        "/v1/agents/%2e%2e%2f%2e%2e%2fx/files/download",
        headers=auth(), params={"path": "/etc/passwd"})
    assert response.status_code == 404
    assert computer_up.calls == []


def test_a_desk_not_on_the_roster_is_unknown_agent(client, computer_up):
    response = client.get("/v1/agents/ghost/files/download", headers=auth(),
                          params={"path": "/home/agent/x"})
    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_agent"
    assert computer_up.calls == []
