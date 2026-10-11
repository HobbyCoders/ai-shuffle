"""Persisted question anchors work without ever opening a transcript."""
import importlib
import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.global_events import global_events
from app.core.models import Session, SessionWithMessages
from app.db import base, database, session_events, sessions
from app.db import session_questions as questions
from tests.db import schema_lifecycle_helpers as lifecycle


CASES = json.loads((Path(__file__).parents[1] / "fixtures/prose_questions.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["content"])
def test_shared_question_predicate(case):
    assert questions.asks_question(case["content"]) is case["expected"]


def event(event_type="message.assistant", **payload):
    return {"type": event_type, "payload": payload}


def row():
    return database.get_session("question")


@pytest.fixture
def chat():
    database.create_session("question", "default")
    return "question"


def test_last_prose_only_and_non_prose_ignored(chat):
    with patch.object(global_events, "lifecycle") as publish:
        session_events.append_events(chat, [event("message.user", content="Start"), event(content="Which?")])
        saved = row()
        assert saved["last_assistant_question_seq"] == 2
        assert saved["last_assistant_question_epoch"] == saved["last_user_message_epoch"]
        publish.assert_called_once()
        assert publish.call_args.args[1]["updates"]["last_assistant_question_seq"] == 2
        for extra in [event(kind="thinking", content="Hmm?"), event("tool.use", name="Read"), event("system.note")]:
            session_events.append_events(chat, [extra])
            assert row()["last_assistant_question_seq"] == 2
        session_events.append_events(chat, [event(content="Done.")])
        assert row()["last_assistant_question_seq"] is None
        assert row()["last_assistant_question_epoch"] == saved["last_assistant_question_epoch"]
        assert publish.call_args.args[1]["updates"]["last_assistant_question_seq"] is None


@pytest.mark.parametrize("name", ["AskUser", "AskUserQuestion"])
def test_question_tool_suppresses_preface(chat, name):
    session_events.append_events(chat, [event(content="Which?"), event("tool.use", name=name)])
    assert row()["last_assistant_question_seq"] is None


def test_stream_restart_and_latest_position(chat):
    session_events.append_events(chat, [event("message.user"), event("assistant.text_delta", text="Which ")])
    assert row()["last_assistant_question_seq"] is None
    importlib.reload(questions)
    session_events.append_events(chat, [event("system.note", subtype="model_info"), event("assistant.text_delta", text="one?")])
    assert row()["last_assistant_question_seq"] is None
    session_events.append_events(chat, [event("turn.completed")])
    assert row()["last_assistant_question_seq"] == 4
    importlib.reload(questions)
    assert row()["last_assistant_question_seq"] == 4
    session_events.append_events(chat, [event("assistant.text_delta", text="Done.")])
    assert row()["last_assistant_question_seq"] is None
    session_events.append_events(chat, [event("turn.completed")])
    assert row()["last_assistant_question_seq"] is None


def test_voice_answer_clear_and_delayed_snapshot(chat):
    session_events.append_events(chat, [event("message.user"), event(content="Which?")])
    old = row()
    with patch.object(global_events, "lifecycle") as publish:
        session_events.append_events(chat, [event("message.user", source="voice", content="First")])
        assert row()["last_user_message_seq"] > row()["last_assistant_question_seq"]
        delayed = publish.call_args.args[1]["updates"]
    sessions.clear_session_conversation(chat)
    assert all(row()[key] is None for key in (
        "last_user_message_seq", "last_user_message_epoch",
        "last_assistant_question_seq", "last_assistant_question_epoch",
    ))
    session_events.append_events(chat, [event(content="Again?")])
    assert row()["last_assistant_question_seq"] == 1
    for stale in [old, delayed]:
        assert stale["last_assistant_question_epoch"] != row()["last_assistant_question_epoch"]


@pytest.mark.parametrize("rollback", [True, False])
def test_atomic_stable_id_retry(chat, rollback):
    question = {**event(content="Which?"), "id": "stable-question"}
    with patch.object(global_events, "lifecycle") as publish:
        with pytest.raises(ValueError) if rollback else nullcontext():
            with base.transaction() as conn:
                session_events.append_events(chat, [question], _conn=conn)
                publish.assert_not_called()
                if rollback:
                    raise ValueError("abort")
        assert publish.call_count == (0 if rollback else 1)
        assert row()["last_assistant_question_seq"] == (None if rollback else 1)
        if not rollback:
            session_events.append_events(chat, [event(content="Done.")])
            publish.reset_mock()
            session_events.append_events(chat, [question])
            publish.assert_not_called()
            assert row()["last_assistant_question_seq"] is None


def test_rewind_clears_discarded_positions_and_does_not_resurrect(chat):
    session_events.append_events(chat, [event("message.user"), event(content="Which?")])
    session_events.append_events(chat, [event("transcript.rewound", from_seq=1)])
    assert row()["last_user_message_seq"] is None
    assert row()["last_user_message_epoch"] is None
    assert row()["last_assistant_question_seq"] is None
    session_events.append_events(chat, [event("turn.completed")])
    assert row()["last_assistant_question_seq"] is None
    session_events.append_events(chat, [event("message.user"), event(content="New?")])
    assert row()["last_user_message_seq"] == 5
    assert row()["last_assistant_question_seq"] == 6


def test_paginated_tail_and_last_nonempty_prose(chat):
    session_events.append_events(chat, [event("assistant.text_delta", text="x") for _ in range(130)] + [
        event("assistant.text_delta", text="?"), event("turn.completed"), event(content=" "),
    ])
    assert row()["last_assistant_question_seq"] == 131


def test_nullable_models_and_old_peer_materialization(chat):
    from app.network import engine, policy
    for model in (Session, SessionWithMessages):
        payload = model(**row()).model_dump(mode="json")
        assert payload["last_assistant_question_seq"] is None
        assert payload["last_assistant_question_epoch"] is None
    session_events.append_events(chat, [event(content="Which?")])
    for model in (Session, SessionWithMessages):
        payload = model(**row()).model_dump(mode="json")
        assert type(payload["last_assistant_question_seq"]) is int
        assert type(payload["last_assistant_question_epoch"]) is str
    with base.get_db() as conn:
        mirrored = engine.materialize(conn.cursor(), policy.COLLECTIONS["sessions"], chat,
                                      {"id": chat, "title": "Old peer"})
    assert mirrored["last_assistant_question_seq"] is None
    assert mirrored["last_assistant_question_epoch"] is None


def test_guarded_upgrade_and_fresh_schema(tmp_path):
    raw = lifecycle.connect(tmp_path / "questions.db")
    try:
        lifecycle.boot(raw)
        raw.execute("INSERT INTO sessions (id, title) VALUES ('old', 'preserved')")
        for field in ("last_assistant_question_seq", "last_assistant_question_epoch"):
            raw.execute(f"ALTER TABLE sessions DROP COLUMN {field}")
        raw.execute("DELETE FROM system_settings WHERE key = ?", (database.MIGRATIONS_FINGERPRINT_KEY,))
        raw.commit()
        lifecycle.boot(raw)
        lifecycle.boot(raw)
        saved = raw.execute("SELECT title, last_assistant_question_seq, last_assistant_question_epoch FROM sessions WHERE id='old'").fetchone()
        assert tuple(saved) == ("preserved", None, None)
    finally:
        raw.close()


@pytest.mark.parametrize("path", ["", "/paginated", "/cursor", "/question", "/question/live?include_messages=false", "/question/state"])
@pytest.mark.parametrize("question", [False, True])
def test_http_surface_parity(chat, path, question):
    from app.api.sessions import router
    from app.api.auth import require_auth
    if question:
        session_events.append_events(chat, [event(content="Which?")])
    app = FastAPI()
    app.include_router(router)

    def auth(request: Request):
        request.state.is_admin = True
        request.state.api_user = None
        return "test-owner"

    app.dependency_overrides[require_auth] = auth
    with TestClient(app) as client:
        response = client.get("/api/v1/sessions" + path)
    assert response.status_code == 200, response.text
    body = response.json()
    payload = body[0] if isinstance(body, list) else body.get("items", [body])[0]
    payload = payload.get("session_meta", payload)
    assert payload["last_assistant_question_seq"] == (1 if question else None)
    epoch = payload["last_assistant_question_epoch"]
    assert (type(epoch) is str) if question else epoch is None


@pytest.mark.asyncio
async def test_real_recorder_stream_produces_question(chat):
    from app.core.turn_recorder import TurnRecorder
    recorder = TurnRecorder(chat, None, None, None, "test-owner")
    try:
        await recorder.start("Start", profile_id="default")
        await recorder.on_event({"type": "stream_delta", "delta_type": "text", "content": "Which "})
        await recorder.on_event({"type": "provider_alive", "signal": "keepalive"})
        assert row()["last_assistant_question_seq"] is None
        await recorder.on_event({"type": "stream_delta", "delta_type": "text", "content": "one?"})
        await recorder.on_event({"type": "done", "metadata": {}})
        saved = row()
        assert saved["last_assistant_question_seq"] > saved["last_user_message_seq"]
        assert saved["last_assistant_question_epoch"] == session_events.get_log_epoch(chat)
    finally:
        await recorder.close()
