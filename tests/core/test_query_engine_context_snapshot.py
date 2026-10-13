"""Snapshot-only usage records must not blank the persisted context size.

fluid's delegated leg reports a mid-turn context breakdown as a SECOND ``Usage``
event for the same node, with every token field left at 0 — the call's tokens
were already billed by the first event and counting them twice would inflate the
turn. The streaming paths in ``query_engine`` / ``query_background`` overwrite
``metadata["last_call_*"]`` from each usage record, so without a guard that
second event would pin the persisted context-window size at zero.

Both streaming sites and the background site call the same predicate, so
covering it here covers all three without a full SDK streaming harness (the
pattern ``test_query_engine_rate_limit`` uses).
"""

import pytest

from app.core.query_streaming import is_context_snapshot_only


SNAPSHOT = {"categories": [], "totalTokens": 128, "maxTokens": 200_000}


class TestIsContextSnapshotOnly:
    def test_true_for_breakdown_with_no_billing(self):
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "context_usage": SNAPSHOT,
        }
        assert is_context_snapshot_only(usage) is True

    def test_true_when_token_keys_are_absent_entirely(self):
        assert is_context_snapshot_only({"context_usage": SNAPSHOT}) is True

    @pytest.mark.parametrize(
        "key",
        [
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ],
    )
    def test_false_when_any_token_field_is_billed(self, key):
        # A record that reports real billing IS the last call, breakdown or not.
        assert is_context_snapshot_only({key: 7, "context_usage": SNAPSHOT}) is False

    def test_false_without_a_breakdown(self):
        # An all-zero usage record with no snapshot is just an empty reading and
        # keeps its existing overwrite behaviour.
        assert is_context_snapshot_only({"input_tokens": 0}) is False

    def test_false_for_a_null_or_empty_breakdown(self):
        assert is_context_snapshot_only({"context_usage": None}) is False
        assert is_context_snapshot_only({"context_usage": {}}) is False

    def test_false_for_non_dict_input(self):
        assert is_context_snapshot_only(None) is False
        assert is_context_snapshot_only("nope") is False


class TestStreamingSitesApplyTheGuard:
    """The guard's effect on the metadata the streaming loops persist."""

    @staticmethod
    def _apply(metadata, usage):
        """The shape all four usage blocks share."""
        if not is_context_snapshot_only(usage):
            metadata["last_call_tokens_in"] = usage.get("input_tokens", 0)
            metadata["last_call_cache_creation_tokens"] = usage.get(
                "cache_creation_input_tokens", 0
            )
            metadata["last_call_cache_read_tokens"] = usage.get(
                "cache_read_input_tokens", 0
            )
        return metadata

    def test_snapshot_only_leaves_the_last_real_call_in_place(self):
        metadata = {}
        self._apply(metadata, {
            "input_tokens": 900,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 0,
        })
        self._apply(metadata, {"context_usage": SNAPSHOT})
        assert metadata["last_call_tokens_in"] == 900
        assert metadata["last_call_cache_read_tokens"] == 100

    def test_a_billed_record_still_overwrites(self):
        metadata = {"last_call_tokens_in": 900}
        self._apply(metadata, {"input_tokens": 1200, "context_usage": SNAPSHOT})
        assert metadata["last_call_tokens_in"] == 1200


def test_streaming_blocks_are_wired_to_the_guard():
    """Guards against the predicate being written but never called."""
    import inspect

    from app.core import query_background, query_engine

    engine_src = inspect.getsource(query_engine)
    background_src = inspect.getsource(query_background)
    # Three usage blocks in query_engine, one in query_background.
    assert engine_src.count("_is_context_snapshot_only(message.usage)") == 3
    assert background_src.count("_is_context_snapshot_only(message.usage)") == 1
