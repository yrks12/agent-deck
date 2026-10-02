"""The public front door: what a stranger reads before running anything.

Detector for the open-source prep (2026-09-30). The docs a stranger follows must
(1) exist, (2) only tell them to run flags and commands the installer really has,
(3) carry no owner-specific address or name, and (4) claim no affiliation with the
companies whose tools this works with. License: PolyForm Noncommercial 1.0.0
(source-available, not open source) in LICENSE + NOTICE.
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


# The official PolyForm Noncommercial 1.0.0 text, byte for byte, as published at
# github.com/polyformproject/polyform-licenses (cross-checked word for word against
# polyformproject.org/licenses/noncommercial/1.0.0 on 2026-09-30).
POLYFORM_NC_SHA256 = "c0ea4a896d2c8c394b29f9427589996db826cd501c512279ff0ed3ef48fabbe5"
REQUIRED_NOTICE = "Required Notice: Copyright © 2026 YA" "IRTECH LTD (https://github.com/yrks" "12)"


def test_license_is_the_official_polyform_noncommercial_text_with_the_required_notice():
    text = read("LICENSE")
    notice, _, body = text.partition("\n\n")
    assert notice == REQUIRED_NOTICE
    import hashlib
    assert hashlib.sha256(body.encode()).hexdigest() == POLYFORM_NC_SHA256, \
        "LICENSE body is not the official PolyForm Noncommercial 1.0.0 text"
    notice_file = read("NOTICE")
    assert "PolyForm Noncommercial License 1.0.0" in notice_file
    assert REQUIRED_NOTICE in notice_file
    assert "Apache" not in notice_file


def _without_license_section(text: str) -> str:
    """README's "## License" section is where the licensor is named, on purpose."""
    return re.sub(r"(?ms)^## License\n.*?(?=^## |\Z)", "", text)


def test_it_is_called_source_available_never_open_source():
    for rel in PUBLIC + ["NOTICE"]:
        low = read(rel).lower()
        assert "open source" not in low and "open-source" not in low, rel
    assert "source-available" in read("README.md").lower()


def test_readme_license_section_says_noncommercial_and_how_to_get_commercial_use():
    text = read("README.md")
    section = re.search(r"(?ms)^## License\n(.*?)(?=^## |\Z)", text).group(1)
    assert "PolyForm Noncommercial" in section and "LICENSE" in section
    assert "Commercial use" in section and "github.com/" in section
    assert not re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", section), "GitHub handle only, no email"


def test_contributions_carry_a_dco_and_an_inbound_relicensing_grant():
    text = read("CONTRIBUTING.md")
    assert "Developer Certificate of Origin" in text and "Signed-off-by" in text
    assert "relicense" in text.lower()
    assert "Apache" not in text


def test_no_paid_apple_account_is_a_blocker():
    for rel in PUBLIC + ["docs/notifications.md", "macos/README.md"]:
        text = read(rel)
        assert "$99" not in text and "paid Apple Developer account" not in text, rel


def test_mac_and_iphone_can_be_had_without_an_apple_developer_account():
    text = read("docs/quickstart.md")
    assert "make app" in text and "xattr -dr com.apple.quarantine" in text
    assert "personal team" in text.lower() and "7 days" in text
    assert re.search(r"(?m)^app:", read("Makefile"))


# Owner-specific values a stranger must never be told to type or see as the default.
OWNER = [r"10\.88\.0\.", r"178\.79\.137\.5", r"yairos", r"yairtech", r"/Users/yairkruskal",
         r"\bPalm\b", r"\bVillas?\b", r"Hamatsesa"]


@pytest.mark.parametrize("rel", PUBLIC)
def test_no_owner_values_in_public_docs(rel):
    text = _without_license_section(read(rel)) if rel == "README.md" else read(rel)
    hits = [p for p in OWNER if re.search(p, text)]
    assert not hits, f"{rel} carries owner values: {hits}"


@pytest.mark.parametrize("rel", PUBLIC)
def test_no_claimed_affiliation(rel):
    # The disclaimer sentence itself ("not affiliated with or endorsed by ...") is
    # the one place those words belong; every other sentence is checked.
    sentences = re.split(r"(?<=[.!?])\s+", read(rel).lower())
    text = " ".join(s for s in sentences if "not affiliated" not in s)
    # Naming a competitor in the dated comparison table is fair; being "inspired by"
    # one is the claim that must never appear.
    for phrase in ("inspired by xai", "inspired by grok", "official anthropic", "by anthropic",
                   "endorsed by", "partnered with", "affiliated with anthropic"):
        assert phrase not in text, f"{rel}: {phrase!r}"


#: The exact line the terms check asked for (claude-dashbaord-oss-terms.md),
#: so a reader who sees nothing else still sees it.
DISCLAIMER = "Independent project, not affiliated with or endorsed by Anthropic"


@pytest.mark.parametrize("rel", ["README.md", "NOTICE"])
def test_the_disclaimer_line_is_there_verbatim_near_the_top(rel):
    head = "\n".join(read(rel).splitlines()[:12])
    assert DISCLAIMER in head, f"{rel} must carry {DISCLAIMER!r} in its first lines"


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


# --- the one-line installs ------------------------------------------------------------

def _slug() -> str:
    m = re.findall(r'^AGENT_DECK_REPO="\$\{AGENT_DECK_REPO:-([^}]+)\}"$', read("install.sh"), re.M)
    assert len(m) == 1
    return m[0]


def _section(text: str, heading: str) -> str:
    m = re.search(rf"(?ms)^{re.escape(heading)}\n(.*?)(?=^## |\Z)", text)
    assert m, heading
    return m.group(1)


def test_readme_opens_with_the_one_liners_server_mac_iphone():
    text = read("README.md")
    heads = re.findall(r"(?m)^## .*$", text)
    # The pitch (what it does, features, comparison) comes first; Get started is the
    # first how-to section, before any reference material.
    assert "## Get started" in heads and heads.index("## Get started") < heads.index("## Requirements")
    raw = f"https://raw.githubusercontent.com/{_slug()}/main"
    cmds = [l.strip() for l in _section(text, "## Get started").splitlines() if l.strip().startswith("curl ")]
    assert len(cmds) == 3, cmds
    assert cmds[0] == f"curl -fsSL {raw}/install.sh | bash"
    assert cmds[1] == f"curl -fsSL {raw}/scripts/install-mac.sh | bash"
    assert cmds[2] == f"curl -fsSL {raw}/scripts/install-iphone.sh | bash"
    assert "git clone <REPO_URL>" not in text


def _oneliner_flags() -> set[str]:
    return set(re.findall(r"^\s+(--[a-z-]+)[|)=]", read("install.sh"), re.M))


def test_every_flag_the_docs_pass_to_the_one_liner_exists():
    known = _oneliner_flags() | _installer_flags()
    used = set()
    for rel in ("README.md", "docs/quickstart.md"):
        for line in read(rel).splitlines():
            if "install.sh | bash -s --" in line and "install-mac" not in line:
                used |= set(re.findall(r"(--[a-z-]+)", line.split("bash -s --", 1)[1]))
    assert {"--ssh-tunnel", "--domain"} <= used, used
    unknown = sorted(u for u in used if u.split("=")[0] not in known)
    assert not unknown, unknown


def test_the_quickstart_shows_the_one_liners_and_their_pairing_step():
    text = read("docs/quickstart.md")
    assert f"raw.githubusercontent.com/{_slug()}/main/install.sh | bash" in text
    assert "install-mac.sh | bash -s -- --pair" in text
    assert "install-iphone.sh" in text
    assert "<REPO_URL>" not in text


def test_the_free_resign_helpers_are_named_neutrally():
    text = read("docs/quickstart.md")
    assert "SideStore" in text and "AltStore" in text
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if "Store" in s and ("Side" in s or "Alt" in s)]
    for s in sentences:
        low = s.lower()
        for word in ("recommend", "best", "official", "endorse", "trusted", "safe"):
            assert word not in low, (word, s)


def test_the_docs_match_a_deck_that_stores_no_claude_token():
    """`deckctl login` runs Claude's own `claude auth login`; Agent Deck never asks
    for, writes or keeps a Claude token, and the installer refuses a token file.
    The public docs must not tell a stranger otherwise."""
    for rel in PUBLIC:
        text = read(rel)
        for stale in ("--claude-token-file", "oauth.env", "sk-ant-oat"):
            assert stale not in text, f"{rel}: {stale!r}"
        if rel != "docs/terms-questions.md":
            assert "setup-token" not in text, rel
    assert "deckctl login" in read("docs/quickstart.md")
    for rel in ("README.md", "docs/security.md"):
        assert "stores no Claude token" in read(rel), rel


def test_the_usage_meter_is_documented_as_unofficial_on_by_default_and_switchable():
    text = read("docs/security.md") + read("docs/terms-questions.md")
    assert "unofficial" in text and "on by default" in text
    assert "claude.usage_meter false" in text
