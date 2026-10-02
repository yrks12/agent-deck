"""Repository-level detector for the Agent Deck product boundary.

This test intentionally landed before the macOS client was imported.  If the
client or the product-status contract disappears later, the backend suite must
turn red instead of silently describing only half of the product again.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_one_repository_contains_the_server_and_native_macos_product():
    required = {
        "AGENTS.md",
        "server/app.py",
        "docs/client-api.md",
        "deploy/templates/agentdeck.service.in",
        "macos/Package.swift",
        "macos/Sources/DeckApp/DeckAppMain.swift",
        "macos/Tests/DeckKitTests/ContractTests.swift",
    }

    missing = sorted(path for path in required if not (ROOT / path).is_file())
    assert not missing, f"Agent Deck is split again; missing: {missing}"


def test_readme_identifies_both_runtime_halves():
    readme = (ROOT / "README.md").read_text()

    assert "macos/" in readme
    assert "server/" in readme
    # A stranger's front door: how to start, never the owner's own address.
    assert "docs/quickstart.md" in readme
    assert "10.88.0.1" not in readme


def test_contributor_guide_is_for_the_unified_product():
    guide = (ROOT / "AGENTS.md").read_text()

    assert guide.startswith("# Repository Guidelines\n")
    assert "macos/" in guide
    assert "server/" in guide
    assert "test before" in guide.lower()
