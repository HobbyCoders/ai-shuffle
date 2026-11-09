"""Uploads follow the session's registered checkout, including v2 branch chats."""
from pathlib import Path
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import projects, chat_ws_handler, shared_files
from app.api.auth import require_auth
from app.core import config, project_resolver, upload_transfer
from app.db import database, checkouts
from tests.shared_fixtures import pin_runner  # noqa: F401


@pytest.fixture
def branch_upload(mock_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_MODE", "true")
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    base = tmp_path / "main"
    base.mkdir()
    def git(*args):
        subprocess.run(["git", "-C", str(base), *args], check=True, capture_output=True)
    git("init", "-b", "main")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "Initial")
    checkout = tmp_path / "outside-main"
    git("worktree", "add", "-b", "fix/attachments", str(checkout))
    database.create_project("upload-project", "Upload project", None, str(base))
    project = database.get_project("upload-project")
    pv2 = project_resolver.resolve_project_v2({"project_id": project["id"]})
    runner = config.settings.effective_instance_id
    checkouts.upsert_checkout(pv2["id"], runner, str(base))
    checkouts.upsert_checkout(pv2["id"], runner, str(checkout), branch="fix/attachments")
    database.create_session("upload-session", "default", project_id=project["id"], project_v2_id=pv2["id"])
    database.set_session_branch("upload-session", "fix/attachments", pv2["id"])
    app = FastAPI()
    app.include_router(projects.router)
    app.include_router(shared_files.router)
    app.dependency_overrides[require_auth] = lambda: "test-token"
    with TestClient(app) as client:
        yield client, project, base, checkout


def upload(client, **data):
    response = client.post("/api/v1/projects/upload-project/upload",
        files={"file": ("proof.txt", b"attachment bytes", "text/plain")}, data=data)
    assert response.status_code == 200, response.text
    return response.json()


def test_v2_branch_upload_uses_the_registered_checkout(branch_upload):
    client, _, base, checkout = branch_upload
    session = database.get_session("upload-session")
    assert not session.get("worktree_id")
    assert not database.get_worktree_by_session(session["id"])
    result = upload(client, session_id=session["id"])
    assert Path(result["full_path"]) == checkout / "uploads" / "proof.txt"
    assert Path(result["full_path"]).read_bytes() == b"attachment bytes"
    assert not (base / "uploads").exists()
    upload_transfer.validate_upload_paths([result["full_path"]], session)


@pytest.mark.parametrize("state", ["missing", "wrong-branch"])
def test_unavailable_registered_checkout_refuses_before_writing(branch_upload, state):
    client, _, base, checkout = branch_upload
    if state == "missing":
        checkout.rename(checkout.with_name("moved-checkout"))
    else:
        subprocess.run(["git", "-C", str(checkout), "switch", "-c", "other"], check=True, capture_output=True)
    response = client.post("/api/v1/projects/upload-project/upload",
        files={"file": ("proof.txt", b"blocked", "text/plain")},
        data={"session_id": "upload-session"})
    assert response.status_code == 409, response.text
    assert "Refusing to run" in response.json()["detail"]
    assert not (base / "uploads").exists()
    assert not (checkout / "uploads").exists()


def test_before_session_exists_upload_still_stages_in_main(branch_upload):
    client, _, base, checkout = branch_upload
    result = upload(client)
    assert Path(result["full_path"]) == base / "uploads" / "proof.txt"
    assert not (checkout / "uploads").exists()


@pytest.mark.parametrize("include_main_staging", [False, True])
async def test_worktree_upload_lifts_and_delivers_to_another_checkout(branch_upload, include_main_staging):
    client, project, base, checkout = branch_upload
    # The producer must choose the worktree; do not seed the consumer's path.
    result = upload(client, session_id="upload-session")
    assert Path(result["full_path"]).parent.parent == checkout
    paths = [result["full_path"]]
    if include_main_staging:
        paths.append(upload(client)["full_path"])
    ids = await chat_ws_handler.ChatWebSocketHandler._lift_uploads(
        object(), paths, project["id"], "upload-session")
    assert len(ids) == len(paths)
    destination = base.parent / "receiver"
    destination.mkdir()
    mapping = upload_transfer.materialize_blobs_for_turn(ids, str(destination))
    target = destination / "uploads" / "proof.txt"
    assert target.read_bytes() == b"attachment bytes"
    assert upload_transfer.rewrite_prompt(f"Read @{result['full_path']}", mapping) == f"Read @{target}"


def test_shared_project_session_without_local_project_keeps_session_staging(branch_upload):
    client, project, base, checkout = branch_upload
    pv2 = database.get_session("upload-session")["project_v2_id"]
    database.create_session("shared-session", "default", project_v2_id=pv2, mode="code")
    result = upload(client, session_id="shared-session")
    source = Path(result["full_path"])
    assert source.parent == upload_transfer.session_upload_dir("shared-session")
    session = database.get_session("shared-session")
    upload_transfer.validate_upload_paths([str(source)], session)
    ids = upload_transfer.lift_session_paths_to_blobs([str(source)], session)
    assert len(ids) == 1
    assert not (base / "uploads").exists()
    assert not (checkout / "uploads").exists()


async def test_worktree_lift_refuses_a_symlink_outside_both_checkouts(branch_upload):
    client, project, base, checkout = branch_upload
    result = upload(client, session_id="upload-session")
    outside = base.parent / "private.txt"
    outside.write_bytes(b"not an attachment")
    source = Path(result["full_path"]).with_name("escape.txt")
    source.symlink_to(outside)
    with pytest.raises(upload_transfer.UploadLiftError, match="outside the project"):
        await chat_ws_handler.ChatWebSocketHandler._lift_uploads(
            object(), [str(source)], project["id"], "upload-session")


@pytest.mark.parametrize("escape", ["traversal", "symlink"])
def test_worktree_upload_keeps_path_confinement(branch_upload, escape):
    client, _, base, checkout = branch_upload
    outside = base.parent / "unrelated"
    outside.mkdir()
    if escape == "symlink":
        (checkout / "uploads").symlink_to(outside, target_is_directory=True)
        path = ""
    else:
        path = "../unrelated"
    response = client.post("/api/v1/projects/upload-project/upload",
        files={"file": ("proof.txt", b"blocked", "text/plain")},
        data={"session_id": "upload-session", "path": path})
    assert response.status_code >= 400
    assert not list(outside.iterdir())


def test_remote_owner_stages_outside_both_checkouts(branch_upload, monkeypatch):
    client, project, base, checkout = branch_upload
    from app.core import placement
    monkeypatch.setattr(placement, "resolve_placement", lambda row: placement.PlacementDecision("runner", "peer-worker", "test pin"))
    result = upload(client, session_id="upload-session")
    source = Path(result["full_path"])
    assert source.parent == upload_transfer.session_upload_dir("upload-session")
    assert source.read_bytes() == b"attachment bytes"
    session = database.get_session("upload-session")
    upload_transfer.validate_upload_paths([str(source)], session)
    ids = upload_transfer.lift_session_paths_to_blobs([str(source)], session)
    receiver = base.parent / "remote-worktree"
    receiver.mkdir()
    mapping = upload_transfer.materialize_blobs_for_turn(ids, str(receiver))
    assert Path(mapping[str(source)]).read_bytes() == source.read_bytes()
    assert not (base / "uploads").exists()
    assert not (checkout / "uploads").exists()


def test_unregistered_new_checkout_stages_without_materializing(branch_upload, monkeypatch):
    client, _, base, checkout = branch_upload
    from app.core import checkout_manager
    database.set_session_branch("upload-session", "new/pending", database.get_session("upload-session")["project_v2_id"])
    def unexpected(*args, **kwargs):
        pytest.fail("upload must not create a checkout")
    monkeypatch.setattr(checkout_manager, "ensure_checkout", unexpected)
    result = upload(client, session_id="upload-session")
    assert Path(result["full_path"]).parent == upload_transfer.session_upload_dir("upload-session")
    assert not (base / "uploads").exists()
    assert not (checkout / "uploads").exists()


def test_local_main_chat_still_uploads_into_main(branch_upload):
    client, _, base, _ = branch_upload
    database.set_session_branch("upload-session", "", database.get_session("upload-session")["project_v2_id"])
    result = upload(client, session_id="upload-session")
    assert Path(result["full_path"]) == base / "uploads" / "proof.txt"


def test_first_message_migration_verifies_removes_and_remembers_deduped_copy(branch_upload):
    from app.api.sessions_worktree import _migrate_uploads_to_dir
    client, project, base, checkout = branch_upload
    original = upload(client)["full_path"]
    (checkout / "uploads").mkdir()
    (checkout / "uploads" / "proof.txt").write_bytes(b"tracked checkout file")
    mapping = {}
    assert _migrate_uploads_to_dir(project["id"], checkout, [original], mapping, session_id="upload-session") == []
    assert not Path(original).exists()
    assert Path(mapping[original]).name == "proof_1.txt"
    assert Path(mapping[original]).read_bytes() == b"attachment bytes"
    assert (checkout / "uploads" / "proof.txt").read_bytes() == b"tracked checkout file"
    # A different draft can now reuse the old main filename. Retry must still
    # resolve this session's own migrated content, not the other draft's bytes.
    Path(original).write_bytes(b"other chat's new upload")
    session = database.get_session("upload-session")
    assert upload_transfer.migrated_upload_paths([original], session) == mapping
    database.create_session("other-chat", "default", project_id=project["id"])
    assert upload_transfer.migrated_upload_paths([original], database.get_session("other-chat")) == {}
    assert Path(original).read_bytes() == b"other chat's new upload"


async def test_partial_migration_keeps_failed_original_and_maps_only_verified_files(branch_upload, monkeypatch):
    from app.api.sessions_worktree import _migrate_uploads_to_dir
    from app.core import confined_files
    client, project, base, checkout = branch_upload
    first = upload(client)["full_path"]
    second = upload(client)["full_path"]
    real_upload = confined_files.upload
    def fail_second(root, directory, filename, data):
        if filename == "proof_1.txt":
            raise OSError("disk full")
        return real_upload(root, directory, filename, data)
    monkeypatch.setattr(confined_files, "upload", fail_second)
    mapping = {}
    errors = _migrate_uploads_to_dir(project["id"], checkout, [first, second], mapping, session_id="upload-session")
    assert len(errors) == 1 and "disk full" in errors[0]
    assert list(mapping) == [first]
    assert not Path(first).exists()
    assert Path(mapping[first]).read_bytes() == b"attachment bytes"
    assert Path(second).read_bytes() == b"attachment bytes"
    assert upload_transfer.migrated_upload_paths([first, second], database.get_session("upload-session")) == mapping
    # Retry the actual lift/materialization path, not just the alias consumer.
    monkeypatch.setattr(confined_files, "upload", real_upload)
    session = database.get_session("upload-session")
    assert upload_transfer.branch_uploads_need_lift([mapping[first], second], session)
    ids = await chat_ws_handler.ChatWebSocketHandler._lift_uploads(object(), [mapping[first], second], project["id"], session["id"])
    retried = upload_transfer.materialize_blobs_for_turn(ids, str(checkout))
    assert all(Path(path).read_bytes() == b"attachment bytes" for path in retried.values())
    assert not Path(first).exists() and not Path(second).exists()


@pytest.mark.parametrize("failure", ["verify", "alias", "destination-symlink", "source-symlink"])
def test_migration_failures_preserve_source(branch_upload, monkeypatch, failure):
    from app.api.sessions_worktree import _migrate_uploads_to_dir
    from app.core import confined_files
    from app.db import upload_blobs
    client, project, base, checkout = branch_upload
    original = upload(client)["full_path"]
    source = original
    outside = base.parent / "unrelated"
    outside.mkdir()
    if failure == "verify":
        real = confined_files.upload
        def corrupt(root, directory, filename, data):
            return real(root, directory, filename, b"corrupt")
        monkeypatch.setattr(confined_files, "upload", corrupt)
    elif failure == "alias":
        def unavailable(*args, **kwargs):
            raise RuntimeError("database unavailable")
        monkeypatch.setattr(upload_blobs, "clear_data", unavailable)
    elif failure == "source-symlink":
        source = str(Path(original).with_name("link.txt"))
        Path(source).symlink_to(original)
    else:
        (checkout / "uploads").symlink_to(outside, target_is_directory=True)
    mapping = {}
    assert _migrate_uploads_to_dir(project["id"], checkout, [source], mapping, session_id="upload-session")
    assert mapping == {}
    assert Path(original).read_bytes() == b"attachment bytes"
    assert not list(outside.iterdir())


async def test_remote_first_message_lift_cleans_main_but_keeps_retry_bytes(branch_upload):
    client, project, base, _ = branch_upload
    original = upload(client)["full_path"]
    ids = await chat_ws_handler.ChatWebSocketHandler._lift_uploads(object(), [original], project["id"], "upload-session")
    assert not Path(original).exists()
    stage = upload_transfer.session_upload_dir("upload-session")
    assert [path.read_bytes() for path in stage.iterdir()] == [b"attachment bytes"]
    session = database.get_session("upload-session")
    mapping = upload_transfer.migrated_upload_paths([original], session)
    assert Path(mapping[original]).read_bytes() == b"attachment bytes"
    receiver = base.parent / "peer-worktree"
    receiver.mkdir()
    delivered = upload_transfer.materialize_blobs_for_turn(ids, str(receiver))
    assert Path(delivered[original]).read_bytes() == b"attachment bytes"
    assert upload_transfer.rewrite_prompt(f"Read @{original}", delivered) == f"Read @{delivered[original]}"


def test_stage_preview_and_thumbnails_use_absolute_path(branch_upload, monkeypatch):
    client, _, _, _ = branch_upload
    from app.core import placement
    monkeypatch.setattr(placement, "resolve_placement", lambda row: placement.PlacementDecision("runner", "peer", "test"))
    staged = upload(client, session_id="upload-session")
    response = client.get("/api/files/by-path", params={"path": staged["full_path"]})
    assert response.status_code == 200, response.text
    assert response.content == b"attachment bytes"
    # Image chips use this same by-path endpoint, not a project/uploads lookup.
    from base64 import b64decode
    png = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY7sAAAAASUVORK5CYII=")
    response = client.post("/api/v1/projects/upload-project/upload", files={"file": ("chip.png", png, "image/png")}, data={"session_id": "upload-session"})
    assert response.status_code == 200, response.text
    image = client.get("/api/files/by-path", params={"path": response.json()["full_path"]})
    assert image.status_code == 200 and image.content == png
    assert image.headers["content-type"] == "image/png"


def test_remote_non_worktree_project_keeps_existing_main_upload_path(branch_upload, monkeypatch):
    client, _, base, _ = branch_upload
    from app.core import placement
    database.set_session_branch("upload-session", "", database.get_session("upload-session")["project_v2_id"])
    monkeypatch.setattr(placement, "resolve_placement", lambda row: placement.PlacementDecision("runner", "peer", "test"))
    result = upload(client, session_id="upload-session")
    assert Path(result["full_path"]) == base / "uploads" / "proof.txt"


def test_remote_compose_cleanup_does_not_delete_reused_filename(branch_upload):
    client, project, _, _ = branch_upload
    original = upload(client)["full_path"]
    session = database.get_session("upload-session")
    ids = upload_transfer.lift_paths_to_blobs([original], project, session["id"])
    Path(original).write_bytes(b"another draft now owns this name")
    errors = upload_transfer.stage_lifted_compose_uploads(ids, project, session["id"])
    assert len(errors) == 1 and "changed after it was lifted" in errors[0]
    assert Path(original).read_bytes() == b"another draft now owns this name"


def test_remote_compose_retry_after_becoming_shared_only_keeps_confinement(branch_upload):
    client, project, _, _ = branch_upload
    original = upload(client)["full_path"]
    session = database.get_session("upload-session")
    ids = upload_transfer.lift_paths_to_blobs([original], project, session["id"])
    assert upload_transfer.stage_lifted_compose_uploads(ids, project, session["id"]) == []
    mirrored = {**session, "project_id": None}
    with pytest.raises(upload_transfer.UploadLiftError):
        upload_transfer.validate_upload_paths([original], mirrored)
    mapping = upload_transfer.migrated_upload_paths([original], mirrored)
    assert Path(mapping[original]).read_bytes() == b"attachment bytes"
    upload_transfer.validate_upload_paths(list(mapping.values()), mirrored)
    assert upload_transfer.migrated_upload_paths(["/untrusted/uploads/forged.txt"], mirrored) == {}
    with pytest.raises(upload_transfer.UploadLiftError):
        upload_transfer.validate_upload_paths(["/untrusted/uploads/forged.txt"], mirrored)


def test_retry_retries_failed_cleanup_before_accepting_alias(branch_upload, monkeypatch):
    from app.api.sessions_worktree import _migrate_uploads_to_dir
    from app.core import confined_files
    client, project, _, checkout = branch_upload
    original = upload(client)["full_path"]
    real_remove = confined_files.remove_verified_upload
    def denied(*args, **kwargs):
        raise OSError("cleanup denied")
    monkeypatch.setattr(confined_files, "remove_verified_upload", denied)
    assert _migrate_uploads_to_dir(project["id"], checkout, [original], {}, session_id="upload-session")
    session = database.get_session("upload-session")
    with pytest.raises(OSError, match="cleanup denied"):
        upload_transfer.migrated_upload_paths([original], session)
    assert Path(original).read_bytes() == b"attachment bytes"
    monkeypatch.setattr(confined_files, "remove_verified_upload", real_remove)
    mapping = upload_transfer.migrated_upload_paths([original], session)
    assert not Path(original).exists()
    assert Path(mapping[original]).read_bytes() == b"attachment bytes"


async def test_local_pending_session_staging_materializes_into_its_checkout(branch_upload):
    _, _, base, checkout = branch_upload
    session = database.get_session("upload-session")
    staged = upload_transfer.stage_session_upload(session["id"], "pending.txt", b"pending bytes")
    assert upload_transfer.branch_uploads_need_lift([staged["full_path"]], session)
    ids = await chat_ws_handler.ChatWebSocketHandler._lift_uploads(object(), [staged["full_path"]], session["project_id"], session["id"])
    mapping = upload_transfer.materialize_blobs_for_turn(ids, str(checkout))
    assert Path(mapping[staged["full_path"]]) == checkout / "uploads" / "pending.txt"
    assert Path(mapping[staged["full_path"]]).read_bytes() == b"pending bytes"
    assert not (base / "uploads").exists()
    assert not upload_transfer.branch_uploads_need_lift(list(mapping.values()), session)


@pytest.mark.parametrize("payload", [b"attachment bytes", b"different bytes"])
def test_alias_never_deletes_a_new_draft_reusing_original_name(branch_upload, payload):
    from app.api.sessions_worktree import _migrate_uploads_to_dir
    client, project, _, checkout = branch_upload
    original = upload(client)["full_path"]
    mapping = {}
    assert not _migrate_uploads_to_dir(project["id"], checkout, [original], mapping, session_id="upload-session")
    Path(original).write_bytes(payload)
    assert upload_transfer.migrated_upload_paths([original], database.get_session("upload-session")) == mapping
    assert Path(original).read_bytes() == payload
