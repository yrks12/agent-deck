"""Sign a Claude account in from the app -- the CLI owns the login (S7c).

`start(id)` runs the CLI's OWN `claude auth login` in a PTY, as the deck's
service user, with that account's CLAUDE_CONFIG_DIR (none for `main`: its
`.claude.json` lives outside `~/.claude`, MEASURED in probe P1). It returns the
authorize URL the CLI printed. `submit(login_id, code)` writes the pasted code
into the PTY, waits for the CLI to finish, and proves the result with
`claude auth status --json` (loggedIn, in the expected configDirectory) before
registering the account.

TERMS (claude-dashbaord-oss-terms.md): the deck does not "collect, store, or
intermediate" a Claude credential. The CLI writes and refreshes its own
`.credentials.json`. The code goes from the request straight into the PTY:
once the URL is captured this module keeps NO output from the CLI (a terminal
echoes what is typed), and the code is never logged or stored.

One login at a time per account. A login nobody finishes expires after
EXPIRE_SECONDS and its CLI is killed.
"""

from __future__ import annotations

import json
import os
import pty
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import accounts, atomic, paths

EXPIRE_SECONDS = 600.0
#: The first https URL the CLI prints. MEASURED: it rides an OSC-8 hyperlink,
#: `ESC ] 8 ; ; <url> BEL`, so BEL and ESC end it as well as whitespace.
URL_RE = re.compile(r"https://[^\s\x07\x1b]+")
_DROP = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
SHARED = ("projects", "skills", "agent-bus")


class LoginError(Exception):
    def __init__(self, status: int, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.status, self.reason, self.detail = status, reason, detail or reason


@dataclass
class _Login:
    login_id: str
    account: str
    label: str
    config_dir: Path
    started: float
    proc: subprocess.Popen | None = None
    master: int | None = None
    status: str = "starting"  # waiting_code | done | failed | expired
    url: str = ""
    detail: str = ""
    seen: list = field(default_factory=list)  # CLI output, ONLY until the URL
    found: threading.Event = field(default_factory=threading.Event)
    timer: threading.Timer | None = None


def _default_claude() -> str:
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


class LoginManager:
    def __init__(self, *, claude_bin: str | None = None, home: Path | None = None,
                 main_dir: Path | None = None, env: dict | None = None,
                 clock=time.time, url_wait: float = 20.0, exit_wait: float = 60.0) -> None:
        self.claude_bin = claude_bin or _default_claude()
        self.home = Path(home or Path.home())
        self.main_dir = Path(main_dir or paths.CLAUDE_HOME)
        self._env = dict(os.environ if env is None else env)
        self.clock, self.url_wait, self.exit_wait = clock, url_wait, exit_wait
        self._logins: dict[str, _Login] = {}
        self._lock = threading.Lock()

    # -- where and how the CLI runs --------------------------------------------

    def _dir_for(self, ident: str) -> Path:
        if ident == accounts.DEFAULT_ID:
            return self.main_dir
        return self.home / accounts.ACCOUNTS_ROOT_NAME / ident

    def _env_for(self, ident: str) -> dict:
        env = {k: v for k, v in self._env.items() if k not in _DROP}
        if ident != accounts.DEFAULT_ID:
            env["CLAUDE_CONFIG_DIR"] = str(self._dir_for(ident))
            env["DECK_BUS_DIR"] = str(self.main_dir / "agent-bus")
        return env

    def _prepare(self, ident: str) -> None:
        """The account's own dir, 0700, sharing transcripts, skills and the bus
        with main (MEASURED, P2/P3), first-run answers seeded."""
        if ident == accounts.DEFAULT_ID:
            return
        acct = self._dir_for(ident)
        acct.mkdir(parents=True, exist_ok=True)
        acct.parent.chmod(0o700)
        acct.chmod(0o700)
        for name in SHARED:
            (self.main_dir / name).mkdir(parents=True, exist_ok=True)
            link = acct / name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(self.main_dir / name)
        seed = acct / ".claude.json"
        if not seed.exists():
            atomic.write_text(seed, json.dumps({"hasCompletedOnboarding": True,
                                                "bypassPermissionsModeAccepted": True}),
                              mode=0o600)

    # -- the PTY ---------------------------------------------------------------

    def _read(self, login: _Login) -> None:
        while True:
            try:
                chunk = os.read(login.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            if login.found.is_set():
                continue  # after the URL nothing is kept: the echo carries the code
            login.seen.append(chunk.decode("utf-8", "replace"))
            hit = URL_RE.search("".join(login.seen))
            if hit:
                login.url = hit.group(0)
                login.seen.clear()
                login.found.set()
        login.found.set()

    def _kill(self, login: _Login) -> None:
        if login.timer is not None:
            login.timer.cancel()
        proc = login.proc
        if proc is not None and proc.poll() is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    break
                try:
                    proc.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if login.master is not None:
            try:
                os.close(login.master)
            except OSError:
                pass
            login.master = None

    def _reap(self) -> None:
        now = self.clock()
        for login in self._logins.values():
            if login.status == "waiting_code" and now - login.started > EXPIRE_SECONDS:
                login.status, login.detail = "expired", "nobody pasted a code in 10 minutes"
                self._kill(login)

    # -- the three verbs ---------------------------------------------------------

    def start(self, ident: str, label: str = "") -> dict:
        if ident != accounts.DEFAULT_ID and not accounts.valid_id(ident):
            raise LoginError(400, "bad_account", "an account id is lowercase letters, "
                             "digits, - or _ (up to 32)")
        with self._lock:
            self._reap()
            if any(l.account == ident and l.status in ("starting", "waiting_code")
                   for l in self._logins.values()):
                raise LoginError(409, "login_in_progress",
                                 f"a sign-in for {ident} is already waiting for its code")
            login = _Login(secrets.token_urlsafe(12), ident, label or ident,
                           self._dir_for(ident), self.clock())
            self._logins[login.login_id] = login
        try:
            self._prepare(ident)
            master, slave = pty.openpty()
            login.master = master
            login.proc = subprocess.Popen(
                [self.claude_bin, "auth", "login"], stdin=slave, stdout=slave, stderr=slave,
                env=self._env_for(ident), cwd=str(self.home), start_new_session=True,
                close_fds=True)
            os.close(slave)
        except OSError as exc:
            login.status, login.detail = "failed", f"could not start the CLI: {exc.strerror}"
            self._kill(login)
            raise LoginError(502, "login_failed", login.detail) from exc
        threading.Thread(target=self._read, args=(login,), daemon=True).start()
        login.found.wait(self.url_wait)
        if not login.url:
            login.status, login.detail = "failed", "the CLI printed no sign-in link"
            self._kill(login)
            raise LoginError(502, "login_failed", login.detail)
        login.status = "waiting_code"
        login.timer = threading.Timer(EXPIRE_SECONDS, self._expire, args=(login.login_id,))
        login.timer.daemon = True
        login.timer.start()
        return self._view(login)

    def _expire(self, login_id: str) -> None:
        with self._lock:
            login = self._logins.get(login_id)
            if login is not None and login.status == "waiting_code":
                login.status, login.detail = "expired", "nobody pasted a code in 10 minutes"
                self._kill(login)

    def submit(self, login_id: str, code: str) -> dict:
        code = (code or "").strip()
        if not code or len(code) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in code):
            # One printable line: anything else could drive the terminal.
            raise LoginError(400, "bad_code", "paste the code exactly as Claude showed it")
        with self._lock:
            self._reap()
            login = self._logins.get(login_id)
            if login is None:
                raise LoginError(404, "unknown_login", "no such sign-in")
            if login.status == "expired":
                raise LoginError(410, "expired", login.detail)
            if login.status != "waiting_code":
                raise LoginError(409, "not_waiting", f"this sign-in is {login.status}")
            login.status = "checking"
        try:
            os.write(login.master, (code + "\r").encode())
        except (OSError, TypeError) as exc:
            login.status, login.detail = "failed", "the CLI was no longer listening"
            self._kill(login)
            raise LoginError(502, "login_failed", login.detail) from exc
        try:
            login.proc.wait(timeout=self.exit_wait)
        except subprocess.TimeoutExpired:
            pass
        self._kill(login)
        if self._signed_in(login):
            if login.account != accounts.DEFAULT_ID:
                self._register(login)
            login.status, login.detail = "done", ""
        else:
            # A refusal, as the app maps it: the CLI has exited, so the way on
            # is a new sign-in (GET still shows this one as `failed`).
            login.status, login.detail = "failed", "Claude did not accept that code"
            raise LoginError(400, "login_failed",
                             "Claude did not accept that code; start the sign-in again")
        return self._view(login)

    def get(self, login_id: str) -> dict:
        with self._lock:
            self._reap()
            login = self._logins.get(login_id)
        if login is None:
            raise LoginError(404, "unknown_login", "no such sign-in")
        return self._view(login)

    def shutdown(self) -> None:
        for login in list(self._logins.values()):
            self._kill(login)

    # -- proof and record ----------------------------------------------------------

    def _signed_in(self, login: _Login) -> bool:
        try:
            out = subprocess.run([self.claude_bin, "auth", "status", "--json"],
                                 env=self._env_for(login.account), capture_output=True,
                                 text=True, timeout=30)
            said = json.loads(out.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        where = str(said.get("configDirectory") or "")
        return said.get("loggedIn") is True and bool(where) and \
            Path(where).resolve() == login.config_dir.resolve()

    def _register(self, login: _Login) -> None:
        path = Path(accounts.REGISTRY)
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            rows = []
        rows = [r for r in rows if isinstance(r, dict) and r.get("id") != login.account]
        rows.append({"id": login.account, "label": login.label, "kind": "subscription",
                     "config_dir": str(login.config_dir), "added_at": self.clock()})
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(path, json.dumps(rows, indent=1), mode=0o600)

    def _view(self, login: _Login) -> dict:
        return {"login_id": login.login_id, "account": login.account, "label": login.label,
                "status": login.status,
                "url": login.url if login.status == "waiting_code" else "",
                "expires_at": login.started + EXPIRE_SECONDS, "detail": login.detail}
