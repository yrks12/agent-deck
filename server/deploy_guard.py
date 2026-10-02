"""Did the box's code change outside `bin/deploy-box`?

MEASURED 2026-09-30: "another session overwrote the box with older code about a
minute after my deploy." Agents rsynced checkouts to the box by hand, and the
box could not tell a newer tree from an older one.

`bin/deploy-box` writes `<root>/DEPLOYED` after every deploy:

    sha=<the commit>
    time=<UTC ISO time>
    who=<who ran it>
    fingerprint=<sha256 of the shipped tree, as `fingerprint()` computes it>

`check()` recomputes the fingerprint of what is on disk now. A mismatch means
the tree was changed by something other than the script -- most often an older
checkout rsynced by hand, which is a silent rollback. The deck's startup and
deckdoctor both say so loudly; neither changes anything.

Stdlib only, and Python 3.9-compatible: `bin/deploy-box` runs it on the box
(`python3 -m server.deploy_guard fingerprint <root>`) and the tests run it on
the Mac's system python.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

#: What `bin/deploy-box` ships. Everything else under the root (.venv, logs,
#: tests, local state) is the box's own and is never fingerprinted.
SHIPPED_DIRS = ("server", "hooks", "bin", "docs", "web", "deploy")
SHIPPED_FILES = ("VERSION",)
RECORD = "DEPLOYED"

_SKIP_DIRS = {"__pycache__", ".pytest_cache"}
_SKIP_SUFFIXES = (".pyc", ".pyo")
_SKIP_NAMES = {".DS_Store"}


def _files(root: Path):
    for name in SHIPPED_FILES:
        p = root / name
        if p.is_file():
            yield name, p
    for top in SHIPPED_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for f in sorted(filenames):
                if f in _SKIP_NAMES or f.endswith(_SKIP_SUFFIXES):
                    continue
                p = Path(dirpath) / f
                yield p.relative_to(root).as_posix(), p


def fingerprint(root) -> str:
    """sha256 over (path, content hash) of every shipped file, in path order."""
    root = Path(root)
    outer = hashlib.sha256()
    for rel, path in sorted(_files(root)):
        inner = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    inner.update(chunk)
        except OSError:
            continue
        outer.update(rel.encode() + b"\0" + inner.hexdigest().encode() + b"\n")
    return outer.hexdigest()


def read_deployed(root) -> dict | None:
    try:
        text = (Path(root) / RECORD).read_text(encoding="utf-8")
    except OSError:
        return None
    rec = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            rec[key.strip()] = value.strip()
    return rec or None


def write_deployed(root, *, sha: str, who: str, when: str, fingerprint: str) -> None:
    root = Path(root)
    tmp = root / (RECORD + ".tmp")
    tmp.write_text(f"sha={sha}\ntime={when}\nwho={who}\nfingerprint={fingerprint}\n",
                   encoding="utf-8")
    os.replace(tmp, root / RECORD)


def check(root) -> tuple[bool, str]:
    """(ok, one sentence). No record is not an alarm: the box predates the script."""
    root = Path(root)
    rec = read_deployed(root)
    if not rec or not rec.get("fingerprint"):
        return True, f"no {RECORD} record yet: this box has not been deployed with bin/deploy-box"
    sha = rec.get("sha", "?")[:12]
    if fingerprint(root) == rec["fingerprint"]:
        return True, f"code matches {RECORD} {sha} ({rec.get('who', '?')}, {rec.get('time', '?')})"
    return False, (
        f"the code under {root} no longer matches {RECORD} {sha} "
        f"(deployed by {rec.get('who', '?')} at {rec.get('time', '?')}): something changed it "
        f"outside bin/deploy-box -- possibly an older checkout rsynced by hand, which is a "
        f"silent ROLLBACK. Redeploy the newest commit with bin/deploy-box.")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in ("fingerprint", "check"):
        print("usage: python3 -m server.deploy_guard fingerprint|check <root>", file=sys.stderr)
        return 2
    if argv[0] == "fingerprint":
        print(fingerprint(argv[1]))
        return 0
    ok, detail = check(argv[1])
    print(detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
