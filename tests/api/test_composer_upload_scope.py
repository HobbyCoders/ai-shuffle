"""Attachments reach the agent wherever the composer staged them.

Two owner-reported failures, and the rule behind both:

* A chat with no project, run by another worker, refused every attachment
  ("Attachments need a project session."): the lift only knew a project's
  folder or the session's own staging area, while the composer stages a
  no-project chat's files in the workspace's ``chat-uploads``.
* The composer stages a file by its state when the file is attached. When the
  owner then picks a project, turns worktrees on or picks another worker, the
  new session's own areas no longer contain the file: a remote create answered
  "Attachment path is outside this session's upload area.", and a same-worker
  create dropped the attachment silently.

The rule: a send lifts (remote) or copies in (same worker) files from exactly
the areas the session admits, plus, for the install owner's own send only
(``session_modes.composer_owner``, not every paired device), the areas
the owner's composer stages into. Each file is checked against the one root
that contains it, with links resolved, never a wider folder.
"""

import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core import upload_transfer
from app.core.session_modes import chat_uploads_dir
from app.db import database
from app.db import upload_blobs as upload_blobs_db

from tests.api.test_worktree_remote_target import (  # noqa: F401  (pin_runner is autouse there)
    PEER, _body, _give_peer_a_base_checkout, _Patches, pin_runner,
)
from app.api import sessions_worktree as swt
from app.db import turns as turns_db


@pytest.fixture
def workspace(mock_db, tmp_path, monkeypatch):
    import app.core.config as config

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", root)
    chat_uploads_dir().mkdir(parents=True, exist_ok=True)
    database.create_profile("scope-agent", "Agent", None, {"model": "opus"})
    return root


def _project(workspace, project_id):
    path = workspace / project_id
    (path / "uploads").mkdir(parents=True)
    database.create_project(project_id=project_id, name=project_id, description=None, path=str(path))
    return path


def _chat_chip(name="shot.png", data=b"chat-lane-bytes"):
    path = chat_uploads_dir() / name
    path.write_bytes(data)
    return str(path)


def _blob_bytes(ids):
    return [upload_blobs_db.get_blob_data(i) for i in ids]


# ═══ Bug 1: a chat with no project, run by another worker ═══════════════════


def test_a_no_project_chat_lifts_its_chat_uploads(workspace, tmp_path):
    database.create_session(session_id="np-chat", profile_id="scope-agent", mode="chat")
    chip = _chat_chip()

    ids = upload_transfer.lift_send_uploads([chip], None, "np-chat")

    assert _blob_bytes(ids) == [b"chat-lane-bytes"]
    assert upload_blobs_db.get_blob(ids[0])["project_v2_id"] is None
    # The executing worker writes it under its own cwd and the prompt follows.
    cwd = tmp_path / "other-worker-chat-uploads"
    cwd.mkdir()
    mapping = upload_transfer.materialize_blobs_for_turn(ids, str(cwd))
    landed = cwd / "uploads" / "shot.png"
    assert landed.read_bytes() == b"chat-lane-bytes"
    assert upload_transfer.rewrite_prompt(f"look at {chip}", mapping) == f"look at {landed}"


def test_the_owner_chat_lifts_from_its_own_folder(workspace, monkeypatch):
    from app.core import session_modes

    database.create_session(session_id="owner-chat", profile_id="scope-agent")
    monkeypatch.setattr(session_modes, "runs_in_owner_chat_folder", lambda row, **kw: True)
    staged = session_modes.owner_chat_dir() / "uploads" / "notes.txt"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"owner-chat")

    ids = upload_transfer.lift_send_uploads([str(staged)], None, "owner-chat")

    assert _blob_bytes(ids) == [b"owner-chat"]


def test_a_no_project_lift_stays_inside_each_root(workspace, tmp_path):
    database.create_session(session_id="np-chat", profile_id="scope-agent", mode="chat")
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"API_KEY=nope")
    link = chat_uploads_dir() / "innocent.png"
    os.symlink(secret, link)
    other_staging = upload_transfer.session_upload_dir("someone-else")
    other_staging.mkdir(parents=True)
    (other_staging / "theirs.txt").write_bytes(b"theirs")
    project = _project(workspace, "proj-a")
    (project / "uploads" / "p.txt").write_bytes(b"p")

    for path in (link, other_staging / "theirs.txt", project / "uploads" / "p.txt", secret):
        with pytest.raises(upload_transfer.UploadLiftError):
            upload_transfer.lift_send_uploads([str(path)], None, "np-chat")
    assert upload_blobs_db.list_blobs_for_session("np-chat") == []


# ═══ Bug 3: the composer's scope changed between attaching and sending ══════


def test_only_an_owner_send_admits_the_composer_areas(workspace):
    _project(workspace, "proj-a")
    other = _project(workspace, "proj-b")
    database.create_session(session_id="code-a", profile_id="scope-agent", project_id="proj-a")
    chip = _chat_chip()
    elsewhere = other / "uploads" / "from-b.txt"
    elsewhere.write_bytes(b"from-b")

    for path in (chip, str(elsewhere)):
        with pytest.raises(upload_transfer.UploadLiftError):
            upload_transfer.lift_send_uploads([path], "proj-a", "code-a")
    ids = upload_transfer.lift_send_uploads([chip, str(elsewhere)], "proj-a", "code-a", composer=True)
    assert _blob_bytes(ids) == [b"chat-lane-bytes", b"from-b"]


def test_the_composer_areas_are_only_upload_folders(workspace, tmp_path):
    other = _project(workspace, "proj-b")
    _project(workspace, "proj-a")
    database.create_session(session_id="code-a", profile_id="scope-agent", project_id="proj-a")
    source = other / "main.py"
    source.write_bytes(b"code")
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"API_KEY=nope")
    escape = other / "uploads" / "escape.txt"
    os.symlink(secret, escape)

    for path in (source, escape, secret):
        with pytest.raises(upload_transfer.UploadLiftError):
            upload_transfer.lift_send_uploads([str(path)], "proj-a", "code-a", composer=True)


def test_a_same_worker_send_copies_a_composer_file_into_the_session(workspace):
    project = _project(workspace, "proj-a")
    database.create_session(session_id="code-a", profile_id="scope-agent", project_id="proj-a")
    session = database.get_session("code-a")
    (project / "uploads" / "shot.png").write_bytes(b"already-here")
    chip = _chat_chip()

    mapping = upload_transfer.adopt_composer_uploads([chip], session)

    copy = Path(mapping[chip])
    # A free name next to the session's existing upload, in its checkout.
    assert copy.parent == project / "uploads" and copy.name != "shot.png"
    assert copy.read_bytes() == b"chat-lane-bytes"
    # What the turn checks when it starts, with no composer allowance.
    upload_transfer.validate_upload_paths([str(copy)], session)
    # A file the session already admits is left where it is.
    assert upload_transfer.adopt_composer_uploads([str(copy)], session) == {}


def test_a_same_worker_no_project_chat_takes_a_project_file(workspace):
    project = _project(workspace, "proj-a")
    database.create_session(session_id="np-chat", profile_id="scope-agent", mode="chat")
    session = database.get_session("np-chat")
    staged = project / "uploads" / "spec.md"
    staged.write_bytes(b"spec")

    mapping = upload_transfer.adopt_composer_uploads([str(staged)], session)

    copy = Path(mapping[str(staged)])
    assert copy.parent == chat_uploads_dir() and copy.read_bytes() == b"spec"
    upload_transfer.validate_upload_paths([str(copy)], session)


def test_adoption_refuses_a_path_outside_every_area(workspace, tmp_path):
    database.create_session(session_id="np-chat", profile_id="scope-agent", mode="chat")
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"x")
    with pytest.raises(upload_transfer.UploadLiftError):
        upload_transfer.adopt_composer_uploads([str(secret)], database.get_session("np-chat"))


# ── POST /sessions/with-worktree ─────────────────────────────────────────────


@pytest.fixture
def remote_project(workspace):
    from app.core import project_resolver

    path = _project(workspace, "proj-1")
    pv2 = project_resolver.resolve_project_v2({"project_id": "proj-1"})
    project = {"legacy": database.get_project("proj-1"), "pv2": pv2, "path": path}
    _give_peer_a_base_checkout(project)
    return project


async def test_a_remote_worktree_create_carries_a_chat_lane_chip(remote_project):
    """The reported sequence: attach with no project picked, then pick the
    project, a new branch, Worktree on and another worker, then send."""
    chip = _chat_chip()
    with _Patches():
        resp = await swt._create_v2_branch_session(
            _body(prompt=f"read {chip}", pending_upload_paths=[chip]),
            remote_project["legacy"], None, entry=True, composer=True,
        )
    blobs = upload_blobs_db.list_blobs_for_session(resp.session_id)
    assert [upload_blobs_db.get_blob_data(b["id"]) for b in blobs] == [b"chat-lane-bytes"]
    assert blobs[0]["project_v2_id"] == remote_project["pv2"]["id"]
    turn = turns_db.get_queued_turns(resp.session_id)[0]
    assert turn["payload"]["upload_blob_ids"] == [blobs[0]["id"]]


async def test_a_remote_worktree_create_carries_another_projects_chip(remote_project, workspace):
    other = _project(workspace, "proj-b")
    staged = other / "uploads" / "b.txt"
    staged.write_bytes(b"from-b")
    with _Patches():
        resp = await swt._create_v2_branch_session(
            _body(prompt="read", pending_upload_paths=[str(staged)]),
            remote_project["legacy"], None, entry=True, composer=True,
        )
    assert [upload_blobs_db.get_blob_data(b["id"]) for b in upload_blobs_db.list_blobs_for_session(resp.session_id)] == [b"from-b"]


async def test_a_non_owner_remote_create_still_refuses_a_chat_lane_chip(remote_project):
    # A person's entry alone (any paired device) is not the owner's authority.
    chip = _chat_chip()
    with _Patches():
        with pytest.raises(HTTPException) as exc:
            await swt._create_v2_branch_session(
                _body(prompt="read", pending_upload_paths=[chip]),
                remote_project["legacy"], None, entry=True, composer=False,
            )
    assert exc.value.status_code == 400
    assert "outside this session's upload area" in exc.value.detail
    assert database.get_sessions() == []


def test_a_same_worker_worktree_create_copies_a_chat_lane_chip(workspace, tmp_path):
    _project(workspace, "proj-1")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    chip = _chat_chip()

    migrated: dict = {}
    assert swt._migrate_uploads_to_dir("proj-1", worktree, [chip], migrated, composer=True) == []
    assert Path(migrated[chip]).read_bytes() == b"chat-lane-bytes"
    assert Path(migrated[chip]).parent == worktree / "uploads"

    # Without the owner's allowance it is refused, never copied.
    errors = swt._migrate_uploads_to_dir("proj-1", worktree, [chip], {}, composer=False)
    assert errors and "outside this session's upload area" in errors[0]


# ═══ Who may reach the composer areas (review S1) ═══════════════════════════

_OWNER = {"kind": "admin", "principal_id": "install", "credential_hash": "h-owner",
          "account_id": None, "owner": "install-admin"}


def _paired(owner_admin, *, client_id="dev-1"):
    return ({"kind": "paired", "principal_id": "dev-1", "credential_hash": "h-dev",
             "account_id": "acct", "owner": "account:acct"},
            {"id": client_id, "account_owner_admin": owner_admin})


@pytest.mark.parametrize("principal, client, expected", [
    (_OWNER, None, True),
    (_paired(True)[0], _paired(True)[1], True),
    # An ordinary paired controller is a person, not the install owner.
    (_paired(False)[0], _paired(False)[1], False),
    # The live client must be the one the principal names.
    (_paired(True)[0], _paired(True, client_id="dev-2")[1], False),
    ({"kind": "api_user", "principal_id": "u", "credential_hash": "h"}, None, False),
    ("preview", None, False),
    (None, None, False),
])
def test_composer_owner_is_the_install_owner_only(monkeypatch, principal, client, expected):
    from app.core import mobile_tool_identity, session_modes

    if principal == "preview":
        principal = mobile_tool_identity.preview_limited(dict(_OWNER))
    monkeypatch.setattr(database, "get_paired_client_by_session_hash", lambda h: client)
    assert session_modes.composer_owner(principal) is expected


def _route(monkeypatch, principal, client, *, legacy):
    """Call the public with-worktree route as ``principal`` would."""
    from unittest.mock import MagicMock

    from app.api import chat_query
    from app.core import mobile_tool_identity

    monkeypatch.setattr(chat_query, "_request_query_principal", lambda req: principal)
    monkeypatch.setattr(database, "get_paired_client_by_session_hash", lambda h: client)
    monkeypatch.setattr(mobile_tool_identity, "request_principal", lambda req: None)
    monkeypatch.setattr(swt, "get_api_user_from_request", lambda req: None)
    real = swt.require_origin_access

    def access(**kw):
        found = real(**kw)
        return {**found, "legacy": found["legacy"] if legacy else None}

    monkeypatch.setattr(swt, "require_origin_access", access)

    async def call(body):
        return await swt.create_session_with_worktree(request=MagicMock(), body=body, token="tok")
    return call


async def test_a_paired_non_owner_cannot_lift_another_projects_upload(remote_project, workspace, monkeypatch):
    """A paired controller without owner authority names a file in another
    project's uploads folder: refused, nothing created (it passed before)."""
    staged = _project(workspace, "proj-b") / "uploads" / "secret.txt"
    staged.write_bytes(b"not yours")
    principal, client = _paired(False)
    call = _route(monkeypatch, principal, client, legacy=True)
    with _Patches():
        with pytest.raises(HTTPException) as exc:
            await call(_body(prompt="read", pending_upload_paths=[str(staged)]))
    assert exc.value.status_code == 400
    assert "outside this session's upload area" in exc.value.detail
    assert database.get_sessions() == []


async def test_the_owners_paired_device_lifts_another_projects_upload(remote_project, workspace, monkeypatch):
    staged = _project(workspace, "proj-b") / "uploads" / "b.txt"
    staged.write_bytes(b"from-b")
    principal, client = _paired(True)
    call = _route(monkeypatch, principal, client, legacy=True)
    with _Patches():
        resp = await call(_body(prompt="read", pending_upload_paths=[str(staged)]))
    assert [upload_blobs_db.get_blob_data(b["id"]) for b in upload_blobs_db.list_blobs_for_session(resp.session_id)] == [b"from-b"]


# ═══ Early refusals run after the owner's admission (review S3) ═════════════


async def test_the_route_carries_a_chip_to_a_project_only_the_other_worker_has(remote_project, monkeypatch):
    """Attach with no project, then pick a project this worker has no checkout
    of and create its worktree chat on the worker that has it."""
    chip = _chat_chip()
    call = _route(monkeypatch, _OWNER, None, legacy=False)
    body = _body(project_id=None, project_v2_id=remote_project["pv2"]["id"],
                 prompt=f"read {chip}", pending_upload_paths=[chip])
    with _Patches():
        resp = await call(body)
    blobs = upload_blobs_db.list_blobs_for_session(resp.session_id)
    assert [upload_blobs_db.get_blob_data(b["id"]) for b in blobs] == [b"chat-lane-bytes"]
    assert turns_db.get_queued_turns(resp.session_id)[0]["payload"]["upload_blob_ids"] == [blobs[0]["id"]]


async def test_the_route_still_refuses_a_non_owner_without_a_checkout(remote_project, monkeypatch):
    from app.api.common import ATTACHMENTS_NEED_LOCAL_CHECKOUT

    chip = _chat_chip()
    principal, client = _paired(False)
    call = _route(monkeypatch, principal, client, legacy=False)
    body = _body(project_id=None, project_v2_id=remote_project["pv2"]["id"],
                 prompt="read", pending_upload_paths=[chip])
    with _Patches():
        with pytest.raises(HTTPException) as exc:
            await call(body)
    assert exc.value.status_code == 400 and exc.value.detail == ATTACHMENTS_NEED_LOCAL_CHECKOUT
    assert database.get_sessions() == []


async def _ws_new_chat(principal, upload_paths, *, mode="code"):
    from unittest.mock import patch

    from app.core import mobile_tool_identity
    from tests.api.test_chat_ws_v2_turns import _make_handler, _Patches as _WsPatches

    # The owner's login is live (a fixture install has no owner session row).
    with _WsPatches(), patch.object(mobile_tool_identity, "validate", lambda value: None):
        handler = _make_handler()
        handler.trusted_principal = principal
        await handler.handle_query({
            "prompt": "what is in this?", "session_id": None, "profile": "scope-agent",
            "project": None, "mode": mode, "upload_paths": [str(p) for p in upload_paths],
        })
    return handler


async def test_a_new_chat_keeps_a_project_chip_after_the_project_is_cleared(workspace):
    """The owner attached in a project, cleared the project, then sent the
    first message of the new chat over the socket."""
    from tests.helpers import sent_frames

    staged = _project(workspace, "proj-b") / "uploads" / "b.txt"
    staged.write_bytes(b"from-b")
    handler = await _ws_new_chat(_OWNER, [staged])
    assert [f["message"] for f in sent_frames(handler, "error")] == []
    session_id = sent_frames(handler, "session_created")[0]["session_id"]
    session = database.get_session(session_id)
    paths = turns_db.get_queued_turns(session_id)[0]["payload"]["upload_paths"]
    # Copied into the new chat's own area, which its turn re-checks.
    assert len(paths) == 1 and Path(paths[0]).read_bytes() == b"from-b"
    upload_transfer.validate_upload_paths(paths, session)


async def test_a_new_chat_from_a_non_owner_still_refuses_a_foreign_chip(workspace):
    from app.api.common import ATTACHMENTS_NEED_LOCAL_CHECKOUT
    from tests.helpers import sent_frames

    staged = _project(workspace, "proj-b") / "uploads" / "b.txt"
    staged.write_bytes(b"from-b")
    handler = await _ws_new_chat({**_OWNER, "kind": "api_user"}, [staged])
    assert [f["message"] for f in sent_frames(handler, "error")] == [ATTACHMENTS_NEED_LOCAL_CHECKOUT]
    assert sent_frames(handler, "session_created") == []


# ═══ Nested projects and linked upload folders (review S4, S5) ══════════════


def test_a_nested_projects_chip_lands_in_the_worktrees_uploads(workspace, tmp_path):
    """Project B lives inside project A: B's chip is still foreign to A."""
    outer = _project(workspace, "proj-a")
    inner = outer / "nested"
    (inner / "uploads").mkdir(parents=True)
    database.create_project(project_id="proj-n", name="n", description=None, path=str(inner))
    chip = inner / "uploads" / "n.txt"
    chip.write_bytes(b"nested")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    migrated: dict = {}
    assert swt._migrate_uploads_to_dir("proj-a", worktree, [str(chip)], migrated, composer=True) == []
    dest = Path(migrated[str(chip)])
    assert dest.parent == worktree / "uploads" and dest.read_bytes() == b"nested"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_linked_uploads_folder_admits_nothing(workspace):
    """``uploads`` pointing at its own project would admit every file in it."""
    base = workspace / "proj-l"
    base.mkdir()
    (base / "private.txt").write_bytes(b"private")
    try:
        os.symlink(base, base / "uploads", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    database.create_project(project_id="proj-l", name="l", description=None, path=str(base))
    roots = upload_transfer.composer_staging_roots()
    assert base.resolve() not in roots
    assert not upload_transfer.composer_staged([str(base / "uploads" / "private.txt")])
    with pytest.raises(upload_transfer.UploadLiftError):
        upload_transfer.validate_upload_paths(
            [str(base / "private.txt")], {"id": "s-l", "project_id": "proj-l"})


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_linked_chat_uploads_folder_admits_nothing(workspace):
    chat = chat_uploads_dir()
    for child in chat.iterdir():
        child.unlink()
    chat.rmdir()
    try:
        os.symlink(workspace, chat, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    (workspace / "secret.txt").write_bytes(b"x")
    assert workspace.resolve() not in upload_transfer.composer_staging_roots()
    assert not upload_transfer.composer_staged([str(chat / "secret.txt")])
