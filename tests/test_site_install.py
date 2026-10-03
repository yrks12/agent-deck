"""https://shaliach.me/install: the one command a stranger pipes into sh.

    curl -fsSL https://shaliach.me/install | sh

The site is static (GitHub Pages), so /install is a plain copy of install.sh in
docs/site. A copy can drift, so the first test fails the moment the two differ:
edit install.sh, then `cp install.sh docs/site/install`.

`| sh` is dash on Ubuntu and bash-in-POSIX-mode on a Mac, and install.sh is bash.
Its first lines hand over to bash; these tests run it through `sh` and `dash`
exactly as curl would, with the same fake machine as test_install_oneliner.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from test_install_oneliner import SCRIPT, Box, _exe

ROOT = Path(__file__).resolve().parents[1]
SITE_COPY = ROOT / "docs" / "site" / "install"
SITE_URL = "https://shaliach.me/install"
MANIFEST = ROOT / "oss" / "manifest.toml"


def test_the_site_serves_the_real_installer_byte_for_byte():
    assert SITE_COPY.exists(), "docs/site/install is missing: cp install.sh docs/site/install"
    assert SITE_COPY.read_bytes() == SCRIPT.read_bytes(), (
        "docs/site/install differs from install.sh: cp install.sh docs/site/install")


@pytest.mark.skipif(not MANIFEST.exists(),
                    reason="oss/manifest.toml lives only in the private repo; the public export has no manifest")
def test_the_export_rewrites_the_site_copy_like_the_installer():
    # The export rewrites the repo slug in install.sh; the site copy must get the
    # same rewrite or the public copies would differ.
    manifest = MANIFEST.read_text()
    assert '"docs/site/install",' in manifest


def test_pages_republishes_when_the_installer_changes():
    wf = (ROOT / ".github" / "workflows" / "pages.yml").read_text()
    assert '"install.sh"' in wf
    assert "cp install.sh docs/site/install" in wf


def _site_box(tmp_path: Path) -> Box:
    """A Box whose curl serves install.sh at the site URL, as Pages would."""
    box = Box(tmp_path)
    _exe(box.bin / "curl",
         'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$FAKE_LOG"\n'
         f'case "$*" in *{SITE_URL}*) exec cat "{SCRIPT}" ;; '
         '*healthz*) [ -f "$FAKE_STATE/loaded" ] || exit 7; echo \'{"ok":true}\' ;; esac\n')
    return box


SHELLS = [s for s in ("sh", "dash") if shutil.which(s)]


def _pipe(box: Box, shell: str, *args: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{box.bin}:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        "HOME": str(box.home), "FAKE_LOG": str(box.log), "FAKE_STATE": str(box.tmp / "state"),
        "FAKE_UID": str(box.uid), "FAKE_UNAME": box.uname,
        "FAKE_OAUTH_ENV": str(box.tmp / "oauth.env"), "GIT_CONFIG_GLOBAL": str(box.gitconfig),
        "AGENT_DECK_REPO": "example-owner/agent-deck-fork", "AGENT_DECK_DIR": str(box.dir),
        "AGENT_DECK_OS_RELEASE": str(box.os_release), "AGENT_DECK_HEALTH_WAIT": "1",
    }
    return subprocess.run([shell, "-s", "--", *args], input=SCRIPT.read_text(), env=env,
                          capture_output=True, text=True, timeout=120, start_new_session=True)


@pytest.mark.parametrize("shell", SHELLS)
def test_piped_into_sh_it_installs_the_server(tmp_path, shell):
    box = _site_box(tmp_path)
    r = _pipe(box, shell)
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("install-deck ") for c in box.calls()), box.calls()
    assert "Shaliach is serving on" in r.stdout


@pytest.mark.parametrize("shell", SHELLS)
def test_piped_into_sh_the_flags_still_arrive(tmp_path, shell):
    box = _site_box(tmp_path)
    r = _pipe(box, shell, "--domain", "deck.example.com")
    assert r.returncode == 0, r.stdout + r.stderr
    deck = [c for c in box.calls() if c.startswith("install-deck ")]
    assert deck and "--tls=domain --hostname deck.example.com" in deck[0], deck


@pytest.mark.parametrize("shell", SHELLS)
def test_piped_into_sh_help_shows_the_site_one_liner(tmp_path, shell):
    r = _pipe(_site_box(tmp_path), shell, "--help")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"curl -fsSL {SITE_URL} | sh" in r.stdout


def test_the_installer_documents_the_site_one_liner():
    assert f"#   curl -fsSL {SITE_URL} | sh" in SCRIPT.read_text()
