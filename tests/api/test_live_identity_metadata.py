"""Breadcrumb identity is opt-in metadata, never a transcript projection."""

from contextlib import ExitStack
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.sessions import router
from app.db import database, session_events
from tests.pg_shim import ShimCursor


@pytest.fixture
def metadata_client():
    app = FastAPI()
    app.include_router(router)

    async def auth(request: Request):
        # Exercise the real route's ownership/scope checks, without ingress auth.
        if caller := request.headers.get("test-api-user"):
            request.state.api_user = {"id": caller}
        if request.headers.get("test-paired-client"):
            request.state.paired_client = {"id": "other-controller"}
        return "test-token"

    app.dependency_overrides[require_auth] = auth
    with TestClient(app) as client:
        yield client


def _live(client, sid, *, owner_fields=True, **kwargs):
    return client.get(f"/api/v1/sessions/{sid}/live", params={
        "include_messages": "false", "owner_fields": str(owner_fields).lower(),
    }, **kwargs)


@pytest.mark.parametrize("title", [None, "", "A named chat"])
@pytest.mark.parametrize("relation", ["root", "fork", "hidden-child"])
def test_breadcrumb_identity_contract(metadata_client, title, relation):
    parent = None
    if relation != "root":
        parent = "parent"
        database.create_session(parent, "default", title="Parent")
    database.create_session(
        "identity", "default", title=title, parent_session_id=parent,
        hidden=relation == "hidden-child", is_worker=relation == "hidden-child",
        fork_point_message_index=2 if relation == "fork" else None,
    )

    response = _live(metadata_client, "identity")
    assert response.status_code == 200, response.text
    body = response.json()
    meta = body["session_meta"]
    assert {key: meta[key] for key in ("id", "title", "hidden", "parent_session_id")} == {
        "id": "identity", "title": title,
        "hidden": relation == "hidden-child", "parent_session_id": parent,
    }
    assert isinstance(meta["hidden"], bool)
    # Additive: existing lifecycle/transcript envelope and owner fields remain.
    assert body["session_id"] == "identity"
    assert body["messages"] == []
    assert body["message_pagination"]["total"] == 0
    assert isinstance(meta["is_remote"], bool)
    assert "device_name" in meta


def test_identity_opt_in_preserves_existing_title(metadata_client):
    database.create_session("plain", "default", title=None)
    meta = _live(metadata_client, "plain", owner_fields=False).json()["session_meta"]
    assert meta["title"] is None
    for key in ("id", "hidden", "parent_session_id", "is_remote", "device_name"):
        assert key not in meta


@pytest.mark.parametrize("include_messages", ["false", "true"])
def test_other_api_user_cannot_read_identity(metadata_client, include_messages):
    database.create_session("private", "default", title="Private", hidden=True)
    response = metadata_client.get(
        "/api/v1/sessions/private/live",
        params={"include_messages": include_messages, "owner_fields": "true"},
        headers={"test-api-user": "not-the-owner"},
    )
    assert response.status_code == 403
    assert "session_meta" not in response.json()
    assert "Private" not in response.text


@pytest.mark.parametrize("worker", [False, True])
def test_api_user_can_read_own_identity_but_not_anothers(metadata_client, worker):
    database.create_api_user("key-owner", "Test caller", "not-a-real-key-hash")
    database.create_session("owned", "default", api_user_id="key-owner")
    sid = "owned"
    if worker:
        sid = "owned-child"
        database.create_session(sid, "default", parent_session_id="owned", is_worker=True, hidden=True)
    response = _live(metadata_client, sid, headers={"test-api-user": "key-owner"})
    assert response.status_code == 200, response.text
    assert response.json()["session_meta"]["id"] == sid
    denied = _live(metadata_client, sid, headers={"test-api-user": "different-user"})
    assert denied.status_code == 403
    assert "session_meta" not in denied.json()


def test_paired_controller_cannot_bypass_transcript_gate(metadata_client, monkeypatch):
    database.create_session("private", "default", title="Private", hidden=True)
    denied = Mock(return_value=False)
    monkeypatch.setattr("app.core.artifact_access.paired_client_may_read", denied)
    response = _live(metadata_client, "private", headers={"test-paired-client": "1"})
    assert response.status_code == 403
    assert "session_meta" not in response.json()
    denied.assert_called_once()


@pytest.mark.parametrize("task_status,parked_kind", [
    (None, None), ("running", None), ("completed", None),
    (None, "monitor"), ("running", "monitor"),
])
def test_metadata_task_presence_matches_detail(metadata_client, task_status, parked_kind):
    from app.db.background_tasks import upsert_background_task

    database.create_session("tasks", "default")
    database.set_session_parked_task_kind("tasks", parked_kind)
    if task_status:
        # Real task persistence, also used for a task mirrored from a peer.
        upsert_background_task({
            "task_id": "work", "session_id": "tasks", "kind": "shell",
            "status": task_status, "title": "Background work",
        })
    body = _live(metadata_client, "tasks", owner_fields=False).json()
    detail = metadata_client.get("/api/v1/sessions/tasks?message_limit=1").json()
    assert body["has_running_tasks"] is (task_status == "running")
    assert body["has_running_tasks"] == detail["has_running_tasks"]
    assert body["parked_task_kind"] == detail["parked_task_kind"] == parked_kind


def _seed_events(sid, count):
    database.create_session(sid, "default", title=sid, hidden=True)
    database.update_session(sid, sdk_session_id=f"sdk-{sid}")
    # Real backfill writer; background progress increases history without
    # increasing the two visible bubbles, as it does in long-running chats.
    events = [{"type": "message.user", "payload": {"content": "Question"}}]
    events.extend({"type": "subagent.progress", "payload": {"task_id": "child", "text": "progress"}}
                  for _ in range(count - 2))
    events.append({"type": "message.assistant", "payload": {"content": "Answer"}})
    assert session_events.append_backfill_events(sid, events) == (1, count)


def _count_queries(monkeypatch):
    # Same ShimCursor boundary as test_owner_coordination_review: count actual
    # domain SQL, including calls moved to a thread, not mocked DB functions.
    queries = []
    execute = ShimCursor.execute

    def measured(cursor, sql, params=None):
        queries.append(sql)
        return execute(cursor, sql, params)

    monkeypatch.setattr(ShimCursor, "execute", measured)
    return queries


def test_live_identity_sql_is_history_independent(metadata_client, monkeypatch, mock_db):
    # Count real busy reads every time, not whichever side of its 1.5 s cache
    # expiry the large fixture happens to land on. This is the uncached budget.
    monkeypatch.setattr("app.core.streaming_status.CACHE_TTL_SECONDS", 0)
    for sid, count in (("short-history", 100), ("long-history", 100_000)):
        _seed_events(sid, count)
        # Warm lazy imports/registries before either measured request.
        assert _live(metadata_client, sid).status_code == 200

    # Equal revision stamps leave only identifier/title lengths as a possible
    # wire-size difference, regardless of when the two fixtures were written.
    mock_db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id IN (?, ?)",
        ("2026-01-01T00:00:00.000000+00:00", "short-history", "long-history"),
    )
    mock_db.commit()
    queries = _count_queries(monkeypatch)
    loaders = (
        "app.api.sessions.load_event_messages",
        "app.api.sessions.parse_session_history_via_store",
        "app.api.sessions.finalize_transcript",
        "app.core.provider.fluid_transcript.is_fluid_session",
        "app.core.provider.fluid_transcript.fluid_live_messages",
        "app.core.db_message_loader.load_messages_from_db",
    )
    with ExitStack() as stack:
        guards = [stack.enter_context(patch(target, side_effect=AssertionError(target)))
                  for target in loaders]
        counts = []
        statements = []
        wire_sizes = []
        non_identity_sizes = []
        for sid in ("short-history", "long-history"):
            queries.clear()
            response = _live(metadata_client, sid)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["session_meta"]["id"] == body["session_meta"]["title"] == body["session_id"] == sid
            assert body["messages"] == []
            wire_sizes.append(len(response.content))
            assert wire_sizes[-1] <= 2 * 1024, wire_sizes
            # The fixture's ID occurs in session_id, session_meta.id and title.
            # All other bytes must stay fixed when history grows 1,000-fold.
            non_identity_sizes.append(wire_sizes[-1] - 3 * len(sid.encode("utf-8")))
            assert not any("session_events" in sql.lower() for sql in queries), queries
            counts.append(len(queries))
            statements.append(list(queries))
        for guard in guards:
            guard.assert_not_called()
    assert counts[0] == counts[1] > 0, (counts, statements)
    assert counts[0] <= 12, (counts, statements)
    assert non_identity_sizes[0] == non_identity_sizes[1], wire_sizes

    # Negative control: a one-message detail window is NOT metadata-only. Its
    # real projection still pages all history, so this same zero-read budget
    # would reject the old breadcrumb request, even with only two bubbles.
    detail_counts = []
    for sid in ("short-history", "long-history"):
        queries.clear()
        response = metadata_client.get(f"/api/v1/sessions/{sid}?message_limit=1")
        assert response.status_code == 200, response.text
        assert len(response.json()["messages"]) == 1
        event_reads = [sql for sql in queries if "from session_events" in " ".join(sql.lower().split())]
        assert event_reads, queries
        detail_counts.append(len(event_reads))
    assert detail_counts[1] > detail_counts[0], detail_counts
    print(f"metadata SQL={counts}, bytes={wire_sizes}; detail session_events reads={detail_counts}")
