"""Real PostgreSQL message-window budgets and differential REST projection tests."""
from __future__ import annotations

import re
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.sessions import router
from app.db import base, database
from scripts.devloop.seed import ensure_dev_agent
from scripts.devloop.seed_long import SESSIONS, TURNS, build_transcript
from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401
from tests.performance.metrics import RequestMeasurements, measure_sql

pytestmark = requires_real_pg


@pytest.fixture(scope="module")
def history(pg_schema):
    ensure_dev_agent()
    for name, turns in (("small", 3), ("large", TURNS)):
        database.create_session(session_id=SESSIONS[name], profile_id="dev-agent",
                                title=f"Window {name}")
        build_transcript(name, turns).save()
    yield
    for name in ("small", "large"):
        database.delete_session(SESSIONS[name])


class SourceRows:
    """Count rows crossing the event SQL/Python boundary, not serialized bubbles."""
    def __init__(self, cursor, counts):
        self.cursor, self.counts, self.events = cursor, counts, False

    def execute(self, sql, *args, **kwargs):
        self.events = bool(re.search(r"\bsession_events\b", str(sql)))
        self.cursor.execute(sql, *args, **kwargs)
        return self

    def fetchall(self):
        rows = self.cursor.fetchall()
        if self.events:
            self.counts.append(len(rows))
        return rows

    def __getattr__(self, name):
        return getattr(self.cursor, name)


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-token"
    return RequestMeasurements(app)


def test_one_message_has_bounded_queries_and_source_rows(history, api):
    counts = {}
    with measure_sql(), TestClient(api) as client:
        for name in ("small", "large"):
            path = f"/api/v1/sessions/{SESSIONS[name]}?message_limit=1"
            assert client.get(path).status_code == 200
            fetched = []
            original = base._PgDictConnection.cursor
            with patch.object(base._PgDictConnection, "cursor",
                              lambda conn, *a, **kw: SourceRows(original(conn, *a, **kw), fetched)):
                started = time.perf_counter()
                response = client.get(path)
                elapsed = (time.perf_counter() - started) * 1000
            assert response.status_code == 200
            assert len(response.json()["messages"]) == 1
            cost = api.requests[-1]
            counts[name] = (cost.sql_count,
                            sum(bool(re.search(r"\bsession_events\b", sql)) for sql in cost.statements),
                            sum(fetched))
            print(f"{name}: sql,event_reads,source_rows={counts[name]} elapsed_ms={elapsed:.3f}")
    assert counts["large"][0] <= 12, counts
    assert counts["large"][1] <= 2, counts
    assert counts["large"][2] <= 12, counts
    assert all(large <= small for small, large in zip(counts["small"], counts["large"])), counts

@pytest.fixture
def chat(pg_schema):
    from uuid import uuid4
    sid = f"message-window-{uuid4()}"
    ensure_dev_agent()
    database.create_session(session_id=sid, profile_id="dev-agent", title="Window parity")
    yield sid
    database.delete_session(sid)


def append(sid, rows):
    """Persist through the real source writer, not a mock returning prepared messages."""
    from app.db.session_events import append_events
    return append_events(sid, [{"type": kind, "payload": payload, "turn_id": turn}
                               for kind, payload, turn in rows])


def assert_route_parity(client, sid, limit, before=None):
    from app.api import sessions
    params = {"message_limit": limit}
    if before is not None:
        params["before_seq"] = before
    actual = client.get(f"/api/v1/sessions/{sid}", params=params)
    with patch.object(sessions, "load_event_message_window", return_value=None):
        full = client.get(f"/api/v1/sessions/{sid}", params=params)
    assert actual.status_code == full.status_code == 200
    assert actual.json()["messages"] == full.json()["messages"]
    assert {k: v for k, v in actual.headers.items() if k.startswith("x-message-")} == {
        k: v for k, v in full.headers.items() if k.startswith("x-message-")}
    return actual


def test_differential_streams_tools_answers_compaction_cost_and_user_occurrences(chat, api):
    from app.api.sessions_helpers import load_event_message_window
    from app.db.analytics import log_usage

    rows = []
    for index in range(4):
        turn = f"t{index}"
        rows += [("turn.started", {"profile_id": f"profile-{index}"}, turn),
                 ("message.user", {"content": "same"}, turn)]
        # Cross the full reader's 500-row page boundary in the first message.
        rows += [("assistant.text_delta", {"text": f"{index}:"}, turn)] * (510 if index == 0 else 2)
        rows += [("tool.input_delta", {"tool_use_id": f"tool-{index}", "partial_json": '{"a":'}, turn),
                 ("assistant.thinking_delta", {"text": "**First**", "subtype": "reasoning"}, turn),
                 ("assistant.thinking_delta", {"text": "**Next**", "subtype": "reasoning"}, turn),
                 ("tool.use", {"tool_use_id": f"tool-{index}", "name": "Read", "input": {"a": 1}}, turn),
                 ("assistant.text_delta", {"text": "final"}, turn),
                 # Updating an existing tool does not split the open text.
                 ("tool.input_delta", {"tool_use_id": f"tool-{index}", "partial_json": "1}"}, turn),
                 ("tool.result", {"tool_use_id": f"tool-{index}", "content_preview": "done"}, turn),
                 ("assistant.text_delta", {"text": " answer"}, turn),
                 ("interaction.requested", {"kind": "question", "request_id": f"q{index}"}, turn),
                 ("interaction.answered", {"request_id": f"q{index}", "answer": {"text": "yes"}}, turn),
                 ("system.note", {"subtype": "model_info"}, turn),
                 ("assistant.text_delta", {"text": "."}, turn),
                 ("system.compaction_boundary", {"subtype": "compact_boundary", "data": {
                     "compact_metadata": {"pre_tokens": 1000}}}, turn),
                 ("turn.completed", {}, turn)]
        database.add_session_message(chat, "user", "same", metadata={"occurrence": index})
        log_usage(chat, "dev-agent", "test-model", 20, index + 1, .125 * (index + 1),
                  100 + index, usage_estimated=index == 3)
    # A much later result must update the original card, not become a new bubble.
    rows.append(("tool.result", {"tool_use_id": "tool-0", "content_preview": "late result"}, "t3"))
    append(chat, rows)
    assert load_event_message_window(chat, limit=1) is not None
    with TestClient(api) as client:
        for limit, before in ((1, None), (2, None), (4, None), (7, None), (100, None),
                              (3, 8), (2, 2), (1, 0), (7, 9999)):
            assert_route_parity(client, chat, limit, before)
        last = client.get(f"/api/v1/sessions/{chat}?message_limit=4").json()["messages"]
        final = next(m for m in last if (m.get("metadata") or {}).get("is_final_message"))
        assert final["metadata"]["total_cost_usd"] == 1.25
        assert final["metadata"]["usage_estimated"] is True
        # Exercise the real lossless retention rewrite too: holes, first IDs
        # retained at the last delta seq, and compacted thinking all still agree.
        from app.db.session_events import compact_session_deltas
        runs, removed = compact_session_deltas(chat, keep_tail_events=0)
        assert runs > 0 and removed > 0
        for limit, before in ((1, None), (7, None), (3, 8)):
            assert_route_parity(client, chat, limit, before)


@pytest.mark.parametrize("reason", ["rewind", "discarded", "approval", "reopened-tool", "reminder", "oversize"])
def test_nonlocal_or_oversize_event_shapes_fall_back_without_changing_pages(chat, api, monkeypatch, reason):
    from app.api.sessions_helpers import load_event_message_window
    from app.db import session_message_windows

    rows = [("turn.started", {}, "a"), ("message.user", {"content": "first"}, "a"),
            ("tool.use", {"tool_use_id": "shared", "name": "Read", "input": {}}, "a"),
            ("message.assistant", {"content": "before"}, "a"), ("turn.completed", {}, "a"),
            ("turn.started", {}, "b"), ("message.user", {"content": "second"}, "b"),
            ("message.assistant", {"content": "after"}, "b")]
    if reason == "rewind":
        rows += [("transcript.rewound", {"from_seq": 7}, "b"),
                 ("message.assistant", {"content": "replacement"}, "b")]
    elif reason == "discarded":
        # A safety check declined the call that streamed "after".
        rows += [("system.note", {"subtype": "attempt_discarded", "data": {"reason": "guardrail"}}, "b"),
                 ("message.assistant", {"content": "replacement"}, "b")]
    elif reason == "approval":
        rows += [("interaction.requested", {"kind": "permission", "request_id": "r1",
                    "request": {"assistant_confirm": {"question": "May I?"}}}, "b"),
                 ("interaction.answered", {"request_id": "r1", "answer": {"decision": "deny"}}, "b"),
                 ("interaction.requested", {"kind": "permission", "request_id": "r2",
                    "request": {"assistant_confirm": {"question": "May I?"}}}, "b")]
    elif reason == "reopened-tool":
        rows += [("tool.use", {"tool_use_id": "shared", "name": "Read", "input": {"late": True}}, "b")]
    elif reason == "reminder":
        rows += [("message.user", {"content": "hi\n<system•reminder>\n# Memory\nsecret fixture\n</system•reminder>"}, "b")]
    else:
        monkeypatch.setattr(session_message_windows, "MAX_WINDOW_SOURCE_ROWS", 2)
    append(chat, rows)
    assert load_event_message_window(chat, limit=1) is None
    with TestClient(api) as client:
        for limit, before in ((1, None), (3, None), (3, 4)):
            assert_route_parity(client, chat, limit, before)


def test_legacy_fallback_and_event_only_nonbubble_history_do_not_resurrect_rows(chat, api):
    database.add_session_message(chat, "user", "legacy prompt")
    database.add_session_message(chat, "assistant", "legacy answer")
    with TestClient(api) as client:
        assert_route_parity(client, chat, 1)
        append(chat, [("turn.started", {}, "t"), ("turn.completed", {}, "t")])
        response = assert_route_parity(client, chat, 1)
        assert response.json()["messages"] == []
        assert response.headers["x-message-total"] == "0"


@pytest.mark.parametrize("source", ["store", "fluid"])
def test_rich_local_sources_keep_their_full_reader_and_exact_windows(chat, api, source):
    import asyncio
    from uuid import uuid4
    from app.api import sessions

    sdk = "ses_" + uuid4().hex[:12] if source == "fluid" else str(uuid4())
    database.update_session(chat, sdk_session_id=sdk)
    if source == "store":
        from app.core.pg_session_store import PgSessionStore
        entries = [{"type": role, "uuid": f"{sdk}-{i}", "message": {"role": role, "content": content}}
                   for i, (role, content) in enumerate([
                       ("user", "one"), ("assistant", "first answer"),
                       ("user", "two"), ("assistant", "last answer")])]
        asyncio.run(PgSessionStore().append({"project_key": "window-project", "session_id": sdk}, entries))
    else:
        from app.core.provider.fluid_pg_store import PgFluidStore
        from fluid.messages import Message, Text
        from fluid.store.base import SessionNode

        async def write():
            store = PgFluidStore()
            await store.create_session(sdk)
            parent = None
            for i, (role, content) in enumerate([
                ("user", "one"), ("assistant", "first answer"),
                ("user", "two"), ("assistant", "last answer")]):
                node = SessionNode(f"node-{i}", parent, Message(role, [Text(content)]))
                await store.append(sdk, node)
                parent = node.node_id
            await store.set_head(sdk, parent)
        asyncio.run(write())
    # If a rich local reader succeeds, the new event-window reader must NOT run.
    with patch.object(sessions, "load_event_message_window", side_effect=AssertionError("local source changed")):
        with TestClient(api) as client:
            for limit, before in ((1, None), (2, None), (2, 2)):
                response = assert_route_parity(client, chat, limit, before)
                assert response.headers["x-message-total"] == "4"



def test_large_default_window_and_nearby_cursor_are_still_bounded(history, api):
    from app.api.sessions_helpers import load_event_message_window
    sid = SESSIONS["large"]
    page = load_event_message_window(sid)
    assert page is not None and len(page[0]) == 2000
    with TestClient(api) as client:
        for before in (None, 8879):
            assert_route_parity(client, sid, 2000, before)


def test_sdk_alias_keeps_full_path_metadata_compatibility(chat, api):
    from uuid import uuid4
    from app.db.analytics import log_usage
    sdk = str(uuid4())
    database.update_session(chat, sdk_session_id=sdk)
    append(chat, [("turn.started", {}, "t"), ("message.user", {"content": "one"}, "t"),
                  ("message.assistant", {"content": "answer"}, "t")])
    log_usage(chat, "dev-agent", "test", 1, 2, .25, 5)
    with TestClient(api) as client:
        assert_route_parity(client, sdk, 1)


def test_differential_adversarial_boundary_grammar(chat, api):
    """Deterministic interleavings, not a timing or random-coverage assertion."""
    import random
    randomizer = random.Random(845)
    rows = [("message.assistant", {"content": "before any user"}, None)]
    for i in range(12):
        turn = f"t{i}"
        rows += [("turn.started", {"profile_id": f"p{i}"}, turn),
                 ("message.user", {"content": "" if i % 3 else None}, turn)]
        for j in range(8):
            kind, payload = randomizer.choice([
                ("assistant.text_delta", {"text": " a"}),
                ("assistant.text_delta", {"text": "\u2003"}),
                ("assistant.thinking_delta", {"text": "**x**", "subtype": "reasoning"}),
                ("assistant.thinking_delta", {"text": " y"}),
                ("tool.input_delta", {"tool_use_id": f"tool-{i}", "partial_json": "{}"}),
                ("tool.use", {"tool_use_id": f"tool-{i}", "name": "Read", "input": {}}),
                ("tool.result", {"tool_use_id": f"tool-{i}", "content_preview": "output"}),
                ("system.note", {"subtype": "model_info"}),
                ("system.note", {"subtype": "notice", "data": {"content": "note"}}),
                ("message.assistant", {"content": "", "kind": "thinking"}),
                ("message.user", {"content": "steer"}),
            ])
            rows.append((kind, payload, turn))
        rows.append(("turn.parked", {}, turn))
    append(chat, rows)
    for index in range(15):
        database.add_session_message(chat, "user", "", metadata={"empty_occurrence": index})
    from app.db.analytics import log_usage
    for i in range(24):
        log_usage(chat, "dev-agent", "test", 1, 2, .0625, i)
    from app.api.sessions_helpers import load_event_message_window
    with TestClient(api) as client:
        for limit, before in ((1, None), (7, None), (15, None), (5, 16), (5, 43), (1000, None)):
            assert load_event_message_window(chat, limit=limit, before_seq=before) is not None
            assert_route_parity(client, chat, limit, before)


# Each event is outside the selected suffix: checking only its identities must
# not hide a full-fold failure (or a different _parse_payload interpretation).
_MALFORMED_PREFIXES = [
    pytest.param(kind, {"subtype": "notice" if kind == "system.note" else "compact_boundary", "data": value},
                 id=f"{kind}-data-{label}")
    for kind in ("system.note", "system.compaction_boundary")
    for label, value in (("string", "scalar-not-an-object"), ("array", ["item"]),
                         ("number", 7), ("boolean", True), ("empty-array", []))
] + [
    pytest.param(kind, {"tool_use_id": "prefix-tool", field: value}, id=f"{kind}-{field}-{label}")
    for kind, field, values in (
        ("tool.use", "screenshot", (("number", 7), ("array", ["image"]), ("object", {"image": "x"}))),
        ("tool.input_delta", "partial_json", (("number", 7), ("array", ["json"]), ("object", {"a": 1}))),
        ("tool.result", "artifact_hashes", (("number", 7), ("boolean", True),
                                           ("string", "not-an-array"), ("object", {"hash": "x"}))),
    )
    for label, value in values
] + [
    pytest.param("tool.result", {"tool_use_id": value}, id=f"tool.result-id-{label}")
    for label, value in (("array", ["prefix-tool"]), ("object", {"id": "prefix-tool"}), ("number", 7))
] + [
    pytest.param("system.note", payload, id=f"payload-{label}")
    for label, payload in (("encoded-object", '{"subtype":"model_info"}'),
                           ("string", "not-an-object"), ("array", ["item"]),
                           ("number", 7), ("boolean", True), ("null", None))
] + [
    # The existing scalar guards get prefix differentials too, not just suffix
    # tests where a fold error would already force the compatibility path.
    pytest.param(kind, payload, id=f"existing-{kind}-{field}")
    for kind, field, payload in (
        ("message.user", "content", {"content": {"text": "not text"}}),
        ("message.assistant", "content", {"content": ["not text"]}),
        ("assistant.text_delta", "text", {"text": 7}),
        ("assistant.thinking_delta", "text", {"text": {"thought": "x"}}),
        ("assistant.thinking_delta", "subtype", {"text": "x", "subtype": ["reasoning"]}),
        ("tool.use", "tool_use_id", {"tool_use_id": ["prefix-tool"]}),
        ("tool.input_delta", "tool_use_id", {"tool_use_id": {"id": "prefix-tool"}}),
    )
]


@pytest.mark.parametrize("kind,payload", _MALFORMED_PREFIXES)
def test_malformed_prefix_uses_full_path_and_preserves_http_result(chat, api, kind, payload):
    from app.api.sessions_helpers import load_event_message_window
    rows = [("turn.started", {}, "a"), ("message.user", {"content": "old"}, "a")]
    if kind.startswith("tool."):
        rows.append(("tool.use", {"tool_use_id": "prefix-tool", "name": "Read", "input": {}}, "a"))
    rows += [(kind, payload, "a"), ("turn.completed", {}, "a"),
             ("turn.started", {}, "b"), ("message.user", {"content": "new"}, "b"),
             ("message.assistant", {"content": "new answer"}, "b")]
    append(chat, rows)
    with TestClient(api) as client:
        assert_route_parity(client, chat, 1)
        assert load_event_message_window(chat, limit=1) is None


@pytest.mark.parametrize("kind,payload", [
    ("system.note", {"subtype": "model_info", "data": "ignored"}),
    ("system.note", {"subtype": "background_tasks_changed", "data": ["ignored"]}),
    ("interaction.requested", {"kind": "permission", "request": "not an object"}),
    ("interaction.requested", {"kind": "permission", "request": {"assistant_confirm": "not an object"}}),
    ("interaction.answered", {"request_id": ["invalid"], "answer": {"decision": {"invalid": True}}}),
    ("interaction.expired", {"request_id": {"invalid": True}}),
    ("tool.result", {"tool_use_id": "prefix-tool", "artifact_hashes": [7, {"ignored": True}, "bad"]}),
    ("tool.use", {"tool_use_id": "prefix-tool", "input": ["opaque", "input"]}),
])
def test_checked_or_ignored_prefix_shapes_still_match_full_path(chat, api, kind, payload):
    from app.api.sessions_helpers import load_event_message_window
    append(chat, [("turn.started", {}, "a"), ("message.user", {"content": "old"}, "a"),
                  ("tool.use", {"tool_use_id": "prefix-tool", "name": "Read", "input": {}}, "a"),
                  (kind, payload, "a"), ("turn.completed", {}, "a"),
                  ("turn.started", {}, "b"), ("message.user", {"content": "new"}, "b"),
                  ("message.assistant", {"content": "new answer"}, "b")])
    with TestClient(api) as client:
        assert_route_parity(client, chat, 1)
        assert load_event_message_window(chat, limit=1) is not None

