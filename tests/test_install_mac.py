"""scripts/install-mac.sh and scripts/release-mac.sh: the Mac app in one command.

    curl -fsSL https://raw.githubusercontent.com/<OWNER>/<REPO>/main/scripts/install-mac.sh | bash -s -- --pair ADK1...

install-mac.sh takes the latest release .zip when there is one (and refuses it unless
its .sha256 matches), otherwise builds from source with `make app`. It installs into
--prefix (default /Applications), clears the quarantine flag, and opens the app,
handing it --pair CODE so the Connect screen opens pre-filled.

release-mac.sh builds the ad-hoc-signed .zip and its checksum, and publishes nothing.

Every external tool is a fake on PATH; the script is fed on stdin as curl would.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-mac.sh"
RELEASE = ROOT / "scripts" / "release-mac.sh"
SLUG = "example-owner/agent-deck-fork"
APP = "Agent Deck.app"


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_app(where: Path, marker: str) -> Path:
    app = where / APP
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "MacOS" / "Agent Deck").write_text(marker)
    return app


LOG = 'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$FAKE_LOG"\n'


class Mac:
    def __init__(self, tmp: Path, uname="Darwin"):
        self.tmp = tmp
        self.log = tmp / "calls.log"
        self.log.touch()
        self.bin = tmp / "fakebin"
        self.home = tmp / "home"
        self.home.mkdir()
        self.prefix = tmp / "Applications"
        self.prefix.mkdir()
        self.src = tmp / "src"
        (self.src / "macos").mkdir(parents=True)
        (self.src / "macos" / "Package.swift").write_text("// fake\n")
        self.downloads = tmp / "downloads"
        self.downloads.mkdir()
        self.api_json = tmp / "latest.json"  # absent = no release (API 404)
        self.list_json = tmp / "list.json"  # the all-releases list; absent = 404
        self.uname = uname
        b = self.bin
        _exe(b / "uname", '[ "$1" = -s ] && { echo "$FAKE_UNAME"; exit 0; }; exec /usr/bin/uname "$@"\n')
        # curl: the releases API answers from latest.json (or 404s); a download copies
        # the file of the same basename out of downloads/.
        _exe(b / "curl", LOG + r'''
out=""; url=""; hdrs=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; -H) hdrs="$hdrs|$2"; shift ;; http*) url="$1" ;; esac; shift
done
# FAKE_PRIVATE: a private repo. Everything needs the token, the browser URL 404s even
# with it, and the asset API hands over the file only when asked for octet-stream.
if [ -n "${FAKE_PRIVATE:-}" ]; then
  case "$hdrs" in *"Authorization: token "*) ;; *) exit 22 ;; esac
  case "$url" in */releases/download/*) exit 22 ;; esac
  case "$url" in */releases/assets/*) case "$hdrs" in *"Accept: application/octet-stream"*) ;; *) exit 22 ;; esac ;; esac
fi
case "$url" in
  */releases/latest) [ -f "$FAKE_API_JSON" ] || exit 22; cat "$FAKE_API_JSON" ;;
  */releases\?per_page=*) [ -f "$FAKE_LIST_JSON" ] || exit 22; cat "$FAKE_LIST_JSON" ;;
  *) f="$FAKE_DOWNLOADS/$(basename "$url")"; [ -f "$f" ] || exit 22
     if [ -n "$out" ]; then cp "$f" "$out"; else cat "$f"; fi ;;
esac
''')
        _exe(b / "ditto", LOG + r'''
if [ "$1" = -x ]; then shift 2; mkdir -p "$2"; unzip -q -o "$1" -d "$2"
elif [ "$1" = -c ]; then shift; while [ "${1#-}" != "$1" ]; do shift; done; (cd "$(dirname "$1")" && zip -qr "$2" "$(basename "$1")")
else cp -R "$1" "$2"; fi
''')
        _exe(b / "xattr", LOG)
        _exe(b / "open", LOG)
        _exe(b / "osascript", LOG)
        _exe(b / "pgrep", LOG + '[ -f "$FAKE_STATE/running" ]\n')
        _exe(b / "swift", "exit 0\n")
        _exe(b / "make", LOG + r'''
d=""; while [ $# -gt 0 ]; do [ "$1" = -C ] && d="$2"; shift; done
mkdir -p "$d/macos/dist/Agent Deck.app/Contents/MacOS"
printf 'source %s' "$(cat "$FAKE_STATE/build-no" 2>/dev/null || echo 1)" > "$d/macos/dist/Agent Deck.app/Contents/MacOS/Agent Deck"
''')
        (tmp / "state").mkdir()

    def release(self, version="1.2.3", marker="release", corrupt=False, prerelease=False):
        stage = self.tmp / "stage"
        _make_app(stage, marker)
        name = f"AgentDeck-{version}-macos.zip"
        zpath = self.downloads / name
        with zipfile.ZipFile(zpath, "w") as z:
            for p in sorted((stage / APP).rglob("*")):
                z.write(p, p.relative_to(stage).as_posix())
        digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
        if corrupt:
            digest = "0" * 64
        (self.downloads / (name + ".sha256")).write_text(f"{digest}  {name}\n")
        base = f"https://github.com/{SLUG}/releases/download/v{version}"
        api = f"https://api.github.com/repos/{SLUG}/releases/assets"
        rel = {"url": f"https://api.github.com/repos/{SLUG}/releases/1", "tag_name": f"v{version}",
               "name": f"Agent Deck {version}", "prerelease": prerelease, "assets": [
                   {"url": f"{api}/{name}", "name": name, "browser_download_url": f"{base}/{name}"},
                   {"url": f"{api}/{name}.sha256", "name": name + ".sha256",
                    "browser_download_url": f"{base}/{name}.sha256"},
               ]}
        if prerelease:  # GitHub's /releases/latest never returns a pre-release
            self.list_json.write_text(json.dumps([rel], indent=2))
        else:
            self.api_json.write_text(json.dumps(rel, indent=2))

    def run(self, *args, script: str | None = None, extra_env=None, from_stdin=True):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "FAKE_STATE": str(self.tmp / "state"),
            "FAKE_UNAME": self.uname,
            "FAKE_API_JSON": str(self.api_json),
            "FAKE_LIST_JSON": str(self.list_json),
            "FAKE_DOWNLOADS": str(self.downloads),
            "AGENT_DECK_REPO": SLUG,
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        env.update(extra_env or {})
        text = SCRIPT.read_text() if script is None else script
        return subprocess.run(["bash", "-s", "--", *args], input=text, env=env,
                              capture_output=True, text=True, timeout=120, start_new_session=True)

    def calls(self):
        return self.log.read_text().splitlines()

    def installed(self) -> str:
        return (self.prefix / APP / "Contents" / "MacOS" / "Agent Deck").read_text()


@pytest.fixture
def mac(tmp_path):
    return Mac(tmp_path)


def test_it_refuses_anything_but_macos(tmp_path):
    m = Mac(tmp_path, uname="Linux")
    r = m.run("--prefix", str(m.prefix), "--src", str(m.src))
    assert r.returncode != 0 and "macOS" in r.stderr
    assert not (m.prefix / APP).exists()


# --- release first ----------------------------------------------------------------

def test_a_release_is_downloaded_verified_and_installed(mac):
    mac.release()
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert mac.installed() == "release"
    assert any(f"api.github.com/repos/{SLUG}/releases/latest" in c for c in mac.calls())
    assert not any(c.startswith("make ") for c in mac.calls())


def test_a_release_with_a_wrong_checksum_is_refused(mac):
    mac.release(corrupt=True)
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode != 0 and "sha256" in r.stderr
    assert not (mac.prefix / APP).exists()


def test_a_release_without_a_checksum_is_refused(mac):
    mac.release()
    data = json.loads(mac.api_json.read_text())
    data["assets"] = [a for a in data["assets"] if not a["name"].endswith(".sha256")]
    mac.api_json.write_text(json.dumps(data))
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode != 0 and "sha256" in r.stderr
    assert not (mac.prefix / APP).exists()


def test_a_pre_release_is_installed_when_there_is_no_stable_one(mac):
    # measured against the real API: /releases/latest 404s while only a pre-release exists
    mac.release(prerelease=True)
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert mac.installed() == "release"
    assert not any(c.startswith("make ") for c in mac.calls())


def test_a_private_repo_downloads_through_the_asset_api_with_gh_token(mac):
    mac.release(prerelease=True)
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src), extra_env={"FAKE_PRIVATE": "1", "GH_TOKEN": "tok-123"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert mac.installed() == "release"
    assert any("releases/assets/" in c and "Accept: application/octet-stream" in c
               and "Authorization: token tok-123" in c for c in mac.calls()), mac.calls()
    assert not any(c.startswith("make ") for c in mac.calls())


def test_a_wrong_checksum_on_a_private_release_still_aborts(mac):
    mac.release(prerelease=True, corrupt=True)
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src), extra_env={"FAKE_PRIVATE": "1", "GH_TOKEN": "tok-123"})
    assert r.returncode != 0 and "sha256" in r.stderr
    assert not (mac.prefix / APP).exists()
    assert not any(c.startswith("make ") for c in mac.calls())


def test_the_token_never_appears_in_output(mac):
    mac.release(prerelease=True)
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src), extra_env={"FAKE_PRIVATE": "1", "GH_TOKEN": "tok-123"})
    assert "tok-123" not in r.stdout + r.stderr


def test_a_private_repo_without_a_token_falls_back_to_source(mac):
    mac.release(prerelease=True)
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src), extra_env={"FAKE_PRIVATE": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("make ") for c in mac.calls())
    assert mac.installed().startswith("source")


def test_an_empty_release_list_falls_back_to_source(mac):
    mac.list_json.write_text("[]")
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("make ") for c in mac.calls())
    assert mac.installed().startswith("source")


# --- source fallback ----------------------------------------------------------------

def test_no_release_falls_back_to_building_from_source(mac):
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("make ") and " app" in c for c in mac.calls()), mac.calls()
    assert mac.installed().startswith("source")


def test_from_source_skips_the_release_lookup(mac):
    mac.release()
    r = mac.run("--from-source", "--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any("releases/latest" in c for c in mac.calls())
    assert mac.installed().startswith("source")


def test_piped_with_no_checkout_it_clones_the_repo_from_the_one_variable(mac, tmp_path):
    origin = tmp_path / "origin"
    (origin / "macos").mkdir(parents=True)
    (origin / "macos" / "Package.swift").write_text("// fake\n")
    genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "one"]):
        subprocess.run(["git", *args], cwd=origin, env=genv, check=True, capture_output=True)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f'[url "{origin}"]\n\tinsteadOf = https://github.com/{SLUG}.git\n')
    r = mac.run("--prefix", str(mac.prefix), extra_env={"GIT_CONFIG_GLOBAL": str(gitconfig)})
    assert r.returncode == 0, r.stdout + r.stderr
    src = mac.home / "agent-deck-src"
    assert (src / "macos" / "Package.swift").exists()
    remote = subprocess.run(["git", "-C", str(src), "config", "--get", "remote.origin.url"],
                            capture_output=True, text=True).stdout.strip()
    assert remote == f"https://github.com/{SLUG}.git"
    assert mac.installed().startswith("source")


# --- install: quarantine, open, pairing -----------------------------------------------

def test_quarantine_is_cleared_on_the_installed_app(mac):
    mac.release()
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"xattr -dr com.apple.quarantine {mac.prefix / APP}" in mac.calls()


def test_it_opens_the_app_with_the_pairing_code(mac):
    r = mac.run("--pair", "ADK1.abc_DEF-123", "--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"open {mac.prefix / APP} --args --pair ADK1.abc_DEF-123" in mac.calls()
    assert "Connect" in r.stdout


def test_it_opens_the_app_without_a_code(mac):
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"open {mac.prefix / APP}" in mac.calls()


def test_no_open_does_not_open(mac):
    r = mac.run("--no-open", "--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(c.startswith("open ") for c in mac.calls())


def test_a_bad_pairing_code_is_refused_before_anything(mac):
    r = mac.run("--pair", "not a code; rm -rf /", "--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode != 0 and "ADK1" in r.stderr
    assert not (mac.prefix / APP).exists()


# --- idempotent ------------------------------------------------------------------

def test_a_re_run_replaces_the_app_in_place(mac):
    assert mac.run("--prefix", str(mac.prefix), "--src", str(mac.src)).returncode == 0
    (mac.tmp / "state" / "build-no").write_text("2")
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert mac.installed() == "source 2"
    assert not (mac.prefix / APP / APP).exists()
    assert sorted(p.name for p in mac.prefix.iterdir()) == [APP]


def test_a_running_copy_at_the_target_is_quit_first(mac):
    assert mac.run("--prefix", str(mac.prefix), "--src", str(mac.src)).returncode == 0
    (mac.tmp / "state" / "running").write_text("")
    r = mac.run("--prefix", str(mac.prefix), "--src", str(mac.src))
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("osascript") and str(mac.prefix / APP) in c for c in mac.calls())


def test_the_default_prefix_is_applications():
    assert re.search(r'prefix="\$\{AGENT_DECK_PREFIX:-/Applications\}"', SCRIPT.read_text())


# --- one variable, main() guard ----------------------------------------------------

def test_the_repo_slug_is_one_variable():
    text = SCRIPT.read_text()
    assigns = re.findall(r'^\s*AGENT_DECK_REPO="\$\{AGENT_DECK_REPO:-([^}]+)\}"', text, re.M)
    assert len(assigns) == 1
    lines = [l for l in text.splitlines() if assigns[0] in l]
    assert all(l.lstrip().startswith(("#", "AGENT_DECK_REPO=")) for l in lines), lines


def test_the_last_line_calls_main():
    lines = [l for l in SCRIPT.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert lines[-1].strip() == 'main "$@"'


@pytest.mark.parametrize("fraction", [0.3, 0.6, 0.9])
def test_a_truncated_download_executes_nothing(mac, fraction):
    text = SCRIPT.read_text()
    mac.run("--prefix", str(mac.prefix), "--src", str(mac.src), script=text[: int(len(text) * fraction)])
    assert mac.calls() == []
    assert not (mac.prefix / APP).exists()


# --- the release builder: ad-hoc .zip + checksum, nothing published ------------------

def test_release_builds_an_ad_hoc_zip_and_checksum_and_publishes_nothing(tmp_path):
    m = Mac(tmp_path)
    repo = tmp_path / "repo"
    (repo / "macos" / "Scripts").mkdir(parents=True)
    (repo / "VERSION").write_text("2.0.1\n")
    _exe(repo / "macos" / "Scripts" / "make-app-bundle.sh",
         'printf "builder bundle=%s env=%s sign=%s\\n" "$DECK_BUNDLE_ID" "$DECK_BUILD_ENV" '
         '"$DECK_SIGN_IDENTITY" >> "$FAKE_LOG"\n'
         'd="$(cd "$(dirname "$0")/.." && pwd)/dist/Agent Deck.app/Contents/MacOS"; mkdir -p "$d"; '
         'echo built > "$d/Agent Deck"\n')
    _exe(m.bin / "codesign", LOG + '[ "$1" = -dv ] && echo "Signature=adhoc" >&2; exit 0\n')
    _exe(m.bin / "gh", LOG)
    env = {"PATH": f"{m.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(m.home),
           "FAKE_LOG": str(m.log), "FAKE_UNAME": "Darwin", "DECK_BUNDLE_ID": "com.someone.private",
           "AGENT_DECK_ROOT": str(repo)}
    r = subprocess.run(["bash", str(RELEASE)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    out = repo / "dist" / "release"
    z = out / "AgentDeck-2.0.1-macos.zip"
    s = out / "AgentDeck-2.0.1-macos.zip.sha256"
    assert z.exists() and s.exists()
    assert s.read_text().split()[0] == hashlib.sha256(z.read_bytes()).hexdigest()
    assert s.read_text().split()[1] == z.name
    with zipfile.ZipFile(z) as zf:
        assert f"{APP}/Contents/MacOS/Agent Deck" in zf.namelist()
    calls = m.calls()
    # a neutral build: never this machine's private bundle id, build.env or signing cert
    builder = [c for c in calls if c.startswith("builder")][0]
    assert "bundle=dev.agentdeck.app" in builder and "com.someone.private" not in builder
    assert "env=/dev/null" in builder
    assert any(c.startswith("codesign") and "--sign -" in c for c in calls), calls
    assert not any(c.startswith(("gh ", "curl ")) for c in calls)
    assert "not published" in r.stdout
