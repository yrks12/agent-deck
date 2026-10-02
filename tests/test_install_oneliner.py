"""install.sh at the repo root: the one command a stranger pipes from curl.

    curl -fsSL https://raw.githubusercontent.com/<OWNER>/<REPO>/main/install.sh | bash

Every test feeds the script to `bash -s` on stdin, exactly as curl does, with no
controlling terminal (start_new_session), and with fakes on PATH for everything that
would touch the machine: apt-get, dpkg, sudo, id, systemctl, launchctl, curl, uv.
`git` is the real one; the "GitHub" it clones from is a local repo, reached through a
git `insteadOf` rewrite so the URL the script builds is the URL that is checked.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.sh"
SLUG = "example-owner/agent-deck-fork"


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class Box:
    """A fake machine: a source repo standing in for GitHub, fakes on PATH, a log."""

    def __init__(self, tmp: Path, os_id="ubuntu", version="24.04", uname="Linux", uid=0):
        self.tmp = tmp
        self.log = tmp / "calls.log"
        self.log.touch()
        self.bin = tmp / "fakebin"
        self.home = tmp / "home"
        self.home.mkdir()
        self.dir = tmp / "opt" / "agent-deck"
        self.src = tmp / "github" / "agent-deck"
        self._make_source()
        self.os_release = tmp / "os-release"
        self.os_release.write_text(f'ID={os_id}\nVERSION_ID="{version}"\n')
        self.uname, self.uid = uname, uid
        self._make_fakes()
        self.gitconfig = tmp / "gitconfig"
        self.gitconfig.write_text(
            f'[url "{self.src}"]\n\tinsteadOf = https://github.com/{SLUG}.git\n'
            "[protocol \"file\"]\n\tallow = always\n")

    def _make_source(self):
        s = self.src
        _exe(s / "deploy" / "install-deck.sh",
             'printf "install-deck %s\\n" "$*" >> "$FAKE_LOG"\n'
             '[ -t 0 ] && echo "install-deck stdin-tty" >> "$FAKE_LOG"; exit 0\n')
        _exe(s / "bin" / "deckctl",
             'printf "deckctl %s\\n" "$*" >> "$FAKE_LOG"\n'
             'case "$1 $2 $3" in\n'
             '  "config get deck.user") echo agentdeck ;;\n'
             '  "config get deck.port") echo 7789 ;;\n'
             '  "config get claude.oauth_env") echo "$FAKE_OAUTH_ENV" ;;\n'
             '  "config get network.hostname") echo "${FAKE_HOSTNAME-203-0-113-7.sslip.io}" ;;\n'
             'esac\n'
             '[ "$1" = pair ] && echo \'{"code": "ADK1.fakecode", "url": "https://x"}\'\n'
             'exit 0\n')
        (s / "VERSION").write_text("1.0.0\n")
        (s / "requirements.txt").write_text("fastapi\n")
        (s / ".gitignore").write_text(".venv/\n")
        _git(s.parent, "init", "-q", "-b", "main", str(s))
        _git(s, "add", "-A")
        _git(s, "commit", "-q", "-m", "one")

    def commit(self, rel: str, text: str):
        (self.src / rel).write_text(text)
        _git(self.src, "add", rel)
        _git(self.src, "commit", "-q", "-m", f"change {rel}")

    def _make_fakes(self):
        b = self.bin
        log = 'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$FAKE_LOG"\n'
        _exe(b / "id", '[ "$1" = -u ] && { echo "$FAKE_UID"; exit 0; }; exec /usr/bin/id "$@"\n')
        _exe(b / "uname", '[ "$1" = -s ] && { echo "$FAKE_UNAME"; exit 0; }\n'
                          '[ "$1" = -m ] && { echo x86_64; exit 0; }; exec /usr/bin/uname "$@"\n')
        _exe(b / "sudo", log + 'exec "$@"\n')
        # dpkg -s says "missing" until apt-get has run once.
        _exe(b / "dpkg", log + '[ -f "$FAKE_STATE/apt-done" ]\n')
        _exe(b / "apt-get", log + 'touch "$FAKE_STATE/apt-done"\n')
        _exe(b / "systemctl", log)
        _exe(b / "launchctl", log + '[ "$1" = print ] && [ ! -f "$FAKE_STATE/loaded" ] && exit 113\n'
                                    '[ "$1" = bootstrap ] && touch "$FAKE_STATE/loaded"; exit 0\n')
        _exe(b / "curl", log + 'case "$*" in *healthz*) [ -f "$FAKE_STATE/loaded" ] || exit 7; '
                                'echo \'{"ok":true}\' ;; esac\n')
        _exe(b / "uv", log + 'if [ "$1" = venv ]; then mkdir -p .venv/bin; '
                             'printf "#!/bin/sh\\nexit 0\\n" > .venv/bin/python; chmod +x .venv/bin/python; fi\n')
        _exe(b / "node", "exit 0\n")
        _exe(b / "claude", "exit 0\n")
        _exe(b / "xcode-select", 'echo /Library/Developer/CommandLineTools\n')
        (self.tmp / "state").mkdir()

    def run(self, *args: str, script: str | None = None, extra_env: dict | None = None):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "FAKE_STATE": str(self.tmp / "state"),
            "FAKE_UID": str(self.uid),
            "FAKE_UNAME": self.uname,
            "FAKE_OAUTH_ENV": str(self.tmp / "oauth.env"),
            "GIT_CONFIG_GLOBAL": str(self.gitconfig),
            "AGENT_DECK_REPO": SLUG,
            "AGENT_DECK_DIR": str(self.dir),
            "AGENT_DECK_OS_RELEASE": str(self.os_release),
            "AGENT_DECK_HEALTH_WAIT": "1",
        }
        env.update(extra_env or {})
        text = SCRIPT.read_text() if script is None else script
        return subprocess.run(["bash", "-s", "--", *args], input=text, env=env,
                              capture_output=True, text=True, timeout=120,
                              start_new_session=True)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


# --- OS detection -----------------------------------------------------------------

@pytest.mark.parametrize("os_id,version", [("ubuntu", "24.04"), ("ubuntu", "24.10"),
                                           ("debian", "12"), ("debian", "13")])
def test_ubuntu_and_debian_take_the_server_path(tmp_path, os_id, version):
    b = Box(tmp_path, os_id=os_id, version=version)
    r = b.run()
    assert r.returncode == 0, r.stdout + r.stderr
    calls = b.calls()
    assert any(c.startswith("apt-get install") and " git" in c for c in calls), calls
    assert any(c.startswith("install-deck") for c in calls), calls
    assert (b.dir / ".git").is_dir()


@pytest.mark.parametrize("os_id,version,needle", [
    ("ubuntu", "22.04", "24.04"), ("ubuntu", "20.04", "24.04"),
    ("debian", "11", "Debian 12"), ("fedora", "40", "Ubuntu"),
])
def test_unsupported_linux_is_refused_before_anything_changes(tmp_path, os_id, version, needle):
    b = Box(tmp_path, os_id=os_id, version=version)
    r = b.run()
    assert r.returncode != 0
    assert needle in r.stderr
    assert not any(c.startswith(("apt-get", "install-deck")) for c in b.calls())
    assert not b.dir.exists()


def test_macos_takes_the_laptop_path(tmp_path):
    b = Box(tmp_path, uname="Darwin")
    deck_dir = tmp_path / "laptop" / "agent-deck"
    r = b.run(extra_env={"AGENT_DECK_DIR": str(deck_dir)})
    assert r.returncode == 0, r.stdout + r.stderr
    calls = b.calls()
    assert not any(c.startswith(("apt-get", "install-deck", "systemctl")) for c in calls)
    assert any(c.startswith("launchctl bootstrap") for c in calls), calls
    plist = b.home / "Library" / "LaunchAgents" / "dev.agentdeck.server.plist"
    text = plist.read_text()
    assert "127.0.0.1" in text and str(deck_dir) in text
    token = b.home / ".claude" / "agent-bus" / "deck-token.txt"
    assert token.stat().st_mode & 0o077 == 0
    assert "deck-token.txt" in r.stdout and token.read_text().strip() not in r.stdout


def test_an_unknown_system_is_refused(tmp_path):
    b = Box(tmp_path, uname="FreeBSD")
    r = b.run()
    assert r.returncode != 0 and "FreeBSD" in r.stderr


# --- the server path ---------------------------------------------------------------

def test_not_root_uses_sudo_for_what_needs_root(tmp_path):
    b = Box(tmp_path, uid=1000)
    r = b.run()
    assert r.returncode == 0, r.stdout + r.stderr
    calls = b.calls()
    assert any(c.startswith("sudo ") and "apt-get install" in c for c in calls), calls
    assert any(c.startswith("sudo ") and "install-deck.sh" in c for c in calls), calls


def test_without_a_terminal_it_installs_non_interactively_and_ends_with_the_sign_in(box):
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    deck = [c for c in box.calls() if c.startswith("install-deck")]
    assert deck and "--yes" in deck[0] and "--skip-login" in deck[0]
    tail = r.stdout.strip().splitlines()[-6:]
    assert any("deckctl login" in line for line in tail), r.stdout


def test_it_prints_the_pairing_code_as_a_ready_mac_command(box):
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("deckctl pair") for c in box.calls())
    want = f"https://raw.githubusercontent.com/{SLUG}/main/scripts/install-mac.sh | bash -s -- --pair ADK1.fakecode"
    assert want in r.stdout
    # the sign-in comes after the pairing info: it is the last thing to do
    assert r.stdout.rindex("deckctl login") > r.stdout.index("ADK1.fakecode")


def test_the_code_the_installer_printed_is_reused_not_minted_twice(box):
    (box.src / "deploy" / "install-deck.sh").write_text(
        '#!/bin/bash\nprintf "install-deck %s\\n" "$*" >> "$FAKE_LOG"\n'
        'echo "Paste this into the Agent Deck app, or scan the picture above:"\n'
        'echo "ADK1.fromInstaller_9"\n')
    _git(box.src, "add", "-A")
    _git(box.src, "commit", "-q", "-m", "prints a code")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(c.startswith("deckctl pair") for c in box.calls())
    assert "--pair ADK1.fromInstaller_9" in r.stdout


def test_tunnel_mode_prints_the_ssh_tunnel_instead_of_a_code(box):
    r = box.run("--ssh-tunnel", extra_env={"FAKE_HOSTNAME": ""})
    assert r.returncode == 0, r.stdout + r.stderr
    deck = [c for c in box.calls() if c.startswith("install-deck")][0]
    assert "--tls=none" in deck
    assert not any(c.startswith("deckctl pair") for c in box.calls())
    assert "ssh -N -L 7789:127.0.0.1:7789" in r.stdout


def test_domain_and_passthrough_flags(box):
    r = box.run("--domain", "deck.example.com", "--no-docker")
    assert r.returncode == 0, r.stdout + r.stderr
    deck = [c for c in box.calls() if c.startswith("install-deck")][0]
    assert "--tls=domain" in deck and "--hostname deck.example.com" in deck
    assert "--no-docker" in deck


def test_a_logged_in_box_is_not_told_to_sign_in(box):
    (box.tmp / "oauth.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-x\n")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "deckctl login" not in r.stdout


# --- idempotent: a re-run upgrades ---------------------------------------------------

def test_a_re_run_upgrades_the_checkout_and_re_runs_the_installer(box):
    assert box.run().returncode == 0
    box.commit("VERSION", "1.1.0\n")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.dir / "VERSION").read_text() == "1.1.0\n"
    assert sum(c.startswith("install-deck") for c in box.calls()) == 2
    # packages were present on run two: apt-get ran once, not twice
    assert sum(c.startswith("apt-get install") for c in box.calls()) == 1


def test_a_re_run_with_nothing_new_changes_nothing(box):
    assert box.run().returncode == 0
    head = _git(box.dir, "rev-parse", "HEAD")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(box.dir, "rev-parse", "HEAD") == head


def test_a_re_run_never_overwrites_local_edits(box):
    assert box.run().returncode == 0
    (box.dir / "VERSION").write_text("hand edited\n")
    box.commit("VERSION", "1.1.0\n")
    r = box.run()
    assert r.returncode != 0
    assert "VERSION" in r.stderr or "local changes" in r.stderr
    assert (box.dir / "VERSION").read_text() == "hand edited\n"


def test_a_non_checkout_in_the_way_is_refused(box):
    box.dir.mkdir(parents=True)
    (box.dir / "something").write_text("x")
    r = box.run()
    assert r.returncode != 0 and str(box.dir) in r.stderr


def test_the_laptop_re_run_keeps_the_token(tmp_path):
    b = Box(tmp_path, uname="Darwin")
    env = {"AGENT_DECK_DIR": str(tmp_path / "laptop")}
    assert b.run(extra_env=env).returncode == 0
    token = (b.home / ".claude" / "agent-bus" / "deck-token.txt").read_text()
    r = b.run(extra_env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (b.home / ".claude" / "agent-bus" / "deck-token.txt").read_text() == token
    assert any(c.startswith("launchctl kickstart") for c in b.calls())


# --- the repo URL: one variable -------------------------------------------------------

def test_the_repo_slug_is_one_variable():
    text = SCRIPT.read_text()
    assigns = re.findall(r'^\s*AGENT_DECK_REPO="\$\{AGENT_DECK_REPO:-([^}]+)\}"', text, re.M)
    assert len(assigns) == 1, assigns
    slug = assigns[0]
    # the slug literal appears only on that line and in the usage comment
    lines = [l for l in text.splitlines() if slug in l]
    assert all(l.lstrip().startswith(("#", "AGENT_DECK_REPO=")) for l in lines), lines


def test_the_clone_url_is_built_from_the_slug(box):
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    remote = _git(box.dir, "config", "--get", "remote.origin.url")
    assert remote == f"https://github.com/{SLUG}.git"


def test_a_repo_url_override_wins(box, tmp_path):
    r = box.run(extra_env={"AGENT_DECK_REPO_URL": f"file://{box.src}"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(box.dir, "config", "--get", "remote.origin.url") == f"file://{box.src}"


# --- main() guard: a partial download runs nothing ----------------------------------

def test_the_last_line_calls_main():
    lines = [l for l in SCRIPT.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert lines[-1].strip() == 'main "$@"'
    assert re.search(r"^main\(\) \{", SCRIPT.read_text(), re.M)


@pytest.mark.parametrize("fraction", [0.25, 0.5, 0.9])
def test_a_truncated_download_executes_nothing(box, fraction):
    text = SCRIPT.read_text()
    cut = text[: int(len(text) * fraction)]
    box.run(script=cut)
    assert box.calls() == []
    assert not box.dir.exists()
