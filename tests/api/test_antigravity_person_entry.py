"""Who may mark a turn "a person entered it" (``turns.PERSON_ENTERED_KEY``).

Only the sender the host authenticated as the owner: the owner's login or the
local listener, and the owner's paired devices. The agent browser's preview
link, an API key and no principal never do, at any admission point: the chat
socket's send, steer and queue (and its HTTP transport), the socket's
question-card answer, a worktree chat's opening prompt, and the Retry route.
Each case drives the real handler into the durable turns table, then asks the
guard of a personal sign-in row (``antigravity.unattended_reason``) what it
makes of the stored row, as the turn start does (``turn_provenance``).
"""
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.api.chat_ws_handler as cwh
from app.core import antigravity as ag
from app.core import mobile_tool_identity as identity
from app.core.config import settings
from app.db import database
from app.db import turns as turns_db
from tests.api.test_chat_ws_v2_turns import _Patches

SID = "chat-entry"


def _owner() -> Dict[str, Any]:
    return identity.principal("admin", "install", "local-transport:" + settings.effective_instance_id)


def _paired() -> Dict[str, Any]:
    # The owner's phone app, as the socket authenticator leaves it.
    return {"kind": "paired", "principal_id": "pc-fake", "account_id": "acct-fake",
            "credential_hash": "0" * 64, "owner": "account:acct-fake"}


def _api_user() -> Dict[str, Any]:
    return identity.principal("api_user", "key-user-fake", "fake-api-key")


PRINCIPALS = {
    "owner": (_owner, True),
    "owner phone": (_paired, True),
    "preview link": (identity.preview_principal, ag.PREVIEW_REASON),
    "phone under a preview link": (lambda: identity.preview_limited(_paired()), ag.PREVIEW_REASON),
    "api user": (_api_user, ag.NOT_ENTERED_REASON),
    "nobody": (lambda: None, ag.NOT_ENTERED_REASON),
}


@pytest.fixture
def chat(mock_db, monkeypatch):
    database.register_install(settings.effective_instance_id, "test-runner", None, "local", "1", {},
                              approve_new=True)
    database.create_session(session_id=SID, profile_id="default", title="Garden plan")
    monkeypatch.setattr(identity, "validate", lambda value: None)
    monkeypatch.setattr("app.core.turn_manager._kick", lambda sid: None)
    monkeypatch.setattr("app.core.turn_claim.notify_turns_changed", lambda sid: None)
    monkeypatch.setattr("app.core.interaction_wait.notify_changed", lambda sid: None)
    with _Patches():
        yield SID


def _handler(principal: Optional[Dict[str, Any]]) -> cwh.ChatWebSocketHandler:
    api_user_id = principal["principal_id"] if principal and principal.get("kind") == "api_user" else None
    handler = cwh.ChatWebSocketHandler(MagicMock(), "dev-1", "admin", api_user_id, principal)
    handler.send_json = AsyncMock(return_value=True)
    # Session access and an API user's agent list are not what this is about.
    handler._authorize_mobile_session = AsyncMock(return_value=True)
    handler._session_refusal = AsyncMock(return_value=None)
    handler._restrict_profile = AsyncMock(side_effect=lambda requested, default: (True, requested or default))
    return handler


def _verdict(row: Dict[str, Any]) -> Optional[str]:
    """What the guard of a personal sign-in row makes of ``row`` at turn start."""
    from app.core.turn_scheduler import turn_provenance

    source, entered = turn_provenance(row)
    options = SimpleNamespace(extra={"app_session_id": row["session_id"]})
    return ag.unattended_reason(options, turn_source=source, person_entered=entered)


def _expect(row: Dict[str, Any], expected: Any) -> None:
    payload = row["payload"]
    if expected is True:
        assert payload.get(turns_db.PERSON_ENTERED_KEY) is True
        assert _verdict(row) is None
    else:
        assert turns_db.PERSON_ENTERED_KEY not in payload
        assert _verdict(row) == expected
    assert bool(payload.get(turns_db.PREVIEW_ENTRY_KEY)) is (expected == ag.PREVIEW_REASON)


def _queued(source: str) -> Dict[str, Any]:
    rows = [row for row in turns_db.get_queued_turns(SID) if row["source"] == source]
    assert rows, turns_db.get_queued_turns(SID)
    return rows[-1]


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_a_composer_send_is_the_owners_only_when_the_owner_sent_it(chat, who):
    make, expected = PRINCIPALS[who]
    await _handler(make()).handle_query({"prompt": "Plan the beds", "session_id": SID,
                                         "profile": "default", "project": None})
    _expect(_queued("user"), expected)


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_a_steer_into_a_running_turn_is_the_owners_only_when_the_owner_sent_it(chat, who):
    running = turns_db.enqueue_turn(SID, "user", prompt="first", profile_id="default", person_entered=True)
    assert turns_db.start_turn(running["id"])["state"] == "running"
    make, expected = PRINCIPALS[who]
    await _handler(make()).handle_query({"prompt": "and the paths", "session_id": SID,
                                         "profile": "default", "project": None})
    _expect(_queued("user"), expected)


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_a_queued_message_is_the_owners_only_when_the_owner_sent_it(chat, who):
    make, expected = PRINCIPALS[who]
    await _handler(make()).handle_queue_message({"content": "later", "session_id": SID})
    _expect(_queued("queue"), expected)


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_a_question_card_answer_is_the_owners_only_when_the_owner_answered(chat, who):
    from app.core.user_question_handler import user_question_handler

    await user_question_handler.post_question(
        request_id="q-entry", tool_use_id="tu", session_id=SID,
        questions=[{"question": "Colour?"}], broadcast_func=AsyncMock(),
    )
    make, expected = PRINCIPALS[who]
    await _handler(make()).handle_user_question_response(
        {"request_id": "q-entry", "session_id": SID, "answers": {"Colour?": "blue"}})
    _expect(_queued("user"), expected)


async def test_an_answer_with_no_statement_is_not_the_owners(chat):
    # Any future caller of ``respond`` that states nothing (Away mode among
    # them) answers, but not as the owner.
    from app.core.user_question_handler import user_question_handler

    await user_question_handler.post_question(
        request_id="q-plain", tool_use_id="tu", session_id=SID,
        questions=[{"question": "Colour?"}], broadcast_func=AsyncMock(),
    )
    assert await user_question_handler.respond("q-plain", SID, {"Colour?": "red"})
    _expect(_queued("user"), ag.NOT_ENTERED_REASON)


@pytest.mark.parametrize("stated,expected", [
    (True, True),
    (False, ag.NOT_ENTERED_REASON),
    ("missing", ag.OUTDATED_WORKER_REASON),  # a worker from before the statement
    ("yes", ag.OUTDATED_WORKER_REASON),      # not a bool: unstated
])
async def test_a_forwarded_answer_carries_what_its_worker_stated(chat, monkeypatch, stated, expected):
    from app.core.user_question_handler import user_question_handler
    from app.network import control

    monkeypatch.setattr(control, "_require_owned", lambda sid: database.get_session(sid))
    await user_question_handler.post_question(
        request_id="q-fwd", tool_use_id="tu", session_id=SID,
        questions=[{"question": "Colour?"}], broadcast_func=AsyncMock(),
    )
    body = {"request_id": "q-fwd", "session_id": SID, "answers": {"Colour?": "green"}}
    if stated != "missing":
        body["person_entered"] = stated
    assert (await control.handle("question", body, from_device="paired-peer"))["success"] is True
    row = _queued("user")
    _expect(row, expected)
    assert bool(row["payload"].get(turns_db.UNSTATED_ENTRY_KEY)) is (expected == ag.OUTDATED_WORKER_REASON)


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_the_socket_forwards_an_answer_with_its_own_statement(chat, who):
    from unittest.mock import patch

    from app.network import control

    make, expected = PRINCIPALS[who]
    handler = _handler(make())
    handler._network_owner = AsyncMock(return_value="owner-device")
    with patch.object(control, "answer_question", AsyncMock(return_value={"success": True})) as forwarded:
        await handler.handle_user_question_response(
            {"request_id": "q-remote", "session_id": "remote", "answers": {"Q": "A"}})
    # It hands on the socket's own decision; ``answer_question`` sends it as
    # the bool the owner worker reads (True only for the owner).
    assert (forwarded.await_args.kwargs["person_entered"] is True) is (expected is True)


async def test_answer_question_always_states_who_answered(monkeypatch):
    from app.network import control

    sent = []

    async def call(device_id, action, body):
        sent.append(body)
        return {"success": True}

    monkeypatch.setattr(control, "call", call)
    common = dict(request_id="q", session_id="s", answers={}, answered_by_device=None,
                  dismissed=False, skipped=None)
    await control.answer_question("dev", **common)
    await control.answer_question("dev", **common, person_entered=True)
    await control.answer_question("dev", **common, person_entered=ag.PREVIEW_REASON)
    assert [b["person_entered"] for b in sent] == [False, True, False]


# --- REST: the HTTP chat transport, a worktree chat's opening prompt, Retry ----

def _rest_request(who: str) -> SimpleNamespace:
    """A REST request as ``require_auth`` leaves it, for each sender."""
    state = SimpleNamespace(auth_token="local-owner", api_user=None, paired_client=None, preview=False)
    headers: Dict[str, str] = {}
    if who == "preview link":
        state.auth_token = "preview:agent-session"
    elif who == "api user":
        state.auth_token, state.api_user = None, {"id": "key-user-fake"}
        headers["Authorization"] = "Bearer fake-api-key"
    elif who == "nobody":
        state.auth_token = None
    return SimpleNamespace(state=state, headers=headers)


REST = {"owner": True, "preview link": ag.PREVIEW_REASON, "api user": ag.NOT_ENTERED_REASON,
        "nobody": ag.NOT_ENTERED_REASON}


@pytest.fixture
def rest(chat, monkeypatch):
    monkeypatch.setattr(identity, "request_preview_limited", lambda request: request.state.preview)
    monkeypatch.setattr("app.core.preview_access.grant", lambda request: None)
    # Session ownership of an API key is not what this is about.
    monkeypatch.setattr(cwh.ChatWebSocketHandler, "_session_refusal", AsyncMock(return_value=None))
    return chat


@pytest.mark.parametrize("who", REST)
async def test_the_http_chat_transport_marks_only_the_owner(rest, who):
    from app.api.chat_query import submit_http_query

    await submit_http_query(_rest_request(who), SID, {"prompt": "Plan the beds", "profile": "default",
                                                      "project": None}, compact_first=False)
    _expect(_queued("user"), REST[who])


@pytest.mark.parametrize("who", REST)
async def test_a_worktree_chats_opening_prompt_is_the_owners_only_when_the_owner_sent_it(
        rest, monkeypatch, who):
    # The endpoint decides from its request's principal and hands that to the
    # intake, which submits the opening prompt with it.
    from app.api import sessions_worktree
    from app.core import turn_manager

    seen = {}

    async def intake(body, project, api_user_id, pv2, *, creator=None, entry=False, **_):
        seen["entry"] = entry
        payload = {"api_user_id": api_user_id} if api_user_id else None
        await turn_manager.submit_async(SID, "worktree", prompt=body.prompt, profile_id="default",
                                        payload=payload, person_entered=entry)
        return None

    monkeypatch.setattr(sessions_worktree, "require_origin_access",
                        lambda **_: {"legacy": None, "project_v2": {"id": "pv2-fake"}})
    monkeypatch.setattr(sessions_worktree, "_create_v2_branch_session", intake)
    body = sessions_worktree.CreateWorktreeSessionRequest(
        project_v2_id="pv2-fake", branch_name="garden", prompt="Plan the beds")
    await sessions_worktree.create_session_with_worktree(_rest_request(who), body, token="t")
    assert seen["entry"] == (True if REST[who] is True else "preview" if who == "preview link" else False)
    row = _queued("worktree")
    _expect(row, REST[who])
    if who == "api user":
        # What else refuses an API user on a personal sign-in: the owner-only
        # subscription rule, from the row's own ``api_user_id``.
        from app.core import credential_gate

        options = SimpleNamespace(aih_provider_id="row-fake", aih_provider_type="antigravity",
                                  aih_provider_auth_mode="oauth-personal")
        assert (credential_gate.requester_refusal(options, row["payload"]["api_user_id"])
                == credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE)


async def test_an_api_user_on_the_socket_is_refused_by_the_owner_only_rule_too(chat):
    from app.core import credential_gate

    await _handler(_api_user()).handle_query({"prompt": "Plan the beds", "session_id": SID,
                                              "profile": "default", "project": None})
    row = _queued("user")
    _expect(row, ag.NOT_ENTERED_REASON)
    options = SimpleNamespace(aih_provider_id="row-fake", aih_provider_type="antigravity",
                              aih_provider_auth_mode="oauth-personal")
    assert (credential_gate.requester_refusal(options, row["payload"]["api_user_id"])
            == credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE)


@pytest.mark.parametrize("who", REST)
async def test_the_retry_route_states_who_pressed_it(rest, monkeypatch, who):
    from app.api import sessions

    async def session(request, session_id):
        return {**database.get_session(session_id), "sdk_session_id": "ses_fake"}

    monkeypatch.setattr(sessions, "require_session", session)
    await sessions.resume_session(_rest_request(who), SID)
    row = _queued("resume")
    assert (row["payload"].get(turns_db.PERSON_ENTERED_KEY) is True) is (REST[who] is True)
    assert bool(row["payload"].get(turns_db.PREVIEW_ENTRY_KEY)) is (who == "preview link")
