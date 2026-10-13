"""Unit tests for app/core/turn_outcome.py — the shared event-log outcome
reader behind automation, the REST conversation endpoint and connector intake.

Focused on ``terminal_found`` (2026-08-28 regression): an event log with no
terminal turn event must be distinguishable from a clean empty completion, or
a caller that read the log too early stamps a bogus empty success.
"""

from unittest.mock import patch

from app.core import turn_outcome


def _events(*events):
    return patch("app.db.session_events.get_events", return_value=list(events))


def test_resumed_attempt_delivers_only_its_own_text_and_outcome():
    log = [
        {"seq": 1, "turn_id": "same", "event_type": "turn.started"},
        {"seq": 2, "turn_id": "same", "event_type": "assistant.text_delta", "payload": {"text": "old"}},
        {"seq": 3, "turn_id": "same", "event_type": "turn.parked", "payload": {"usage": {"tokens_out": 1}}},
        {"seq": 4, "turn_id": "same", "event_type": "turn.started", "payload": {"continued": True}},
    ]
    with _events(*log):
        assert turn_outcome.final_turn_outcome("s1")["terminal_found"] is False
        assert turn_outcome.extract_final_assistant_text("s1") == ""
    log.extend([
        {"seq": 5, "turn_id": "same", "event_type": "assistant.text_delta", "payload": {"text": "new"}},
        {"seq": 6, "turn_id": "same", "event_type": "turn.parked", "payload": {"usage": {"tokens_out": 2}}},
    ])
    with _events(*log):
        assert turn_outcome.final_turn_outcome("s1")["usage"] == {"tokens_out": 2}
        assert turn_outcome.extract_final_assistant_text("s1") == "new"


def test_no_terminal_event_is_flagged_not_shaped_as_success():
    log = [
        {"seq": 1, "turn_id": "t1", "event_type": "turn.started", "payload": {}},
        {"seq": 2, "turn_id": "t1", "event_type": "assistant.text_delta",
         "payload": {"text": "still streaming"}},
    ]
    with _events(*log):
        outcome = turn_outcome.final_turn_outcome("s1")
    assert outcome["terminal_found"] is False
    # The derived fields still read "clean and empty" for callers that only
    # consume them — terminal_found is what fail-closed callers key on.
    assert outcome["error"] is None
    assert outcome["interrupted"] is False
    assert outcome["usage"] == {}


def test_empty_log_is_flagged():
    with _events():
        assert turn_outcome.final_turn_outcome("s1")["terminal_found"] is False


def test_completed_turn_sets_terminal_found_and_usage():
    with _events(
        {"seq": 1, "turn_id": "t1", "event_type": "turn.completed",
         "payload": {"usage": {"tokens_out": 7}}},
    ):
        outcome = turn_outcome.final_turn_outcome("s1")
    assert outcome["terminal_found"] is True
    assert outcome["usage"] == {"tokens_out": 7}
    assert outcome["error"] is None


def test_park_does_not_borrow_usage_across_an_abort_or_older_park():
    for boundary in ("turn.aborted", "turn.parked"):
        with _events(
            {"seq": 1, "turn_id": "old", "event_type": "turn.completed",
             "payload": {"usage": {"tokens_out": 99}}},
            {"seq": 2, "event_type": boundary, "payload": {}},
            {"seq": 3, "event_type": "turn.parked", "payload": {}},
        ):
            assert turn_outcome.final_turn_outcome("s1")["usage"] == {}


def test_park_does_not_borrow_previous_turns_usage_across_turn_started():
    # The parked turn's own done event was never written: the nearest earlier
    # completion belongs to the PREVIOUS turn and must not be reported.
    with _events(
        {"seq": 1, "turn_id": "old", "event_type": "turn.completed",
         "payload": {"usage": {"tokens_out": 99}}},
        {"seq": 2, "turn_id": "t2", "event_type": "turn.started", "payload": {}},
        {"seq": 3, "turn_id": "t2", "event_type": "assistant.text_delta",
         "payload": {"text": "waiting on a Monitor"}},
        {"seq": 4, "event_type": "turn.parked", "payload": {}},
    ):
        assert turn_outcome.final_turn_outcome("s1")["usage"] == {}


def test_park_keeps_its_own_turns_usage():
    with _events(
        {"seq": 1, "turn_id": "t2", "event_type": "turn.started", "payload": {}},
        {"seq": 2, "turn_id": "t2", "event_type": "turn.completed",
         "payload": {"usage": {"tokens_out": 5}}},
        {"seq": 3, "event_type": "turn.parked", "payload": {}},
    ):
        assert turn_outcome.final_turn_outcome("s1")["usage"] == {"tokens_out": 5}


def test_aborted_and_parked_turns_set_terminal_found():
    with _events(
        {"seq": 1, "turn_id": "t1", "event_type": "turn.aborted",
         "payload": {"reason": "error", "message": "boom"}},
    ):
        outcome = turn_outcome.final_turn_outcome("s1")
    assert outcome["terminal_found"] is True
    assert outcome["error"] == "boom"

    with _events(
        {"seq": 1, "turn_id": "t1", "event_type": "turn.parked",
         "payload": {}},
    ):
        outcome = turn_outcome.final_turn_outcome("s1")
    assert outcome["terminal_found"] is True
    assert outcome["error"] is None
