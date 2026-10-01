#!/usr/bin/env python3
"""Build the server tarball and manifest.json for a release (plan K8).

    RELEASE_BASE=https://host/path scripts/release_manifest.py --out dist/release --dmg X.dmg

Nothing is published; the base URL only lands inside manifest.json.
"""
import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?$")
INCLUDE = ("server", "requirements.txt", "VERSION", "bin", "deploy", "hooks",
           "docker")
SKIP_DIRS = {"__pycache__", ".venv", ".pytest_cache"}


def read_version(path: Path) -> str:
    v = Path(path).read_text().strip()
    if not SEMVER.match(v):
        raise ValueError(f"{path}: {v!r} is not a version like 1.2.3")
    return v


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(src: Path):
    for name in INCLUDE:
        p = src / name
        if p.is_file():
            yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                rel = f.relative_to(src)
                if f.is_file() and not (set(rel.parts) & SKIP_DIRS) \
                        and f.suffix != ".pyc":
                    yield f


def build_server_tarball(src: Path, out: Path, version: str) -> Path:
    """Reproducible: sorted entries, zeroed mtime/owner, no gzip timestamp."""
    src, out = Path(src), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    root = f"agent-deck-server-{version}"
    dest = out / f"{root}.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for f in _files(src):
            info = tar.gettarinfo(str(f), f"{root}/{f.relative_to(src)}")
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if info.mode & 0o111 else 0o644
            with open(f, "rb") as fh:
                tar.addfile(info, fh)
    with open(dest, "wb") as raw, \
            gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
        gz.write(buf.getvalue())
    return dest


def build_manifest(version, base, server_tar: Path, dmg: Path, *,
                   released=None, notes="", min_macos="14.0"):
    base = base.rstrip("/")
    return {
        "version": version,
        "released": released or datetime.date.today().isoformat(),
        "server": {"url": f"{base}/{Path(server_tar).name}",
                   "sha256": sha256_file(server_tar)},
        "app": {"url": f"{base}/{Path(dmg).name}",
                "sha256": sha256_file(dmg), "min_macos": min_macos},
        "min_app": version,
        "min_server": version,
        "notes": notes,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--dmg", type=Path, required=False)
    ap.add_argument("--base", default=os.environ.get("RELEASE_BASE"))
    ap.add_argument("--notes", default="")
    ap.add_argument("--src", type=Path, default=ROOT)
    a = ap.parse_args(argv)
    if not a.base:
        ap.error("RELEASE_BASE (or --base) is required; no destination is "
                 "hardcoded")
    if not a.dmg or not a.dmg.is_file():
        ap.error("--dmg must point at the built DMG")
    version = read_version(a.src / "VERSION")
    a.out.mkdir(parents=True, exist_ok=True)
    tar = build_server_tarball(a.src, a.out, version)
    dmg = a.out / f"AgentDeck-{version}.dmg"
    if dmg.resolve() != a.dmg.resolve():
        shutil.copyfile(a.dmg, dmg)
    m = build_manifest(version, a.base, tar, dmg, notes=a.notes)
    (a.out / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")
    print(f"wrote {a.out}/manifest.json ({version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
