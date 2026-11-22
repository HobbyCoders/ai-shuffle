"""File-serving authorization must match the API user's project boundary."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture
def scoped_files(tmp_path, monkeypatch):
    from app.api import (
        common,
        generated_audio,
        generated_images,
        generated_videos,
        shared_files,
    )
    from app.api.auth import require_auth
    from app.core import project_resolver
    from app.db import checkouts, projects_v2

    workspace, own, other, checkout = [tmp_path / name for name in ("workspace", "own", "other", "checkout")]
    for root in (workspace, own, other, checkout):
        root.mkdir()
        for directory, extension in (("shared-files", "txt"), ("generated-images", "png"), ("generated-audio", "mp3"), ("generated-videos", "mp4")):
            (root / directory).mkdir()
            (root / directory / f"fixture.{extension}").write_bytes(b"synthetic fixture")
    config = SimpleNamespace(effective_workspace_dir=workspace, effective_instance_id="runner")
    monkeypatch.setattr(common, "settings", config)
    for module in (shared_files, generated_images, generated_audio, generated_videos):
        monkeypatch.setattr(module, "settings", config)
    project = {"id": "own", "path": str(own)}
    monkeypatch.setattr(common.database, "get_project", lambda pid: project if pid == "own" else None)
    monkeypatch.setattr(common.database, "get_git_repository_by_project", lambda pid: None)
    monkeypatch.setattr(project_resolver, "origin_for_legacy_project", lambda row: "local:own")
    monkeypatch.setattr(projects_v2, "get_project_v2_by_origin", lambda origin: {"id": "logical-own"})
    monkeypatch.setattr(checkouts, "list_checkouts", lambda pid: [{"path": str(checkout), "runner_id": "runner"}])
    monkeypatch.setattr(common, "_registered_serve_roots", lambda: (own, other, checkout))
    app = FastAPI()
    for module in (shared_files, generated_images, generated_audio, generated_videos):
        app.include_router(module.router)

    def authenticate(request: Request):
        request.state.api_user = {"id": "scoped", "project_id": "own"}
        return "synthetic-api-user"

    app.dependency_overrides[require_auth] = authenticate
    with TestClient(app) as client:
        yield client, app, own, other, workspace, checkout


@pytest.mark.parametrize("prefix,directory,extension", [
    ("/api/files", "shared-files", "txt"),
    ("/api/generated-images", "generated-images", "png"),
    ("/api/generated-audio", "generated-audio", "mp3"),
    ("/api/generated-videos", "generated-videos", "mp4"),
])
def test_scoped_files_and_media(scoped_files, prefix, directory, extension):
    client, _, own, other, workspace, checkout = scoped_files
    name = f"fixture.{extension}"
    for allowed in (own, checkout):
        assert client.get(f"{prefix}/by-path", params={"path": str(allowed / directory / name)}).status_code == 200
    for forbidden in (other, workspace):
        assert client.get(f"{prefix}/by-path", params={"path": str(forbidden / directory / name)}).status_code == 403
    assert client.get(f"{prefix}/{name}").status_code == 403
    assert client.get(f"{prefix}/").status_code == 403


def test_metadata_is_scoped(scoped_files):
    client, _, own, other, _, _ = scoped_files
    assert client.get("/api/files/info", params={"path": str(own / "shared-files/fixture.txt")}).status_code == 200
    assert client.get("/api/files/info", params={"path": str(other / "shared-files/fixture.txt")}).status_code == 403


def test_scope_cannot_escape_through_symlink(scoped_files):
    client, _, own, other, _, _ = scoped_files
    link = own / "shared-files/escape.txt"
    try:
        link.symlink_to(other / "shared-files/fixture.txt")
    except OSError:
        pytest.skip("This host does not allow symlink creation")
    assert client.get("/api/files/by-path", params={"path": str(link)}).status_code == 403


def test_admin_keeps_registered_project_access(scoped_files):
    from app.api.auth import require_auth
    client, app, _, other, _, _ = scoped_files
    app.dependency_overrides[require_auth] = lambda: "synthetic-admin"
    assert client.get("/api/files/by-path", params={"path": str(other / "shared-files/fixture.txt")}).status_code == 200


@pytest.fixture
def scoped_workspace_files(tmp_path, monkeypatch):
    """Projects live under the workspace root, the layout the browser routes serve."""
    from app.api import common, workspace_files
    from app.api.auth import require_auth
    from app.core import project_resolver
    from app.db import projects_v2

    workspace = tmp_path / "workspace"
    own, other = workspace / "own", workspace / "other"
    for root in (own, other):
        root.mkdir(parents=True)
        (root / "secret.txt").write_text("synthetic fixture")
    config = SimpleNamespace(effective_workspace_dir=workspace, effective_instance_id="runner")
    monkeypatch.setattr(common, "settings", config)
    monkeypatch.setattr(workspace_files, "settings", config)
    project = {"id": "own", "path": str(own)}
    monkeypatch.setattr(common.database, "get_project", lambda pid: project if pid == "own" else None)
    monkeypatch.setattr(common.database, "get_git_repository_by_project", lambda pid: None)
    monkeypatch.setattr(project_resolver, "origin_for_legacy_project", lambda row: "local:own")
    monkeypatch.setattr(projects_v2, "get_project_v2_by_origin", lambda origin: None)
    app = FastAPI()
    app.include_router(workspace_files.router)

    def authenticate(request: Request):
        request.state.api_user = {"id": "scoped", "project_id": "own"}
        return "synthetic-api-user"

    app.dependency_overrides[require_auth] = authenticate
    with TestClient(app) as client:
        yield client, app, own, other


def test_workspace_browser_routes_stay_inside_the_scoped_project(scoped_workspace_files):
    client, _, own, other = scoped_workspace_files
    prefix = "/api/v1/workspace"
    assert client.get(f"{prefix}/files/download", params={"path": "own/secret.txt"}).status_code == 200
    assert client.get(f"{prefix}/files", params={"path": "own"}).status_code == 200
    assert client.get(f"{prefix}/files").status_code == 403
    assert client.get(f"{prefix}/files", params={"path": "other"}).status_code == 403
    assert client.get(f"{prefix}/files/download", params={"path": "other/secret.txt"}).status_code == 403
    assert client.delete(f"{prefix}/files", params={"path": "other/secret.txt"}).status_code == 403
    assert client.put(f"{prefix}/files/rename", params={"path": "other/secret.txt"}, json={"new_name": "taken.txt"}).status_code == 403
    assert client.post(f"{prefix}/files/folder", params={"path": "other"}, json={"name": "planted"}).status_code == 403
    assert client.post(f"{prefix}/upload", data={"path": "other"}, files={"file": ("planted.txt", b"x")}).status_code == 403
    assert (other / "secret.txt").read_text() == "synthetic fixture"
    assert not (other / "planted").exists() and not (other / "planted.txt").exists()


def test_workspace_browser_keeps_whole_workspace_for_unscoped_callers(scoped_workspace_files):
    from app.api.auth import require_auth
    client, app, _, other = scoped_workspace_files
    app.dependency_overrides[require_auth] = lambda: "synthetic-admin"
    assert client.get("/api/v1/workspace/files").status_code == 200
    assert client.get("/api/v1/workspace/files/download", params={"path": "other/secret.txt"}).status_code == 200


@pytest.fixture
def cli_screenshots(scoped_files, tmp_path, monkeypatch):
    """A Claude Code config folder holding a browser screenshot and other tool results."""
    config_dir = tmp_path / "cli-config"
    results = config_dir / "projects" / "-example-project" / "0f0e0d0c-aaaa-bbbb-cccc-000000000001" / "tool-results"
    results.mkdir(parents=True)
    shot = results / "mcp-browser-blob-1795000000000-abc123.jpg"
    shot.write_bytes(b"\xff\xd8\xff synthetic jpeg")
    (results / "mcp-browser-browser_snapshot-1795000000000.txt").write_text("synthetic tool output")
    (results / "notes.jpg").write_bytes(b"\xff\xd8\xff synthetic jpeg")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return scoped_files, config_dir, results, shot


def test_owner_can_open_browser_screenshots_from_the_cli_cache(cli_screenshots):
    from app.api.auth import require_auth
    (client, app, *_), config_dir, results, shot = cli_screenshots
    app.dependency_overrides[require_auth] = lambda: "synthetic-admin"
    response = client.get("/api/files/by-path", params={"path": str(shot)})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    # Only the screenshot shape: other tool results, other names and other depths stay refused.
    for refused in (
        results / "mcp-browser-browser_snapshot-1795000000000.txt",
        results / "notes.jpg",
        config_dir / "projects" / "mcp-browser-blob-1795000000000-abc123.jpg",
        config_dir / "mcp-browser-blob-1795000000000-abc123.jpg",
    ):
        if not refused.exists():
            refused.write_bytes(b"\xff\xd8\xff synthetic jpeg")
        assert client.get("/api/files/by-path", params={"path": str(refused)}).status_code == 403, refused


def test_screenshot_name_cannot_smuggle_another_file(cli_screenshots, tmp_path):
    from app.api.auth import require_auth
    (client, app, *_), _, results, _ = cli_screenshots
    app.dependency_overrides[require_auth] = lambda: "synthetic-admin"
    secret = tmp_path / "secret.txt"
    secret.write_text("synthetic secret")
    link = results / "mcp-browser-blob-1795000000001-def456.jpg"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("This host does not allow symlink creation")
    assert client.get("/api/files/by-path", params={"path": str(link)}).status_code == 403
    traversal = f"{results}/../../../../secret/mcp-browser-blob-1795000000002-ghi789.jpg"
    assert client.get("/api/files/by-path", params={"path": traversal}).status_code == 403


def test_project_scoped_users_cannot_open_cli_screenshots(cli_screenshots):
    (client, *_), _, _, shot = cli_screenshots
    assert client.get("/api/files/by-path", params={"path": str(shot)}).status_code == 403


def test_missing_assigned_project_fails_closed(scoped_files, monkeypatch):
    from app.api import common
    client, _, own, _, _, _ = scoped_files
    monkeypatch.setattr(common.database, "get_project", lambda pid: None)
    assert client.get("/api/files/by-path", params={"path": str(own / "shared-files/fixture.txt")}).status_code == 403
