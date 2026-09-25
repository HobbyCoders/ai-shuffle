"""Remote worktree creation — ``POST /sessions/with-worktree`` with a device
pick (D4).

The remote path must do NO local disk work: no ``ensure_checkout``, no upload
migration. Instead the session is born pinned to the target (D1), the checkout
work travels as a durable ``ensure_worktree`` git op (D5), any attachments
travel as ``upload_blobs`` (D8), and the first turn hands off ``no_kick`` +
claim-loop wake exactly like the WS remote path.

The preflight is the load-bearing half: a session pinned to a device that cannot
materialize the branch is stranded, so an unknown/offline target — or one with
no ACTIVE BASE checkout of the project — is refused BEFORE anything is created.
``capable_runners`` is deliberately not used there; it also matches branch-only
rows, which cannot be forked from.

Driven through ``_create_v2_branch_session`` directly (real ``database`` /
git_ops / turns on the pg_shim) rather than the HTTP layer, mirroring how
tests/api/test_ws_device_selection.py drives ``handle_query``.
"""

from tests.conftest import async_twin
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.sessions_worktree as swt
from app.api.sessions_helpers import CreateWorktreeSessionRequest
from app.core import placement as placement_mod
from app.core.config import settings
from app.db import checkouts as checkouts_db
from app.db import database, git_ops as git_ops_db
from app.db import turns as turns_db

ME = "local-me"
PEER = "peer-install-1"


@pytest.fixture(autouse=True)
def pin_runner(monkeypatch):
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: ME)
    )


@pytest.fixture
def project(mock_db, tmp_path):
    """A legacy project bound to a projects_v2 row, with an ACTIVE base checkout
    on PEER (the happy-path precondition)."""
    path = tmp_path / "repo"
    (path / "uploads").mkdir(parents=True)
    database.create_project(
        project_id="proj-1", name="Repo", description=None, path=str(path)
    )
    from app.core import project_resolver

    # Let the resolver mint (or find) the origin-keyed row exactly as the
    # endpoint will — a hand-rolled projects_v2 row would have a different
    # origin and the preflight would look at the wrong project.
    pv2 = project_resolver.resolve_project_v2({"project_id": "proj-1"})
    return {"legacy": database.get_project("proj-1"), "pv2": pv2, "path": path}


def _row(session_id: str):
    """The session row, asserted present. ``branch`` / ``project_v2_id`` are
    real columns the SessionRow TypedDict does not enumerate, so read them
    off a plain dict copy."""
    row = database.get_session(session_id)
    assert row is not None, f"session {session_id} vanished"
    return dict(row)


def _give_peer_a_base_checkout(project, runner=PEER, branch=None, status="active"):
    checkouts_db.upsert_checkout(
        project["pv2"]["id"],
        runner,
        str(project["path"]) if branch is None else str(project["path"] / branch),
        branch=branch,
        status=status,
    )


class _Patches:
    """Patch only the collaborators the endpoint reaches out to; keep database /
    git_ops / turns real."""

    def __init__(self, *, online=True, device_name="Studio"):
        self._online = online
        self._device_name = device_name

    def __enter__(self):
        self._stack = ExitStack()
        s = self._stack

        async def _run_sync(fn, *a, **k):
            return fn(*a, **k)

        s.enter_context(
            patch.object(swt, "run_sync", new=AsyncMock(side_effect=_run_sync))
        )
        self.get_install = s.enter_context(
            patch.object(
                database,
                "get_install",
                MagicMock(
                    return_value={
                        "id": PEER,
                        "device_name": self._device_name,
                        "online": self._online,
                    }
                ),
            )
        )
        # Local disk work must NOT happen on the remote path — these are the
        # witnesses.
        from app.core import checkout_manager

        self.ensure_checkout = s.enter_context(
            patch.object(
                checkout_manager,
                "ensure_checkout",
                MagicMock(side_effect=AssertionError("no local checkout on remote path")),
            )
        )
        self.migrate = s.enter_context(
            patch.object(
                swt,
                "_migrate_uploads_to_dir",
                MagicMock(side_effect=AssertionError("no local upload migration")),
            )
        )
        s.enter_context(patch.object(swt, "_broadcast_session_created", MagicMock()))
        s.enter_context(
            patch.object(
                swt, "_persist_and_broadcast_first_message", new=AsyncMock()
            )
        )
        from app.core import turn_claim

        self.notify = s.enter_context(
            patch.object(turn_claim, "notify_turns_changed", MagicMock())
        )
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False


def _body(**kw) -> CreateWorktreeSessionRequest:
    base: dict[str, Any] = {
        "project_id": "proj-1",
        "branch_name": "feat/x",
        "base_branch": "main",
        "create_new_branch": True,
        "target_instance_id": PEER,
    }
    base.update(kw)
    return CreateWorktreeSessionRequest(**base)


async def _create(project, body):
    return await swt._create_v2_branch_session(body, project["legacy"], None)


# ═══ preflight refusals (nothing created) ════════════════════════════════════


async def test_unknown_target_is_refused(project):
    from fastapi import HTTPException

    with _Patches() as p:
        p.get_install.return_value = None
        with pytest.raises(HTTPException) as exc:
            await _create(project, _body())

    assert exc.value.status_code == 409
    assert "Unknown device" in exc.value.detail
    assert database.get_sessions() == []
    assert git_ops_db.list_queued_for_runner(PEER) == []


async def test_offline_target_is_refused(project):
    from fastapi import HTTPException

    _give_peer_a_base_checkout(project)
    with _Patches(online=False):
        with pytest.raises(HTTPException) as exc:
            await _create(project, _body())

    assert exc.value.status_code == 409
    assert "Studio is offline" in exc.value.detail
    assert database.get_sessions() == []


async def test_target_without_a_base_checkout_is_refused(project):
    """No checkout row at all on the target."""
    from fastapi import HTTPException

    with _Patches():
        with pytest.raises(HTTPException) as exc:
            await _create(project, _body())

    assert exc.value.status_code == 409
    assert "isn't checked out on Studio" in exc.value.detail
    assert database.get_sessions() == []
    assert git_ops_db.list_queued_for_runner(PEER) == []


async def test_branch_only_checkout_on_target_is_not_a_base_checkout(project):
    """``capable_runners`` would accept this row; the base-checkout gate must
    not — Phase 3 never clones, so there is nothing to fork the new branch from."""
    from fastapi import HTTPException

    _give_peer_a_base_checkout(project, branch="some/other-branch")

    with _Patches():
        with pytest.raises(HTTPException) as exc:
            await _create(project, _body())

    assert exc.value.status_code == 409
    assert "isn't checked out on Studio" in exc.value.detail


async def test_a_base_checkout_on_a_DIFFERENT_runner_does_not_count(project):
    from fastapi import HTTPException

    _give_peer_a_base_checkout(project, runner=ME)

    with _Patches():
        with pytest.raises(HTTPException) as exc:
            await _create(project, _body())

    assert exc.value.status_code == 409


# ═══ success path ════════════════════════════════════════════════════════════


async def test_remote_create_pins_session_queues_op_and_does_no_local_work(project):
    _give_peer_a_base_checkout(project)

    with _Patches() as p:
        resp = await _create(project, _body())

    # Session is born OWNED BY and PINNED TO the target (D1).
    row = _row(resp.session_id)
    assert row["instance_id"] == PEER
    assert placement_mod._normalize_placement(row["placement"]) == {
        "where": f"runner:{PEER}"
    }
    assert row["branch"] == "feat/x"
    assert row["project_v2_id"] == project["pv2"]["id"]

    # The checkout work travels as a durable op carrying create_new_branch.
    queued = git_ops_db.list_queued_for_runner(PEER)
    assert len(queued) == 1
    assert queued[0]["op"] == "ensure_worktree"
    assert queued[0]["project_v2_id"] == project["pv2"]["id"]
    assert queued[0]["params"] == {
        "branch": "feat/x", "base_branch": "main", "create_new_branch": True,
    }

    # No local disk work, and no path is invented for the response.
    p.ensure_checkout.assert_not_called()
    p.migrate.assert_not_called()
    assert resp.worktree_path is None
    assert resp.branch_name == "feat/x"
    assert resp.base_branch == "main"
    assert resp.query_started is False  # no prompt in this body


async def test_first_turn_hands_off_no_kick_with_a_claim_wake(project):
    _give_peer_a_base_checkout(project)

    from app.core import turn_manager

    _submit = MagicMock(return_value="turn-1")
    with _Patches() as p, patch.object(
        turn_manager, "submit", _submit
    ) as submit, patch.object(
        turn_manager, "submit_async", async_twin(_submit)
    ):
        resp = await _create(project, _body(prompt="do the thing"))

    assert resp.query_started is True
    # Title exists at creation, before the (mocked) local first-message backfill.
    # The row sent to an independent owner must not contain a blank title.
    assert _row(resp.session_id)["title"] == "do the thing"
    # The durable queued row IS the hand-off; the wake is latency only.
    assert submit.call_args.kwargs["no_kick"] is True
    assert submit.call_args.kwargs["profile_id"] == "default"
    p.notify.assert_called_once()


async def test_remote_create_sends_title_in_owner_payload(project):
    from types import SimpleNamespace
    from app.network import control, engine, git as network_git

    _give_peer_a_base_checkout(project)
    with _Patches(), patch.object(
        engine, "context", return_value=SimpleNamespace(device_id="local-device")
    ), patch.object(
        network_git, "device_id", return_value="remote-device"
    ), patch.object(
        network_git, "enqueue", new=AsyncMock()
    ), patch.object(
        control, "call", new=AsyncMock()
    ) as create_owner, patch.object(
        control, "submit_turn", new=AsyncMock()
    ):
        resp = await _create(project, _body(prompt="Fix remote session titles"))

    device, action, payload = create_owner.call_args.args
    assert (device, action) == ("remote-device", "sessions")
    assert payload["session"]["id"] == resp.session_id
    assert payload["session"]["title"] == "Fix remote session titles"


async def test_the_no_profile_sentinel_never_rides_a_remote_turn(project):
    """'claude-code' is the install-LOCAL no-profile sentinel; sending it to
    another install would bind the turn to a profile row that may not exist
    there (the cluster profile-sentinel rule)."""
    _give_peer_a_base_checkout(project)
    database.create_profile(
        profile_id="claude-code", name="Claude Code", description=None, config={}
    )

    from app.core import turn_manager

    _submit = MagicMock(return_value="turn-1")
    with _Patches(), patch.object(
        turn_manager, "submit", _submit
    ) as submit, patch.object(
        turn_manager, "submit_async", async_twin(_submit)
    ):
        await _create(project, _body(prompt="hi", profile_id="claude-code"))

    assert submit.call_args.kwargs["profile_id"] is None


async def test_first_turn_is_durably_queued(project):
    _give_peer_a_base_checkout(project)

    with _Patches():
        resp = await _create(project, _body(prompt="do the thing"))

    queued_turns = turns_db.get_queued_turns(resp.session_id)
    assert len(queued_turns) == 1
    assert queued_turns[0]["prompt"] == "do the thing"
    assert queued_turns[0]["source"] == "worktree"


async def test_uploads_travel_as_blobs_attached_to_the_turn(project):
    _give_peer_a_base_checkout(project)
    staged = project["path"] / "uploads" / "spec.md"
    staged.write_bytes(b"# spec")

    with _Patches():
        resp = await _create(
            project,
            _body(prompt=f"read @{staged}", pending_upload_paths=[str(staged)]),
        )

    from app.db import upload_blobs as upload_blobs_db

    blobs = upload_blobs_db.list_blobs_for_session(resp.session_id)
    assert len(blobs) == 1
    assert blobs[0]["source_path"] == str(staged)
    assert upload_blobs_db.get_blob_data(blobs[0]["id"]) == b"# spec"

    turn = turns_db.get_queued_turns(resp.session_id)[0]
    assert turn["payload"]["upload_blob_ids"] == [blobs[0]["id"]]


async def test_promptless_create_does_not_orphan_upload_blobs(project):
    """Blobs are carried BY A TURN. The promptless compose flow POSTs first and
    sends its message over the WS afterwards, so lifting here would strand rows
    with nothing to attach them to — that follow-up query does the lifting."""
    _give_peer_a_base_checkout(project)
    staged = project["path"] / "uploads" / "spec.md"
    staged.write_bytes(b"# spec")

    with _Patches():
        resp = await _create(project, _body(pending_upload_paths=[str(staged)]))

    from app.db import upload_blobs as upload_blobs_db

    assert upload_blobs_db.list_blobs_for_session(resp.session_id) == []
    assert turns_db.get_queued_turns(resp.session_id) == []


async def test_a_bad_attachment_refuses_the_whole_create(project, tmp_path):
    """A path outside the project fails the request BEFORE a session exists —
    half-creating it would leave a session whose prompt names a missing file."""
    from fastapi import HTTPException

    _give_peer_a_base_checkout(project)
    outside = tmp_path / "elsewhere.txt"
    outside.write_bytes(b"nope")

    with _Patches():
        with pytest.raises(HTTPException) as exc:
            await _create(
                project, _body(prompt="hi", pending_upload_paths=[str(outside)])
            )

    assert exc.value.status_code == 400
    assert database.get_sessions() == []
    assert git_ops_db.list_queued_for_runner(PEER) == []


# ═══ the local path is untouched ═════════════════════════════════════════════


async def test_remote_create_by_project_v2_id_without_a_local_projects_row(
    mock_db, tmp_path
):
    """Only ``project_v2_id`` + a remote target queues the op. No local
    ``projects`` row, ``sessions.project_id`` is NULL."""
    from app.db import projects_v2

    path = tmp_path / "remote-repo"
    path.mkdir()
    pv2 = projects_v2.upsert_project("git:example/remote-only", name="Remote")
    checkouts_db.upsert_checkout(pv2["id"], PEER, str(path))

    body = _body(project_id=None, project_v2_id=pv2["id"])
    with _Patches():
        resp = await swt._create_v2_branch_session(body, None, None)

    row = _row(resp.session_id)
    assert row["project_id"] is None
    assert row["project_v2_id"] == pv2["id"]
    assert row["instance_id"] == PEER
    queued = git_ops_db.list_queued_for_runner(PEER)
    assert len(queued) == 1
    assert queued[0]["op"] == "ensure_worktree"
    assert queued[0]["project_v2_id"] == pv2["id"]
    assert resp.worktree_path is None


async def test_remote_v2_only_refuses_attachments(mock_db, tmp_path):
    """Attachments without a local legacy projects row are 400 — D8 is
    project-dir-relative; no v2-keyed staging dir this wave."""
    from fastapi import HTTPException
    from app.api.common import ATTACHMENTS_NEED_LOCAL_CHECKOUT
    from app.db import projects_v2

    path = tmp_path / "remote-repo"
    path.mkdir()
    pv2 = projects_v2.upsert_project("git:example/remote-only", name="Remote")
    checkouts_db.upsert_checkout(pv2["id"], PEER, str(path))
    staged = tmp_path / "spec.md"
    staged.write_bytes(b"# spec")

    body = _body(
        project_id=None,
        project_v2_id=pv2["id"],
        prompt="read this",
        pending_upload_paths=[str(staged)],
    )
    with _Patches():
        with pytest.raises(HTTPException) as exc:
            await swt._create_v2_branch_session(body, None, None)

    assert exc.value.status_code == 400
    assert exc.value.detail == ATTACHMENTS_NEED_LOCAL_CHECKOUT
    assert database.get_sessions() == []
    assert git_ops_db.list_queued_for_runner(PEER) == []


async def test_with_worktree_xor_neither_is_400(monkeypatch):
    from fastapi import HTTPException
    from app.api.common import XOR_PROJECT_IDS_DETAIL

    monkeypatch.setattr(swt, "get_api_user_from_request", lambda req: None)

    with pytest.raises(HTTPException) as exc:
        await swt.create_session_with_worktree(
            request=MagicMock(),
            body=_body(project_id=None),
            token="tok",
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == XOR_PROJECT_IDS_DETAIL


async def test_with_worktree_xor_both_is_400(project, monkeypatch):
    from fastapi import HTTPException
    from app.api.common import XOR_PROJECT_IDS_DETAIL

    monkeypatch.setattr(swt, "get_api_user_from_request", lambda req: None)

    with pytest.raises(HTTPException) as exc:
        await swt.create_session_with_worktree(
            request=MagicMock(),
            body=_body(project_v2_id=project["pv2"]["id"]),
            token="tok",
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == XOR_PROJECT_IDS_DETAIL


async def test_target_equal_to_self_takes_the_ordinary_local_path(project):
    """A pick of THIS install is not a remote target — today's behavior stands
    (local ensure_checkout runs, and a real path comes back)."""
    from app.core import checkout_manager

    async def _run_sync(fn, *a, **k):
        return fn(*a, **k)

    with ExitStack() as s:
        s.enter_context(
            patch.object(swt, "run_sync", new=AsyncMock(side_effect=_run_sync))
        )
        ensure = s.enter_context(
            patch.object(
                checkout_manager, "ensure_checkout",
                MagicMock(return_value=str(project["path"])),
            )
        )
        s.enter_context(patch.object(swt, "_broadcast_session_created", MagicMock()))
        s.enter_context(
            patch.object(swt, "_persist_and_broadcast_first_message", new=AsyncMock())
        )
        resp = await _create(project, _body(target_instance_id=ME))

    ensure.assert_called_once()
    assert resp.worktree_path == str(project["path"])
    assert _row(resp.session_id)["instance_id"] == ME
    assert git_ops_db.list_queued_for_runner(ME) == []
