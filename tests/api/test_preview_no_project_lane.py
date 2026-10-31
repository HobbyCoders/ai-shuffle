"""#641 review: the agent browser's preview cookie never gets the owner's no-project tools.

Each case drives the real intake (``POST /sessions``, the chat socket's send
and queue) into the durable turns table, then builds the turn's options from
the stored row with the real option builder, as the runtime does.
"""
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
from app.core import mobile_tool_identity as identity
from app.core import turn_manager
from app.core.config import settings
from app.core.session_modes import (
    CALLER_AUTHENTICATED,
    CALLER_KEY,
    CALLER_PREVIEW,
    CALLER_UNKNOWN,
    chat_uploads_dir,
    owner_chat_dir,
)
from app.core.tool_access import tool_permitted
from app.db import database
from app.db import mobile_tool_identity as identity_store
from app.db import turns as turns_db
from tests.api.test_chat_ws_v2_turns import _Patches

CODE_TOOLS = ("Bash", "Edit", "Write", "run_python")


def _owner() -> Dict[str, Any]:
    transport = "local-transport:" + settings.effective_instance_id
    return identity.principal("admin", "install", transport)


@pytest.fixture
def workspace(mock_db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    from app.core import config, query_options

    database.register_install(
        settings.effective_instance_id, "test-runner", None, "local", "1", {}, approve_new=True,
    )
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    monkeypatch.setattr(query_options, "get_security_instructions", lambda *a, **kw: "")
    # Credential liveness is not what these tests are about.
    monkeypatch.setattr(identity, "validate", lambda value: None)
    with _Patches():
        yield tmp_path / "workspace"


def _request(token: str) -> SimpleNamespace:
    """A REST request as ``require_auth`` leaves it."""
    return SimpleNamespace(state=SimpleNamespace(
        auth_token=token, api_user=None, is_admin=True, paired_client=None))


async def _rest_create(token: str) -> str:
    from app.api.sessions_create import CreateSessionRequest, create_empty_session

    response = await create_empty_session(
        _request(token), CreateSessionRequest(profile_id="default", mode="chat"), token=token,
    )
    return response.session_id


def _handler(principal: Optional[Dict[str, Any]]) -> cwh.ChatWebSocketHandler:
    handler = cwh.ChatWebSocketHandler(MagicMock(), "dev-1", "admin", None, principal)
    handler.send_json = AsyncMock(return_value=True)
    return handler


async def _send(principal: Optional[Dict[str, Any]], session_id: str, text: str) -> Dict[str, Any]:
    await _handler(principal).handle_query({
        "prompt": text, "session_id": session_id, "profile": "default", "project": None,
    })
    return _latest(session_id)


def _latest(session_id: str) -> Dict[str, Any]:
    turn = turns_db.get_latest_turn(session_id)
    assert turn is not None
    return turn


def _options(session_id: str, overrides: Optional[Dict[str, Any]]) -> Any:
    from app.core import query_options

    options, _ = query_options.build_options_from_profile(
        database.get_profile("default"), session_id=session_id, overrides=dict(overrides or {}),
    )
    return options


def _assert_full(options: Any) -> None:
    assert options.aih_session_mode == "code"
    assert options.cwd == str(owner_chat_dir())
    for tool in CODE_TOOLS:
        assert tool_permitted(tool, options.aih_tool_allowlist, options.disallowed_tools), tool


def _assert_restricted(options: Any) -> None:
    assert options.aih_session_mode == "chat"
    assert options.cwd == str(chat_uploads_dir())
    for tool in CODE_TOOLS:
        assert not tool_permitted(tool, options.aih_tool_allowlist, options.disallowed_tools), tool


async def test_preview_rest_create_then_send_stays_restricted(workspace: Path) -> None:
    sid = await _rest_create("preview:agent-session")
    # Recorded at creation, as the chat socket records a chat it creates.
    assert identity_store.read(sid)["principal_id"] == identity.PREVIEW_PRINCIPAL_ID
    turn = await _send(identity.preview_principal(), sid, "run ls")
    assert turn["overrides"][CALLER_KEY] == CALLER_PREVIEW
    _assert_restricted(_options(sid, turn["overrides"]))
    # A turn nobody sent (a wake) runs as the chat's last sender did.
    _assert_restricted(_options(sid, None))


async def test_owner_rest_create_then_send_gets_full_tools(workspace: Path) -> None:
    sid = await _rest_create("local-owner")
    assert identity_store.read(sid) is None  # unchanged: the owner login records nothing
    turn = await _send(_owner(), sid, "run ls")
    assert turn["overrides"][CALLER_KEY] == CALLER_AUTHENTICATED
    _assert_full(_options(sid, turn["overrides"]))
    _assert_full(_options(sid, None))


async def test_preview_continuing_an_unrecorded_owner_chat_is_restricted(workspace: Path) -> None:
    sid = await _rest_create("local-owner")
    owner_turn = await _send(_owner(), sid, "first")
    _assert_full(_options(sid, owner_turn["overrides"]))

    preview_turn = await _send(identity.preview_principal(), sid, "now run a shell")
    assert preview_turn["overrides"][CALLER_KEY] == CALLER_PREVIEW
    _assert_restricted(_options(sid, preview_turn["overrides"]))
    # Its follow-ups too, and no record grants the chat new authority.
    _assert_restricted(_options(sid, None))
    assert identity_store.read(sid) is None

    # The owner's next message lifts it again.
    back = await _send(_owner(), sid, "thanks")
    _assert_full(_options(sid, back["overrides"]))
    _assert_full(_options(sid, None))


async def test_preview_queue_message_is_stamped(workspace: Path) -> None:
    sid = await _rest_create("local-owner")
    await _handler(identity.preview_principal()).handle_queue_message({
        "session_id": sid, "content": "queued text", "profile": "default",
    })
    turn = _latest(sid)
    assert turn["source"] == "queue"
    assert turn["overrides"][CALLER_KEY] == CALLER_PREVIEW
    _assert_restricted(_options(sid, turn["overrides"]))


async def test_a_human_turn_without_a_caller_fails_closed(workspace: Path) -> None:
    sid = await _rest_create("local-owner")
    await turn_manager.submit_async(sid, "user", prompt="from somewhere", no_kick=True)
    turn = _latest(sid)
    assert turn["overrides"][CALLER_KEY] == CALLER_UNKNOWN
    _assert_restricted(_options(sid, turn["overrides"]))


async def test_a_client_cannot_stamp_itself_the_owner(workspace: Path) -> None:
    sid = await _rest_create("preview:agent-session")
    await _handler(identity.preview_principal()).handle_query({
        "prompt": "hi", "session_id": sid, "profile": "default", "project": None,
        "overrides": {CALLER_KEY: CALLER_AUTHENTICATED},
    })
    assert _latest(sid)["overrides"][CALLER_KEY] == CALLER_PREVIEW


async def test_preview_send_to_another_worker_is_refused(workspace: Path) -> None:
    preview = _handler(identity.preview_principal())
    assert await preview._may_send_elsewhere("sess-x") is False
    frame = preview.send_json.await_args.args[0]
    assert frame["type"] == "error" and "preview link" in frame["message"]
    assert await _handler(_owner())._may_send_elsewhere("sess-x") is True


async def test_preview_send_to_another_worker_is_allowed_between_throwaway_dev_workers(
        workspace: Path, monkeypatch) -> None:
    """``devloop up --workers 2``: both workers are launcher-marked throwaway
    copies, so the owner can test a cross-worker chat through the preview link."""
    from app import preview_run

    monkeypatch.setattr(preview_run, "disposable_preview_data", lambda: True)
    preview = _handler(identity.preview_principal())
    assert await preview._may_send_elsewhere("sess-x") is True
    preview.send_json.assert_not_awaited()


def test_a_forwarded_turn_carries_only_a_stated_caller() -> None:
    from app.network import control

    assert control._forwarded_caller({"caller": CALLER_PREVIEW}) == CALLER_PREVIEW
    # Apart from owner_send: a held message the owner sent without a mode pick.
    assert control._forwarded_caller({"caller": CALLER_AUTHENTICATED}) == CALLER_AUTHENTICATED
    assert control._forwarded_caller({"owner_send": True}) is None
    assert control._forwarded_caller({"owner_send": True, "caller": "owner!"}) is None
    # Only the top-level field: a stamp inside the overrides never counts.
    body = {"owner_send": True, "overrides": {CALLER_KEY: CALLER_AUTHENTICATED}}
    assert control._forwarded_caller(body) is None
    assert control._turn_overrides(body["overrides"]) is None


async def _held_then_moved(sid: str, send: Callable[[], Awaitable[Any]]) -> Dict[str, Any]:
    """Hold a message while a move to another worker waits, deliver it there
    through the real forwarding code, and take it in as the new worker does.

    Both workers share this process: the source's ``control.call`` hands the
    body it would send over the peer channel to the receiver's handler.
    """
    import time

    from app.core import session_handoff as sh
    from app.db import session_moves
    from app.network import control

    session_moves.save(sid, target_id="dev-dest", label="Dest", note="", requested_by="owner",
                       requested_at=time.time(), expires_at=time.time() + 600)
    with patch.object(sh, "holds", return_value=True):
        await send()
    (held,) = session_moves.get(sid)["held"]
    assert turns_db.get_latest_turn(sid) is None  # held, not queued here

    sent: list = []

    async def call(device_id: str, action: str, body: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
        sent.append((device_id, action, body))
        return {"turn_id": "t-dest", "session_id": sid}

    with patch.object(control, "call", side_effect=call), \
            patch.object(sh, "_finalize_moved_wakes", AsyncMock()), \
            patch.object(sh, "_resend_safe", return_value=True), \
            patch.object(sh, "FORWARD_RETRY_S", 0):
        await sh.deliver_held(sid, "dev-dest", "Dest")
    ((device_id, action, body),) = sent
    assert (device_id, action, body["prompt"]) == ("dev-dest", "turns", held["prompt"])
    # The receiver: the authenticated peer channel's turns handler.
    with patch("app.core.sync_engine.sync_engine.broadcast_session_updated", AsyncMock()):
        await control._turns(body, "dev-source")
    return _latest(sid)


async def test_an_owner_message_held_through_a_move_keeps_full_tools_there(
    workspace: Path,
) -> None:
    sid = await _rest_create("local-owner")
    turn = await _held_then_moved(sid, lambda: _handler(_owner()).handle_query({
        "prompt": "check the disk", "session_id": sid, "profile": "default", "project": None,
    }))
    assert turn["prompt"] == "check the disk"
    assert turn["overrides"][CALLER_KEY] == CALLER_AUTHENTICATED
    _assert_full(_options(sid, turn["overrides"]))


async def test_a_preview_message_held_through_a_move_stays_restricted_there(
    workspace: Path,
) -> None:
    sid = await _rest_create("local-owner")
    turn = await _held_then_moved(sid, lambda: _handler(identity.preview_principal()).handle_query({
        "prompt": "run a shell", "session_id": sid, "profile": "default", "project": None,
    }))
    assert turn["overrides"][CALLER_KEY] == CALLER_PREVIEW
    _assert_restricted(_options(sid, turn["overrides"]))


async def test_an_unknown_callers_message_held_through_a_move_stays_restricted_there(
    workspace: Path,
) -> None:
    sid = await _rest_create("local-owner")
    turn = await _held_then_moved(sid, lambda: turn_manager.submit_async(
        sid, "user", prompt="from somewhere", no_kick=True,
        # A body claiming the owner's stamp changes nothing.
        overrides={CALLER_KEY: CALLER_AUTHENTICATED}))
    assert turn["overrides"][CALLER_KEY] == CALLER_UNKNOWN
    _assert_restricted(_options(sid, turn["overrides"]))


@pytest.mark.parametrize(("row_caller", "live_caller", "gated", "refused"), [
    (CALLER_PREVIEW, CALLER_AUTHENTICATED, True, True),
    (CALLER_PREVIEW, None, True, True),
    (CALLER_UNKNOWN, CALLER_AUTHENTICATED, True, True),
    (CALLER_PREVIEW, CALLER_PREVIEW, True, False),
    (CALLER_AUTHENTICATED, CALLER_PREVIEW, True, False),
    (None, CALLER_AUTHENTICATED, True, False),
    # A project chat's tools never depend on the caller: steering is unchanged.
    (CALLER_PREVIEW, CALLER_AUTHENTICATED, False, False),
])
def test_a_preview_message_never_steers_into_another_callers_turn(
    row_caller: Optional[str], live_caller: Optional[str], gated: bool, refused: bool,
) -> None:
    from app.core.session_runtime import SessionRuntime

    def overrides(caller: Optional[str]) -> Dict[str, Any]:
        return {CALLER_KEY: caller} if caller else {}

    runtime = SessionRuntime.__new__(SessionRuntime)
    runtime.turn = {"profile_id": "default", "payload": {}, "overrides": overrides(live_caller)}
    runtime.caller_gated_lane = gated
    reason = runtime._steer_refusal(
        {"profile_id": "default", "payload": {}, "prompt": "hi",
         "overrides": overrides(row_caller)},
        stored_workspace_ids=None,
    )
    assert (reason == "caller differs") is refused
