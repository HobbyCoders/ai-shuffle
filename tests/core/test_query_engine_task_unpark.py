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
# _cli_turn_events_from_message — CLI-driven (autonomous) turn converter
#
# Round 4 of the duplicate-notification fix: when a top-level backgrounded
# Task/Agent completes while the parent is parked, the CLI injects its OWN
# wake turn on the pooled client. The drain pump forwards that turn's top-level
# content through the normal dispatch pipeline using this pure converter.
# (PR #498's text-sniffing _latch_cli_task_notifications was removed as
# proven-dead: at-rest injections never pass through stream_to_websocket.)
# =============================================================================

from app.core.provider.events import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app.core.query_engine import _cli_turn_events_from_message


class TestCliTurnEventsFromMessage:
    def setup_method(self):
        task_registry.reset_all()

    def teardown_method(self):
        task_registry.reset_all()

    def test_assistant_text_becomes_chunk(self):
        msg = AssistantMessage(content=[TextBlock("the answer")], model="m")
        events = _cli_turn_events_from_message("sess-1", msg, {})
        assert events == [{"type": "chunk", "content": "the answer"}]

    def test_whitespace_only_text_dropped(self):
        msg = AssistantMessage(content=[TextBlock("\n\n")], model="m")
        assert _cli_turn_events_from_message("sess-1", msg, {}) == []

    def test_thinking_becomes_thinking_block_marker(self):
        msg = AssistantMessage(
            content=[ThinkingBlock(thinking="hmm", signature="s")], model="m"
        )
        events = _cli_turn_events_from_message("sess-1", msg, {})
        assert events == [{"type": "thinking_block", "content": "hmm"}]

    def test_plain_tool_use_and_result_flow_with_name_resolution(self):
        names = {}
        use = AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "/x"})],
            model="m",
        )
        events = _cli_turn_events_from_message("sess-1", use, names)
        assert events == [
            {"type": "tool_use", "name": "Read", "id": "t1", "input": {"file_path": "/x"}}
        ]
        res = UserMessage(
            content=[ToolResultBlock(tool_use_id="t1", content="data", is_error=False)]
        )
        revents = _cli_turn_events_from_message("sess-1", res, names)
        assert revents == [{
            "type": "tool_result",
            "name": "Read",
            "tool_use_id": "t1",
            "output": "data",
            "is_error": False,
        }]

    def test_task_tool_becomes_subagent_start(self):
        msg = AssistantMessage(
            content=[ToolUseBlock(
                id="t2", name="Agent",
                input={"description": "probe", "prompt": "go", "subagent_type": "Explore"},
            )],
            model="m",
        )
        events = _cli_turn_events_from_message("sess-1", msg, {})
        assert events == [{
            "type": "subagent_start",
            "tool_id": "t2",
            "agent_type": "Explore",
            "description": "probe",
            "prompt": "go",
            "agent_id": "t2",
        }]

    def test_run_in_background_input_arms_launch_gate(self):
        msg = AssistantMessage(
            content=[ToolUseBlock(
                id="t3", name="Agent",
                input={"description": "bg", "prompt": "go", "run_in_background": True},
            )],
            model="m",
        )
        _cli_turn_events_from_message("sess-1", msg, {})
        assert task_registry.was_background_launch("sess-1", "t3") is True

    def test_launch_ack_tool_result_arms_launch_gate(self):
        # A CLI-driven turn launches an async Agent (no input flag); the launch
        # ACK ToolResult is what reveals backgroundness — the converter must arm
        # the gate or the chained agent's own wake would read as foreground.
        names = {"t4": "Agent"}
        res = UserMessage(content=[ToolResultBlock(
            tool_use_id="t4",
            content="Async agent launched successfully. It is working in the background.",
            is_error=False,
        )])
        _cli_turn_events_from_message("sess-1", res, names)
        assert task_registry.was_background_launch("sess-1", "t4") is True

    def test_injected_user_string_is_hidden(self):
        msg = UserMessage(content=(
            "[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
            "<task-notification>\n<task-id>task-1</task-id>\n</task-notification>"
        ))
        assert _cli_turn_events_from_message("sess-1", msg, {}) == []

    def test_subagent_inner_messages_excluded(self):
        # parent_tool_use_id set → belongs to _subagent_events_from_message.
        msg = AssistantMessage(
            content=[TextBlock("inner")], model="m", parent_tool_use_id="tu-1"
        )
        assert _cli_turn_events_from_message("sess-1", msg, {}) == []

    def test_non_chat_messages_yield_nothing(self):
        assert _cli_turn_events_from_message("sess-1", SimpleNamespace(), {}) == []


# =============================================================================
# _should_defer_subagent_done — the #660 defer classifier
# =============================================================================

from app.core.query_engine import _should_defer_subagent_done  # noqa: E402


class TestShouldDeferSubagentDone:
    """The defer-don't-terminate classifier: suppress on positive background
    evidence, DEFER when ambiguous. Registry membership must suppress WITHOUT any
    ACK phrase match (the reverse-lookup case #660 adds); a plain foreground
    result with no evidence defers."""

    def setup_method(self):
        task_registry.reset_all()

    def teardown_method(self):
        task_registry.reset_all()

    def test_run_in_background_flag_suppresses(self):
        info = {"agent_type": "Explore", "run_in_background": True}
        assert _should_defer_subagent_done("s1", "tu-1", info, "anything") == "suppress"

    def test_registry_reverse_lookup_suppresses_without_phrase_match(self):
        # A running background task (SDK marker) registered for this launch id,
        # but was_background_launch("tu-1") was NEVER recorded — so ONLY the new
        # reverse lookup can see it. A REAL result (no ACK phrase) must still
        # suppress, and the classifier arms the turn-local + durable gate.
        task_registry.start_task("s1", "task-1", "agent", "bg", tool_use_id="tu-1")
        task_registry.mark_sdk_background_task("s1", "task-1")
        assert task_registry.was_background_launch("s1", "tu-1") is False
        info = {"agent_type": "Explore", "run_in_background": False}
        decision = _should_defer_subagent_done(
            "s1", "tu-1", info, "the real synchronous result, no launch-ack wording"
        )
        assert decision == "suppress"
        # Armed for turn consistency (case b side-effect).
        assert info["run_in_background"] is True
        assert task_registry.was_background_launch("s1", "tu-1") is True

    def test_recorded_background_launch_suppresses(self):
        task_registry.mark_background_launch("s1", "tu-1")
        info = {"agent_type": "Explore", "run_in_background": False}
        assert _should_defer_subagent_done("s1", "tu-1", info, "real result") == "suppress"

    def test_ack_phrase_suppresses(self):
        info = {"agent_type": "Explore", "run_in_background": False}
        ack = "Async agent launched successfully. It is working in the background."
        assert _should_defer_subagent_done("s1", "tu-1", info, ack) == "suppress"
        # Sniffer fallback alone does not arm the durable gate (the in-stream ACK
        # arms do that upstream); the classifier's case c is suppress-only.
        assert task_registry.was_background_launch("s1", "tu-1") is False

    def test_ambiguous_real_result_defers(self):
        # No flag, no registry evidence, no ACK phrase -> DEFER (park).
        info = {"agent_type": "Explore", "run_in_background": False}
        decision = _should_defer_subagent_done(
            "s1", "tu-1", info, "All three commands ran successfully: STEP-1, STEP-2."
        )
        assert decision == "defer"
        assert info["run_in_background"] is False
        assert task_registry.was_background_launch("s1", "tu-1") is False


# =============================================================================
# _task_launch_failure_error — dispatch rejections settle immediately
# =============================================================================

from app.core.query_engine import _task_launch_failure_error  # noqa: E402


class TestTaskLaunchFailureError:
    """A Task launch that dies before a worker spawns is positive terminal
    evidence — the classifier must recognise it so the card settles at result
    time instead of parking on the end-of-turn flush (which an interrupt skips,
    leaving the card spinning forever)."""

    def test_rejection_json_returns_error_text(self):
        out = ('{"ok": false, "error": "Model \'sonnet\' is not in provider '
               '\'ChatGPT\'\'s curated model list."}')
        assert _task_launch_failure_error(out, False) == (
            "Model 'sonnet' is not in provider 'ChatGPT''s curated model list."
        )

    def test_rejection_json_without_error_field_returns_raw(self):
        out = '{"ok": false}'
        assert _task_launch_failure_error(out, False) == out

    def test_successful_dispatch_is_not_a_failure(self):
        assert _task_launch_failure_error('{"ok": true, "task_id": "t1"}', False) is None

    def test_plain_prose_result_is_not_a_failure(self):
        assert _task_launch_failure_error("Here is the full report.", False) is None

    def test_malformed_json_is_not_a_failure(self):
        assert _task_launch_failure_error('{"ok": false,', False) is None

    def test_is_error_returns_the_text(self):
        assert _task_launch_failure_error("boom", True) == "boom"

    def test_is_error_with_empty_output_falls_back(self):
        assert _task_launch_failure_error("", True) == "Task launch failed."

    def test_non_str_output_does_not_raise(self):
        # A dict coerced through str() is not JSON (single quotes) — the sniffer
        # must degrade quietly rather than blow up the stream.
        assert _task_launch_failure_error({"ok": False}, False) is None
