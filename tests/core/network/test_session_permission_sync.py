"""Only the worker that runs a chat sets its permission binding through sync (#1887).

The session catalog took any member's copy that won the clock, so a worker
that does not run a chat could raise (or lower) its ``permission_mode`` and
``permission_ceiling`` on the worker that runs it and on every mirror. The
columns now follow the ``provenance_owner`` rule (#1873): a copy from another
worker keeps the values each worker holds. Every copy here goes through the
real publish/apply path between in-process workers.
"""
import json

from app.db import database
from app.network import crypto, engine, handoff
from tests.core.network.harness import converge
from tests.core.network.test_mirrored_chat_image_access import (
    CHATS, _network, _publish, _publish_copy, _start_chats, _third,
)


def _binding(worker, session_id):
    with worker.active():
        row = database.get_session(session_id)
        return row["permission_mode"], row["permission_ceiling"]


def _owner_sets(owner, session_id, mode, ceiling):
    with owner.active():
        owner.sql(f"UPDATE sessions SET permission_mode = '{mode}', permission_ceiling = '{ceiling}' "
                  f"WHERE id = '{session_id}'")


def test_a_worker_that_does_not_run_the_chat_cannot_change_its_permissions(workers):
    viewer, owner = _network(workers)
    _start_chats(owner, {"member-chat": CHATS["member-chat"]})
    _owner_sets(owner, "member-chat", "default", "plan")
    _publish(owner, viewer)
    third = _third(viewer, workers)
    everyone = [viewer, owner, third]
    converge(everyone)
    for worker in everyone:
        assert _binding(worker, "member-chat") == ("default", "plan")

    # The third worker raises the chat to bypass, then tries to clear it.
    _publish_copy(third, "member-chat", permission_mode="bypassPermissions",
                  permission_ceiling="bypassPermissions", title="forged")
    converge(everyone)
    _publish_copy(third, "member-chat", permission_mode=None, permission_ceiling=None,
                  title="forged again")
    converge(everyone)
    for worker in (viewer, owner):
        with worker.active():
            # The copies applied (their titles landed); the binding did not,
            # on the mirror nor on the worker that runs the chat.
            assert database.get_session("member-chat")["title"] == "forged again"
        assert _binding(worker, "member-chat") == ("default", "plan")


def test_the_owner_still_changes_its_chats_permissions_everywhere(workers):
    viewer, owner = _network(workers)
    _start_chats(owner, {"member-chat": CHATS["member-chat"]})
    _publish(owner, viewer)
    third = _third(viewer, workers)
    everyone = [viewer, owner, third]
    converge(everyone)

    _owner_sets(owner, "member-chat", "acceptEdits", "default")
    with owner.active():
        engine.scan_all()
    converge(everyone)
    for worker in everyone:
        assert _binding(worker, "member-chat") == ("acceptEdits", "default")


def test_a_handoff_carries_the_binding_and_moves_the_authority(workers, monkeypatch):
    from tests.core.network.test_ownership_revocation import COMMIT, REMOTE, _stub_git_checkout

    viewer, owner = _network(workers)
    _start_chats(owner, {"member-chat": CHATS["member-chat"]})
    _owner_sets(owner, "member-chat", "acceptEdits", "plan")
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
    for worker in everyone:
        with worker.active():
            assert database.get_session("member-chat")["owner_device_id"] == target.device_id
        assert _binding(worker, "member-chat") == ("acceptEdits", "plan")

    # The new owner sets the binding; the old owner no longer can.
    _owner_sets(target, "member-chat", "default", "default")
    with target.active():
        engine.scan_all()
    converge(everyone)
    _publish_copy(owner, "member-chat", permission_mode="bypassPermissions",
                  permission_ceiling="bypassPermissions")
    converge(everyone)
    # (The old owner's own row holds its local edit; nobody else takes it.)
    for name, worker in (("viewer", viewer), ("new owner", target)):
        assert _binding(worker, "member-chat") == ("default", "default"), name
