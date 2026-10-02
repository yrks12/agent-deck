"""macos/Scripts/preflight.sh catches every locally checkable submission defect.

Gate G-static of docs/plans/2026-09-30-submission-ready.md. Each test builds a
small but REAL signed .app in a temp dir (a universal Mach-O from clang, signed
ad-hoc by codesign with the channel's entitlements), breaks exactly one thing,
and asserts preflight names that one check as FAIL. The good bundles must pass
clean, so a check that fails everything cannot hide here either.

macOS only (clang, lipo, codesign, nm). No network, no keychain, no Xcode
project, nothing launched.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PREFLIGHT = REPO / "macos" / "Scripts" / "preflight.sh"
APP_DIR = REPO / "macos" / "App"
VERSION = (REPO / "VERSION").read_text().strip()

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("clang") or not shutil.which("codesign"),
    reason="preflight inspects Mach-O signatures; needs macOS with clang + codesign",
)

PLAIN_C = "int main(void){return 0;}\n"
SPAWNING_C = (
    "#include <spawn.h>\n"
    "extern char **environ;\n"
    "int main(void){pid_t p; char *a[]={\"/usr/bin/sandbox-exec\",0};"
    "return posix_spawn(&p,a[0],0,0,a,environ);}\n"
)

DIRECT_ENT = {"com.apple.security.device.audio-input": True}
MAS_ENT = {
    "com.apple.security.app-sandbox": True,
    "com.apple.security.network.client": True,
    "com.apple.security.device.audio-input": True,
    "com.apple.security.files.user-selected.read-only": True,
}


#: The neutral id a build gets with no DECK_BUNDLE_ID (macos/Scripts/bundle-id.sh).
BUNDLE_ID = "dev.agentdeck.app"


def _info():
    """The C2 Info.plist with the build variables filled in, as Xcode would."""
    with (APP_DIR / "Info.plist").open("rb") as fh:
        info = plistlib.load(fh)
    subs = {
        "$(EXECUTABLE_NAME)": "Agent Deck",
        "$(PRODUCT_BUNDLE_IDENTIFIER)": BUNDLE_ID,
        "$(DECK_COPYRIGHT)": "© 2026 Agent Deck contributors",
        "$(MARKETING_VERSION)": VERSION,
        "$(CURRENT_PROJECT_VERSION)": "412",
    }
    return {k: subs.get(v, v) if isinstance(v, str) else v for k, v in info.items()}


def make_app(tmp_path, *, entitlements, archs=("arm64", "x86_64"), source=PLAIN_C,
             runtime=True, info_edit=None, privacy=True, icon=True):
    app = tmp_path / "Agent Deck.app"
    macos = app / "Contents" / "MacOS"
    res = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    res.mkdir(parents=True)

    src = tmp_path / "main.c"
    src.write_text(source)
    cmd = ["clang", "-o", str(macos / "Agent Deck"), str(src), "-mmacosx-version-min=14.0"]
    for arch in archs:
        cmd += ["-arch", arch]
    subprocess.run(cmd, check=True, capture_output=True)

    info = _info()
    if info_edit:
        info_edit(info)
    with (app / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump(info, fh)
    if privacy:
        shutil.copy(APP_DIR / "PrivacyInfo.xcprivacy", res / "PrivacyInfo.xcprivacy")
    if icon:
        (res / "Assets.car").write_bytes(b"not really a car, presence is what is checked")

    ent = tmp_path / "app.entitlements"
    with ent.open("wb") as fh:
        plistlib.dump(entitlements, fh)
    sign = ["codesign", "--force", "--sign", "-", "--entitlements", str(ent)]
    if runtime:
        sign += ["--options", "runtime"]
    subprocess.run(sign + [str(app)], check=True, capture_output=True)
    return app


def preflight(channel, app, **env):
    # DECK_BUNDLE_ID pinned: a build machine's own build.env must not leak in.
    full = {**os.environ, "DECK_EXPECT_VERSION": VERSION, "DECK_BUNDLE_ID": BUNDLE_ID, **env}
    proc = subprocess.run(["bash", str(PREFLIGHT), channel, str(app)],
                          capture_output=True, text=True, env=full, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


def failed(out):
    return {line.split()[1] for line in out.splitlines() if line.startswith("FAIL ")}


# ── The good bundles pass ────────────────────────────────────────────────────

def test_a_correct_direct_bundle_passes(tmp_path):
    code, out = preflight("direct", make_app(tmp_path, entitlements=DIRECT_ENT))
    assert code == 0, out
    assert not failed(out), out
    for check in ("plist-keys", "version", "privacy-manifest", "icon", "arch",
                  "signature", "entitlements", "get-task-allow", "sandbox",
                  "hardened-runtime", "forbidden-symbols"):
        assert f"PASS {check}" in out, f"{check} not reported as PASS:\n{out}"


def test_a_correct_appstore_bundle_passes(tmp_path):
    code, out = preflight("appstore", make_app(tmp_path, entitlements=MAS_ENT))
    assert code == 0, out
    assert not failed(out), out
    assert "PASS forbidden-symbols" in out


def test_an_ad_hoc_signature_is_a_warning_not_a_failure(tmp_path):
    """No Apple identity exists yet (D1); preflight must say so without failing."""
    _, out = preflight("direct", make_app(tmp_path, entitlements=DIRECT_ENT))
    assert "WARN identity" in out


# ── Each defect is named ─────────────────────────────────────────────────────

def test_the_app_store_build_must_not_spawn_processes(tmp_path):
    app = make_app(tmp_path, entitlements=MAS_ENT, source=SPAWNING_C)
    code, out = preflight("appstore", app)
    assert code == 1
    assert failed(out) == {"forbidden-symbols"}, out
    assert "posix_spawn" in out and "sandbox-exec" in out


def test_the_direct_build_may_spawn_processes(tmp_path):
    app = make_app(tmp_path, entitlements=DIRECT_ENT, source=SPAWNING_C)
    code, out = preflight("direct", app)
    assert code == 0, out


def test_app_store_without_the_sandbox_fails(tmp_path):
    ent = {k: v for k, v in MAS_ENT.items() if k != "com.apple.security.app-sandbox"}
    code, out = preflight("appstore", make_app(tmp_path, entitlements=ent))
    assert code == 1
    assert {"sandbox", "entitlements"} <= failed(out), out


def test_direct_with_the_sandbox_fails(tmp_path):
    code, out = preflight("direct", make_app(tmp_path, entitlements=MAS_ENT))
    assert code == 1
    assert {"sandbox", "entitlements"} <= failed(out), out


def test_forbidden_app_store_entitlements_fail(tmp_path):
    ent = {**MAS_ENT, "com.apple.security.network.server": True}
    code, out = preflight("appstore", make_app(tmp_path, entitlements=ent))
    assert code == 1
    assert "entitlements" in failed(out) and "network.server" in out


def test_get_task_allow_fails_a_release(tmp_path):
    ent = {**DIRECT_ENT, "com.apple.security.get-task-allow": True}
    code, out = preflight("direct", make_app(tmp_path, entitlements=ent))
    assert code == 1
    assert "get-task-allow" in failed(out), out


def test_direct_without_hardened_runtime_fails(tmp_path):
    app = make_app(tmp_path, entitlements=DIRECT_ENT, runtime=False)
    code, out = preflight("direct", app)
    assert code == 1
    assert failed(out) == {"hardened-runtime"}, out


def test_a_single_arch_binary_fails(tmp_path):
    app = make_app(tmp_path, entitlements=DIRECT_ENT, archs=("arm64",))
    code, out = preflight("direct", app)
    assert code == 1
    assert failed(out) == {"arch"}, out
    assert "x86_64" in out


def test_a_missing_plist_key_fails_and_is_named(tmp_path):
    app = make_app(tmp_path, entitlements=DIRECT_ENT,
                   info_edit=lambda i: i.pop("ITSAppUsesNonExemptEncryption"))
    code, out = preflight("direct", app)
    assert code == 1
    assert failed(out) == {"plist-keys"}, out
    assert "ITSAppUsesNonExemptEncryption" in out


def test_automatic_termination_true_fails(tmp_path):
    def edit(info):
        info["NSSupportsAutomaticTermination"] = True
    code, out = preflight("direct", make_app(tmp_path, entitlements=DIRECT_ENT, info_edit=edit))
    assert code == 1
    assert "NSSupportsAutomaticTermination" in out and failed(out) == {"plist-keys"}


def test_a_version_other_than_the_repo_version_fails(tmp_path):
    def edit(info):
        info["CFBundleShortVersionString"] = "0.1"
    code, out = preflight("direct", make_app(tmp_path, entitlements=DIRECT_ENT, info_edit=edit))
    assert code == 1
    assert failed(out) == {"version"}, out


def test_a_non_integer_build_number_fails(tmp_path):
    def edit(info):
        info["CFBundleVersion"] = "0.9.0"
    code, out = preflight("direct", make_app(tmp_path, entitlements=DIRECT_ENT, info_edit=edit))
    assert failed(out) == {"version"}, out


def test_a_missing_privacy_manifest_fails(tmp_path):
    app = make_app(tmp_path, entitlements=DIRECT_ENT, privacy=False)
    code, out = preflight("direct", app)
    assert code == 1
    assert failed(out) == {"privacy-manifest"}, out


def test_a_missing_icon_fails(tmp_path):
    app = make_app(tmp_path, entitlements=DIRECT_ENT, icon=False)
    code, out = preflight("direct", app)
    assert failed(out) == {"icon"}, out


def test_a_missing_bundle_fails_cleanly(tmp_path):
    code, out = preflight("direct", tmp_path / "Nope.app")
    assert code == 1
    assert "FAIL bundle" in out


def test_an_unknown_channel_is_a_usage_error(tmp_path):
    code, out = preflight("testflight", tmp_path)
    assert code == 2
    assert "direct" in out and "appstore" in out
