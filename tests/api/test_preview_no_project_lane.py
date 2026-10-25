"""#641 review: the agent browser's preview cookie never gets the owner's no-project tools.

Each case drives the real intake (``POST /sessions``, the chat socket's send
and queue) into the durable turns table, then builds the turn's options from
the stored row with the real option builder, as the runtime does.
"""
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock

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
    return identity.principal("admin", "install", "local-transport:" + settings.effective_instance_id)


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


def test_a_forwarded_send_carries_only_a_stated_owner_caller() -> None:
    from app.network import control

    assert control._forwarded_caller({"owner_send": True, "caller": CALLER_PREVIEW}) == CALLER_PREVIEW
    assert control._forwarded_caller({"owner_send": True}) is None
    assert control._forwarded_caller({"caller": CALLER_AUTHENTICATED}) is None
    assert control._forwarded_caller({"owner_send": True, "caller": "owner!"}) is None


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
        {"profile_id": "default", "payload": {}, "prompt": "hi", "overrides": overrides(row_caller)},
        stored_workspace_ids=None,
    )
    assert (reason == "caller differs") is refused
