"""TaskGet is how a woken parent reads its background worker's report.

The 2026-08-31 audit: a parent dispatched a background Explore child, the child
finished in two minutes with a 27KB report, and TaskGet answered with the bare
registry record — id, status, and a summary that was only the model label. The
parent guessed the sync report path, found nothing (background never wrote a
file), searched the filesystem, gave up, and redid ~30 minutes of research.
"""

import asyncio
import json
from unittest.mock import patch

import pytest


def _builtin_handlers(session_id="session-parent"):
    import claude_agent_sdk as sdk

    from app.mcp import builtin_tools

    captured = {}
    real_create = sdk.create_sdk_mcp_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
        builtin_tools.create_builtin_tools_server(session_id=session_id)

    return {tool.name: tool for tool in captured["tools"]}


def _get(task_id="w-1", session_id="session-parent"):
    handler = _builtin_handlers(session_id)["TaskGet"].handler
    return json.loads(
        asyncio.run(handler({"task_id": task_id}))["content"][0]["text"]
    )


@pytest.fixture()
def completed_agent(monkeypatch, tmp_path):
    """A completed agent task whose report is on disk, as shipped."""
    from app.core.task_registry import task_registry

    directory = tmp_path / ".fluid" / "task-reports"
    directory.mkdir(parents=True)

    def _record(body):
        path = directory / "w-1.md"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setattr(
            task_registry,
            "get_task",
            lambda sid, tid, **_kw: {
                "id": tid,
                "kind": "agent",
                "status": "completed",
                "subject": "Audit the retry path",
                "summary": "Retry backoff is unbounded",
                "model": "OpenRouter · openai/gpt-5.6-luna",
                "reportPath": str(path),
                "reportChars": len(body),
            },
        )
        return path

    return _record


def test_completed_agent_task_serves_its_report(mock_db, completed_agent):
    written = completed_agent("Retry backoff is unbounded.\n\nDetails follow.")

    body = _get()

    assert body["report_head"] == "Retry backoff is unbounded.\n\nDetails follow."
    assert body["report_path"] == str(written)
    assert body["report_chars"] == len(body["report_head"])
    assert "nothing further to read" in body["note"]


def test_a_long_report_is_bounded_and_points_at_the_file(mock_db, completed_agent):
    from app.core.task_dispatch import SYNC_REPORT_HEAD_CHARS

    long_body = "z" * (SYNC_REPORT_HEAD_CHARS + 5_000)
    written = completed_agent(long_body)

    body = _get()

    assert len(body["report_head"]) == SYNC_REPORT_HEAD_CHARS
    assert body["report_chars"] == len(long_body)
    assert body["report_path"] == str(written)
    assert "report_path" in body["note"]


def test_a_pre_existing_task_falls_back_to_the_event_log(mock_db, monkeypatch):
    """Workers that completed BEFORE the path was recorded are still readable."""
    from app.core import provider_agent as pa
    from app.core.task_registry import task_registry

    monkeypatch.setattr(
        task_registry,
        "get_task",
        lambda sid, tid, **_kw: {"id": tid, "kind": "agent", "status": "completed"},
    )
    monkeypatch.setattr(
        pa, "final_turn_outcome",
        lambda sid: {
            "usage": {}, "error": None, "interrupted": False,
            "terminal_found": True,
        },
    )
    monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "recovered report")

    body = _get()

    assert body["report_head"] == "recovered report"
    assert body["report_chars"] == len("recovered report")
    # Present and null, never absent: `j['report_path']` on a report with no
    # file used to raise KeyError and abort the caller's whole cell.
    assert body["report_path"] is None


def test_a_vanished_report_file_falls_back_too(mock_db, monkeypatch, tmp_path):
    """The row can outlive the worktree it pointed into."""
    from app.core import provider_agent as pa
    from app.core.task_registry import task_registry

    monkeypatch.setattr(
        task_registry,
        "get_task",
        lambda sid, tid, **_kw: {
            "id": tid, "kind": "agent", "status": "completed",
            "reportPath": str(tmp_path / "gone" / "w-1.md"),
            "reportChars": 12,
        },
    )
    monkeypatch.setattr(
        pa, "final_turn_outcome",
        lambda sid: {
            "usage": {}, "error": None, "interrupted": False,
            "terminal_found": True,
        },
    )
    monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "from the log")

    body = _get()

    assert body["report_head"] == "from the log"
    assert body["report_path"] is None


def test_a_running_agent_task_gets_no_report_fields(mock_db, monkeypatch):
    from app.core.task_registry import task_registry

    monkeypatch.setattr(
        task_registry,
        "get_task",
        lambda sid, tid, **_kw: {"id": tid, "kind": "agent", "status": "running"},
    )

    body = _get()

    assert "report_head" not in body
    assert body["note"].startswith("all tasks still running")


def test_a_shell_task_is_untouched(mock_db, monkeypatch):
    from app.core.task_registry import task_registry

    monkeypatch.setattr(
        task_registry,
        "get_task",
        lambda sid, tid, **_kw: {"id": tid, "kind": "shell", "status": "completed"},
    )

    body = _get()

    assert "report_head" not in body and "report_path" not in body


def test_the_description_documents_the_agent_report_fields(mock_db):
    """The model probed the filesystem because the description only ever
    described shell tasks."""
    description = _builtin_handlers()["TaskGet"].description

    for field in ("report_head", "report_path", "report_chars"):
        assert field in description


def test_agent_summary_is_not_the_bare_model_label(mock_db, completed_agent):
    completed_agent("Retry backoff is unbounded.")

    task = _get()["task"]

    assert task["summary"] == "Retry backoff is unbounded"
    assert task["model"] == "OpenRouter · openai/gpt-5.6-luna"
    assert task["summary"] != task["model"]


def test_a_task_only_the_retention_store_remembers_still_serves_its_report(
    mock_db, tmp_path
):
    """The wake can land long after the pooled client was evicted.

    ``clear_session`` wipes the live map; the retention snapshot survives it.
    TaskGet used to read only the live map, so a parent woken about its own
    worker got "task not found" and went and redid the work by hand.
    """
    from app.core.task_registry import task_registry

    report = tmp_path / "w-1.md"
    report.write_text("PR #1084 is up; CI is green.", encoding="utf-8")

    task_registry.reset_all()
    try:
        task_registry.start_task(
            "session-parent", "w-1", "agent", "Ship the fix",
        )
        task_registry.complete_task(
            "session-parent", "w-1", "completed",
            summary="PR #1084 is up", report_path=str(report),
        )
        task_registry.clear_session("session-parent")

        body = _get()

        assert body["success"] is True
        assert body["task"]["status"] == "completed"
        assert body["report_head"].startswith("PR #1084 is up")
        assert body["report_path"] == str(report)
    finally:
        task_registry.reset_all()


def test_a_task_only_the_durable_row_remembers_is_still_found(mock_db, monkeypatch):
    """Last resort: nothing in memory at all, and the DB row is the record."""
    from app.core import provider_agent as pa
    from app.core.task_registry import task_registry
    from app.db import background_tasks as bt_db

    task_registry.reset_all()
    monkeypatch.setattr(
        bt_db, "get_background_task",
        lambda tid, sid: {
            "task_id": tid, "session_id": sid, "kind": "agent",
            "tool_use_id": tid, "title": "Ship the fix", "status": "completed",
            "started_at": "2026-09-04T10:00:00+00:00",
            "updated_at": "2026-09-04T10:09:00+00:00",
        },
    )
    monkeypatch.setattr(
        pa, "final_turn_outcome",
        lambda sid: {
            "usage": {}, "error": None, "interrupted": False,
            "terminal_found": True,
        },
    )
    monkeypatch.setattr(pa, "final_assistant_text", lambda sid: "Merged in #1084.")

    body = _get()

    assert body["success"] is True
    assert body["task"]["status"] == "completed"
    assert body["report_head"] == "Merged in #1084."
