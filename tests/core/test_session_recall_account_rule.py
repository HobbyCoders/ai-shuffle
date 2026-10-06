"""SessionSearch and SessionView follow the REST transcript rule (#1897).

The agent tools read past chats through the session store. Until #1897 the
store scoped only API keys' chats, so a chat a member's phone (or a direct
controller) started could search and open every chat on the worker, around
the account rule the REST and WebSocket transcript routes apply (#1877,
#1889). A chat now reads past chats only as the device that started it may
read them over REST (``artifact_access.paired_client_may_read``); the
owner's chats read everything, as before.
"""
from __future__ import annotations

import pytest

from app.core import artifact_access
from app.core import mobile_tool_identity as identity
from app.core.provider.fluid_pg_store import PgFluidStore
from app.db import database, mobile_tool_identity
from fluid.messages.content import Message, Text
from fluid.store.base import SessionNode
from fluid.tools.builtins import session_search, session_view
from fluid.tools.registry import ToolCtx

MEMBER = "account-member"
OWNER = "account-owner"

#: paired client id -> its account (None: paired directly, no account)
PHONES = {"member-phone": MEMBER, "direct-phone": None, "owner-phone": OWNER}

#: app id -> (store id, provenance owner, kind, principal id, account id)
CHATS = {
    "owner-chat": ("ses_owner", "install-admin", "admin", "install", None),
    "member-chat": ("ses_member", "account:" + MEMBER, "paired", "member-phone", MEMBER),
    "member-other": ("ses_member_other", "account:" + MEMBER, "paired", "member-phone", MEMBER),
    "direct-chat": ("ses_direct", "paired:direct-phone", "paired", "direct-phone", None),
    "owner-phone-chat": ("ses_owner_phone", "account:" + OWNER, "paired", "owner-phone", OWNER),
    "legacy-chat": ("ses_legacy", None, None, None, None),
}


def _node(node_id, text):
    return SessionNode(node_id=node_id, parent_id=None,
                       message=Message(role="user", content=[Text(text=text)]))


@pytest.fixture
async def store(mock_db, monkeypatch):
    store = PgFluidStore()
    for app_id, (sdk_id, owner, kind, principal_id, account_id) in CHATS.items():
        database.create_session(session_id=app_id, profile_id="default", title=app_id)
        mock_db.execute("UPDATE sessions SET sdk_session_id = ? WHERE id = ?", (sdk_id, app_id))
        mock_db.commit()
        if owner:
            mobile_tool_identity.bind(app_id, dict(
                owner=owner, kind=kind, principal_id=principal_id, account_id=account_id,
                credential_hash="fixture"), new=True)
        await store.create_session(sdk_id)
        await store.append(sdk_id, _node("n1", f"the secret zebrafish notes of {app_id}"))
    # A subagent of the member's chat: no record of its own.
    database.create_session(session_id="member-worker", profile_id="default", title="worker",
                            is_worker=True, parent_session_id="member-chat")
    mock_db.execute("UPDATE sessions SET sdk_session_id = 'ses_member_worker' WHERE id = 'member-worker'")
    mock_db.commit()

    def ownership(device_id=None, *, paired_client_id=None):
        account = PHONES.get(paired_client_id)
        return {"account_id": account} if account else None

    monkeypatch.setattr(artifact_access.mobile_hub, "ownership", ownership)
    monkeypatch.setattr(identity.mobile_hub, "ownership", ownership)
    monkeypatch.setattr(identity.pairing, "get_paired_client",
                        lambda cid: {"id": cid} if cid in PHONES else None)
    # Only the owner's own account phone holds this install's owner authority.
    monkeypatch.setattr(identity.account_hub, "check_paired_client", lambda c: c["id"] == "owner-phone")
    return store


#: What each chat's recall tools may read: the REST answers for its device.
READABLE = {
    "ses_owner": {"ses_owner", "ses_member", "ses_member_other", "ses_direct",
                  "ses_owner_phone", "ses_legacy"},
    "ses_legacy": {"ses_owner", "ses_member", "ses_member_other", "ses_direct",
                   "ses_owner_phone", "ses_legacy"},
    "ses_member": {"ses_member", "ses_member_other"},
    "ses_member_worker": {"ses_member", "ses_member_other"},
    "ses_direct": {"ses_direct"},
    "ses_owner_phone": {"ses_owner", "ses_owner_phone", "ses_legacy"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("viewer", sorted(READABLE))
async def test_search_returns_only_what_the_viewer_may_read(store, viewer):
    hits = await store.search(["zebrafish"], limit=25, exclude_session_id=viewer)
    assert {h.session_id for h in hits} == READABLE[viewer] - {viewer}


@pytest.mark.asyncio
@pytest.mark.parametrize("viewer", sorted(READABLE))
async def test_listing_returns_only_what_the_viewer_may_read(store, viewer):
    rows = await store.recent_sessions(limit=25, exclude_session_id=viewer)
    # The listing reads session rows: the member chat's subagent (no
    # transcript, so never a search hit) is listed wherever its parent is.
    expected = set(READABLE[viewer])
    if "ses_member" in expected:
        expected.add("ses_member_worker")
    assert {r.session_id for r in rows} == expected - {viewer}


@pytest.mark.asyncio
@pytest.mark.parametrize("viewer", sorted(READABLE))
async def test_view_opens_only_what_the_viewer_may_read(store, viewer):
    for target, *_rest in CHATS.values():
        window = await store.session_slice(target, viewer_session_id=viewer)
        if target in READABLE[viewer]:
            assert window is not None and window.total == 1, (viewer, target)
        else:
            assert window is None, (viewer, target)


@pytest.mark.asyncio
async def test_viewer_is_named_by_app_id_too(store):
    """A chat whose store id is not recorded yet passes its app id."""
    assert await store.session_slice("ses_owner", viewer_session_id="member-chat") is None
    assert await store.session_slice("ses_member_other", viewer_session_id="member-chat") is not None


def _ctx(store, viewer):
    ctx = ToolCtx(cwd=".", session_id=viewer)
    ctx.extra.update(session_search=store.search, session_recent=store.recent_sessions,
                     session_view=store.session_slice)
    return ctx


@pytest.mark.asyncio
async def test_member_chat_tools_cannot_read_the_owners_chat(store):
    """The tools themselves, unchanged: an owner chat id reads as not found."""
    ctx = _ctx(store, "ses_member")
    found = await session_search.handler({"query": "zebrafish", "scope": "all"}, ctx)
    assert "ses_member_other" in found.text
    assert "ses_owner" not in found.text and "ses_direct" not in found.text

    listed = await session_search.handler({"scope": "all"}, ctx)
    assert "ses_member_other" in listed.text and "ses_owner" not in listed.text

    viewed = await session_view.handler({"session_id": "ses_owner"}, ctx)
    assert viewed.is_error and "no stored session" in viewed.text
    assert "secret" not in viewed.text


@pytest.mark.asyncio
async def test_owner_chat_tools_still_read_every_chat(store):
    ctx = _ctx(store, "ses_owner")
    found = await session_search.handler({"query": "zebrafish", "scope": "all", "limit": 25}, ctx)
    for sid in ("ses_member", "ses_direct", "ses_owner_phone", "ses_legacy"):
        assert sid in found.text
    viewed = await session_view.handler({"session_id": "ses_member"}, ctx)
    assert not viewed.is_error and "secret zebrafish notes of member-chat" in viewed.text


@pytest.mark.asyncio
async def test_a_member_reader_without_any_account_lookup_fails_closed(store, monkeypatch):
    """An account check that errors gives the device no owner authority."""
    def broken(client):
        raise RuntimeError("hub down")

    monkeypatch.setattr(identity.account_hub, "check_paired_client", broken)
    rows = await store.recent_sessions(limit=25, exclude_session_id="ses_owner_phone")
    assert {r.session_id for r in rows} == set()
