"""Same-key hand-off corrections invalidate resumed readers on real PostgreSQL."""
import json

import pytest

from app.api.events import _resume_check
from app.db import database, session_events as events
from app.network import handoff
from tests.core.network.test_transcript_op_retention_postgres import cluster, pg_workers  # noqa: F401


def test_handoff_corrects_an_incomplete_merged_row_and_resets_surviving_cursor(pg_workers, tmp_path):
    source, mirror = pg_workers("source"), pg_workers("mirror")
    with source.active():
        database.create_session(session_id="chat", profile_id="default", project_id=None, title="Chat")
        events.append_events("chat", [{"type": "message.user", "payload": {"content": "hello"}}, *[
            {"type": "assistant.text_delta", "payload": {"text": text}} for text in "ABCDEF"
        ], {"type": "message.assistant", "payload": {"content": "done"}}])
        original = handoff._history({"id": "chat"})
    with mirror.active():
        database.create_session(session_id="chat", profile_id="default", project_id=None, title="Chat")
        handoff.install_history("chat", {"history": original}, str(tmp_path))
    mirror.sql("DELETE FROM session_events WHERE session_id='chat' AND seq=3")
    for worker in (source, mirror):
        with worker.active():
            events.compact_session_deltas("chat", keep_tail_events=0)
    with source.active():
        history = handoff._history({"id": "chat"})
    with mirror.active():
        before = events.get_reader_epoch("chat")
        held = events.get_events("chat")
        assert held[1]["payload"]["text"] == "ACDEF"
        assert held[1]["seq"] == 7
        assert held[1]["id"] == next(row["id"] for row in history["session_events"] if row["seq"] == 7)
        handoff.install_history("chat", {"history": history}, str(tmp_path))
        assert events.get_events("chat")[1]["payload"]["text"] == "ABCDEF"
        current, reason = _resume_check("chat", 8, before)
        assert reason == "epoch" and current != before
        assert events.has_event_at("chat", 8)
        # Export/transport round trips stringify dates and JSON, but do not rewrite.
        portable = json.loads(json.dumps(history, default=str))
        for row in portable["session_events"]:
            row["payload"] = json.dumps(row["payload"], sort_keys=True)
        handoff.install_history("chat", {"history": portable}, str(tmp_path))
        assert _resume_check("chat", 8, current) == (current, None)


@pytest.mark.parametrize("column,value", [
    ("id", "correct-event"), ("event_type", "message.user"), ("command_id", "correct-command"),
    ("turn_id", "correct-turn"), ("created_at", "2026-01-01T00:00:00Z"),
])
def test_same_key_replay_metadata_corrections_reset_even_at_the_last_row(pg_workers, tmp_path, column, value):
    worker = pg_workers("mirror")
    with worker.active():
        database.create_session(session_id="chat", profile_id="default", project_id=None, title="Chat")
        events.append_events("chat", [{"type": "message.assistant", "payload": {"content": "done"}}])
        before = events.get_reader_epoch("chat")
        history = handoff._history({"id": "chat"})
        history["session_events"][0][column] = value
        handoff.install_history("chat", {"history": history}, str(tmp_path))
        current, reason = _resume_check("chat", 1, before)
        assert reason == "epoch" and current != before
