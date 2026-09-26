"""API users act only on the sessions they own (review pass 5, R5-2/R5-3/R5-4).

Ownership is ``sessions.api_user_id == caller id``, the rule the session list
already applies; worker sessions an agent spawns belong to whoever owns the
session that spawned them. It is enforced by the shared
``check_session_access_for_api_user`` (REST ``require_session`` routes, the
conversation endpoints, the sync socket) and, on the chat WebSocket, by
``ChatWebSocketHandler._may_act_on_session`` in front of every frame that names
a session. The owner (no API user) is unrestricted.

An API user's ``remember: "profile"`` permission answer is kept to its own
session: an agent-wide rule would auto-approve the tool in the owner's
sessions and other keys' sessions on that agent.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import app.api.chat_ws_handler as cwh
from app.api import sessions_helpers
from app.api.sessions_helpers import api_user_owns_session, check_session_access_for_api_user
from app.db import database
from app.db import turns as turns_db
from tests.api.test_chat_ws_v2_turns import _Patches, _make_session
from tests.core.interaction_harness import (  # noqa: F401  (autouse fixtures)
    no_connector_delivery,
    no_gateway_listener,
    no_push,
    reset_backfill_memo_on_setup,
    reset_interaction_wait_state,
)
from tests.helpers import sent_frames

KEY_A = {"id": "key-a", "project_id": None, "profile_ids": []}
DENIED = "Access denied to this session"


# -- the shared rule -----------------------------------------------------------


class TestSharedRule:
    def test_owner_is_unrestricted(self):
        for session in ({"id": "s", "api_user_id": "key-a"}, {"id": "s", "api_user_id": None}):
            check_session_access_for_api_user(None, session)

    def test_own_session_is_allowed(self):
        check_session_access_for_api_user(KEY_A, {"id": "s", "api_user_id": "key-a"})

    @pytest.mark.parametrize("owner", [None, "key-b"])
    def test_foreign_session_is_403_even_for_an_unrestricted_key(self, owner):
        with pytest.raises(HTTPException) as exc:
            check_session_access_for_api_user(KEY_A, {"id": "s", "api_user_id": owner})
        assert exc.value.status_code == 403

    def test_project_key_reaches_a_projectless_session_only_when_it_owns_it(self):
        key = {"id": "key-a", "project_id": "p1", "profile_ids": []}
        check_session_access_for_api_user(key, {"id": "s", "api_user_id": "key-a", "project_id": None})
        with pytest.raises(HTTPException):
            check_session_access_for_api_user(key, {"id": "s", "api_user_id": None, "project_id": None})

    def test_worker_session_belongs_to_the_owner_of_its_parent(self):
        rows = {
            "root": {"id": "root", "api_user_id": "key-a"},
            "mid": {"id": "mid", "api_user_id": None, "is_worker": True, "parent_session_id": "root"},
        }
        worker = {"id": "w", "api_user_id": None, "is_worker": True, "parent_session_id": "mid"}
        with patch.object(sessions_helpers.database, "get_session", side_effect=rows.get):
            assert api_user_owns_session(KEY_A, worker)
            assert not api_user_owns_session({"id": "key-b"}, worker)

    def test_a_fork_does_not_inherit_its_parents_owner(self):
        # The owner forking key A's session makes a session that is the owner's.
        fork = {"id": "f", "api_user_id": None, "is_worker": False, "parent_session_id": "root"}
        with patch.object(sessions_helpers.database, "get_session") as lookup:
            assert not api_user_owns_session(KEY_A, fork)
        lookup.assert_not_called()


# -- REST: GET /sessions/{id} ----------------------------------------------------


def test_get_session_by_id_is_limited_to_the_keys_own_sessions(client):
    from app.api import auth

    database.create_api_user("key-a", "A", auth.hash_api_key("aih_key_a"))
    database.create_api_user("key-b", "B", auth.hash_api_key("aih_key_b"))
    database.create_session("mine", "default", api_user_id="key-a")
    database.create_session("other-key", "default", api_user_id="key-b")
    database.create_session("owners", "default")
    headers = {"Authorization": "Bearer aih_key_a"}

    assert client.get("/api/v1/sessions/mine", headers=headers).status_code == 200
    for session_id in ("other-key", "owners"):
        response = client.get(f"/api/v1/sessions/{session_id}", headers=headers)
        assert response.status_code == 403, session_id


# -- chat WebSocket frames -------------------------------------------------------


@pytest.fixture
def ws_world(mock_db):
    database.create_api_user("key-a", "A", "hash-a")
    database.create_api_user("key-b", "B", "hash-b")
    _make_session("own")
    _make_session("foreign")
    from app.db.base import get_db

    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE sessions SET api_user_id = %s WHERE id = %s", ("key-a", "own"))
        c.execute("UPDATE sessions SET api_user_id = %s WHERE id = %s", ("key-b", "foreign"))
    # A queued turn in each session, for remove_queued_message.
    own_turn = turns_db.enqueue_turn("own", "queue", prompt="mine")
    foreign_turn = turns_db.enqueue_turn("foreign", "queue", prompt="theirs")
    return {"own_turn": own_turn["id"], "foreign_turn": foreign_turn["id"]}


class _Effects:
    """Patch what the frames act through, so a test can see whether anything ran."""

    def __enter__(self):
        self._patches = _Patches().__enter__()
        self.sync = self._patches.sync
        self.sync.broadcast_session_closed = AsyncMock()
        self.perm = MagicMock()
        self.perm.respond = AsyncMock(return_value={"resolved": True})
        self.perm.requeue_deferred = AsyncMock(return_value=["r1"])
        self.perm.get_pending_requests = MagicMock(
            return_value=[{"request_id": "r1", "tool_name": "Bash", "tool_input": {"command": "ls"}}])
        self.perm.get_deferred_requests = MagicMock(return_value=[])
        self.perm.get_queue_count = MagicMock(return_value=0)
        self.questions = MagicMock()
        self.questions.respond = AsyncMock(return_value=True)
        self.questions.get_pending_questions = MagicMock(return_value=[{"request_id": "q1"}])
        self.turns = MagicMock()
        self.turns.interrupt_async = AsyncMock(return_value=None)
        self.turns.submit_async = AsyncMock()
        self.turns.remove_queued = MagicMock()
        self._extra = [
            patch.object(cwh, "permission_handler", self.perm),
            patch.object(cwh, "user_question_handler", self.questions),
            patch.object(cwh, "turn_manager", self.turns),
            patch("app.core.session_runtime.try_cancel_steer",
                  new=AsyncMock(return_value="not_submitted")),
        ]
        for p in self._extra:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._extra):
            p.stop()
        self._patches.__exit__(*exc)
        return False


def _handler(api_user_id, current=None):
    handler = cwh.ChatWebSocketHandler(
        websocket=MagicMock(), device_id="dev-1", user_id=api_user_id or "admin",
        api_user_id=api_user_id,
    )
    handler.send_json = AsyncMock(return_value=True)
    handler._authorize_mobile_session = AsyncMock(return_value=True)
    handler._network_owner = AsyncMock(return_value=None)
    handler.current_session_id = current
    return handler


def _refused(handler, frame_type="error"):
    return any(
        f.get("message") == DENIED for f in sent_frames(handler, frame_type)
    )


# Each frame: (type, data for session ``sid``, handler preset, "did it act?").
def _frames(world):
    return {
        "watch_session": (
            lambda sid: {"session_id": sid}, False,
            lambda h, fx: fx.sync.register_device.await_count > 0),
        "close_session": (
            lambda sid: {}, True,
            lambda h, fx: bool(sent_frames(h, "session_closed"))),
        "get_pending_permissions": (
            lambda sid: {}, True,
            lambda h, fx: bool(sent_frames(h, "pending_permissions"))),
        "get_pending_questions": (
            lambda sid: {}, True,
            lambda h, fx: bool(sent_frames(h, "pending_questions"))),
        "permission_response": (
            lambda sid: {"session_id": sid, "request_id": "r1", "decision": "allow"}, False,
            lambda h, fx: fx.perm.respond.await_count > 0),
        "user_question_response": (
            lambda sid: {"session_id": sid, "request_id": "q1", "answers": {"a": "b"}}, False,
            lambda h, fx: fx.questions.respond.await_count > 0),
        "requeue_deferred_permissions": (
            lambda sid: {"session_id": sid}, False,
            lambda h, fx: fx.perm.requeue_deferred.await_count > 0),
        "stop": (
            lambda sid: {"session_id": sid}, False,
            lambda h, fx: fx.turns.interrupt_async.await_count > 0),
        "interrupt_for_query": (
            lambda sid: {"session_id": sid}, False,
            lambda h, fx: fx.turns.interrupt_async.await_count > 0),
        "remove_queued_message": (
            lambda sid: {"session_id": sid,
                         "message_id": world["own_turn" if sid == "own" else "foreign_turn"]}, False,
            lambda h, fx: fx.turns.remove_queued.call_count > 0),
        "clear_queued_messages": (
            lambda sid: {"session_id": sid}, False,
            lambda h, fx: fx.turns.remove_queued.call_count > 0),
        "queue_message": (
            lambda sid: {"session_id": sid, "content": "later"}, False,
            lambda h, fx: fx.turns.submit_async.await_count > 0),
    }


FRAMES = [
    "watch_session", "close_session", "get_pending_permissions", "get_pending_questions",
    "permission_response", "user_question_response", "requeue_deferred_permissions",
    "stop", "interrupt_for_query", "remove_queued_message", "clear_queued_messages",
    "queue_message",
]


async def _send(handler, frame_type, data):
    await handler._handlers[frame_type]({"type": frame_type, **data})


@pytest.mark.parametrize("frame_type", FRAMES)
async def test_api_user_frame_on_a_foreign_session_is_refused(ws_world, frame_type):
    make, preset, acted = _frames(ws_world)[frame_type]
    handler = _handler("key-a", current="foreign" if preset else None)
    with _Effects() as fx:
        await _send(handler, frame_type, make("foreign"))
    refusal = "interrupt_failed" if frame_type == "interrupt_for_query" else "error"
    assert _refused(handler, refusal), handler.send_json.call_args_list
    assert not acted(handler, fx)
    # Nothing about the foreign session reached the socket but the refusal.
    for call in handler.send_json.call_args_list:
        assert call.args[0].get("type") in ("error", "interrupt_failed"), call.args[0]
    if frame_type == "watch_session":
        assert handler.current_session_id is None
    assert [t["id"] for t in turns_db.get_queued_turns("foreign")] == [ws_world["foreign_turn"]]


@pytest.mark.parametrize("frame_type", FRAMES)
async def test_api_user_frame_on_its_own_session_runs(ws_world, frame_type):
    make, preset, acted = _frames(ws_world)[frame_type]
    handler = _handler("key-a", current="own" if preset else None)
    with _Effects() as fx:
        await _send(handler, frame_type, make("own"))
    assert not _refused(handler) and not _refused(handler, "interrupt_failed")
    assert acted(handler, fx), handler.send_json.call_args_list


@pytest.mark.parametrize("frame_type", FRAMES)
async def test_owner_frames_are_unrestricted(ws_world, frame_type):
    make, preset, acted = _frames(ws_world)[frame_type]
    handler = _handler(None, current="foreign" if preset else None)
    with _Effects() as fx, patch.object(cwh.database, "get_api_user") as lookup:
        await _send(handler, frame_type, make("foreign"))
    assert not _refused(handler) and not _refused(handler, "interrupt_failed")
    assert acted(handler, fx), handler.send_json.call_args_list
    lookup.assert_not_called()


async def test_unknown_session_is_refused_like_a_foreign_one(ws_world):
    handler = _handler("key-a")
    with _Effects() as fx:
        await _send(handler, "stop", {"session_id": "no-such-session"})
    assert _refused(handler)
    fx.turns.interrupt_async.assert_not_awaited()


async def test_remove_queued_message_cannot_reach_a_foreign_turn_through_its_own_session(ws_world):
    handler = _handler("key-a")
    with _Effects() as fx:
        await _send(handler, "remove_queued_message",
                    {"session_id": "own", "message_id": ws_world["foreign_turn"]})
    assert _refused(handler)
    fx.turns.remove_queued.assert_not_called()


@pytest.mark.parametrize("frame_type", ["load_session", "query"])
async def test_load_and_query_on_a_foreign_session_are_refused(ws_world, frame_type):
    data = {"session_id": "foreign"}
    if frame_type == "query":
        data["prompt"] = "hi"
    handler = _handler("key-a")
    with _Effects() as fx:
        await _send(handler, frame_type, data)
    if frame_type == "load_session":
        # Answered like an unknown id, so the reply does not reveal the session.
        assert sent_frames(handler, "session_not_found") == [
            {"type": "session_not_found", "session_id": "foreign"}]
    else:
        assert _refused(handler)
    assert not sent_frames(handler, "history")
    fx.sync.register_device.assert_not_awaited()
    fx.turns.submit_async.assert_not_awaited()
    assert handler.current_session_id is None


# -- R5-3: remember scope --------------------------------------------------------


@pytest.mark.parametrize("api_user_id, expected", [("key-a", "session"), (None, "profile")])
async def test_remember_profile_is_kept_to_the_session_for_api_users(ws_world, api_user_id, expected):
    handler = _handler(api_user_id)
    with _Effects() as fx:
        await _send(handler, "permission_response", {
            "session_id": "own", "request_id": "r1", "decision": "allow",
            "remember": "profile", "pattern": "*",
        })
    assert fx.perm.respond.await_args.kwargs["remember"] == expected
    assert fx.perm.respond.await_args.kwargs["pattern"] == "*"


@pytest.mark.parametrize("api_user_id, persisted", [("key-a", False), (None, True)])
async def test_remember_profile_persists_an_agent_rule_only_for_the_owner(
        ws_world, monkeypatch, api_user_id, persisted):
    from app.core.permission_handler import PermissionHandler

    monkeypatch.setattr("app.core.interaction_wait.notify_changed", lambda sid: None)
    perm = PermissionHandler()
    request = asyncio.create_task(perm.request_permission(
        request_id="r1", session_id="own", profile_id="default",
        tool_name="Bash", tool_input={"command": "ls"}, broadcast_func=AsyncMock(),
    ))
    for _ in range(200):
        if perm.get_pending_requests("own"):
            break
        await asyncio.sleep(0.01)
    assert perm.get_pending_requests("own")

    handler = _handler(api_user_id)
    with _Patches(), patch.object(cwh, "permission_handler", perm):
        await _send(handler, "permission_response", {
            "session_id": "own", "request_id": "r1", "decision": "allow",
            "remember": "profile", "pattern": "*",
        })
    await asyncio.wait_for(request, 12.0)

    rules = database.get_permission_rules(profile_id="default")
    assert bool(rules) is persisted
    # The API user's answer still applies within its own session.
    assert bool(perm._check_rules("own", "default", "Bash", {"command": "rm -rf /"})) is True
    assert bool(perm._check_rules("other-session", "default", "Bash", {"command": "rm"})) is persisted
