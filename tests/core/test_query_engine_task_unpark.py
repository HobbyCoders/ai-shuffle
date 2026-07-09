"""
Tests for the background-agent "unpark" helpers in app/core/query_engine.py:

- _read_task_output_file: reads the full sub-agent result off local disk,
  truncates oversize content, and returns "" on any error (missing path, etc.).
- _task_notification_frame: builds the enriched terminal task_notification wire
  frame (output_file / uuid / usage alongside task_id / status / summary /
  tool_use_id), shared by both streaming paths.

No SDK client, pool, or Postgres is required — these are pure helper functions.
"""

from types import SimpleNamespace

import pytest

from app.core.query_engine import (
    _latch_cli_task_notifications,
    _read_task_output_file,
    _task_notification_frame,
)
from app.core.task_registry import task_registry


# =============================================================================
# _read_task_output_file
# =============================================================================

class TestReadTaskOutputFile:
    def test_reads_full_file(self, tmp_path):
        f = tmp_path / "result.txt"
        f.write_text("the full sub-agent result", encoding="utf-8")
        assert _read_task_output_file(str(f)) == "the full sub-agent result"

    def test_truncates_oversize(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 5000, encoding="utf-8")
        out = _read_task_output_file(str(f), max_chars=100)
        # Truncated well below the original size and carrying a marker.
        assert len(out) < 5000
        assert "x" in out
        assert "…" in out or "truncated" in out.lower()

    def test_exact_max_chars_not_truncated(self, tmp_path):
        f = tmp_path / "exact.txt"
        f.write_text("y" * 100, encoding="utf-8")
        out = _read_task_output_file(str(f), max_chars=100)
        assert out == "y" * 100

    def test_missing_path_returns_empty(self, tmp_path):
        missing = tmp_path / "does-not-exist.txt"
        assert _read_task_output_file(str(missing)) == ""

    def test_blank_path_returns_empty(self):
        assert _read_task_output_file("") == ""
        assert _read_task_output_file(None) == ""  # type: ignore[arg-type]


# =============================================================================
# _task_notification_frame
# =============================================================================

def _msg(**kwargs):
    """A duck-typed stand-in for TaskNotificationMessage."""
    base = {
        "task_id": "task-1",
        "status": "completed",
        "summary": "did the thing",
        "tool_use_id": "tu-1",
        "output_file": "/tmp/out.txt",
        "uuid": "uuid-1",
        "usage": {"total_tokens": 1234, "tool_uses": 5, "duration_ms": 9999},
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestTaskNotificationFrame:
    def test_includes_enrichment_fields(self):
        frame = _task_notification_frame(_msg())
        assert frame["type"] == "task_notification"
        assert frame["task_id"] == "task-1"
        assert frame["status"] == "completed"
        assert frame["summary"] == "did the thing"
        assert frame["tool_use_id"] == "tu-1"
        # The enrichment under test:
        assert frame["output_file"] == "/tmp/out.txt"
        assert frame["uuid"] == "uuid-1"
        assert frame["usage"] == {
            "total_tokens": 1234,
            "tool_uses": 5,
            "duration_ms": 9999,
        }

    def test_usage_none_when_absent(self):
        frame = _task_notification_frame(_msg(usage=None))
        assert frame["usage"] is None

    def test_status_enum_unwrapped(self):
        # A status carrying a .value (enum-like) is unwrapped to the string.
        status = SimpleNamespace(value="failed")
        frame = _task_notification_frame(_msg(status=status))
        assert frame["status"] == "failed"


# =============================================================================
# _latch_cli_task_notifications — in-process straddle latch
#
# When the CLI injects a <task-notification> user-lane steering message MID-turn
# (which flows through stream_to_websocket as a plain-text UserMessage), we spend
# mark_unparked at the source so the between-turns drain pump's existing per-task
# dedup suppresses the straddle duplicate. RED against pre-latch code (the token
# is never spent, so the pump re-delivers).
# =============================================================================

def _cli_task_notification_text(task_id="task-1", tool_use_id="tu-1"):
    """The unindented CLI-format steering message the SDK injects in-stream."""
    return (
        "[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
        "<status>completed</status>\n"
        "<summary>done</summary>\n"
        "</task-notification>"
    )


class TestLatchCliTaskNotifications:
    def setup_method(self):
        task_registry.reset_all()

    def teardown_method(self):
        task_registry.reset_all()

    def test_spends_mark_unparked_for_task_id(self):
        text = _cli_task_notification_text("task-1", "tu-1")
        assert task_registry.was_unparked("sess-1", "task-1") is False
        _latch_cli_task_notifications("sess-1", text)
        # Token spent — a later pump mark_unparked returns False (dedup → suppress).
        assert task_registry.was_unparked("sess-1", "task-1") is True
        assert task_registry.mark_unparked("sess-1", "task-1") is False

    def test_multiple_blocks_all_latched(self):
        text = (
            _cli_task_notification_text("task-1", "tu-1")
            + "\n"
            + _cli_task_notification_text("task-2", "tu-2")
        )
        _latch_cli_task_notifications("sess-1", text)
        assert task_registry.was_unparked("sess-1", "task-1") is True
        assert task_registry.was_unparked("sess-1", "task-2") is True

    def test_idempotent_relatch_is_harmless(self):
        text = _cli_task_notification_text("task-1", "tu-1")
        _latch_cli_task_notifications("sess-1", text)
        # The app's OWN synthetic unpark prompt flows back as a UserMessage on a
        # follow-up turn; re-latching the same task must not error / re-arm.
        _latch_cli_task_notifications("sess-1", text)
        assert task_registry.was_unparked("sess-1", "task-1") is True

    def test_resolves_task_via_tool_use_id_when_no_task_id(self):
        # A block with only <tool-use-id> resolves the task via the registry's
        # recorded launch tool_use_id.
        task_registry.start_task(
            "sess-1", "task-9", "agent", "bg work", tool_use_id="tu-9"
        )
        text = (
            "<task-notification>\n"
            "<tool-use-id>tu-9</tool-use-id>\n"
            "<status>completed</status>\n"
            "</task-notification>"
        )
        _latch_cli_task_notifications("sess-1", text)
        assert task_registry.was_unparked("sess-1", "task-9") is True

    def test_no_task_id_no_tool_use_id_is_skipped(self):
        text = "<task-notification>\n<status>completed</status>\n</task-notification>"
        # No crash; nothing latched.
        _latch_cli_task_notifications("sess-1", text)
        assert task_registry.was_unparked("sess-1", "task-1") is False

    def test_non_notification_text_is_noop(self):
        _latch_cli_task_notifications("sess-1", "just a normal user message")
        assert task_registry._unparked.get("sess-1") in (None, set())
