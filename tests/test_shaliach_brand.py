"""The product is called Shaliach (owner ruling 2026-10-02; it was "Agent Deck").

Detector for the rebrand. What a person SEES says Shaliach: the README, the
quickstart, the installers' one-liners, the app names on the Mac and the iPhone,
the web page title and the phone push title. The public repository moved to
github.com/<owner>/shaliach; GitHub redirects the old name, but no user-facing doc
or installer may still send people to it.

What deliberately did NOT change, and is pinned here so nobody "finishes" the
rename by accident: the bundle identifiers (keychain items, settings, pairings and
privacy grants are scoped to them), the server's install paths and service name,
and the DECK_* / AGENT_DECK_* settings. Renaming any of those breaks installs that
already exist.
"""
from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Spelled as github.com/... so only the public-slug spans appear in this file.
NEW_SLUG = "github.com/yrks12/shaliach".split("/", 1)[1]
OLD_SLUG = "github.com/yrks12/agent-deck".split("/", 1)[1]
OLD_SLUG_RE = re.compile(re.escape(OLD_SLUG) + r"(?![\w-])")
EXPLAINS_THE_OLD_NAME = re.compile(r"rename|until 0\.9\.2|LEGACY_APP_NAME=|Debug-iphoneos/Agent Deck\.app")

# Every file a person reads or pipes into a shell to install the product.
USER_FACING = [
    "README.md",
    "docs/quickstart.md",
    "install.sh",
    "scripts/install-mac.sh",
    "scripts/install-iphone.sh",
    "ios/install.sh",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "SUPPORT.md",
    "NOTICE",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", USER_FACING)
def test_no_user_facing_file_points_at_the_old_repository(rel):
    hits = [n for n, line in enumerate(read(rel).splitlines(), 1) if OLD_SLUG_RE.search(line)]
    assert not hits, f"{rel} still names the old repository on lines {hits}"


@pytest.mark.parametrize("rel", USER_FACING)
def test_no_user_facing_file_says_agent_deck(rel):
    # The iPhone build product keeps its internal file name (Xcode PRODUCT_NAME);
    # the phone shows CFBundleDisplayName, which is Shaliach.
    # Lines that explain the old name (the Mac installer removes an old
    # "Agent Deck.app" so there is one app) are the exception.
    hits = [n for n, line in enumerate(read(rel).splitlines(), 1)
            if "Agent Deck" in line and not EXPLAINS_THE_OLD_NAME.search(line)]
    assert not hits, f"{rel} still says Agent Deck on lines {hits}"


@pytest.mark.parametrize("rel,script", [
    ("README.md", "scripts/install-mac.sh"),
    ("README.md", "scripts/install-iphone.sh"),
    ("docs/quickstart.md", "scripts/install-mac.sh"),
])
def test_the_one_liners_fetch_from_the_new_repository(rel, script):
    line = f"curl -fsSL https://raw.githubusercontent.com/{NEW_SLUG}/main/{script} | bash"
    assert line in read(rel)


@pytest.mark.parametrize("rel", ["README.md", "docs/quickstart.md", "docs/site/index.html"])
def test_the_server_one_liner_comes_from_our_own_domain(rel):
    text = read(rel)
    assert "curl -fsSL https://shaliach.me/install | sh" in text
    assert "/install.sh | bash" not in text


@pytest.mark.parametrize("rel", ["install.sh", "scripts/install-mac.sh", "scripts/install-iphone.sh"])
def test_the_installers_clone_the_new_repository_by_default(rel):
    assert f'AGENT_DECK_REPO="${{AGENT_DECK_REPO:-{NEW_SLUG}}}"' in read(rel)


def test_the_readme_and_notice_name_the_product():
    assert read("README.md").startswith("# Shaliach")
    assert read("NOTICE").startswith("Shaliach")


def test_the_mac_app_is_called_shaliach_and_keeps_its_bundle_id():
    info = plistlib.loads((ROOT / "macos/App/Info.plist").read_bytes())
    assert info["CFBundleName"] == "Shaliach"
    assert info["CFBundleDisplayName"] == "Shaliach"
    assert info["CFBundleIdentifier"] == "$(PRODUCT_BUNDLE_IDENTIFIER)"
    script = read("macos/Scripts/make-app-bundle.sh")
    assert '<key>CFBundleName</key><string>Shaliach</string>' in script
    assert '<key>CFBundleDisplayName</key><string>Shaliach</string>' in script
    assert 'APP="$ROOT/dist/Shaliach.app"' in script
    # The bundle id still comes from this machine's settings (dev.agentdeck.app by
    # default), so keychain items, settings and pairings survive the rename.
    assert "<string>$DECK_BUNDLE_ID</string>" in script
    assert "dev.agentdeck.app" in read("macos/Scripts/bundle-id.sh")


def test_the_iphone_app_is_called_shaliach_and_keeps_its_bundle_id():
    project = read("ios/project.yml")
    assert re.search(r"^\s+CFBundleDisplayName: Shaliach$", project, re.M)
    assert "PRODUCT_BUNDLE_IDENTIFIER" in project
    assert "Agent Deck uses" not in project


def test_the_mac_release_zip_is_named_shaliach():
    script = read("scripts/release-mac.sh")
    assert 'NAME="Shaliach-${VERSION}-macos.zip"' in script
    assert 'APP="${ROOT}/macos/dist/Shaliach.app"' in script


def test_the_mac_installer_installs_one_shaliach_app():
    script = read("scripts/install-mac.sh")
    assert 'APP_NAME="Shaliach.app"' in script
    # An upgrade from an "Agent Deck.app" install leaves exactly one copy.
    assert 'LEGACY_APP_NAME="Agent Deck.app"' in script


def test_the_web_page_and_the_phone_push_say_shaliach():
    assert "<title>Shaliach" in read("web/index.html")
    assert '"Shaliach"' in read("server/ntfy.py")


@pytest.mark.parametrize("rel,kept", [
    ("deploy/install-deck.sh", "/opt/agent-deck"),
    ("deploy/install-deck.sh", "/etc/agent-deck"),
    ("install.sh", "AGENT_DECK_DIR"),
])
def test_internal_names_stay_so_existing_installs_keep_working(rel, kept):
    assert kept in read(rel)


# --- the sweep: no user-visible "Agent Deck" anywhere that ships ------------------
#
# Docs are read whole. Code is read for what a person sees: string literals on
# non-comment lines. Each allowed mention below is deliberate and says why.

ALLOWED = [
    (re.compile(r"\[Agent Deck"), "the deck's own-line marker in agent messages: agent protocol, "
                                  "stripped from captions by the apps"),
    (re.compile(r"Application Support/Agent Deck"), "the Mac app's data folder: renaming it orphans settings"),
    (re.compile(r"Agent Deck Workspace"), "the folder already granted to agents on existing Macs"),
    (re.compile(r"Agent Deck Local"), "the self-signed code-signing certificate's name in the keychain"),
    (re.compile(r"MacOS/Agent Deck|\"-x\", \"Agent Deck\"|CFBundleExecutable|PRODUCT_NAME"),
     "the executable / Xcode product keeps its file name; people see CFBundleName"),
    (re.compile(r"BUILT_PRODUCTS_DIR\)/Agent Deck\.app|Debug-iphoneos/Agent Deck\.app|App/build/\$CHANNEL/Agent Deck\.app|path/to/Agent Deck\.app"),
     "Xcode build products keep their internal file name"),
    (re.compile(r"formerly Agent Deck|was \"Agent Deck\"|Agent Deck is now|rename|until 0\.9\.2|"
                r"LEGACY_APP_NAME|`Agent Deck\.app`"),
     "says what the product used to be called"),
]

DOC_GLOBS = ["*.md", "docs/*.md", "docs/app-store/*", "macos/*.md", "ios/*.md",
             ".github/**/*.yml", ".github/**/*.md", "docs/site/*.html"]
DOC_SKIP = {"STATUS.md"}  # internal, not shipped
CODE_GLOBS = ["web/*.html", "web/*.js", "web/*.css", "install.sh", "scripts/install-*.sh",
              "deploy/install.sh", "deploy/install-deck.sh", "bin/deckctl", "bin/deckdoctor",
              "server/doctor_checks.py", "server/ntfy.py", "server/push_policy.py",
              "server/pairing.py", "server/mac_api.py", "server/mac_mcp.py",
              "macos/Sources/**/*.swift", "ios/AgentDeckPhone/**/*.swift", "macos/App/Info.plist",
              "ios/project.yml", "macos/Scripts/make-app-bundle.sh"]
COMMENT = re.compile(r"^\s*(#|//|/\*|\*|<!--)")


def _files(globs):
    seen = []
    for g in globs:
        for p in sorted(ROOT.glob(g)):
            if p.is_file() and p not in seen and p.name not in DOC_SKIP:
                seen.append(p)
    return seen


def _allowed(line: str) -> bool:
    return any(rx.search(line) for rx, _why in ALLOWED)


def _changelog_current(text: str) -> str:
    """Only the releases since the rename; older entries are history."""
    return text.split("\n## 0.9.1", 1)[0]


def _hits(path: Path, code: bool) -> list[int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.name == "CHANGELOG.md":
        text = _changelog_current(text)
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if "Agent Deck" not in line or _allowed(line):
            continue
        if code and (COMMENT.match(line) or not re.search(r"[\"'`>]", line)):
            continue
        out.append(n)
    return out


def test_the_sweep_reads_the_files_it_means_to():
    assert len(_files(DOC_GLOBS)) > 20 and len(_files(CODE_GLOBS)) > 40


@pytest.mark.parametrize("path", _files(DOC_GLOBS), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_shipped_doc_says_agent_deck(path):
    hits = _hits(path, code=False)
    assert not hits, f"{path.relative_to(ROOT)} says Agent Deck on lines {hits}"


@pytest.mark.parametrize("path", _files(CODE_GLOBS), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_user_visible_string_says_agent_deck(path):
    hits = _hits(path, code=True)
    assert not hits, f"{path.relative_to(ROOT)} shows Agent Deck on lines {hits}"
