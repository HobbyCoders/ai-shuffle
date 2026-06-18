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


class TestDbLoaderTimestampSerializable:
    """Regression: PostgreSQL returns timestamp columns as datetime objects
    (SQLite returned ISO strings). A raw datetime in metadata.timestamp is not
    JSON-serializable and breaks every stdlib json.dumps() consumer of these
    messages — the JSONL parse cache pre-seed/store and the SyncEngine WS
    broadcast — so history loads pay a slow re-parse and modals/sync events
    silently drop. The loader must normalize it to an ISO string."""

    import json
    from datetime import datetime, timezone

    def test_datetime_created_at_normalized_to_string(self):
        from app.core.db_message_loader import _transform_row

        row = {
            "id": 7,
            "role": "user",
            "content": "hi",
            "metadata": {},
            "created_at": self.datetime(2026, 6, 18, 9, 22, 40, tzinfo=self.timezone.utc),
        }
        msg = _transform_row(row)
        ts = msg["metadata"]["timestamp"]
        assert isinstance(ts, str), f"timestamp must be a string, got {type(ts).__name__}"
        # And the whole message must be json.dumps-able (the property that broke).
        self.json.dumps(msg)

    def test_string_created_at_passed_through(self):
        from app.core.db_message_loader import _transform_row

        row = {
            "id": 8,
            "role": "user",
            "content": "hi",
            "metadata": {},
            "created_at": "2026-06-18T09:22:40Z",
        }
        msg = _transform_row(row)
        assert msg["metadata"]["timestamp"] == "2026-06-18T09:22:40Z"
