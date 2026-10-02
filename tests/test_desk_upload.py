"""A desk can put a file into a web page's upload -- never "my browser can't".

LIVE, 2026-10-01: desk a growth desk told the owner "My browser can't upload
the avatar or posts ... The permanent fix is a file-upload tool in our
browsers." Owner: "why can't they, they should be able to." The computer had
navigate/click/type and no way to hand a file to an <input type=file>: the
native picker is a GTK dialog the desk cannot see into, and the file lived on
the box while Chromium runs in the desk's container.

So `upload_file`:
* copies the file into the container-visible home (`/home/agent`, a bind
  mount of `~/.claude/agent-bus/browser/computers/<desk>` on the box);
* sets it on the input over CDP `DOM.setFileInputFiles` -- found by selector,
  or by intercepting the file chooser a click on an "Upload" button opens
  (`Page.setInterceptFileChooserDialog` + `Page.fileChooserOpened`);
* or drops it on a drop zone (`Input.dispatchDragEvent` with files).

Hermetic: the one fake is the CDP socket.
"""

import json
from pathlib import Path

import pytest

from server import browser, computer_mcp, desk_computer, desk_upload, sandbox

DESK = "acme"


class FakeSocket:
    """Answers CDP commands from `answers`; `on_click` events are queued by
    the test's click callback, as Chromium would emit them."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.sent = []
        self.inbox = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        answer = self.answers.get(msg["method"], {})
        if callable(answer):
            answer = answer(msg["params"])
        self.inbox.append(json.dumps({"id": msg["id"], "result": answer}))

    def recv(self, timeout=None):
        if not self.inbox:
            raise sandbox.SandboxError("computer_not_responding", "quiet")
        return self.inbox.pop(0)

    def emit(self, method, params):
        self.inbox.append(json.dumps({"method": method, "params": params}))

    def methods(self):
        return [m["method"] for m in self.sent]

    def params(self, method):
        return [m["params"] for m in self.sent if m["method"] == method]


def a_driver(sock):
    driver = desk_computer.ContainerCDP(DESK)
    driver._connect = lambda: sock
    return driver


FILES = ["/home/agent/uploads/ab12/avatar.png"]

_NAMES = {"result": {"type": "object", "value": ["avatar.png"]}}


# ── by selector ─────────────────────────────────────────────────────────────


def test_a_selector_sets_the_files_on_that_input_and_reads_them_back():
    sock = FakeSocket({
        "Runtime.evaluate": {"result": {"type": "object", "subtype": "node",
                                        "objectId": "obj-1"}},
        "Runtime.callFunctionOn": _NAMES,
    })
    names = desk_upload.set_files_by_selector(a_driver(sock), FILES,
                                              "input#avatar")
    assert names == ["avatar.png"]
    assert sock.methods() == ["Runtime.evaluate", "DOM.setFileInputFiles",
                              "Runtime.callFunctionOn"]
    assert "input#avatar" in sock.params("Runtime.evaluate")[0]["expression"]
    assert sock.params("Runtime.evaluate")[0].get("returnByValue") is not True
    assert sock.params("DOM.setFileInputFiles") == [
        {"files": FILES, "objectId": "obj-1"}]
    assert sock.params("Runtime.callFunctionOn")[0]["objectId"] == "obj-1"


def test_no_selector_means_the_pages_first_file_input():
    sock = FakeSocket({
        "Runtime.evaluate": {"result": {"type": "object", "objectId": "o"}},
        "Runtime.callFunctionOn": _NAMES})
    desk_upload.set_files_by_selector(a_driver(sock), FILES, "")
    assert "input[type=file]" in sock.params("Runtime.evaluate")[0]["expression"]


def test_a_selector_that_finds_nothing_sets_nothing():
    sock = FakeSocket({"Runtime.evaluate": {"result": {"type": "object",
                                                       "subtype": "null"}}})
    with pytest.raises(browser.BrowserError) as err:
        desk_upload.set_files_by_selector(a_driver(sock), FILES, "#nope")
    assert err.value.reason == "no_file_input"
    assert "DOM.setFileInputFiles" not in sock.methods()


# ── by clicking an Upload button: the file chooser is intercepted ──────────


def test_a_click_opens_the_chooser_and_the_deck_fills_it():
    sock = FakeSocket({"DOM.resolveNode": {"object": {"objectId": "o-42"}},
                       "Runtime.callFunctionOn": _NAMES})
    order = []

    def click():
        order.append(list(sock.methods()))
        sock.emit("Page.fileChooserOpened", {"frameId": "F", "mode":
                                             "selectSingle",
                                             "backendNodeId": 42})

    names = desk_upload.set_files_by_chooser(a_driver(sock), FILES, click)
    assert names == ["avatar.png"]
    # Interception is on BEFORE the click, on the same connection.
    assert order == [["Page.enable", "Page.setInterceptFileChooserDialog"]]
    assert sock.params("Page.setInterceptFileChooserDialog")[0] == {
        "enabled": True}
    assert sock.params("DOM.setFileInputFiles") == [
        {"files": FILES, "backendNodeId": 42}]
    assert sock.params("Page.setInterceptFileChooserDialog")[-1] == {
        "enabled": False}


def test_a_single_file_chooser_gets_one_file():
    sock = FakeSocket({"DOM.resolveNode": {"object": {"objectId": "o"}},
                       "Runtime.callFunctionOn": _NAMES})
    two = FILES + ["/home/agent/uploads/cd34/b.png"]
    desk_upload.set_files_by_chooser(a_driver(sock), two, lambda: sock.emit(
        "Page.fileChooserOpened", {"mode": "selectSingle",
                                   "backendNodeId": 7}))
    assert sock.params("DOM.setFileInputFiles")[0]["files"] == FILES


def test_a_click_that_opens_no_chooser_says_so_and_sets_nothing():
    sock = FakeSocket()
    with pytest.raises(browser.BrowserError) as err:
        desk_upload.set_files_by_chooser(a_driver(sock), FILES, lambda: None,
                                         wait=0.05)
    assert err.value.reason == "no_file_chooser"
    assert "DOM.setFileInputFiles" not in sock.methods()


# ── drop zones ──────────────────────────────────────────────────────────────


def test_drop_drags_the_files_onto_the_target_centre():
    sock = FakeSocket({"Runtime.evaluate": {"result": {
        "type": "object", "value": {"x": 200, "y": 300}}}})
    desk_upload.drop_files(a_driver(sock), FILES, ".dropzone")
    drags = sock.params("Input.dispatchDragEvent")
    assert [d["type"] for d in drags] == ["dragEnter", "dragOver", "drop"]
    for d in drags:
        assert (d["x"], d["y"]) == (200, 300)
        assert d["data"]["files"] == FILES
        assert d["data"]["dragOperationsMask"] >= 1


# ── the file must be visible INSIDE the container ──────────────────────────


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "computers" / DESK
    h.mkdir(parents=True)
    return h


def test_a_box_file_is_copied_into_the_mounted_home(home, tmp_path):
    src = tmp_path / "work" / "avatar B-teal.png"
    src.parent.mkdir()
    src.write_bytes(b"\x89PNG fake")
    inside = desk_upload.stage(src, home=home)
    assert inside.startswith("/home/agent/uploads/")
    assert inside.endswith("/avatar B-teal.png")  # the site shows the name
    copied = home / inside[len("/home/agent/"):]
    assert copied.read_bytes() == b"\x89PNG fake"
    assert desk_upload.stage(src, home=home) == inside  # same file, same place


def test_a_file_already_in_the_home_is_not_copied(home):
    (home / "shots").mkdir()
    (home / "shots" / "a.png").write_bytes(b"x")
    assert desk_upload.stage(home / "shots" / "a.png", home=home) == \
        "/home/agent/shots/a.png"
    assert desk_upload.stage("/home/agent/shots/a.png", home=home) == \
        "/home/agent/shots/a.png"
    assert not (home / "uploads").exists()


@pytest.mark.parametrize("bad", ["/home/agent/../../etc/passwd",
                                 "/home/agent/missing.png"])
def test_a_container_path_must_exist_inside_the_home(home, bad):
    with pytest.raises(ValueError):
        desk_upload.stage(bad, home=home)


def test_a_missing_file_or_a_directory_is_refused(home, tmp_path):
    with pytest.raises(ValueError):
        desk_upload.stage(tmp_path / "nope.png", home=home)
    with pytest.raises(ValueError):
        desk_upload.stage(tmp_path, home=home)


@pytest.mark.parametrize("rel", [".ssh/id_ed25519", "proj/.env",
                                 ".claude/.credentials.json"])
def test_secrets_never_leave_for_a_website(home, tmp_path, rel):
    src = tmp_path / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("secret")
    with pytest.raises(PermissionError):
        desk_upload.stage(src, home=home)


# ── the tool ────────────────────────────────────────────────────────────────


def test_upload_file_is_a_tool_with_only_paths_required():
    tools = computer_mcp.handle(DESK, {"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/list"})["result"]
    tool = next(t for t in tools["tools"] if t["name"] == "upload_file")
    assert tool["inputSchema"]["required"] == ["paths"]
    assert {"paths", "selector", "x", "y", "drop"} <= set(
        tool["inputSchema"]["properties"])


def test_the_tool_stages_then_sets_on_its_own_desk(monkeypatch, home, tmp_path):
    src = tmp_path / "post.jpg"
    src.write_bytes(b"jpg")
    seen = {}
    monkeypatch.setattr(desk_upload, "home_of", lambda desk: home)
    monkeypatch.setattr(desk_computer, "_guard", lambda desk: None)
    monkeypatch.setattr(desk_computer, "ensure", lambda desk: seen.setdefault(
        "desk", desk) and "driver")
    monkeypatch.setattr(desk_upload, "set_files_by_selector",
                        lambda drv, files, sel: seen.update(files=files,
                                                            sel=sel)
                        or ["post.jpg"])
    reply = computer_mcp.handle(DESK, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "upload_file", "arguments": {
            "paths": [str(src)], "selector": "input[type=file]",
            "desk": "harbor"}}})
    assert reply["result"]["isError"] is False
    assert seen["desk"] == DESK
    assert seen["files"][0].startswith("/home/agent/uploads/")
    assert "post.jpg" in reply["result"]["content"][0]["text"]


def test_the_tool_clicks_through_the_owners_click_path_for_a_point(
        monkeypatch, home, tmp_path):
    src = tmp_path / "a.png"
    src.write_bytes(b"png")
    clicks = []
    monkeypatch.setattr(desk_upload, "home_of", lambda desk: home)
    monkeypatch.setattr(desk_computer, "_guard", lambda desk: None)
    monkeypatch.setattr(desk_computer, "ensure", lambda desk: "driver")
    monkeypatch.setattr(sandbox, "send_input",
                        lambda desk, action: clicks.append((desk, action)))

    def chooser(drv, files, click, **kw):
        click()
        return ["a.png"]

    monkeypatch.setattr(desk_upload, "set_files_by_chooser", chooser)
    text = desk_upload.upload_file(DESK, [str(src)], x=640, y=400)
    assert clicks == [(DESK, {"action": "click", "x": 640, "y": 400})]
    assert "a.png" in text
