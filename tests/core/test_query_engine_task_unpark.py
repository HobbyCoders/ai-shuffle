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

from app.core.query_engine import _read_task_output_file, _task_notification_frame


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
