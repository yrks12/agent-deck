"""The /explain feed.

`/explain` writes one directory per run under the trend-video skill's output
folder: explain.mp4, narration.wav, script.txt, duration.txt and a review/
folder of frame grabs. There are 100 of them and no way to browse them, so the
deck indexes them into a feed.

`/speak` is not here: it streams straight to CoreAudio via ffplay and never
writes a file, so there is nothing to index. See the README.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

MEDIA_ROOT = Path.home() / ".claude" / "skills" / "trend-video" / "output"

# Rescan is cheap (a stat per run) but pointless at 1 Hz.
REFRESH_SECONDS = 10.0

VIDEO_NAME = "explain.mp4"
AUDIO_NAME = "narration.wav"


def _title(slug: str) -> str:
    name = slug[len("explain-"):] if slug.startswith("explain-") else slug
    return name.replace("-", " ").strip() or slug


class MediaIndex:
    def __init__(self, root: Path = MEDIA_ROOT) -> None:
        self.root = root
        self._cache: list[dict] = []
        self._scanned_at = 0.0

    def resolve(self, slug: str, kind: str) -> Path | None:
        """Map a slug to a file on disk, refusing anything outside the root."""
        name = VIDEO_NAME if kind == "video" else AUDIO_NAME
        try:
            candidate = (self.root / slug / name).resolve()
            root = self.root.resolve()
        except OSError:
            return None
        # Path traversal guard: a slug like "../../etc" must not escape.
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        return candidate

    def scan(self, force: bool = False) -> list[dict]:
        now = time.monotonic()
        if not force and now - self._scanned_at < REFRESH_SECONDS:
            return self._cache
        self._scanned_at = now

        runs: list[dict] = []
        try:
            entries = list(self.root.iterdir())
        except OSError:
            self._cache = []
            return self._cache

        for directory in entries:
            if not directory.is_dir():
                continue
            video = directory / VIDEO_NAME
            audio = directory / AUDIO_NAME
            has_video = video.is_file()
            has_audio = audio.is_file()
            if not (has_video or has_audio):
                continue

            primary = video if has_video else audio
            try:
                stat = primary.stat()
            except OSError:
                continue

            script = ""
            try:
                script = (directory / "script.txt").read_text(errors="ignore").strip()
            except OSError:
                pass

            duration = 0.0
            try:
                duration = float((directory / "duration.txt").read_text().strip())
            except (OSError, ValueError):
                pass

            size = 0
            for f in (video, audio):
                try:
                    size += f.stat().st_size
                except OSError:
                    pass

            runs.append(
                {
                    "slug": directory.name,
                    "title": _title(directory.name),
                    "kind": "explain" if directory.name.startswith("explain-") else "video",
                    "created_at": stat.st_mtime,
                    "duration": duration,
                    "bytes": size,
                    "has_video": has_video,
                    "has_audio": has_audio,
                    "script": script[:1200],
                    "words": len(script.split()),
                }
            )

        runs.sort(key=lambda r: -r["created_at"])
        self._cache = runs
        return self._cache

    def summary(self) -> dict:
        runs = self.scan()
        return {
            "count": len(runs),
            "bytes": sum(r["bytes"] for r in runs),
            "newest": runs[0]["created_at"] if runs else 0,
        }
