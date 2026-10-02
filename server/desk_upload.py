"""A desk puts files into a web page's upload: avatars, posts, documents.

LIVE, 2026-10-01: a growth desk told the owner "My browser can't
upload the avatar or posts ... The permanent fix is a file-upload tool in our
browsers." The owner: "why can't they, they should be able to." Two things
stood in the way, and this module removes both:

* **The picker.** Clicking "Upload" opens a native file dialog, which the desk
  cannot read or type into reliably. Over CDP the deck sets the files on the
  `<input type=file>` itself (`DOM.setFileInputFiles`) -- found by selector,
  or by intercepting the chooser a click opens
  (`Page.setInterceptFileChooserDialog` + `Page.fileChooserOpened`), so no
  dialog appears at all. Drop zones get `Input.dispatchDragEvent` with files.
* **Where the file is.** The desk's Claude and its workspace are on the box;
  Chromium runs in the desk's container and sees only the container's
  filesystem. The container's `/home/agent` is a bind mount of the desk's
  computer home on the box (`docker inspect`, MEASURED: `~/.claude/agent-bus/
  browser/computers/<desk> -> /home/agent`), so a file is copied into
  `<home>/uploads/<hash>/<its own name>` -- the site shows the name, so it is
  kept -- and handed to Chromium as `/home/agent/uploads/<hash>/<name>`.

What it refuses: a file that is a key or a credential (`~/.ssh`, `.env`, the
CLI's `.credentials.json`, the deck vault). The desk has a shell on the box
and could read them anyway; this tool is just never the road that carries
them to a website.

Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Callable

from . import browser, desk_computer, sandbox, vault

UPLOAD_DIR = "uploads"
MAX_FILES = 20
MAX_BYTES = 1024 * 1024 * 1024  # a long Reel is hundreds of MB; 1 GiB is plenty
CHOOSER_WAIT = 6.0

_SECRET_DIRS = frozenset({".ssh", ".gnupg", ".aws", ".kube", ".docker"})
_SECRET_NAMES = frozenset({".env", ".credentials.json", ".netrc", ".pgpass",
                           "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"})

# Finds the input a selector means: the input itself, a file input inside it
# (an "Upload" button wrapping a hidden input), or the input a <label> is for.
_FIND_INPUT_JS = """
(() => {
  const sel = %s;
  const isFile = (e) => e instanceof HTMLInputElement && e.type === 'file';
  let el = sel ? document.querySelector(sel)
               : document.querySelector('input[type=file]');
  if (!el) return null;
  if (isFile(el)) return el;
  const inner = el.querySelector && el.querySelector('input[type=file]');
  if (inner) return inner;
  const label = el.closest && el.closest('label');
  if (label && isFile(label.control)) return label.control;
  return null;
})()
"""

_CENTRE_JS = """
(() => {
  const el = document.querySelector(%s);
  if (!el) return null;
  el.scrollIntoView({block: 'center'});
  const r = el.getBoundingClientRect();
  return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
})()
"""

_NAMES_FN = "function() { return Array.from(this.files || []).map(f => f.name); }"


# ── where the file must be: inside the container's view ────────────────────


def home_of(desk: str) -> Path:
    """The box-side directory mounted at `/home/agent` in `desk`'s container.

    Asked of Docker -- the mount that is really there -- and only if Docker
    cannot say, the path `sandbox.start` would have used.
    """
    try:
        proc = sandbox._run(["docker", "inspect", "-f",
                             "{{range .Mounts}}{{if eq .Destination \"%s\"}}"
                             "{{.Source}}{{end}}{{end}}" % sandbox.DESK_HOME,
                             sandbox.container_name(desk)])
        source = proc.stdout.decode("utf-8", "replace").strip()
        if proc.returncode == 0 and source:
            return Path(source)
    except sandbox.SandboxError:
        pass
    return sandbox.home_for(desk)


def _refuse_secret(path: Path) -> None:
    parts = set(path.parts)
    if parts & _SECRET_DIRS or path.name in _SECRET_NAMES or \
            path.name.startswith(".env.") or path == vault.DEFAULT_PATH.resolve():
        raise PermissionError(
            f"{path.name} looks like a key or a credential; upload_file never "
            f"sends those to a website")


def _inside(path: Path, home: Path) -> str | None:
    """`/home/agent/...` for a box path under `home`, else None. PURE."""
    try:
        rel = path.relative_to(home)
    except ValueError:
        return None
    return f"{sandbox.DESK_HOME}/{rel.as_posix()}" if rel.parts else None


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()[:12]


def stage(path, *, home: Path) -> str:
    """The container path Chromium can read `path` at, copying if needed.

    `path` is a file on the box (the desk's workspace, an attachment) or a
    path already under `/home/agent`. Raises ValueError for a path that is
    not a readable regular file, PermissionError for a credential.
    """
    home = Path(home).resolve()
    raw = str(path)
    if raw == sandbox.DESK_HOME or raw.startswith(sandbox.DESK_HOME + "/"):
        local = (home / raw[len(sandbox.DESK_HOME):].lstrip("/")).resolve()
        if _inside(local, home) is None:
            raise ValueError(f"{raw} leaves {sandbox.DESK_HOME}")
    else:
        local = Path(raw).expanduser().resolve()
    if not local.is_file():
        raise ValueError(f"no such file: {raw}")
    _refuse_secret(local)
    size = local.stat().st_size
    if size > MAX_BYTES:
        raise ValueError(f"{local.name} is {size} bytes, over {MAX_BYTES}")

    inside = _inside(local, home)
    if inside:
        return inside

    folder = home / UPLOAD_DIR / _digest(local)
    dest = folder / local.name
    if not (dest.is_file() and dest.stat().st_size == size):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for d in (home / UPLOAD_DIR, folder):
                os.chmod(d, 0o755)
            shutil.copyfile(local, dest)
            os.chmod(dest, 0o644)
        except OSError as exc:
            raise ValueError(f"could not copy {local.name} where the browser "
                             f"can read it: {exc}") from exc
        for p in (home / UPLOAD_DIR, folder, dest):
            try:
                os.chown(p, sandbox.DESK_UID, sandbox.DESK_GID)
            except OSError:
                pass  # not root: readable by the container's uid either way
    return f"{sandbox.DESK_HOME}/{UPLOAD_DIR}/{folder.name}/{local.name}"


# ── the CDP sequences ───────────────────────────────────────────────────────


def _names(sess, object_id: str) -> list[str]:
    result = sess.call("Runtime.callFunctionOn", {
        "objectId": object_id, "functionDeclaration": _NAMES_FN,
        "returnByValue": True})
    value = (result.get("result") or {}).get("value")
    return [str(v) for v in value] if isinstance(value, list) else []


def set_files_by_selector(driver, files: list[str], selector: str) -> list[str]:
    """Set `files` on the file input `selector` means. Returns what it holds."""
    with driver.session() as sess:
        found = sess.call("Runtime.evaluate", {
            "expression": _FIND_INPUT_JS % json.dumps(str(selector or ""))})
        obj = found.get("result") or {}
        object_id = obj.get("objectId")
        if not object_id or obj.get("subtype") == "null":
            raise browser.BrowserError(
                "no_file_input",
                f"no file input at {selector or 'input[type=file]'!r}: give "
                f"the x,y of the page's Upload button instead, and the deck "
                f"catches the file picker it opens")
        sess.call("DOM.setFileInputFiles", {"files": list(files),
                                            "objectId": object_id})
        return _names(sess, object_id)


def set_files_by_chooser(driver, files: list[str], click: Callable[[], None],
                         *, wait: float = CHOOSER_WAIT) -> list[str]:
    """Click (via `click`) something that opens a file picker; fill it.

    Interception is switched on first, on the same connection, so Chromium
    hands the chooser to the deck instead of drawing a dialog.
    """
    with driver.session() as sess:
        sess.call("Page.enable")
        sess.call("Page.setInterceptFileChooserDialog", {"enabled": True})
        try:
            click()
            opened = sess.wait_event("Page.fileChooserOpened", wait)
            if opened is None:
                raise browser.BrowserError(
                    "no_file_chooser",
                    "that click opened no file picker: click exactly on the "
                    "Upload/Choose button, or pass `selector` for the file "
                    "input or drop zone")
            chosen = list(files)
            if opened.get("mode") == "selectSingle":
                chosen = chosen[:1]
            node = opened.get("backendNodeId")
            sess.call("DOM.setFileInputFiles", {"files": chosen,
                                                "backendNodeId": node})
            resolved = sess.call("DOM.resolveNode", {"backendNodeId": node})
            object_id = (resolved.get("object") or {}).get("objectId")
            return _names(sess, object_id) if object_id else \
                [Path(f).name for f in chosen]
        finally:
            try:
                sess.call("Page.setInterceptFileChooserDialog",
                          {"enabled": False})
            except (browser.BrowserError, sandbox.SandboxError, OSError):
                pass


def drop_files(driver, files: list[str], selector: str) -> None:
    """Drag `files` onto the element `selector` names, as a person would."""
    with driver.session() as sess:
        found = sess.call("Runtime.evaluate", {
            "expression": _CENTRE_JS % json.dumps(str(selector)),
            "returnByValue": True})
        point = (found.get("result") or {}).get("value")
        if not isinstance(point, dict):
            raise browser.BrowserError("no_drop_target",
                                       f"nothing matches {selector!r}")
        data = {"items": [], "files": list(files), "dragOperationsMask": 1}
        for kind in ("dragEnter", "dragOver", "drop"):
            sess.call("Input.dispatchDragEvent", {
                "type": kind, "x": point["x"], "y": point["y"], "data": data})


# ── the tool ────────────────────────────────────────────────────────────────


def upload_file(desk: str, paths, selector: str = "", x=None, y=None,
                drop: bool = False) -> str:
    """Stage `paths` where the browser can read them and give them to the page.

    Target, in order: `drop` onto `selector`; a click at x,y that opens a
    file picker; else the file input `selector` means (empty = the page's
    first one).
    """
    if isinstance(paths, str):
        paths = [paths]
    paths = [str(p) for p in (paths or []) if str(p).strip()]
    if not paths:
        raise ValueError("paths: give at least one file")
    if len(paths) > MAX_FILES:
        raise ValueError(f"at most {MAX_FILES} files at once")
    if drop and not selector:
        raise ValueError("drop needs `selector`: the drop zone")
    home = home_of(desk)
    files = [stage(p, home=home) for p in paths]
    desk_computer._guard(desk)
    driver = desk_computer.ensure(desk)
    if drop:
        drop_files(driver, files, selector)
        return (f"dropped {', '.join(Path(f).name for f in files)} on "
                f"{selector} -- take a screenshot to see the page take it")
    if x is not None and y is not None:
        names = set_files_by_chooser(driver, files, lambda: sandbox.send_input(
            desk, {"action": "click", "x": x, "y": y}))
    else:
        names = set_files_by_selector(driver, files, selector)
    held = ", ".join(names) if names else "nothing"
    return (f"the page's file input now holds: {held}. The site may now show "
            f"a preview, a crop or a Save/Next step -- take a screenshot and "
            f"finish it.")
