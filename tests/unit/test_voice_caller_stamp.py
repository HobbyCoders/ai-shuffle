"""#654: a voice request carries who started the call, as a typed send does.

The owner's no-project chat gets its full code tools only for a turn stamped
``authenticated`` (#641). A voice call used to submit with no caller, so its
turns were stamped ``unknown`` and ran restricted. The call's caller is now
the identity the route that opened the call authenticated, and it reaches
both submit paths: the local queue and the forward to the worker that runs
the chat (``control.submit_turn``). The receiving side takes it only from
that authentication, never from a message, a payload or the overrides.

Each case runs the real submit and the real lane decision
(``runtime_session_mode`` / ``build_options_from_profile``).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import session_orchestration as orch
from app.core import turn_manager, voice_remote, voice_stream
from app.core.session_modes import (
    CALLER_AUTHENTICATED,
    CALLER_KEY,
    CALLER_PREVIEW,
    CALLER_UNKNOWN,
    runtime_session_mode,
    turn_caller,
)
from app.db import database
from app.db import turns as turns_db
from app.network import control
from tests.core.test_session_orchestration import chat

PEER = "peer-device-office-pc"
NON_OWNER = {"owner": "account:someone-else", "kind": "paired", "principal_id": "phone",
             "account_id": "someone-else"}


@pytest.fixture
def owner_chats(mock_db, monkeypatch, tmp_path):
    """Owner no-project chats on this worker, submitted for real (no kick)."""
    from app.core import config, query_options, session_runtime

    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    monkeypatch.setattr(query_options, "get_security_instructions", lambda *a, **kw: "")
    principals = {}
    monkeypatch.setattr(orch, "_principal", lambda sid: principals.get(sid))
    monkeypatch.setattr(turn_manager, "_kick", lambda *a, **k: None)
    monkeypatch.setattr(session_runtime, "try_steer_pending", AsyncMock(return_value=0))
    return principals


def _lane(session_id, turn_id):
    """The lane the queued voice turn runs in, from its own stamp."""
    turn = turns_db.get_turn(turn_id)
    return runtime_session_mode(database.get_session(session_id), caller=turn_caller(turn["overrides"]))


def _has_shell(session_id, turn_id, **kwargs):
    from app.core import query_options
    from app.core.tool_access import tool_permitted
    options, _ = query_options.build_options_from_profile(
        database.get_profile("agent"), session_id=session_id,
        overrides=turns_db.get_turn(turn_id)["overrides"], **kwargs)
    return tool_permitted("Bash", options.aih_tool_allowlist, options.disallowed_tools)


async def _voice_turn(session_id, caller):
    submitted = AsyncMock()
    result = await voice_stream.delegate_to_selected_model(
        session_id, "Run the tests.", on_submitted=submitted, caller=caller)
    assert result == ""
    return submitted.await_args.args[0]


# -- the local submit ---------------------------------------------------------


async def test_an_owner_call_gets_the_no_project_chat_its_full_tools(owner_chats):
    chat("owner-chat", mode="chat")

    turn_id = await _voice_turn("owner-chat", CALLER_AUTHENTICATED)

    assert turns_db.get_turn(turn_id)["overrides"][CALLER_KEY] == CALLER_AUTHENTICATED
    assert _lane("owner-chat", turn_id) == "code"
    assert _has_shell("owner-chat", turn_id)


@pytest.mark.parametrize("caller, stamp", [
    (None, CALLER_UNKNOWN),             # A call nobody authenticated.
    ("authenticated ", CALLER_UNKNOWN),  # Not a caller this build knows.
    (CALLER_PREVIEW, CALLER_PREVIEW),    # The agent browser's preview cookie.
    (CALLER_UNKNOWN, CALLER_UNKNOWN),
])
async def test_a_call_that_is_not_owner_authenticated_stays_restricted(owner_chats, caller, stamp):
    chat("owner-chat", mode="chat")

    turn_id = await _voice_turn("owner-chat", caller)

    assert turns_db.get_turn(turn_id)["overrides"][CALLER_KEY] == stamp
    assert _lane("owner-chat", turn_id) == "chat"
    assert not _has_shell("owner-chat", turn_id)


async def test_an_authenticated_call_on_another_principals_chat_stays_restricted(owner_chats):
    """The stamp lifts nothing by itself: the owner checks still decide."""
    chat("phone-chat", mode="chat")
    owner_chats["phone-chat"] = NON_OWNER

    turn_id = await _voice_turn("phone-chat", CALLER_AUTHENTICATED)

    assert _lane("phone-chat", turn_id) == "chat"
    assert not _has_shell("phone-chat", turn_id)


async def test_an_api_key_call_stays_restricted(owner_chats):
    chat("key-chat", mode="chat", api_user_id="key-user")

    turn_id = await _voice_turn("key-chat", CALLER_AUTHENTICATED)

    assert turns_db.get_turn(turn_id)["payload"]["api_user_id"] == "key-user"
    assert _lane("key-chat", turn_id) == "chat"
    assert not _has_shell("key-chat", turn_id, api_user_id="key-user")


# -- the transports pass their own caller -------------------------------------


@pytest.mark.parametrize("caller", [CALLER_AUTHENTICATED, None])
async def test_a_streamed_call_submits_with_the_caller_it_was_opened_by(monkeypatch, caller):
    seen = []

    async def delegate(session_id, transcript, on_submitted=None, **kwargs):
        seen.append(kwargs.get("caller"))
        await on_submitted("turn-1")
        return ""

    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    socket = SimpleNamespace(send_json=AsyncMock(), send_bytes=AsyncMock(), close=AsyncMock())
    call = voice_stream.VoiceStream(socket, {"id": "s1"}, {"model": "gpt-live-1", "voice": "marin"}, "k", 5,
                                    caller=caller)
    call.flush = AsyncMock()
    call.progress = SimpleNamespace(submitted=AsyncMock())
    call.transcript.append({"role": "user", "text": "Run the tests.", "seq": 1})

    await call.run_delegation("d1")

    assert seen == [caller]


async def test_a_sideband_submit_to_chat_carries_the_caller_it_was_attached_by(monkeypatch):
    from app.core import voice_bridge
    seen = []

    async def delegate(session_id, transcript, on_submitted=None, **kwargs):
        seen.append(kwargs.get("caller"))
        return ""

    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    monkeypatch.setattr(voice_bridge, "_record_session_usage", lambda *a, **k: None)
    bridge = voice_bridge.VoiceBridge("s1", "rtc_1", api_key="k", model="gpt-realtime-2", max_call_minutes=30,
                                      conversation_surface=True, caller=CALLER_AUTHENTICATED)
    bridge._send = AsyncMock()
    bridge._flush_exchange = AsyncMock()
    bridge._record_tool_exchange = AsyncMock()

    # The model's own arguments cannot name a caller.
    await bridge._service_call("call-1", "submit_to_chat",
                               '{"request": "Fix the test", "caller": "preview", "_caller": "preview"}')

    assert seen == [CALLER_AUTHENTICATED]


@pytest.mark.parametrize("token, api_user, expected", [
    ("local-owner", None, CALLER_AUTHENTICATED),
    ("owner-session-token", None, CALLER_AUTHENTICATED),
    ("preview:some-session", None, CALLER_PREVIEW),
    ("api_key:key-user", {"id": "key-user"}, CALLER_AUTHENTICATED),
    (None, None, CALLER_UNKNOWN),  # No authentication recorded: fails closed.
])
def test_a_call_route_names_the_caller_its_request_authenticated(token, api_user, expected):
    from app.api.voice import _request_caller
    headers = {"Authorization": "Bearer raw-api-key"} if api_user else {}
    request = SimpleNamespace(state=SimpleNamespace(auth_token=token, api_user=api_user), headers=headers)

    assert _request_caller(request) == expected


# -- the forward to the worker that runs the chat -----------------------------


@pytest.fixture
def forwarded(monkeypatch):
    sent = []

    async def owner_call(device_id, action, body=None, **_kwargs):
        sent.append(body)
        return {"turn_id": "owner-turn-1", "session_id": body["session_id"]}

    monkeypatch.setattr(control, "call", owner_call)
    return sent


@pytest.mark.parametrize("caller", [CALLER_AUTHENTICATED, CALLER_PREVIEW])
async def test_a_forwarded_voice_request_carries_the_callers_stamp(forwarded, caller):
    await voice_remote.submit(PEER, {"id": "owner-chat"}, "Run the tests.", instructions="note", caller=caller)

    assert forwarded[0]["caller"] == caller
    assert CALLER_KEY not in (forwarded[0]["overrides"] or {})


async def test_a_forward_without_a_caller_names_none(forwarded):
    await voice_remote.submit(PEER, {"id": "owner-chat"}, "Run the tests.", instructions="note")

    assert "caller" not in forwarded[0]


async def test_the_owner_stamps_a_forwarded_owner_call_and_gives_it_the_full_tools(
        owner_chats, forwarded, monkeypatch):
    """The body a call's worker sends, received by the worker that runs the chat."""
    chat("owner-chat", mode="chat")
    await voice_remote.submit(PEER, {"id": "owner-chat"}, "Run the tests.", instructions="note",
                              command="voice:call:1", caller=CALLER_AUTHENTICATED)
    monkeypatch.setattr(control, "owner_of", lambda row: None)  # This worker runs it.

    result = await control._turns(forwarded[0], "calling-worker")

    assert turns_db.get_turn(result["turn_id"])["overrides"][CALLER_KEY] == CALLER_AUTHENTICATED
    assert _lane("owner-chat", result["turn_id"]) == "code"


@pytest.mark.parametrize("claim", [
    {"overrides": {CALLER_KEY: CALLER_AUTHENTICATED}},
    {"payload": {CALLER_KEY: CALLER_AUTHENTICATED, "voice_delegation": True}},
    {"caller": "owner"},  # Not a caller this build knows.
])
async def test_the_owner_never_takes_a_bare_claim_from_the_message(owner_chats, forwarded, monkeypatch, claim):
    chat("owner-chat", mode="chat")
    await voice_remote.submit(PEER, {"id": "owner-chat"}, "Run the tests.", instructions="note")
    body = {**forwarded[0], **claim}
    monkeypatch.setattr(control, "owner_of", lambda row: None)

    result = await control._turns(body, "calling-worker")

    assert turns_db.get_turn(result["turn_id"])["overrides"][CALLER_KEY] == CALLER_UNKNOWN
    assert _lane("owner-chat", result["turn_id"]) == "chat"
