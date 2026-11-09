"""Uploads follow the session's registered checkout, including v2 branch chats."""
from pathlib import Path
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import projects, chat_ws_handler
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
def test_unavailable_checkout_falls_back_to_main(branch_upload, state):
    client, _, base, checkout = branch_upload
    if state == "missing":
        checkout.rename(checkout.with_name("moved-checkout"))
    else:
        subprocess.run(["git", "-C", str(checkout), "switch", "-c", "other"], check=True, capture_output=True)
    result = upload(client, session_id="upload-session")
    assert Path(result["full_path"]) == base / "uploads" / "proof.txt"
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
