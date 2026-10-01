"""The public front door: what a stranger reads before running anything.

Detector for the open-source prep (2026-09-30). The docs a stranger follows must
(1) exist, (2) only tell them to run flags and commands the installer really has,
(3) carry no owner-specific address or name, and (4) claim no affiliation with the
companies whose tools this works with. License: Apache-2.0 in LICENSE + NOTICE.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ["README.md", "docs/quickstart.md", "docs/security.md", "docs/terms-questions.md",
          "CONTRIBUTING.md"]
INSTALLER = ROOT / "deploy" / "install-deck.sh"
DECKCTL = ROOT / "bin" / "deckctl"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", PUBLIC + ["LICENSE", "NOTICE"])
def test_the_public_files_exist(rel):
    assert (ROOT / rel).is_file(), rel


def test_license_is_the_full_apache_2_text_and_notice_names_the_project():
    text = read("LICENSE")
    assert "Apache License" in text and "Version 2.0, January 2004" in text
    assert "END OF TERMS AND CONDITIONS" in text
    notice = read("NOTICE")
    assert "Agent Deck" in notice and "Apache License, Version 2.0" in notice


# Owner-specific values a stranger must never be told to type or see as the default.
OWNER = [r"10\.88\.0\.", r"178\.79\.137\.5", r"yairos", r"yairtech", r"/Users/yairkruskal",
         r"\bPalm\b", r"\bVillas?\b", r"Hamatsesa"]


@pytest.mark.parametrize("rel", PUBLIC)
def test_no_owner_values_in_public_docs(rel):
    text = read(rel)
    hits = [p for p in OWNER if re.search(p, text)]
    assert not hits, f"{rel} carries owner values: {hits}"


@pytest.mark.parametrize("rel", PUBLIC)
def test_no_claimed_affiliation(rel):
    # The disclaimer sentence itself ("not affiliated with or endorsed by ...") is
    # the one place those words belong; every other sentence is checked.
    sentences = re.split(r"(?<=[.!?])\s+", read(rel).lower())
    text = " ".join(s for s in sentences if "not affiliated" not in s)
    for phrase in ("inspired by xai", "grok bot", "official anthropic", "by anthropic",
                   "endorsed by", "partnered with", "affiliated with anthropic"):
        assert phrase not in text, f"{rel}: {phrase!r}"


def test_readme_says_it_is_not_affiliated_and_is_honest_about_runtimes():
    text = read("README.md")
    assert "not affiliated" in text.lower()
    for engine in ("Claude Code", "OpenCode", "Codex"):
        assert engine in text
    assert "docs/quickstart.md" in text and "docs/security.md" in text


def _installer_flags() -> set[str]:
    return set(re.findall(r"^\s+(--[a-z-]+)[|)=]", INSTALLER.read_text(), re.M))


def _documented_install_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if "install-deck.sh" in ln and "--" in ln]


def test_every_installer_flag_the_quickstart_uses_exists():
    flags = _installer_flags()
    assert "--tls" in {f.split("=")[0] for f in flags} or "--tls" in flags
    used = set()
    for line in _documented_install_lines(read("docs/quickstart.md")):
        used |= set(re.findall(r"(--[a-z-]+)", line))
    assert used, "the quickstart shows no installer command"
    unknown = sorted(u for u in used if u not in flags)
    assert not unknown, f"quickstart uses flags the installer does not take: {unknown}"


def test_every_tls_mode_the_quickstart_names_is_one_the_installer_accepts():
    modes = set(re.findall(r"--tls=([a-z-]+)", read("docs/quickstart.md")))
    accepted = {"sslip", "domain", "self-signed", "tunnel", "none"}
    assert modes and modes <= accepted, modes


def test_every_deckctl_command_the_docs_use_exists():
    known = set(re.findall(r'^\s+(?:\w+ = )?add\("([a-z-]+)"', DECKCTL.read_text(), re.M))
    assert {"login", "doctor", "uninstall", "revoke"} <= known, known
    for rel in ("docs/quickstart.md", "docs/security.md", "README.md"):
        used = set(re.findall(r"deckctl ([a-z-]+)", read(rel)))
        unknown = sorted(used - known)
        assert not unknown, f"{rel} uses deckctl commands that do not exist: {unknown}"


def test_the_quickstart_does_not_require_wireguard():
    text = read("docs/quickstart.md")
    assert "WireGuard" not in text.split("## ")[1], "the first path must not need WireGuard"
    for option in ("Tailscale", "SSH tunnel", "ssh -L"):
        assert option in text


def test_the_mac_app_gatekeeper_step_is_documented():
    text = read("docs/quickstart.md")
    assert "Open Anyway" in text and "Privacy & Security" in text


def test_terms_questions_are_questions_not_conclusions():
    text = read("docs/terms-questions.md")
    items = [ln for ln in text.splitlines() if re.match(r"^\d+\.\s", ln)]
    assert len(items) >= 3
    assert all(ln.rstrip().endswith("?") for ln in items), items
