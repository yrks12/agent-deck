"""S8a: the mover's edges -- the resume into another account, and the
transcript it needs there. See tests/test_accounts_move.py for the move.
"""

from __future__ import annotations

import json

import pytest

from server import accounts, mover

SID = "c91c8d85-4bad-4529-a026-2f2ab6956b43"


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps([{"id": "work", "label": "Work", "kind": "subscription",
                                 "config_dir": str(tmp_path / "work"), "added_at": 1.0}]))
    monkeypatch.setattr(accounts, "REGISTRY", path)
    return path


# -- the default edges -------------------------------------------------------


def test_resume_into_passes_the_flags_and_the_accounts_env(reg, monkeypatch, tmp_path):
    seen = []

    class _Ran:
        returncode, stdout, stderr = 0, "backgrounded · c91c8d85 · atlas\n", ""

    monkeypatch.setattr(mover.subprocess, "run",
                        lambda argv, **kw: (seen.append((argv, kw)) or _Ran()))
    got = mover.resume_into(SID, cwd=str(tmp_path), flags=["--name", "atlas"],
                            note="N", account="work")
    assert got == "c91c8d85"
    argv, kw = seen[-1]
    assert argv == ["claude", "--bg", "--resume", SID, "--name", "atlas", "N"]
    assert kw["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "work")


def test_ensure_transcript_copies_only_when_the_dir_is_not_shared(reg, tmp_path, monkeypatch):
    main_projects = tmp_path / "main-projects"
    monkeypatch.setattr(accounts.paths, "PROJECTS_DIR", main_projects)
    slug = main_projects / "-srv-atlas"
    (slug / SID / "subagents").mkdir(parents=True)
    (slug / f"{SID}.jsonl").write_text("{}\n")
    (slug / SID / "subagents" / "a.jsonl").write_text("{}\n")
    work = accounts.get("work")
    mover.ensure_transcript(accounts.main_account(), work, "/srv/atlas", SID)
    copied = tmp_path / "work" / "projects" / "-srv-atlas"
    assert (copied / f"{SID}.jsonl").read_text() == "{}\n"
    assert (copied / SID / "subagents" / "a.jsonl").exists()
