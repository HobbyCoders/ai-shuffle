"""A worker continued with SendMessage saves its new report (#104).

The continuation's report replaces the one at ``task-reports/<id>.md``, the
earlier round stays readable beside it, the registry row records the path,
and TaskGet's report_head/report_path describe the latest round.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core import provider_agent, task_dispatch
from app.core.task_registry import task_registry


@pytest.fixture(autouse=True)
def _clean():
    task_registry.reset_all()
    yield
    task_registry.reset_all()


@pytest.fixture
def report_dir(tmp_path, monkeypatch):
    directory = tmp_path / "task-reports"
    monkeypatch.setattr(task_dispatch, "task_report_dir", lambda row: directory)
    return directory


# ─── The report file keeps every round, latest at the fixed path ────────────


def test_a_new_report_moves_the_previous_round_aside(report_dir):
    first = task_dispatch.write_task_report(None, "w1", "Round one: DO NOT MERGE")
    second = task_dispatch.write_task_report(None, "w1", "Round two: MERGE")
    assert first == second
    assert Path(second).read_text() == "Round two: MERGE"
    (earlier,) = task_dispatch.earlier_task_reports(second)
    assert Path(earlier).name == "w1.round-1.md"
    assert Path(earlier).read_text() == "Round one: DO NOT MERGE"


def test_the_same_report_written_again_is_not_a_new_round(report_dir):
    path = task_dispatch.write_task_report(None, "w1", "Only report")
    task_dispatch.write_task_report(None, "w1", "Only report")
    assert task_dispatch.earlier_task_reports(path) == []


def test_earlier_rounds_are_listed_oldest_first(report_dir):
    for n in range(12):
        path = task_dispatch.write_task_report(None, "w1", f"report {n}")
    earlier = task_dispatch.earlier_task_reports(path)
    assert [Path(p).read_text() for p in earlier] == [f"report {n}" for n in range(11)]
    assert Path(path).read_text() == "report 11"
    # Another worker's rounds are never mixed in.
    task_dispatch.write_task_report(None, "w10", "a")
    task_dispatch.write_task_report(None, "w10", "b")
    assert len(task_dispatch.earlier_task_reports(path)) == 11


# ─── Text blocks are separated ──────────────────────────────────────────────


def test_separate_text_blocks_are_joined_with_a_blank_line():
    joined = provider_agent.join_assistant_text([
        (False, "I will check when it lands."),
        (False, "The missing dialog is fixed."),
    ])
    assert joined == "I will check when it lands.\n\nThe missing dialog is fixed."


def test_streamed_deltas_of_one_block_join_as_they_arrived():
    assert provider_agent.join_assistant_text([
        (True, "The miss"), (True, "ing dialog"), (False, "Verdict: MERGE"),
    ]) == "The missing dialog\n\nVerdict: MERGE"


def test_a_first_report_separates_its_closing_text_blocks(monkeypatch):
    monkeypatch.setattr(provider_agent, "_load_session_events", lambda sid: [
        {"turn_id": "t1", "event_type": "message.assistant",
         "payload": {"kind": "text", "content": "Looking."}},
        {"turn_id": "t1", "event_type": "tool.result", "payload": {}},
        {"turn_id": "t1", "event_type": "message.assistant",
         "payload": {"kind": "text", "content": "Done when it lands."}},
        {"turn_id": "t1", "event_type": "message.assistant",
         "payload": {"kind": "text", "content": "Verdict: MERGE"}},
        {"turn_id": "t1", "event_type": "turn.completed", "payload": {}},
    ])
    assert provider_agent.final_assistant_text("w1") == "Done when it lands.\n\nVerdict: MERGE"


# ─── The continuation's completion ──────────────────────────────────────────


async def test_a_continued_worker_saves_its_new_report_for_task_get(report_dir, monkeypatch):
    from app.core import agent_messaging, turn_scheduler
    from app.db import database
    from app.mcp.builtin_tools import _agent_report_fields

    # Round one: a background worker finished and wrote its report.
    task_registry.start_task("parent-1", "w1", "agent", "Review", tool_use_id="w1")
    first, chars = provider_agent._persist_worker_report(None, "w1", "Verdict: DO NOT MERGE")
    task_registry.complete_task("parent-1", "w1", "completed", summary="DO NOT MERGE",
                                report_path=first, report_chars=chars)
    # SendMessage continued it: the row is re-armed for the new round.
    task_registry.resume_task("parent-1", "w1")
    assert task_registry.get_task("parent-1", "w1").get("reportPath") is None

    settled = {"id": "wt2", "payload": {"agent_message_reply_to": "parent-1"}}
    monkeypatch.setattr(provider_agent, "_load_session_events", lambda sid: [
        {"turn_id": "wt1", "event_type": "message.assistant",
         "payload": {"kind": "text", "content": "Verdict: DO NOT MERGE"}},
        {"turn_id": "wt2", "event_type": "message.assistant",
         "payload": {"kind": "text", "content": "Rechecked; the fix landed."}},
        {"turn_id": "wt2", "event_type": "message.assistant",
         "payload": {"kind": "text", "content": "Verdict: MERGE"}},
    ])
    monkeypatch.setattr(database, "get_session", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "_session_has_parked_turn", lambda sid: False)
    monkeypatch.setattr("app.db.turns.session_has_unsettled_turns", lambda sid: False)
    monkeypatch.setattr("app.db.turns.get_cwd_failure", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "_latest_context_turn", lambda sid: {"id": "wt2"})
    monkeypatch.setattr("app.core.turn_recorder.record_task_settle_frame", AsyncMock())
    monkeypatch.setattr("app.core.provider_agent_feed.record_continuation_report", AsyncMock())
    terminal = AsyncMock()
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", terminal)
    monkeypatch.setattr("app.core.worker_checkout.reclaim_untouched_worker_checkout",
                        lambda sid: None)

    await agent_messaging.finish_message_resume("w1", settled, "completed")

    row = task_registry.get_task("parent-1", "w1")
    assert row["status"] == "completed"
    assert row["reportPath"] == first
    new_report = "Rechecked; the fix landed.\n\nVerdict: MERGE"
    assert Path(first).read_text() == new_report
    fields = _agent_report_fields("w1", row)
    assert fields["report_path"] == first
    assert fields["report_head"] == new_report
    assert fields["report_chars"] == len(new_report)
    (earlier,) = fields["earlier_report_paths"]
    assert Path(earlier).read_text() == "Verdict: DO NOT MERGE"
    # The woken parent is pointed at the same, latest file.
    _, frame = terminal.await_args.args
    assert frame["report_path"] == first
    assert frame["summary"] == new_report
