"""scripts/install-iphone.sh: one command puts the iPhone app on a phone with a
free Apple ID. Every external tool is a fake on PATH; HOME is a tmp dir."""

import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "install-iphone.sh"

FAKE_XCRUN = r"""#!/bin/bash
echo "xcrun $*" >> "$FAKE_LOG"
if [ "$1 $2 $3" = "devicectl list devices" ]; then
  out=""
  while [ $# -gt 0 ]; do [ "$1" = "--json-output" ] && out="$2"; shift; done
  cp "$FAKE_DEVICES" "$out"
  exit 0
fi
exit 0
"""
FAKE_XCODEBUILD = r"""#!/bin/bash
echo "xcodebuild $*" >> "$FAKE_LOG"
dd=""
while [ $# -gt 0 ]; do [ "$1" = "-derivedDataPath" ] && dd="$2"; shift; done
mkdir -p "$dd/Build/Products/Debug-iphoneos/Agent Deck.app"
"""
FAKE_XCODEGEN = '#!/bin/bash\necho "xcodegen $*" >> "$FAKE_LOG"\necho "bundle=$DECK_BUNDLE_ID" >> "$FAKE_LOG"\n'
FAKE_GIT = r"""#!/bin/bash
echo "git $*" >> "$FAKE_LOG"
if [ "$1" = clone ]; then
  for last; do :; done
  mkdir -p "$last/ios" "$last/.git" && touch "$last/ios/project.yml"
fi
exit 0
"""
FAKE_DEFAULTS = r"""#!/bin/bash
[ -n "${FAKE_TEAMS_FILE:-}" ] && [ -f "$FAKE_TEAMS_FILE" ] && { cat "$FAKE_TEAMS_FILE"; exit 0; }
echo "The domain/default pair does not exist" >&2
exit 1
"""
FAKE_SECURITY = r"""#!/bin/bash
if [ "$1" = find-certificate ] && [ -n "${FAKE_CERT_TEAMS:-}" ]; then
  for t in $FAKE_CERT_TEAMS; do
    printf -- '-----BEGIN CERTIFICATE-----\nOU=%s\n-----END CERTIFICATE-----\n' "$t"
  done
fi
exit 0
"""
FAKE_OPENSSL = r"""#!/bin/bash
sed -n 's/^OU=/subject=UID=x, CN=Apple Development: a (b), OU=/p'
"""

TEAMS_ONE = """{
    "X" = (
        {
            isFreeProvisioningTeam = 1;
            teamID = ABCDE12345;
            teamName = "A (Personal Team)";
        }
    );
}
"""
TEAMS_TWO = TEAMS_ONE + TEAMS_ONE.replace("ABCDE12345", "ZZZZZ99999")


def device(ident, state="connected", kind="iPhone", name="iPhone", udid=None):
    hw = {"deviceType": kind, "marketingName": name}
    if udid:
        hw["udid"] = udid
    return {
        "identifier": ident,
        "connectionProperties": {"tunnelState": state},
        "hardwareProperties": hw,
        "deviceProperties": {"name": name},
    }


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {
        "xcrun": FAKE_XCRUN, "xcodebuild": FAKE_XCODEBUILD, "xcodegen": FAKE_XCODEGEN,
        "git": FAKE_GIT, "defaults": FAKE_DEFAULTS, "security": FAKE_SECURITY,
        "openssl": FAKE_OPENSSL,
    }.items():
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    (bindir / "python3").symlink_to(sys.executable)
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("")
    teams = tmp_path / "teams.txt"
    teams.write_text(TEAMS_ONE)
    work = tmp_path / "work"
    work.mkdir()
    e = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(home),
        "FAKE_LOG": str(log),
        "FAKE_DEVICES": str(tmp_path / "devices.json"),
        "FAKE_TEAMS_FILE": str(teams),
        "TMPDIR": str(tmp_path),
    }
    return {"env": e, "home": home, "log": log, "devices": tmp_path / "devices.json",
            "teams": teams, "bin": bindir, "tmp": tmp_path, "work": work}


def set_devices(ctx, *devs):
    ctx["devices"].write_text(json.dumps({"result": {"devices": list(devs)}}))


def run(ctx, *args, script=SCRIPT, extra_env=None):
    e = dict(ctx["env"])
    e.update(extra_env or {})
    return subprocess.run(
        ["/bin/bash", str(script), *args], env=e, capture_output=True, text=True, cwd=str(ctx["work"])
    )


def calls(ctx):
    return ctx["log"].read_text().splitlines()


def xcodebuild_call(ctx):
    return [c for c in calls(ctx) if c.startswith("xcodebuild")][0]


def test_script_exists_and_is_bash():
    assert SCRIPT.is_file()
    assert SCRIPT.read_text().startswith("#!/usr/bin/env bash")


def test_one_connected_iphone_is_used(env):
    set_devices(env, device("AAA-111"), device("OLD-000", state="unavailable"), device("WATCH-1", kind="appleWatch"))
    r = run(env)
    assert r.returncode == 0, r.stderr
    inst = [c for c in calls(env) if "device install app" in c]
    assert len(inst) == 1 and "--device AAA-111" in inst[0]
    assert inst[0].endswith("Agent Deck.app")


def test_no_device_is_a_clear_error(env):
    set_devices(env, device("OLD-000", state="unavailable"))
    r = run(env)
    assert r.returncode != 0
    assert "no iphone" in (r.stdout + r.stderr).lower()
    assert not [c for c in calls(env) if c.startswith("xcodebuild")]


def test_several_devices_is_an_error_naming_them_unless_device_given(env):
    set_devices(env, device("AAA-111"), device("BBB-222"))
    r = run(env)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "AAA-111" in out and "BBB-222" in out and "--device" in out
    r = run(env, "--device", "BBB-222")
    assert r.returncode == 0, r.stderr
    assert any("--device BBB-222" in c and "device install app" in c for c in calls(env))


def test_team_is_detected_from_xcode(env):
    set_devices(env, device("AAA-111"))
    r = run(env)
    assert r.returncode == 0, r.stderr
    assert "DEVELOPMENT_TEAM=ABCDE12345" in xcodebuild_call(env)


def test_team_is_detected_from_certificate_when_xcode_has_none(env):
    set_devices(env, device("AAA-111"))
    env["teams"].unlink()
    r = run(env, extra_env={"FAKE_CERT_TEAMS": "QQQQQ11111"})
    assert r.returncode == 0, r.stderr
    assert "DEVELOPMENT_TEAM=QQQQQ11111" in xcodebuild_call(env)


def test_team_flag_wins_over_detection(env):
    set_devices(env, device("AAA-111"))
    env["teams"].write_text(TEAMS_TWO)
    r = run(env, "--team", "MANUAL0001")
    assert r.returncode == 0, r.stderr
    assert "DEVELOPMENT_TEAM=MANUAL0001" in xcodebuild_call(env)


def test_no_team_explains_adding_an_apple_id(env):
    set_devices(env, device("AAA-111"))
    env["teams"].unlink()
    r = run(env)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "Settings" in out and "Accounts" in out and "Apple ID" in out
    assert not [c for c in calls(env) if c.startswith("xcodebuild")]


def test_ambiguous_teams_ask_for_team_flag(env):
    set_devices(env, device("AAA-111"))
    env["teams"].write_text(TEAMS_TWO)
    r = run(env)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "ABCDE12345" in out and "ZZZZZ99999" in out and "--team" in out


def test_bundle_suffix_is_persisted_and_reused(env):
    set_devices(env, device("AAA-111"))
    assert run(env).returncode == 0
    cfg = env["home"] / ".config" / "agent-deck" / "iphone.env"
    assert cfg.is_file()
    first = cfg.read_text()
    m = re.search(r"IPHONE_BUNDLE_SUFFIX=(\S+)", first)
    assert m, first
    suffix = m.group(1).strip("'\"")
    xb1 = xcodebuild_call(env)
    assert f"PRODUCT_BUNDLE_IDENTIFIER=dev.agentdeck.{suffix}.ios" in xb1
    env["log"].write_text("")
    assert run(env).returncode == 0
    assert cfg.read_text() == first
    assert xcodebuild_call(env) == xb1


def test_persisted_suffix_is_honoured_over_a_fresh_derivation(env):
    set_devices(env, device("AAA-111"))
    cfg = env["home"] / ".config" / "agent-deck"
    cfg.mkdir(parents=True)
    (cfg / "iphone.env").write_text("IPHONE_TEAM=ABCDE12345\nIPHONE_BUNDLE_SUFFIX=kept42\n")
    assert run(env).returncode == 0
    assert "PRODUCT_BUNDLE_IDENTIFIER=dev.agentdeck.kept42.ios" in xcodebuild_call(env)


def test_xcodegen_runs_on_the_ios_spec_with_the_same_bundle_id(env):
    set_devices(env, device("AAA-111"))
    r = run(env)
    assert r.returncode == 0, r.stderr
    cs = calls(env)
    gen = [c for c in cs if c.startswith("xcodegen")]
    assert gen and "ios/project.yml" in gen[0]
    bundle = [c for c in cs if c.startswith("bundle=")][0]
    assert re.match(r"bundle=dev\.agentdeck\.\w+$", bundle)
    assert f"PRODUCT_BUNDLE_IDENTIFIER={bundle[len('bundle='):]}.ios" in xcodebuild_call(env)


def test_xcodebuild_flags(env):
    set_devices(env, device("AAA-111"))
    assert run(env).returncode == 0
    xb = xcodebuild_call(env)
    assert "-allowProvisioningUpdates" in xb
    assert "-allowProvisioningDeviceRegistration" in xb
    assert "-destination id=AAA-111" in xb
    assert "-derivedDataPath" in xb
    assert "CODE_SIGN_STYLE=Automatic" in xb
    # SwiftTerm ships a build plug-in; a first command-line build fails on
    # "Validate plug-in" unless it is skipped (measured with Xcode 26).
    assert "-skipPackagePluginValidation" in xb


def test_xcodebuild_gets_the_udid_not_the_coredevice_identifier(env):
    # xcodebuild -destination id= only knows the hardware UDID; devicectl's own
    # CoreDevice identifier makes it fail with "Unable to find a device" (measured on
    # a real iPhone 16). devicectl install accepts the UDID too.
    set_devices(env, device("CD77C815-B713", udid="00008140-0001"))
    assert run(env).returncode == 0
    assert "-destination id=00008140-0001" in xcodebuild_call(env)
    assert any("install app --device 00008140-0001" in c for c in calls(env))


def test_missing_xcodegen_says_brew_install(env):
    set_devices(env, device("AAA-111"))
    (env["bin"] / "xcodegen").unlink()
    r = run(env)
    assert r.returncode != 0
    assert "brew install xcodegen" in r.stdout + r.stderr


def test_prints_trust_and_seven_day_text(env):
    set_devices(env, device("AAA-111"))
    out = run(env).stdout
    assert "VPN & Device Management" in out
    assert "7 days" in out
    assert "re-run" in out.lower()


def test_dry_run_runs_no_build_and_no_install(env):
    set_devices(env, device("AAA-111"))
    r = run(env, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert not [c for c in calls(env) if c.startswith("xcodebuild") or "device install" in c]
    assert "xcodebuild" in r.stdout and "device install app" in r.stdout


def test_inside_a_checkout_uses_it_and_does_not_clone(env):
    set_devices(env, device("AAA-111"))
    assert run(env).returncode == 0
    assert not [c for c in calls(env) if c.startswith("git clone")]


def _piped_copy(ctx):
    lone = ctx["tmp"] / "lone"
    lone.mkdir()
    s = lone / "install-iphone.sh"
    s.write_text(SCRIPT.read_text())
    return s


def test_outside_a_checkout_clones_the_repo_slug(env):
    set_devices(env, device("AAA-111"))
    r = run(env, script=_piped_copy(env))
    assert r.returncode == 0, r.stderr
    clone = [c for c in calls(env) if c.startswith("git clone")]
    assert clone and f"https://github.com/{_default_slug()}.git" in clone[0]
    assert str(env["home"] / "agent-deck-src") in clone[0]


def test_outside_a_checkout_rerun_fetches_and_fast_forwards(env):
    set_devices(env, device("AAA-111"))
    s = _piped_copy(env)
    assert run(env, script=s).returncode == 0
    env["log"].write_text("")
    assert run(env, script=s).returncode == 0
    cs = calls(env)
    assert not [c for c in cs if c.startswith("git clone")]
    assert any(" fetch" in c for c in cs) and any("--ff-only" in c for c in cs)


def test_src_flag_and_repo_override(env):
    set_devices(env, device("AAA-111"))
    dest = env["tmp"] / "elsewhere"
    r = run(env, "--src", str(dest), script=_piped_copy(env),
            extra_env={"AGENT_DECK_REPO": "someone/else", "AGENT_DECK_REF": "v9"})
    assert r.returncode == 0, r.stderr
    clone = [c for c in calls(env) if c.startswith("git clone")][0]
    assert "someone/else" in clone and str(dest) in clone and "v9" in clone


def _default_slug() -> str:
    m = re.findall(r'^AGENT_DECK_REPO="\$\{AGENT_DECK_REPO:-([^}]+)\}"$', SCRIPT.read_text(), re.M)
    assert len(m) == 1, m
    return m[0]


def test_repo_slug_is_one_variable_near_the_top():
    text = SCRIPT.read_text()
    lines = text.splitlines()
    slug = _default_slug()
    idx = [i for i, l in enumerate(lines) if l.startswith("AGENT_DECK_REPO=")]
    assert len(idx) == 1 and idx[0] < 30
    assert any(l == 'AGENT_DECK_REF="${AGENT_DECK_REF:-main}"' for l in lines[:35])
    # the slug is written once; besides that line only the usage comment shows it
    others = [l for l in lines if slug in l and not l.startswith("AGENT_DECK_REPO=")]
    assert all(l.startswith("#") for l in others), others
    assert any(l.lstrip("# ") == f"curl -fsSL https://raw.githubusercontent.com/{slug}/main/scripts/install-iphone.sh | bash" for l in lines)


def test_curl_one_liner_usage_line_present():
    text = SCRIPT.read_text()
    assert "curl -fsSL https://raw.githubusercontent.com/" in text
    assert "| bash" in text


def test_strict_mode_and_main_guard():
    text = SCRIPT.read_text()
    assert "set -euo pipefail" in text
    nonblank = [l for l in text.splitlines() if l.strip()]
    assert nonblank[-1] == 'main "$@"'
    assert text.count("main()") == 1


def test_a_truncated_download_executes_nothing(env):
    set_devices(env, device("AAA-111"))
    lines = SCRIPT.read_text().splitlines()
    cut = env["tmp"] / "cut.sh"
    cut.write_text("\n".join(lines[: len(lines) // 2]) + "\n")
    r = run(env, script=cut)
    assert r.returncode != 0
    assert calls(env) == []
    assert not (env["home"] / ".config").exists()
