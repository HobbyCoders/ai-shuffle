"""Tests for the shared system-message subtype filter used on history reload."""

from app.core.system_message_filter import (
    HIDDEN_SYSTEM_SUBTYPES,
    is_hidden_system_subtype,
)


class TestHiddenSystemSubtypes:
    """Only internal/transient routed-and-returned subtypes are hidden."""

    def test_thinking_tokens_hidden(self):
        # Routed to the live thinking-block counter; never a card live.
        assert is_hidden_system_subtype("thinking_tokens") is True

    def test_task_progress_hidden(self):
        # Routed to subagent progress; never a card live.
        assert is_hidden_system_subtype("task_progress") is True

    def test_legitimate_subtypes_not_hidden(self):
        # These are intentionally shown to the user and must NOT be filtered.
        for subtype in (
            "compact_boundary",
            "microcompact_boundary",
            "api_error",
            "local_command",
            "context",
            "hook_response",
            "plugin_warning",
            "error",
            "status",
            "",
            None,
        ):
            assert is_hidden_system_subtype(subtype) is False

    def test_hidden_set_is_exactly_the_two_routed_subtypes(self):
        assert HIDDEN_SYSTEM_SUBTYPES == frozenset(
            {"thinking_tokens", "task_progress"}
        )


class TestDbLoaderFiltersHiddenSystemSubtypes:
    """The DB loader's row transform drops hidden system subtypes."""

    def _row(self, subtype):
        return {
            "id": 1,
            "role": "system",
            "content": "{}",
            "metadata": {"subtype": subtype, "system_data": {}},
            "created_at": "2024-01-01T00:00:00Z",
        }

    def test_thinking_tokens_row_dropped(self):
        from app.core.db_message_loader import _transform_row

        assert _transform_row(self._row("thinking_tokens")) is None

    def test_task_progress_row_dropped(self):
        from app.core.db_message_loader import _transform_row

        assert _transform_row(self._row("task_progress")) is None

    def test_compact_boundary_row_kept(self):
        from app.core.db_message_loader import _transform_row

        msg = _transform_row(self._row("compact_boundary"))
        assert msg is not None
        assert msg["type"] == "system"
        assert msg["subtype"] == "compact_boundary"
