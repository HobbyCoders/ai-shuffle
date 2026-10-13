"""Cost attachment must stay correct when it is handed a WINDOW of a transcript.

`attach_cost_metadata` matches usage_log rows to turns positionally, counting
from the start of the list it is given. That is right for a full transcript and
silently wrong for a paginated slice: every message gets the cost of a different
turn, and the window's last turn falsely claims to be the end of the session.

These tests pin the offset/total contract that makes windowed pagination safe to
adopt in the session-open endpoints.
"""
from unittest.mock import patch

from app.core.cost_utils import attach_cost_metadata


def _usage(n):
    """n usage rows, each distinguishable by cost."""
    return [
        {
            "cost_usd": float(i + 1),
            "duration_ms": (i + 1) * 100,
            "tokens_out": (i + 1) * 10,
            "cumulative_cost_usd": sum(float(j + 1) for j in range(i + 1)),
        }
        for i in range(n)
    ]


def _turns(n, start=0):
    """n user->assistant turns, assistant text labelled by absolute turn index."""
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"q{start + i}"})
        msgs.append({"role": "assistant", "type": "text", "content": f"a{start + i}"})
    return msgs


def _costs(messages):
    return [
        m["metadata"]["cost_usd"]
        for m in messages
        if m.get("role") == "assistant" and m.get("metadata")
    ]


class TestWindowedCostAttachment:
    def test_full_transcript_is_unchanged_by_the_new_parameters(self):
        messages = _turns(5)
        with patch("app.core.cost_utils.get_cached_usage", return_value=_usage(5)):
            attach_cost_metadata(messages, "s1")
        assert _costs(messages) == [1.0, 2.0, 3.0, 4.0, 5.0]

    def test_a_window_without_an_offset_would_attach_the_wrong_costs(self):
        """Pins the failure mode: this is why turn_offset has to exist."""
        window = _turns(2, start=3)
        with patch("app.core.cost_utils.get_cached_usage", return_value=_usage(5)):
            attach_cost_metadata(window, "s1")
        # Turns 3 and 4 wrongly receive the costs of turns 0 and 1.
        assert _costs(window) == [1.0, 2.0]

    def test_a_window_with_an_offset_attaches_the_right_costs(self):
        window = _turns(2, start=3)
        with patch("app.core.cost_utils.get_cached_usage", return_value=_usage(5)):
            attach_cost_metadata(window, "s1", turn_offset=3, total_turns=5)
        assert _costs(window) == [4.0, 5.0]

    def test_windowed_costs_match_the_full_parse_slice(self):
        """Page-by-page equivalence: windows must agree with the full transcript."""
        full = _turns(6)
        with patch("app.core.cost_utils.get_cached_usage", return_value=_usage(6)):
            attach_cost_metadata(full, "s1")
        expected = _costs(full)

        got = []
        for offset in (0, 2, 4):
            window = _turns(2, start=offset)
            with patch("app.core.cost_utils.get_cached_usage", return_value=_usage(6)):
                attach_cost_metadata(window, "s1", turn_offset=offset, total_turns=6)
            got.extend(_costs(window))
        assert got == expected

    def test_only_the_sessions_real_last_turn_is_final(self):
        window = _turns(2, start=2)  # turns 2,3 of a 6-turn session
        with patch("app.core.cost_utils.get_cached_usage", return_value=_usage(6)):
            attach_cost_metadata(window, "s1", turn_offset=2, total_turns=6)
        finals = [
            m["metadata"]["is_final_message"]
            for m in window
            if m.get("role") == "assistant" and m.get("metadata")
        ]
        assert finals == [False, False]
        assert all("total_cost_usd" not in m.get("metadata", {}) for m in window)

    def test_the_last_window_still_carries_the_cumulative_total(self):
        window = _turns(2, start=4)  # turns 4,5 of a 6-turn session
        with patch("app.core.cost_utils.get_cached_usage", return_value=_usage(6)):
            attach_cost_metadata(window, "s1", turn_offset=4, total_turns=6)
        last = [m for m in window if m.get("role") == "assistant"][-1]
        assert last["metadata"]["is_final_message"] is True
        assert last["metadata"]["total_cost_usd"] == sum(float(i + 1) for i in range(6))
