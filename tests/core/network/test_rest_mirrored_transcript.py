"""REST reads of a real network mirror retain the recorder's assistant reply."""
import asyncio
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.sessions import router
from app.contract.events import TranscriptRewound
from app.core.turn_recorder import TurnRecorder, _build_event
from app.db import database, session_events
from app.network import engine
from tests.core.network.harness import converge, join


@pytest.fixture
def mirrored_chat(workers, request):
    viewer, owner = workers("unraid"), workers("remote")
    with viewer.active():
        engine.create_network("Home", device_name="Unraid")
    join(viewer, owner)
    with owner.active():
        database.create_session(session_id="remote-chat", profile_id="default", title="Remote reply")

        async def record():
            rec = TurnRecorder("remote-chat", None, None, None, owner.device_id)
            await rec.start("Say OK", profile_id="default")
            scenario = getattr(request, "param", "text")
            if scenario == "tools":
                await rec.on_event({"type": "stream_delta", "delta_type": "thinking", "content": "Plan"})
                await rec.on_event({"type": "tool_use", "name": "Read", "id": "tu1", "input": {"file_path": "/missing"}})
                await rec.on_event({"type": "tool_result", "tool_use_id": "tu1", "output": "not found", "is_error": True})
            if scenario == "thinking":
                for subtype, text in [
                    ("reasoning", "**One**"), ("reasoning", "**Two**"),
                    ("summary", "Summary"),
                ]:
                    await rec.on_event({"type": "stream_delta", "delta_type": "thinking",
                                        "subtype": subtype, "content": text})
                    await rec.on_event({"type": "provider_alive", "signal": "keepalive"})
            if scenario == "long":
                # A provider-alive frame flushes the recorder's pending delta
                # without closing the replay bubble. More than one DB page.
                for _ in range(505):
                    await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": "x"})
                    await rec.on_event({"type": "provider_alive", "signal": "keepalive"})
            else:
                await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": "O"})
                await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": "K"})
            await rec.on_event({"type": "done", "metadata": {}})
            await rec.close()

        asyncio.run(record())
        if getattr(request, "param", "text") == "rewound":
            # Same typed event write as the successful rewind command; keep
            # the retracted recorder rows in the append-only owner log.
            session_events.append_events("remote-chat", [
                _build_event(TranscriptRewound, "remote-chat", from_seq=1),
            ])
        engine.scan_all()
    converge([viewer, owner])
    with viewer.active():
        row = database.get_session("remote-chat")
        assert row["owner_device_id"] == owner.device_id
        assert not row.get("sdk_session_id")
        # The legacy rows on the viewer may be only the submitted prompt. They
        # cannot replace the real transcript carried by the event mirror.
        database.add_session_message("remote-chat", "user", "Say OK", profile_id="default")
        assert any(e["event_type"] == "assistant.text_delta" for e in session_events.get_events("remote-chat"))
        app = FastAPI()
        app.include_router(router)

        def authenticate(request: Request):
            request.state.is_admin = True
            request.state.api_user = None
            return "test-owner"

        app.dependency_overrides[require_auth] = authenticate
        with TestClient(app) as client:
            yield client


@pytest.mark.parametrize("suffix", ["", "/live"])
def test_mirrored_reply_survives_rest(mirrored_chat, suffix):
    response = mirrored_chat.get("/api/v1/sessions/remote-chat" + suffix)
    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [("user", "Say OK"), ("assistant", "OK")]
    assert all(m["profileId"] == "default" for m in messages)


@pytest.mark.parametrize("suffix", ["", "/live"])
@pytest.mark.parametrize("mirrored_chat", ["tools"], indirect=True)
def test_mirrored_tool_result_updates_its_card(mirrored_chat, suffix):
    response = mirrored_chat.get("/api/v1/sessions/remote-chat" + suffix + "?message_limit=2")
    assert response.status_code == 200, response.text
    tool, reply = response.json()["messages"]
    assert tool["type"] == "tool_use"
    assert tool["toolId"] == "tu1"
    assert tool["toolName"] == "Read"
    assert tool["toolInput"] == {"file_path": "/missing"}
    assert tool["toolResult"] == "not found"
    assert tool["toolStatus"] == "error"
    assert reply["content"] == "OK"
    cursor = response.headers["X-Message-Next-Before-Seq"] if not suffix else response.json()["message_pagination"]["next_before_seq"]
    older = mirrored_chat.get("/api/v1/sessions/remote-chat" + suffix + f"?message_limit=2&before_seq={cursor}")
    assert older.status_code == 200, older.text
    assert [(m["type"], m["content"]) for m in older.json()["messages"]] == [("text", "Say OK"), ("thinking", "Plan")]


@pytest.mark.parametrize("suffix", ["", "/live"])
@pytest.mark.parametrize("mirrored_chat", ["long"], indirect=True)
def test_event_pages_do_not_split_a_reply(mirrored_chat, suffix):
    response = mirrored_chat.get("/api/v1/sessions/remote-chat" + suffix + "?message_limit=1")
    assert response.status_code == 200, response.text
    assert [m["content"] for m in response.json()["messages"]] == ["x" * 505]


def test_metadata_only_live_does_not_read_events(mirrored_chat, monkeypatch):
    def fail(*args):
        raise AssertionError("metadata-only read touched the transcript")

    monkeypatch.setattr("app.api.sessions.load_event_messages", fail)
    response = mirrored_chat.get("/api/v1/sessions/remote-chat/live?include_messages=false")
    assert response.status_code == 200, response.text
    assert response.json()["messages"] == []


@pytest.mark.parametrize("suffix", ["", "/live"])
@pytest.mark.parametrize("store", ["absent", "empty", "raised"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("history", [
    "absent", "event_only", "retracted", "unavailable", "counter_unavailable",
    "event_only_unavailable", "retracted_unavailable",
])
def test_legacy_rescue_requires_no_event_history(
    mirrored_chat, monkeypatch, suffix, store, legacy, history,
):
    database.create_session(session_id="legacy-chat", profile_id="default")
    if legacy:
        database.add_session_message("legacy-chat", "user", "Legacy row")
    if store != "absent":
        database.update_session("legacy-chat", sdk_session_id="legacy-sdk")
        monkeypatch.setattr("app.core.message_resolver.resolve_working_dir", lambda session: "/missing")
        monkeypatch.setattr("app.core.provider.fluid_transcript.is_fluid_session", lambda sid: False)

        def parse(*args):
            if store == "raised":
                raise RuntimeError("local store unavailable")
            return [], {}

        monkeypatch.setattr("app.api.sessions.parse_session_history_via_store", parse)
    if history.startswith("event_only"):
        session_events.append_events("legacy-chat", [{"type": "turn.started", "payload": {}}])
    elif history.startswith("retracted"):
        session_events.append_events("legacy-chat", [
            {"type": "message.user", "payload": {"content": "Removed prompt"}},
            {"type": "assistant.text_delta", "payload": {"text": "Removed reply"}},
            {"type": "transcript.rewound", "payload": {"from_seq": 1}},
        ])
    if "unavailable" in history:
        def unavailable(*args, **kwargs):
            raise RuntimeError("event mirror unavailable")
        monkeypatch.setattr(session_events, "get_events", unavailable)
        if history == "counter_unavailable":
            monkeypatch.setattr(session_events, "get_max_seq", unavailable)
    response = mirrored_chat.get("/api/v1/sessions/legacy-chat" + suffix)
    assert response.status_code == 200, response.text
    rescue = legacy and store != "raised" and history in ("absent", "unavailable")
    expected = ["Legacy row"] if rescue else []
    assert [m["content"] for m in response.json()["messages"]] == expected


@pytest.mark.parametrize("suffix", ["", "/live"])
@pytest.mark.parametrize("store", ["empty", "raised"])
def test_event_mirror_rescues_missing_or_failed_local_store(mirrored_chat, monkeypatch, suffix, store):
    database.update_session("remote-chat", sdk_session_id="missing-sdk")
    monkeypatch.setattr("app.core.message_resolver.resolve_working_dir", lambda session: "/missing")
    monkeypatch.setattr("app.core.provider.fluid_transcript.is_fluid_session", lambda sid: False)

    def parse(*args):
        if store == "raised":
            raise RuntimeError("local store unavailable")
        return [], {}

    monkeypatch.setattr("app.api.sessions.parse_session_history_via_store", parse)
    response = mirrored_chat.get("/api/v1/sessions/remote-chat" + suffix)
    assert response.status_code == 200, response.text
    assert [m["content"] for m in response.json()["messages"]] == ["Say OK", "OK"]

@pytest.mark.parametrize("suffix", ["", "/live"])
@pytest.mark.parametrize("mirrored_chat", ["text", "rewound"], indirect=True)
@pytest.mark.parametrize("fail_after_page", [False, True])
def test_failed_mirror_read_does_not_resurrect_legacy(
    mirrored_chat, monkeypatch, suffix, fail_after_page,
):
    # This counter was written by the actual peer-mirror ingestion, not seeded
    # on the viewer. It remains nonzero after an owner rewind to an empty chat.
    assert session_events.get_max_seq("remote-chat") > 0
    read = session_events.get_events
    calls = []

    def unavailable(sid, from_seq=0, limit=500):
        calls.append(from_seq)
        if not fail_after_page or from_seq:
            raise RuntimeError("event mirror unavailable")
        return read(sid, from_seq, limit)

    monkeypatch.setattr(session_events, "get_events", unavailable)
    response = mirrored_chat.get("/api/v1/sessions/remote-chat" + suffix)
    assert response.status_code == 200, response.text
    assert response.json()["messages"] == []
    assert len(calls) == (2 if fail_after_page else 1)


@pytest.mark.parametrize("suffix", ["", "/live"])
def test_populated_store_precedes_mirror_and_legacy(mirrored_chat, monkeypatch, suffix):
    from app.core import pg_session_store as pss
    from tests import pg_shim
    from tests.pg_shim import ShimCursor

    # Adapt SQLite's JSONB and summary timestamp expression at the driver
    # boundary; run the real writer (including its fold), parser and transforms.
    translate_sql = pg_shim.translate_sql

    def store_sql(sql):
        return translate_sql(sql.replace(
            "(EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT",
            "CAST(strftime('%s', 'now') AS INTEGER) * 1000",
        ))

    monkeypatch.setattr(pg_shim, "translate_sql", store_sql)
    as_dict = ShimCursor._as_dict

    def store_jsonb(row):
        result = as_dict(row)
        if result and ("entry" in result or "folded_through_id" in result):
            for field in ("entry", "data"):
                if isinstance(result.get(field), str):
                    result[field] = json.loads(result[field])
        return result

    fetchall = ShimCursor.fetchall

    def store_fetchall(cursor):
        return [store_jsonb(row) for row in fetchall(cursor)]

    monkeypatch.setattr(ShimCursor, "_as_dict", staticmethod(store_jsonb))
    monkeypatch.setattr(ShimCursor, "fetchall", store_fetchall)
    sdk_id = "populated-store-sdk-" + ("live" if suffix else "detail")
    database.update_session("remote-chat", sdk_session_id=sdk_id)
    monkeypatch.setattr("app.core.message_resolver.resolve_working_dir", lambda session: "/missing")
    monkeypatch.setattr("app.core.provider.fluid_transcript.is_fluid_session", lambda sid: False)
    asyncio.run(pss.PgSessionStore().append(
        {"project_key": "-missing", "session_id": sdk_id},
        [{"type": "assistant", "uuid": "store-reply", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "Store reply"}],
        }}],
    ))
    def unexpected(*args, **kwargs):
        pytest.fail("populated store consulted the mirror/legacy rescue")

    monkeypatch.setattr(session_events, "get_events", unexpected)
    monkeypatch.setattr(session_events, "get_max_seq", unexpected)
    monkeypatch.setattr("app.api.sessions._legacy_db_messages", unexpected)
    monkeypatch.setattr("app.core.db_message_loader.load_messages_from_db", unexpected)
    response = mirrored_chat.get("/api/v1/sessions/remote-chat" + suffix)
    assert response.status_code == 200, response.text
    assert [m["content"] for m in response.json()["messages"]] == ["Store reply"]


@pytest.mark.parametrize("suffix", ["", "/live"])
@pytest.mark.parametrize("mirrored_chat", ["thinking"], indirect=True)
def test_recorder_thinking_subtypes_survive_rest(mirrored_chat, suffix):
    response = mirrored_chat.get("/api/v1/sessions/remote-chat" + suffix)
    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    assert [(m["type"], m["content"]) for m in messages] == [
        ("text", "Say OK"), ("thinking", "**One**\n\n**Two**"),
        ("thinking", "Summary"), ("text", "OK"),
    ]
    assert [m["thinkingSubtype"] for m in messages[1:3]] == ["reasoning", "summary"]


@pytest.mark.parametrize("suffix", ["", "/live"])
def test_closed_thinking_and_steering_keep_metadata(mirrored_chat, suffix):
    # Contract-edge rows supplement the real recorder/mirror path above.
    session_events.append_events("remote-chat", [
        {"type": "turn.started", "turn_id": "t2", "payload": {"profile_id": "default"}},
        {"type": "message.user", "turn_id": "t2", "payload": {"content": "Steer", "profile_id": "sender"}},
        {"type": "message.assistant", "turn_id": "t2", "payload": {
            "kind": "thinking", "subtype": "summary", "content": "Closed thought",
            "model": "recorded-model", "timestamp": "2026-10-07T01:02:03Z",
        }},
        {"type": "assistant.text_delta", "turn_id": "t2", "payload": {"text": "Next"}},
    ])
    response = mirrored_chat.get("/api/v1/sessions/remote-chat" + suffix)
    assert response.status_code == 200, response.text
    steer, thought, reply = response.json()["messages"][-3:]
    assert steer["profileId"] == "sender"
    assert thought["type"] == "thinking"
    assert thought["thinkingSubtype"] == "summary"
    assert thought["metadata"]["model"] == "recorded-model"
    assert thought["metadata"]["timestamp"] == "2026-10-07T01:02:03Z"
    assert thought["profileId"] == reply["profileId"] == "default"


@pytest.mark.parametrize("suffix", ["", "/live"])
def test_late_tool_result_and_rewind_keep_page_order(mirrored_chat, suffix):
    digest = "a" * 64
    session_events.append_events("remote-chat", [
        {"id": "args", "type": "tool.input_delta", "payload": {"tool_use_id": "edge-tool", "partial_json": '{"path":'}},
        {"type": "tool.use", "payload": {"tool_use_id": "edge-tool", "name": "Read", "input": {"path": "/x"}}},
        {"id": "later", "type": "message.assistant", "payload": {"content": "Later"}},
        {"type": "tool.result", "payload": {"tool_use_id": "edge-tool", "content_preview": "Read result", "artifact_hashes": ["invalid", digest]}},
    ])
    url = "/api/v1/sessions/remote-chat" + suffix
    response = mirrored_chat.get(url + "?message_limit=1")
    assert response.status_code == 200, response.text
    assert response.json()["messages"][0]["id"] == "later"
    cursor = response.headers["X-Message-Next-Before-Seq"] if not suffix else response.json()["message_pagination"]["next_before_seq"]
    older = mirrored_chat.get(url + f"?message_limit=1&before_seq={cursor}")
    assert older.status_code == 200, older.text
    tool = older.json()["messages"][0]
    assert tool["id"] == "tooluse-edge-tool"
    assert tool["toolInput"] == {"path": "/x"}
    assert tool["toolResult"] == "Read result"
    assert tool["toolStatus"] == "complete"
    assert tool["toolScreenshot"] == f"/api/v1/artifacts/{digest}?session_id=remote-chat"
    origin = next(e["seq"] for e in session_events.get_events("remote-chat") if e["id"] == "args")
    session_events.append_events("remote-chat", [
        {"type": "transcript.rewound", "payload": {"from_seq": origin}},
        {"id": "replacement", "type": "assistant.text_delta", "payload": {"text": "Replacement"}},
        # A result for a retracted card must not recreate it.
        {"type": "tool.result", "payload": {"tool_use_id": "edge-tool", "content_preview": "stale"}},
    ])
    after = mirrored_chat.get(url)
    assert after.status_code == 200, after.text
    assert [m["content"] for m in after.json()["messages"]] == ["Say OK", "OK", "Replacement"]
    assert after.json()["messages"][-1]["id"] == "text-replacement"


@pytest.mark.parametrize("suffix", ["", "/live"])
def test_late_argument_update_does_not_split_open_text(mirrored_chat, suffix):
    session_events.append_events("remote-chat", [
        {"type": "tool.input_delta", "payload": {"tool_use_id": "partial", "partial_json": "{"}},
        {"id": "answer", "type": "assistant.text_delta", "payload": {"text": "A"}},
        {"type": "tool.input_delta", "payload": {"tool_use_id": "partial", "partial_json": "}"}},
        {"type": "tool.use", "payload": {}},  # malformed: no card, no boundary
        {"type": "assistant.text_delta", "payload": {"text": "B", "subtype": "ignored"}},
    ])
    response = mirrored_chat.get("/api/v1/sessions/remote-chat" + suffix)
    assert response.status_code == 200, response.text
    assert [m["content"] for m in response.json()["messages"]] == ["Say OK", "OK", "", "AB"]
    assert response.json()["messages"][-1]["id"] == "text-answer"
