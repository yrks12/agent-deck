"""R0 release pipeline: VERSION, server tarball, manifest.json (plan K8)."""
import hashlib
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import release_manifest as rm  # noqa: E402

SEMVER = re.compile(r"^\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?$")


def test_version_file_is_one_semver_line():
    text = (ROOT / "VERSION").read_text()
    assert text.endswith("\n") and SEMVER.match(text.strip()), text


def test_read_version_rejects_garbage(tmp_path):
    f = tmp_path / "VERSION"
    f.write_text("not a version\n")
    with pytest.raises(ValueError):
        rm.read_version(f)


def _fake_tree(tmp_path):
    src = tmp_path / "src"
    (src / "server").mkdir(parents=True)
    (src / "server" / "app.py").write_text("x = 1\n")
    (src / "server" / "__pycache__").mkdir()
    (src / "server" / "__pycache__" / "app.pyc").write_bytes(b"junk")
    (src / "requirements.txt").write_text("fastapi\n")
    (src / "VERSION").write_text("0.9.0\n")
    return src


def test_tarball_has_versioned_root_and_no_bytecode(tmp_path):
    src = _fake_tree(tmp_path)
    out = tmp_path / "out"
    tar = rm.build_server_tarball(src, out, "0.9.0")
    assert tar.name == "agent-deck-server-0.9.0.tar.gz"
    names = tarfile.open(tar).getnames()
    assert "agent-deck-server-0.9.0/server/app.py" in names
    assert "agent-deck-server-0.9.0/VERSION" in names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)


def test_tarball_is_reproducible(tmp_path):
    src = _fake_tree(tmp_path)
    a = rm.build_server_tarball(src, tmp_path / "a", "0.9.0")
    b = rm.build_server_tarball(src, tmp_path / "b", "0.9.0")
    assert rm.sha256_file(a) == rm.sha256_file(b)


def test_manifest_shape_and_hashes(tmp_path):
    src = _fake_tree(tmp_path)
    out = tmp_path / "out"
    tar = rm.build_server_tarball(src, out, "0.9.0")
    dmg = out / "AgentDeck-0.9.0.dmg"
    dmg.write_bytes(b"dmg-bytes")
    m = rm.build_manifest("0.9.0", "https://h.example/d/abc", tar, dmg,
                          released="2026-10-05", notes="first")
    assert m["version"] == "0.9.0" and m["released"] == "2026-10-05"
    assert m["server"] == {
        "url": "https://h.example/d/abc/agent-deck-server-0.9.0.tar.gz",
        "sha256": hashlib.sha256(tar.read_bytes()).hexdigest()}
    assert m["app"] == {
        "url": "https://h.example/d/abc/AgentDeck-0.9.0.dmg",
        "sha256": hashlib.sha256(b"dmg-bytes").hexdigest(),
        "min_macos": "14.0"}
    assert m["min_app"] == "0.9.0" and m["min_server"] == "0.9.0"
    assert m["notes"] == "first"


def test_base_url_trailing_slash_is_normalised(tmp_path):
    src = _fake_tree(tmp_path)
    tar = rm.build_server_tarball(src, tmp_path / "o", "0.9.0")
    dmg = tmp_path / "o" / "AgentDeck-0.9.0.dmg"
    dmg.write_bytes(b"x")
    m = rm.build_manifest("0.9.0", "https://h/x/", tar, dmg)
    assert "//agent" not in m["server"]["url"].replace("https://", "")


def test_cli_requires_release_base(tmp_path):
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/release_manifest.py"),
         "--out", str(tmp_path)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"})
    assert r.returncode != 0
    assert "RELEASE_BASE" in r.stderr


def test_cli_writes_manifest_json(tmp_path):
    dmg = tmp_path / "AgentDeck-x.dmg"
    dmg.write_bytes(b"d")
    version = (ROOT / "VERSION").read_text().strip()
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/release_manifest.py"),
         "--out", str(tmp_path / "rel"), "--base", "https://h/p",
         "--dmg", str(dmg)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    m = json.loads((tmp_path / "rel/manifest.json").read_text())
    assert m["version"] == version
    assert (tmp_path / "rel" / f"agent-deck-server-{version}.tar.gz").exists()
    assert (tmp_path / "rel" / f"AgentDeck-{version}.dmg").exists()


# ---- shell scripts are pinned statically (no build needed) ----

def test_make_dmg_has_free_and_paid_switch():
    s = (ROOT / "macos/Scripts/make-dmg.sh").read_text()
    for needle in ("DECK_SIGN_IDENTITY", "DEVELOPER_ID", "NOTARY_PROFILE",
                   "notarytool", "stapler", "hdiutil", "/Applications"):
        assert needle in s, needle


def test_bundle_script_reads_version_file():
    s = (ROOT / "macos/Scripts/make-app-bundle.sh").read_text()
    assert "VERSION" in s
    assert "<string>0.1</string>" not in s


def test_release_sh_has_no_hardcoded_destination():
    s = (ROOT / "scripts/release.sh").read_text()
    assert "RELEASE_BASE" in s
    assert "initech.example" not in s
    assert "scp " not in s and "gh release" not in s


def test_tarball_carries_every_path_the_installer_requires(tmp_path):
    """install-deck.sh checks ${REPO_DIR}/hooks and builds ${REPO_DIR}/docker/...
    If it names a top-level path the tarball lacks, a real release fails install."""
    installer = (ROOT / "deploy" / "install-deck.sh").read_text()
    wanted = set(re.findall(r'\$\{REPO_DIR\}/([A-Za-z0-9_.-]+)', installer))
    wanted -= {".venv", ".cache", ".agent-deck.log"}   # made on the box, not shipped
    wanted.discard("bin")        # already listed; kept in the assertion below
    wanted |= {"hooks", "docker", "bin", "server", "deploy"}
    missing = sorted(w for w in wanted if w not in rm.INCLUDE and w != "..")
    assert not missing, f"installer needs {missing}; release_manifest.INCLUDE lacks them"


def test_real_tarball_contains_hooks_and_docker(tmp_path):
    t = rm.build_server_tarball(ROOT, tmp_path, "9.9.9")
    with tarfile.open(t) as tar:
        names = tar.getnames()
    p = "agent-deck-server-9.9.9/"
    assert any(n.startswith(p + "hooks/") and n.endswith(".js") for n in names)
    assert p + "docker/desk-computer/Dockerfile" in names
