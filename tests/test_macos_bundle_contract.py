"""The Mac app's bundle files match contract C2 of the submission plan.

docs/plans/2026-09-30-submission-ready.md, C1 + C2. These files are what App
Review and notarization read; a missing key surfaces only at upload, days after
the change that dropped it. So the keys are pinned here, parsed with plistlib,
with no Xcode and no network.
"""

import plistlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "macos" / "App"

MIC_TEXT = (
    "Shaliach listens only while you hold the mic button or are on a call, "
    "so you can talk to your agents instead of typing."
)
SPEECH_TEXT = (
    "Shaliach turns what you say into text on this Mac and sends that text to "
    "the agent you are talking to. Your voice recording never leaves the Mac."
)


def _plist(name):
    path = APP / name
    assert path.is_file(), f"{path} is missing"
    with path.open("rb") as fh:
        return plistlib.load(fh)


# ── Info.plist ───────────────────────────────────────────────────────────────

def test_info_plist_carries_every_c2_key():
    info = _plist("Info.plist")
    expected = {
        "CFBundleName": "Shaliach",
        "CFBundleDisplayName": "Shaliach",
        "CFBundleExecutable": "$(EXECUTABLE_NAME)",
        "CFBundleIdentifier": "$(PRODUCT_BUNDLE_IDENTIFIER)",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "$(MARKETING_VERSION)",
        "CFBundleVersion": "$(CURRENT_PROJECT_VERSION)",
        "CFBundleIconName": "AppIcon",
        "LSMinimumSystemVersion": "14.0",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "ITSAppUsesNonExemptEncryption": False,
        "NSSupportsAutomaticTermination": False,
        "NSSupportsSuddenTermination": False,
        "NSMicrophoneUsageDescription": MIC_TEXT,
        "NSSpeechRecognitionUsageDescription": SPEECH_TEXT,
        "NSLocalNetworkUsageDescription": (
            "Shaliach connects to your agent server on your local network."
        ),
    }
    wrong = {k: (info.get(k), v) for k, v in expected.items() if info.get(k) != v}
    assert not wrong, f"Info.plist keys differ from C2 (got, want): {wrong}"


def test_copyright_names_a_year_and_a_holder():
    """The holder is the build's (DECK_COPYRIGHT, per build machine); the
    project's default still names a year and a holder."""
    info = _plist("Info.plist")
    assert info.get("NSHumanReadableCopyright") == "$(DECK_COPYRIGHT)"
    spec = (APP / "project.yml").read_text()
    default = re.search(r'^\s*DECK_COPYRIGHT:\s*"([^"]*)"', spec, re.M)
    assert default and re.fullmatch(r"© 20\d\d \S.*", default.group(1))


# ── Entitlements, one file per channel ───────────────────────────────────────

def test_direct_entitlements_are_audio_input_only():
    ent = _plist("AgentDeck-Direct.entitlements")
    assert ent == {"com.apple.security.device.audio-input": True}


def test_appstore_entitlements_are_exactly_the_sandbox_four():
    ent = _plist("AgentDeck-AppStore.entitlements")
    assert ent == {
        "com.apple.security.app-sandbox": True,
        "com.apple.security.network.client": True,
        "com.apple.security.device.audio-input": True,
        "com.apple.security.files.user-selected.read-only": True,
    }


# ── Privacy manifest ─────────────────────────────────────────────────────────

def test_privacy_manifest_declares_no_tracking_and_userdefaults_reason():
    manifest = _plist("PrivacyInfo.xcprivacy")
    assert manifest["NSPrivacyTracking"] is False
    assert manifest["NSPrivacyTrackingDomains"] == []
    assert manifest["NSPrivacyCollectedDataTypes"] == []
    assert manifest["NSPrivacyAccessedAPITypes"] == [{
        "NSPrivacyAccessedAPIType": "NSPrivacyAccessedAPICategoryUserDefaults",
        "NSPrivacyAccessedAPITypeReasons": ["CA92.1"],
    }]


# ── Icon ─────────────────────────────────────────────────────────────────────

def test_asset_catalog_has_a_1024_app_icon():
    import json
    iconset = APP / "Assets.xcassets" / "AppIcon.appiconset"
    contents = json.loads((iconset / "Contents.json").read_text())
    files = [i.get("filename") for i in contents["images"] if i.get("filename")]
    assert files, "AppIcon.appiconset names no image"
    for name in files:
        assert (iconset / name).is_file(), f"{name} is listed but missing"
    big = [i for i in contents["images"]
           if i.get("filename") and i.get("size") == "512x512" and i.get("scale") == "2x"]
    assert big, "no 512x512@2x (1024 px) mac icon"


# ── The xcodegen spec: two channels + the UI tests ──────────────────────────

def _spec():
    text = (APP / "project.yml").read_text()
    return text


def test_project_defines_both_channels_and_the_ui_tests():
    spec = _spec()
    for target in ("AgentDeck:", "AgentDeckMAS:", "DeckUITests:"):
        assert re.search(rf"^  {target}", spec, re.M), f"no target {target}"


def test_each_channel_has_its_compile_condition_and_entitlements():
    spec = _spec()
    direct = spec.split("\n  AgentDeck:\n", 1)[1].split("\n  AgentDeckMAS:\n", 1)[0]
    mas = spec.split("\n  AgentDeckMAS:\n", 1)[1].split("\n  DeckUITests:\n", 1)[0]
    assert "DECK_DIRECT" in direct and "DECK_APPSTORE" not in direct
    assert "AgentDeck-Direct.entitlements" in direct
    assert re.search(r"ENABLE_HARDENED_RUNTIME:\s*\"?YES", direct)
    assert "ENABLE_APP_SANDBOX" not in direct
    assert "DECK_APPSTORE" in mas and "DECK_DIRECT" not in mas
    assert "AgentDeck-AppStore.entitlements" in mas
    assert re.search(r"ENABLE_APP_SANDBOX:\s*\"?YES", mas)
    assert "DeckBridgeExec" not in mas, "the App Store build must never link the bridge executor"
    assert re.search(r'ARCHS:\s*"?arm64 x86_64"?\s*$', spec, re.M), "not universal"
    for body in (direct, mas):
        assert "INFOPLIST_FILE: Info.plist" in body
        assert "PRODUCT_BUNDLE_IDENTIFIER: $(DECK_BUNDLE_ID)" in body


def test_ui_tests_compile_the_files_beside_deckuitests():
    """F6: OverhaulScreenshotTests + MacBridgeUITests sat outside every target."""
    spec = _spec()
    ui = spec.split("\n  DeckUITests:\n", 1)[1]
    assert re.search(r"path:\s*\.\./UITests\s*$", ui, re.M), \
        "DeckUITests must take ../UITests (all of it), not only ../UITests/DeckUITests"
    assert "TEST_TARGET_NAME: AgentDeck" in ui


def test_the_old_ui_tests_spec_is_retired():
    old = REPO / "macos" / "UITests" / "project.yml"
    if old.exists():
        body = old.read_text()
        assert "targets:" not in body and "App/project.yml" in body
