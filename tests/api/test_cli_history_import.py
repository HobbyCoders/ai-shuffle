"""Discovering Claude Code CLI conversations for import at onboarding."""

import json
import os
import time
from pathlib import Path

import pytest

from app.core.cli_history import find_cli_conversations, transcript_cwd


def _write_transcript(path: Path, *, cwd: str, prompt: str, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "cwd": cwd, "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "cwd": cwd, "message": {"role": "assistant", "content": [{"type": "text", "text": "Sure."}]}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    os.utime(path, (mtime, mtime))
    return path


NOW = 1_800_000_000.0


@pytest.fixture
def projects_dir(tmp_path):
    root = tmp_path / "projects"
    _write_transcript(root / "-work-app" / "new.jsonl", cwd="/work/app", prompt="Fix the login bug", mtime=NOW - 60)
    _write_transcript(root / "-work-app" / "older.jsonl", cwd="/work/app", prompt="Write tests", mtime=NOW - 3 * 86_400)
    _write_transcript(root / "-work-app" / "known.jsonl", cwd="/work/app", prompt="Already here", mtime=NOW - 120)
    _write_transcript(root / "-work-app" / "stale.jsonl", cwd="/work/app", prompt="Long ago", mtime=NOW - 90 * 86_400)
    _write_transcript(root / "-elsewhere" / "other.jsonl", cwd="/elsewhere", prompt="Unrelated", mtime=NOW - 600)
    # Never offered: subagent logs, hook sidecars, nested subagent files.
    _write_transcript(root / "-work-app" / "agent-abc.jsonl", cwd="/work/app", prompt="sub", mtime=NOW)
    _write_transcript(root / "-work-app" / "new.hooks.jsonl", cwd="/work/app", prompt="hook", mtime=NOW)
    _write_transcript(root / "-work-app" / "new" / "subagents" / "agent-x.jsonl", cwd="/work/app", prompt="sub", mtime=NOW)
    return root


def _summarize(path: Path):
    first = json.loads(path.read_text().splitlines()[0])
    return first["message"]["content"], None, 2


def test_lists_recent_unregistered_conversations_newest_first(projects_dir):
    found = find_cli_conversations(
        projects_dir,
        is_registered=lambda sdk_id: sdk_id == "known",
        summarize=_summarize,
        project_dirs={"/work/app": "proj-app"},
        days=30,
        now=NOW,
    )
    assert [c["sdk_session_id"] for c in found] == ["new", "other", "older"]
    assert found[0]["title"] == "Fix the login bug"
    assert found[0]["project_id"] == "proj-app"
    assert found[1]["project_id"] is None
    assert found[1]["cwd"] == "/elsewhere"


def test_limit_and_unreadable_transcripts(projects_dir):
    def summarize(path):
        if path.stem == "new":
            raise ValueError("corrupt")
        return _summarize(path)

    found = find_cli_conversations(
        projects_dir, is_registered=lambda _: False, summarize=summarize,
        project_dirs={}, days=30, limit=2, now=NOW,
    )
    assert [c["sdk_session_id"] for c in found] == ["known", "other"]


def test_missing_projects_dir_is_empty(tmp_path):
    assert find_cli_conversations(
        tmp_path / "nope", is_registered=lambda _: False, summarize=_summarize, project_dirs={}, now=NOW,
    ) == []


def test_transcript_cwd_skips_bad_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('not json\n{"type":"summary"}\n{"cwd":"/a/b"}\n')
    assert transcript_cwd(path) == "/a/b"


def test_endpoint_lists_and_import_registers(authenticated_client, tmp_path, monkeypatch):
    from app.core import config
    from app.db import database

    projects = tmp_path / "claude" / "projects"
    workdir = tmp_path / "work" / "app"
    workdir.mkdir(parents=True)
    monkeypatch.setattr(config.settings, "claude_projects_dir", projects)
    now = time.time()
    transcript = _write_transcript(projects / "-work-app" / "cli-1.jsonl", cwd=str(workdir), prompt="Refactor the parser", mtime=now - 60)
    database.create_project("proj-app", "App", None, str(workdir))

    response = authenticated_client.get("/api/v1/sessions/import/cli-history")
    assert response.status_code == 200, response.text
    conversations = response.json()["conversations"]
    assert [(c["sdk_session_id"], c["project_id"]) for c in conversations] == [("cli-1", "proj-app")]
    assert conversations[0]["title"].startswith("Refactor the parser")

    imported = authenticated_client.post(
        "/api/v1/sessions/import-jsonl",
        json={"jsonl_path": str(transcript), "project_id": "proj-app"},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["status"] == "success"

    # Once imported it is no longer offered.
    again = authenticated_client.get("/api/v1/sessions/import/cli-history")
    assert again.json()["conversations"] == []


def test_endpoint_is_owner_only(client):
    from app.api import auth
    from app.db import database

    database.create_api_user("integration", "Integration", auth.hash_api_key("aih_cli"))
    response = client.get("/api/v1/sessions/import/cli-history", headers={"Authorization": "Bearer aih_cli"})
    assert response.status_code == 403
