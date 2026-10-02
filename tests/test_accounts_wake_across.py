"""S9e: a desk moved while asleep is resumed in its NEW account when it wakes.

The mover now only changes an asleep desk's account (no turn, no process). So
the wake is where the move completes: when the roster names an account other
than the one its last job is filed in, the Waker resumes it across -- the same
steps the mover uses (transcript, trust, its flags where that account has no
job: MEASURED P2b and the S8d fork).
"""

from __future__ import annotations

import json

import pytest

from server import accounts, roster, wake


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps([{"id": "work", "label": "Work", "kind": "subscription",
                                 "config_dir": str(tmp_path / "work"), "added_at": 1.0}]))
    monkeypatch.setattr(accounts, "REGISTRY", path)


def _waker(desk_account: str, job_account: str, calls: list):
    job = wake.Job(short="s1", session_id="sid-1", cwd="/w", created_at="",
                   account=job_account)
    return wake.Waker(
        live=lambda name: set(), running=lambda sid: False, last=lambda name: job,
        desk_of=lambda name: roster.Desk(name=name, cwd="/w", engine="claude",
                                         mission="m", account=desk_account),
        resume=lambda sid, **kw: calls.append(("resume", sid, kw)) or "sid-1",
        resume_across=lambda job, account, seed: calls.append(
            ("across", job.session_id, job.account, account)) or "sid-1",
        pending=lambda name: [], ack=lambda mid: None, record=lambda e: None,
        free_bytes=lambda: 1 << 40)


def test_a_desk_moved_while_asleep_wakes_in_its_new_account(reg):
    calls: list = []
    got = _waker("work", "main", calls).ensure_awake("atlas", reason="owner_message")
    assert got.state == "woken" and got.session_id == "sid-1"
    assert calls == [("across", "sid-1", "main", "work")]


def test_a_desk_on_the_account_of_its_job_wakes_as_before(reg):
    calls: list = []
    _waker("", "main", calls).ensure_awake("atlas", reason="owner_message")
    assert [c[0] for c in calls] == ["resume"]


def test_the_default_across_uses_the_movers_steps(reg, monkeypatch):
    from server import mover
    seen = []
    monkeypatch.setattr(mover, "ensure_transcript", lambda *a: seen.append("transcript"))
    monkeypatch.setattr(mover, "vouch", lambda *a: seen.append("vouch"))
    monkeypatch.setattr(mover, "has_job", lambda sid, account: False)
    monkeypatch.setattr(mover, "job_state", lambda sid: {"respawnFlags": ["--name", "atlas"]})
    monkeypatch.setattr(mover, "resume_into", lambda sid, **kw: (
        seen.append(("resume_into", kw["flags"], kw["account"], kw["note"])) or "sid-1"))
    job = wake.Job(short="s1", session_id="sid-1", cwd="/w", created_at="", account="main")
    assert mover.wake_into(job, "work", "SEED") == "sid-1"
    assert seen == ["transcript", "vouch", ("resume_into", ["--name", "atlas"], "work", "SEED")]
