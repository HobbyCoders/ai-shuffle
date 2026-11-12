"""Tool images of a chat another worker runs keep their account rule (#1849).

Who a chat belongs to is its ``mobile_tool_identity`` record, which stays on
the worker that runs the chat. The session row now carries the record's
owner (``sessions.provenance_owner``) through the catalog, so the worker
that only mirrors the chat applies the same account check to its images.

Every mirror here is produced by the real publish/apply path between two
in-process workers; the viewer never gets a hand-written local record.
"""
import asyncio
import dataclasses
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.artifact_access import authorize_session_artifact
from app.db import database, mobile_tool_identity
from app.db.base import get_db
from app.network import engine, policy
from tests.core.network.harness import converge, join

OWNER_ACCOUNT = "account-owner"
MEMBER_ACCOUNT = "account-member"

#: chat id -> the principal that started it on the owner worker
CHATS = {
    "member-chat": dict(owner="account:" + MEMBER_ACCOUNT, kind="paired", principal_id="pcl_member_phone_on_a",
                        account_id=MEMBER_ACCOUNT, credential_hash="member-hash"),
    "owner-phone-chat": dict(owner="account:" + OWNER_ACCOUNT, kind="paired", principal_id="pcl_owner_phone_on_a",
                             account_id=OWNER_ACCOUNT, credential_hash="owner-phone-hash"),
    "desktop-chat": dict(owner="install-admin", kind="admin", principal_id="install", account_id=None,
                         credential_hash="desktop-hash"),
}

#: The viewer's own callers: paired client id -> (account, owner authority)
DEVICES = {
    "owner-phone": (OWNER_ACCOUNT, True),
    "member-phone": (MEMBER_ACCOUNT, False),
    # A controller paired directly (no account): never the owner's authority.
    "direct-phone": (None, False),
    # The phones that started the chats, paired with the worker that runs them.
    "pcl_owner_phone_on_a": (OWNER_ACCOUNT, True),
    "pcl_member_phone_on_a": (MEMBER_ACCOUNT, False),
}


def _network(workers):
    """DesktopWorker views chats that ServerWorker runs."""
    viewer, owner = workers("desktopworker"), workers("unraid")
    for worker in (viewer, owner):
        with worker.active():
            mobile_tool_identity.init_schema()
    with viewer.active():
        engine.create_network("Home", device_name="DesktopWorker")
    join(viewer, owner)
    return viewer, owner


def _start_chats(owner, chats=CHATS):
    with owner.active():
        for sid, principal in chats.items():
            database.create_session(session_id=sid, profile_id="default", project_id=None,
                                    title=sid, is_active=True)
            mobile_tool_identity.bind(sid, principal, new=True)
        database.create_session(session_id="legacy-chat", profile_id="default", project_id=None,
                                title="No record", is_active=True)


def _publish(owner, viewer):
    with owner.active():
        engine.scan_all()
    converge([viewer, owner])


@pytest.fixture
def callers(monkeypatch):
    """The viewer's admin login, the owner's phone and an account member's phone."""
    from app.core import account_hub, preference_identity
    from app.db import mobile_hub, pairing

    def ownership(device_id=None, *, paired_client_id=None):
        account = DEVICES.get(paired_client_id, (None, None))[0]
        return dict(account_id=account, paired_client_id=paired_client_id, device_id="dev-" + paired_client_id,
                    hub_device_id="hub-" + paired_client_id, server_id="server") if account else None

    monkeypatch.setattr(mobile_hub, "ownership", ownership)
    monkeypatch.setattr(pairing, "get_paired_client", lambda cid: {"id": cid} if cid in DEVICES else None)
    monkeypatch.setattr(account_hub, "check_paired_client", lambda client: DEVICES[client["id"]][1])
    monkeypatch.setattr(preference_identity, "local_owner_account", lambda: OWNER_ACCOUNT)

    def phone(client_id):
        return SimpleNamespace(state=SimpleNamespace(api_user=None, is_admin=DEVICES[client_id][1],
            paired_client={"id": client_id, "account_owner_admin": DEVICES[client_id][1]}))

    return {
        "admin": SimpleNamespace(state=SimpleNamespace(api_user=None, paired_client=None, is_admin=True)),
        "owner-phone": phone("owner-phone"),
        "member-phone": phone("member-phone"),
    }


def _allowed(request, session_id):
    try:
        authorize_session_artifact(request, {"created_by_session": session_id}, "image-hash")
        return True
    except HTTPException as exc:
        assert exc.status_code == 404
        return False


def _matrix(worker, callers, chats):
    with worker.active():
        return {(who, sid): _allowed(request, sid) for who, request in callers.items() for sid in chats}


def test_the_account_record_travels_with_the_session_catalog():
    coll = policy.collection("sessions")
    assert "provenance_owner" in coll.fields
    assert "provenance_owner" not in coll.local_fields


def test_mirror_applies_the_account_rule_of_the_worker_that_runs_the_chat(workers, callers):
    viewer, owner = _network(workers)
    _start_chats(owner)
    _publish(owner, viewer)
    with viewer.active():
        for sid, principal in CHATS.items():
            row = database.get_session(sid)
            assert row["owner_device_id"] == owner.device_id
            assert row["provenance_owner"] == principal["owner"]
            # A real mirror: no local record was written on the viewer.
            assert mobile_tool_identity.read(sid) is None

    chats = [*CHATS, "legacy-chat"]
    expected = {
        # The admin login sees the owner's account and install chats, not a member's.
        ("admin", "member-chat"): False,
        ("admin", "owner-phone-chat"): True,
        ("admin", "desktop-chat"): True,
        ("admin", "legacy-chat"): True,
        # The owner's phone: its account's chats and the owner's desktop chats.
        ("owner-phone", "member-chat"): False,
        ("owner-phone", "owner-phone-chat"): True,
        ("owner-phone", "desktop-chat"): True,
        ("owner-phone", "legacy-chat"): True,
        # A member's phone sees only its own account's chats.
        ("member-phone", "member-chat"): True,
        ("member-phone", "owner-phone-chat"): False,
        ("member-phone", "desktop-chat"): False,
        ("member-phone", "legacy-chat"): False,
    }
    assert _matrix(viewer, callers, chats) == expected
    # The worker that runs the chats decides the same way from its local records.
    assert _matrix(owner, {"admin": callers["admin"]}, chats) == {
        k: v for k, v in expected.items() if k[0] == "admin"}


def test_owner_on_an_older_build_leaves_the_owners_images_and_opens_nothing(workers, callers, monkeypatch):
    """An owner build that predates the field publishes session rows without it."""
    viewer, owner = _network(workers)
    _start_chats(owner)
    coll = policy.collection("sessions")
    old = dataclasses.replace(coll, fields=tuple(f for f in coll.fields if f != "provenance_owner"))
    with monkeypatch.context() as old_build:
        old_build.setitem(policy.COLLECTIONS, "sessions", old)
        with owner.active():
            engine.scan_all()
    converge([viewer, owner])
    with viewer.active():
        assert database.get_session("member-chat")["provenance_owner"] is None
        assert mobile_tool_identity.read("member-chat") is None
    # Unknown account: the owner group keeps what it had (the owner's own
    # images still load), nobody else is admitted.
    assert _matrix(viewer, callers, ["member-chat", "owner-phone-chat", "desktop-chat"]) == {
        (who, sid): who != "member-phone"
        for who in callers for sid in ["member-chat", "owner-phone-chat", "desktop-chat"]}

    # Once the owner is updated its next pass carries the record.
    _publish(owner, viewer)
    with viewer.active():
        assert database.get_session("member-chat")["provenance_owner"] == "account:" + MEMBER_ACCOUNT
    assert not _matrix(viewer, {"admin": callers["admin"]}, ["member-chat"])[("admin", "member-chat")]


def test_the_owner_corrects_its_own_label_and_mirrors_follow(workers, callers):
    """The worker that runs the chat is the authority on its label (review of #1873).

    A wrong label is not permanent: the owning worker's next copy replaces it
    on every mirror, and a copy that names none clears it there too.
    """
    viewer, owner = _network(workers)
    _start_chats(owner, {"member-chat": CHATS["member-chat"]})
    _publish(owner, viewer)
    assert not _matrix(viewer, {"admin": callers["admin"]}, ["member-chat"])[("admin", "member-chat")]
    with owner.active():
        owner.sql("UPDATE sessions SET provenance_owner = 'install-admin', title = 'again' WHERE id = 'member-chat'")
    _publish(owner, viewer)
    with viewer.active():
        assert database.get_session("member-chat")["title"] == "again"
        assert database.get_session("member-chat")["provenance_owner"] == "install-admin"
    assert _matrix(viewer, {"admin": callers["admin"]}, ["member-chat"])[("admin", "member-chat")]
    with owner.active():
        owner.sql("UPDATE sessions SET provenance_owner = NULL, title = 'renamed' WHERE id = 'member-chat'")
    _publish(owner, viewer)
    with viewer.active():
        assert database.get_session("member-chat")["title"] == "renamed"
        assert database.get_session("member-chat")["provenance_owner"] is None


ADMIN = dict(owner="install-admin", kind="admin", principal_id="install", account_id=None,
             credential_hash="admin-hash")


def _third(viewer, workers, name="laptop"):
    """Another member worker, bootstrapped from the viewer's snapshot."""
    third = workers(name)
    with third.active():
        mobile_tool_identity.init_schema()
    join(viewer, third)
    return third


def _publish_copy(worker, session_id, **changes):
    """``worker`` publishes its copy of a chat it does not run, edited first."""
    with worker.active():
        with get_db() as conn:
            cursor = conn.cursor()
            for column, value in changes.items():
                cursor.execute(f"UPDATE sessions SET {column} = %s WHERE id = %s", (value, session_id))
            cursor.execute("SELECT * FROM sessions WHERE id = %s", (session_id,))
            assert engine.record(cursor, "sessions", session_id, dict(cursor.fetchone()), force=True) is not None


def test_a_worker_that_does_not_run_the_chat_cannot_label_it(workers, callers):
    """A third member's copy never sets, changes or clears the label (review of #1873)."""
    viewer, owner = _network(workers)
    _start_chats(owner, {"member-chat": CHATS["member-chat"]})
    _publish(owner, viewer)
    third = _third(viewer, workers)
    everyone = [viewer, owner, third]
    converge(everyone)
    with third.active():
        # The snapshot a mirror served carries the owner's label.
        assert database.get_session("member-chat")["provenance_owner"] == "account:" + MEMBER_ACCOUNT
        assert database.get_session("legacy-chat")["owner_device_id"] == owner.device_id

    # The legacy chat has no label anywhere; the third worker names one, and
    # tries to replace and then erase the member chat's.
    _publish_copy(third, "legacy-chat", provenance_owner="account:" + MEMBER_ACCOUNT, title="forged")
    _publish_copy(third, "member-chat", provenance_owner="install-admin", title="forged")
    converge(everyone)
    _publish_copy(third, "member-chat", provenance_owner=None, title="forged again")
    converge(everyone)
    for worker in (viewer, owner):
        with worker.active():
            # The copies applied (their titles landed); their labels did not,
            # on the mirror nor on the worker that runs the chat.
            assert database.get_session("legacy-chat")["title"] == "forged"
            assert database.get_session("member-chat")["title"] == "forged again"
            assert database.get_session("legacy-chat")["provenance_owner"] is None
            assert database.get_session("member-chat")["provenance_owner"] == "account:" + MEMBER_ACCOUNT
    expected = {("admin", "legacy-chat"): True, ("admin", "member-chat"): False,
                ("member-phone", "legacy-chat"): False, ("member-phone", "member-chat"): True}
    for worker in (viewer, owner):
        assert _matrix(worker, {k: callers[k] for k in ("admin", "member-phone")},
                       ["legacy-chat", "member-chat"]) == expected

    # The worker that runs the legacy chat labels it (an admin claim): every
    # mirror takes that, including the one that tried to label it first.
    with owner.active():
        mobile_tool_identity.bind("legacy-chat", ADMIN, claim=True)
        engine.scan_all()
    converge(everyone)
    for worker in everyone:
        with worker.active():
            assert database.get_session("legacy-chat")["provenance_owner"] == "install-admin"


def test_after_a_handoff_an_admin_claim_relabels_the_chat_everywhere(workers, callers, monkeypatch):
    """The new owner's label follows the record it decides from (review of #1873)."""
    import json

    from app.network import crypto, handoff
    from tests.core.network.test_ownership_revocation import COMMIT, REMOTE, _stub_git_checkout

    viewer, owner = _network(workers)
    _start_chats(owner, {"member-chat": CHATS["member-chat"]})
    _publish(owner, viewer)
    target = _third(viewer, workers, "target")
    everyone = [viewer, owner, target]
    converge(everyone)
    _stub_git_checkout(monkeypatch)
    with owner.active():
        reserved = handoff.begin("member-chat", target.device_id, REMOTE)
    with target.active():
        key = engine.context().keyring[reserved["key_version"]]
        token = json.loads(crypto.decrypt_payload(
            key, reserved["payload"], handoff._aad("member-chat", target.device_id)))["token"]
    with owner.active():
        assert handoff.finish("member-chat", target.device_id,
                              {"phase": "commit", "token": token, **COMMIT}) == {"committed": True}
    with target.active():
        handoff._accept_owner("member-chat", target.device_id, COMMIT["branch"])
    converge(everyone)

    admin = {"admin": callers["admin"]}
    for worker in everyone:
        with worker.active():
            assert database.get_session("member-chat")["owner_device_id"] == target.device_id
            assert database.get_session("member-chat")["provenance_owner"] == "account:" + MEMBER_ACCOUNT
        assert _matrix(worker, admin, ["member-chat"]) == {("admin", "member-chat"): False}

    # The record stayed on the old owner, so the new owner has none and the
    # install admin may claim the chat there.
    with target.active():
        assert mobile_tool_identity.read("member-chat") is None
        mobile_tool_identity.bind("member-chat", ADMIN, claim=True)
        assert database.get_session("member-chat")["provenance_owner"] == "install-admin"
        engine.scan_all()
    converge(everyone)
    for worker in everyone:
        with worker.active():
            assert database.get_session("member-chat")["provenance_owner"] == "install-admin"
        # The old owner still holds its own record; the running worker's label wins there.
        assert _matrix(worker, admin, ["member-chat"]) == {("admin", "member-chat"): True}


#: The chat socket's principal for each caller (``identity.principal``).
SOCKETS = {
    "admin": ADMIN,
    "owner-phone": dict(owner="account:" + OWNER_ACCOUNT, kind="paired", principal_id="owner-phone",
                        account_id=OWNER_ACCOUNT, credential_hash="owner-phone-socket"),
    "member-phone": dict(owner="account:" + MEMBER_ACCOUNT, kind="paired", principal_id="member-phone",
                         account_id=MEMBER_ACCOUNT, credential_hash="member-phone-socket"),
    "direct-phone": dict(owner="paired:direct-phone", kind="paired", principal_id="direct-phone",
                         account_id=None, credential_hash="direct-phone-socket"),
}

#: A chat a directly paired controller started on the worker that runs it.
DIRECT_CHAT = {"direct-chat": dict(owner="paired:direct-phone", kind="paired", principal_id="direct-phone",
                                   account_id=None, credential_hash="direct-hash")}

PHONES = ("owner-phone", "member-phone", "direct-phone")


def _direct_request():
    return SimpleNamespace(state=SimpleNamespace(api_user=None, is_admin=False, paired_client={
        "id": "direct-phone", "account_owner_admin": False}))


def _rest_allowed(request, session_id):
    """The REST transcript routes' check (``GET /sessions/{id}`` and the rest)."""
    from app.api.sessions_helpers import check_transcript_access
    try:
        asyncio.run(check_transcript_access(request, database.get_session(session_id)))
        return True
    except HTTPException as exc:
        assert exc.status_code == 403
        return False


def _socket_allowed(who, session_id):
    """The chat socket's ``load_session``/``query`` check after the live
    credential check (``bind_session`` -> ``store.bind``)."""
    try:
        mobile_tool_identity.bind(session_id, SOCKETS[who])
        return True
    except PermissionError:
        return False


def _transcript_matrix(worker, requests, chats):
    """(who, chat) -> (images, REST transcript, chat socket) on ``worker``.

    The admin login's REST reads are not account-checked (owner access across
    workers), so its REST column is None.
    """
    result = {}
    with worker.active():
        for who, request in requests.items():
            for sid in chats:
                rest = None if who == "admin" else _rest_allowed(request, sid)
                result[(who, sid)] = (_allowed(request, sid), rest, _socket_allowed(who, sid))
    return result


def _agree(matrix):
    """Every caller gets one answer for a chat: images, REST and socket."""
    for key, (images, rest, socket) in matrix.items():
        assert socket == images and rest in (None, images), (key, matrix[key])
    return {key: answers[0] for key, answers in matrix.items()}


def test_transcript_follows_the_image_rule_on_the_running_worker_and_the_mirror(workers, callers):
    """A member's phone and a direct controller: one answer for images and transcript (#1877)."""
    viewer, owner = _network(workers)
    _start_chats(owner, {**CHATS, **DIRECT_CHAT})
    _publish(owner, viewer)
    requests = {**callers, "direct-phone": _direct_request()}
    chats = [*CHATS, *DIRECT_CHAT, "legacy-chat"]
    expected = {
        # Another member's phone reads only its own account's chats.
        **{("member-phone", sid): sid == "member-chat" for sid in chats},
        # A direct controller reads only the chats it started.
        **{("direct-phone", sid): sid == "direct-chat" for sid in chats},
        # The owner's phone: its account's chats and the owner's install chats.
        **{("owner-phone", sid): sid in ("owner-phone-chat", "desktop-chat", "legacy-chat") for sid in chats},
    }
    for worker in (owner, viewer):
        with worker.active():
            if worker is viewer:
                for sid in chats:  # A real mirror: no local record here.
                    assert mobile_tool_identity.read(sid) is None
        answers = _agree(_transcript_matrix(worker, {k: requests[k] for k in PHONES}, chats))
        assert answers == expected, worker.name
        # The admin login: the socket answers as the images do. (A direct
        # controller's chat is the admin's REST/image vs socket asymmetry
        # left open in #1877, so it is not compared here.)
        admin = _agree(_transcript_matrix(worker, {"admin": callers["admin"]}, [*CHATS, "legacy-chat"]))
        assert admin == {("admin", "member-chat"): False, ("admin", "owner-phone-chat"): True,
                         ("admin", "desktop-chat"): True, ("admin", "legacy-chat"): True}, worker.name
    with viewer.active():
        # Deciding on the mirror wrote nothing there.
        for sid in chats:
            assert mobile_tool_identity.read(sid) is None


def test_transcript_with_an_owner_on_an_older_build(workers, callers, monkeypatch):
    """No label on the mirror: the owner group keeps the transcript, nobody else gets it."""
    viewer, owner = _network(workers)
    _start_chats(owner)
    coll = policy.collection("sessions")
    old = dataclasses.replace(coll, fields=tuple(f for f in coll.fields if f != "provenance_owner"))
    with monkeypatch.context() as old_build:
        old_build.setitem(policy.COLLECTIONS, "sessions", old)
        with owner.active():
            engine.scan_all()
    converge([viewer, owner])
    with viewer.active():
        assert database.get_session("member-chat")["provenance_owner"] is None
    requests = {**callers, "direct-phone": _direct_request()}
    chats = ["member-chat", "owner-phone-chat", "desktop-chat"]
    answers = _agree(_transcript_matrix(viewer, requests, chats))
    assert answers == {(who, sid): who in ("admin", "owner-phone") for who in requests for sid in chats}


def test_after_a_handoff_the_transcript_follows_the_running_workers_label(workers, callers, monkeypatch):
    """The new owner has the label but no record; every worker answers as for images."""
    import json

    from app.network import crypto, handoff
    from tests.core.network.test_ownership_revocation import COMMIT, REMOTE, _stub_git_checkout

    viewer, owner = _network(workers)
    _start_chats(owner, {"member-chat": CHATS["member-chat"]})
    _publish(owner, viewer)
    target = _third(viewer, workers, "target")
    everyone = [viewer, owner, target]
    converge(everyone)
    _stub_git_checkout(monkeypatch)
    with owner.active():
        reserved = handoff.begin("member-chat", target.device_id, REMOTE)
    with target.active():
        key = engine.context().keyring[reserved["key_version"]]
        token = json.loads(crypto.decrypt_payload(
            key, reserved["payload"], handoff._aad("member-chat", target.device_id)))["token"]
    with owner.active():
        handoff.finish("member-chat", target.device_id, {"phase": "commit", "token": token, **COMMIT})
    with target.active():
        handoff._accept_owner("member-chat", target.device_id, COMMIT["branch"])
    converge(everyone)

    requests = {**callers, "direct-phone": _direct_request()}
    for worker in everyone:
        answers = _agree(_transcript_matrix(worker, requests, ["member-chat"]))
        assert answers == {(who, "member-chat"): who == "member-phone" for who in requests}, worker.name
    with target.active():
        # Continuing the chat on the worker that now runs it took no record.
        assert mobile_tool_identity.read("member-chat") is None
        # The admin claim (#1873) still relabels it there.
        mobile_tool_identity.bind("member-chat", ADMIN, claim=True)
        engine.scan_all()
    converge(everyone)
    for worker in everyone:
        answers = _agree(_transcript_matrix(worker, {"admin": callers["admin"]}, ["member-chat"]))
        assert answers == {("admin", "member-chat"): True}, worker.name


def test_a_claim_needs_the_install_admin_and_a_chat_with_no_record(workers):
    """Nobody but the install admin relabels a chat through a claim."""
    (worker,) = [workers("solo")]
    with worker.active():
        mobile_tool_identity.init_schema()
        database.create_session(session_id="legacy", profile_id="default", project_id=None, title="legacy")
        with pytest.raises(PermissionError):
            mobile_tool_identity.bind("legacy", CHATS["member-chat"], claim=True)
        assert database.get_session("legacy")["provenance_owner"] is None
        database.create_session(session_id="owned", profile_id="default", project_id=None, title="owned")
        mobile_tool_identity.bind("owned", CHATS["member-chat"], new=True)
        # A recorded chat: another principal is refused, the admin's claim
        # changes nothing.
        with pytest.raises(PermissionError):
            mobile_tool_identity.bind("owned", CHATS["owner-phone-chat"], claim=True)
        with pytest.raises(PermissionError):
            mobile_tool_identity.bind("owned", ADMIN, claim=True)
        assert database.get_session("owned")["provenance_owner"] == "account:" + MEMBER_ACCOUNT
        assert mobile_tool_identity.read("owned")["owner"] == "account:" + MEMBER_ACCOUNT


def test_binding_the_record_stamps_the_session_once(workers):
    (worker,) = [workers("solo")]
    with worker.active():
        mobile_tool_identity.init_schema()
        database.create_session(session_id="s1", profile_id="default", project_id=None, title="s1")
        assert database.get_session("s1")["provenance_owner"] is None
        mobile_tool_identity.bind("s1", CHATS["desktop-chat"], new=True)
        assert database.get_session("s1")["provenance_owner"] == "install-admin"
        # A continuation by the same owner re-binds without changing it.
        mobile_tool_identity.bind("s1", CHATS["desktop-chat"])
        assert database.get_session("s1")["provenance_owner"] == "install-admin"
        assert mobile_tool_identity.mirrored(database.get_session("s1"))["kind"] == "admin"


@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), ("account:", None), ("someone-else", None),
    ("install-admin", ("admin", None)), ("account:acct-1", ("paired", "acct-1")),
    ("paired:pcl_x", ("paired", None)), ("api_user:key", ("api_user", None)),
])
def test_mirrored_record_shape(value, expected):
    record = mobile_tool_identity.mirrored({"id": "s", "provenance_owner": value})
    assert (None if record is None else (record["kind"], record["account_id"])) == expected
    if record is not None:
        assert record["principal_id"] is None and record["credential_hash"] is None


def test_after_a_handoff_the_owner_keeps_its_own_chats_everywhere(workers, callers, monkeypatch):
    """The owner's phone and admin keep the owner's chats on every worker after a move (#1877)."""
    import json

    from app.network import crypto, handoff
    from tests.core.network.test_ownership_revocation import COMMIT, REMOTE, _stub_git_checkout

    viewer, owner = _network(workers)
    mine = {sid: CHATS[sid] for sid in ("owner-phone-chat", "desktop-chat")}
    _start_chats(owner, mine)
    _publish(owner, viewer)
    target = _third(viewer, workers, "target")
    everyone = [viewer, owner, target]
    converge(everyone)
    _stub_git_checkout(monkeypatch)
    for sid in mine:
        with owner.active():
            reserved = handoff.begin(sid, target.device_id, REMOTE)
        with target.active():
            key = engine.context().keyring[reserved["key_version"]]
            token = json.loads(crypto.decrypt_payload(
                key, reserved["payload"], handoff._aad(sid, target.device_id)))["token"]
        with owner.active():
            handoff.finish(sid, target.device_id, {"phase": "commit", "token": token, **COMMIT})
        with target.active():
            handoff._accept_owner(sid, target.device_id, COMMIT["branch"])
    converge(everyone)

    requests = {**callers, "direct-phone": _direct_request()}
    for worker in everyone:
        with worker.active():
            for sid in mine:
                assert database.get_session(sid)["owner_device_id"] == target.device_id, worker.name
        answers = _agree(_transcript_matrix(worker, requests, list(mine)))
        assert answers == {(who, sid): who in ("admin", "owner-phone") for who in requests for sid in mine}, \
            worker.name
