"""His screenshots and files, carried to the box so a desk can open them.

Owner, 2026-09-30: "On the phone I can't add or paste screenshots or files."

The Mac's [+] puts a local path in the message and `api.attachments` derives
the chip from it. A phone has no path the desk can read, so the bytes travel:

* `POST /v1/threads/{id}/attachments` -- the file is the raw body (the same
  shape as a voice note: no multipart parser on the box), `Content-Type` its
  type, `X-Deck-Filename` its percent-encoded name. Stored under
  `<bus>/attachments/<att_id>/<safe name>` and answered with that path.
* The app sends an ordinary message carrying `Attached image: <path>`; the
  desk opens the path with Read (an image is shown to it as an image).
* `GET /v1/attachments/{id}/{name}` serves the bytes back for his bubble.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from . import office
from .api import Refused, StrictJSON, _authorise, _parse_thread_id

#: The same ceiling as a file a desk hands back (`sandbox.DOWNLOAD_MAX`).
MAX_BYTES = 25 * 1024 * 1024
ID = re.compile(r"^att_[0-9a-f]{16}$")
#: Exactly the characters `api._PATH` keeps inside a path, so the path in the
#: message text is derived whole, never cut at a space.
_UNSAFE = re.compile(r"[^A-Za-z0-9._+@-]+")
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "gif": "image/gif", "webp": "image/webp", "heic": "image/heic"}
#: What each extension is served as. Owner, 2026-10-01: a video an agent sends
#: him has to PLAY on his phone, and iOS AVPlayer and QuickLook both decide
#: from the content type. Never HTML or SVG: anything a browser would run is
#: octet-stream, whatever its name (`content_type`).
SERVED_TYPES = {**IMAGE_TYPES,
                "mp4": "video/mp4", "m4v": "video/x-m4v",
                "mov": "video/quicktime", "webm": "video/webm",
                "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
                "wav": "audio/wav", "ogg": "audio/ogg", "flac": "audio/flac",
                "pdf": "application/pdf",
                "txt": "text/plain; charset=utf-8", "md": "text/plain; charset=utf-8",
                "log": "text/plain; charset=utf-8", "csv": "text/csv; charset=utf-8",
                "json": "application/json"}
_EXT_FOR = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
            "image/webp": "webp", "image/heic": "heic",
            "application/pdf": "pdf", "text/plain": "txt"}


def folder() -> Path:
    return office.BUS_DIR / "attachments"


def safe_name(raw: str, mime: str) -> str:
    name = unquote(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE.sub("-", name).strip(".-")[:120]
    if not name:
        name = "attachment." + _EXT_FOR.get(mime, "bin")
    return name


def content_type(name: str) -> str:
    """What `name` is served as. PURE."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return SERVED_TYPES.get(ext, "application/octet-stream")


_MEDIA = {"video": ("mp4", "m4v", "mov", "webm"),
          "audio": ("mp3", "m4a", "aac", "wav", "ogg", "flac"),
          "pdf": ("pdf",)}
#: The poster of a video, or the thumbnail of a big image, sits next to it:
#: `<name>.poster.jpg`. One attachment per folder, so it cannot collide.
PREVIEW_SUFFIX = ".poster.jpg"
#: An image smaller than this is its own thumbnail.
THUMB_OVER = 512 * 1024
PREVIEW_WIDTH = 640
PREVIEW_TIMEOUT = 30.0


def media_of(name: str) -> str:
    """What the app should draw: image, video, audio, pdf or file. PURE."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in IMAGE_TYPES:
        return "image"
    return next((m for m, exts in _MEDIA.items() if ext in exts), "file")


def describe(path: str) -> dict:
    """What a bubble needs for a held attachment, else {}.

    `url` always (the path names a held file even if it has since gone);
    `bytes` only for a file that is there; `preview_url` only once a poster
    has been made (`make_preview`).
    """
    url = url_for(path)
    if not url:
        return {}
    name = path.rsplit("/", 1)[-1]
    out = {"url": url, "media": media_of(name),
           "mime": content_type(name).split(";")[0], "name": name}
    try:
        out["bytes"] = os.stat(path).st_size
    except OSError:
        return out
    if os.path.isfile(path + PREVIEW_SUFFIX):
        out["preview_url"] = url + PREVIEW_SUFFIX
    return out


def make_preview(src: Path, *, run=subprocess.run,
                 ffmpeg: str | None = "") -> Path | None:
    """A video's first-second frame, or a big image's thumbnail, as a JPEG
    next to it. Best effort: no ffmpeg, a small image, or ffmpeg failing is
    None and never an error -- the bubble then draws a plain tile."""
    ffmpeg = shutil.which("ffmpeg") if ffmpeg == "" else ffmpeg
    media = media_of(src.name)
    if not ffmpeg or media not in ("video", "image"):
        return None
    if media == "image":
        try:
            if src.stat().st_size <= THUMB_OVER:
                return None
        except OSError:
            return None
    out = src.with_name(src.name + PREVIEW_SUFFIX)
    scale = f"scale='min({PREVIEW_WIDTH},iw)':-2"
    seek = ["-ss", "0.5"] if media == "video" else []
    for attempt in ([seek, []] if seek else [[]]):
        argv = [ffmpeg, "-y", "-v", "error", *attempt, "-i", str(src),
                "-frames:v", "1", "-vf", scale, "-q:v", "4", str(out)]
        try:
            done = run(argv, capture_output=True, timeout=PREVIEW_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            done = None
        if done is not None and done.returncode == 0 and out.is_file() \
                and out.stat().st_size > 0:
            return out
        out.unlink(missing_ok=True)  # a clip shorter than 0.5 s: from 0
    return None


def kind_of(name: str) -> str:
    return "image" if name.rsplit(".", 1)[-1].lower() in IMAGE_TYPES else "file"


def url_for(path: str) -> str | None:
    """The app-facing url of a stored attachment's box path, else None."""
    root = str(folder()) + "/"
    if not path.startswith(root):
        return None
    att_id, _, name = path[len(root):].partition("/")
    if not ID.match(att_id) or not name or "/" in name:
        return None
    return f"/v1/attachments/{att_id}/{name}"


def build_router(surface) -> APIRouter:
    router = APIRouter(prefix="/v1", default_response_class=StrictJSON)

    @router.post("/threads/{thread_id}/attachments", status_code=201)
    async def upload(thread_id: str, request: Request) -> StrictJSON:
        _authorise(request)
        if int(request.headers.get("content-length") or 0) > MAX_BYTES:
            raise Refused(413, "too_large", "an attachment is at most 25 MB")
        await asyncio.to_thread(surface._refresh_holding)
        thread_id = surface._canonical_thread(thread_id)
        kind, who = _parse_thread_id(thread_id)
        if kind != "direct":
            raise Refused(400, "direct_only",
                          "an attachment goes to one agent's own chat")
        surface.agent(who[0])
        data = await request.body()
        if not data:
            raise Refused(400, "empty_file", "the file was empty")
        if len(data) > MAX_BYTES:
            raise Refused(413, "too_large", "an attachment is at most 25 MB")
        mime = (request.headers.get("content-type") or "").split(";")[0].strip()
        name = safe_name(request.headers.get("x-deck-filename") or "", mime)
        att_id = "att_" + uuid.uuid4().hex[:16]
        path = folder() / att_id / name
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        if media_of(name) == "video":
            await asyncio.to_thread(make_preview, path)
        return StrictJSON({"id": att_id, "name": name, "kind": kind_of(name),
                           "bytes": len(data), "path": str(path),
                           "url": f"/v1/attachments/{att_id}/{name}"},
                          status_code=201)

    @router.get("/attachments/{att_id}/{name}")
    async def download(att_id: str, name: str, request: Request) -> Response:
        _authorise(request)
        path = folder() / att_id / name
        if (not ID.match(att_id) or name != safe_name(name, "")
                or not path.is_file()):
            raise Refused(404, "unknown_attachment", "no such attachment")
        # FileResponse streams from disk (a 200 MB video is never read whole
        # into memory) and answers `Range` with 206 -- AVPlayer will not play
        # a video from a server that does not.
        return FileResponse(path, media_type=content_type(name),
                            content_disposition_type="inline",
                            headers={"Cache-Control": "private, max-age=86400",
                                     "Accept-Ranges": "bytes",
                                     "X-Content-Type-Options": "nosniff"})

    return router
