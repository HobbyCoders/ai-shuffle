"""Folder browser, diagnostics and DB-maintenance routes in ``app.api.system``.

``GET /api/v1/filesystem/browse`` is owner-only; in Docker it must stay inside
the workspace folder (400 outside, 404 missing, 400 for a file) and never
offer a parent above the workspace. The workspace is the *effective* one:
the folder picked in setup overrides the startup default at runtime.
"""
import pytest

from app.api.auth import hash_api_key
from app.db import database

KEY = "aih_system_browse_key"
AUTH = {"Authorization": f"Bearer {KEY}"}
BROWSE = "/api/v1/filesystem/browse"


@pytest.fixture
def owner(client, tmp_path, monkeypatch):
    from app.api import auth
    from app.core import config

    monkeypatch.setattr(auth.owner_auth, "local_access",
                        lambda request: "authorization" not in request.headers)
    client.headers["Origin"] = "http://testserver"
    workspace = tmp_path / "workspace"
    (workspace / "proj" / "src").mkdir(parents=True)
    (workspace / "proj" / "README.md").write_text("x")
    (workspace / "empty").mkdir()
    (workspace / ".hidden").mkdir()
    (workspace / "notes.txt").write_text("n")
    (tmp_path / "outside").mkdir()
    # The workspace chosen in setup (a runtime override loaded from the
    # database) is not the startup default in ``settings.workspace_dir``.
    (tmp_path / "default-ws").mkdir()
    monkeypatch.setattr(config.settings, "workspace_dir", tmp_path / "default-ws")
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", workspace)
    database.create_api_user("k-sys", "Key", hash_api_key(KEY), project_id=None)
    return client


@pytest.fixture
def docker(monkeypatch):
    monkeypatch.setattr("app.core.platform.is_local_mode", lambda: False)


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr("app.core.platform.is_local_mode", lambda: True)


def test_api_key_cannot_browse(owner, local):
    assert owner.get(BROWSE, headers=AUTH).status_code == 403


class TestBrowseDocker:
    def test_start_is_the_workspace(self, owner, docker, tmp_path):
        r = owner.get(BROWSE)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_local_mode"] is False and body["parent"] is None
        assert [e["path"] for e in body["entries"]] == [str((tmp_path / "workspace").resolve())]

    def test_lists_folders_first_without_hidden_entries(self, owner, docker, tmp_path):
        ws = (tmp_path / "workspace").resolve()
        body = owner.get(BROWSE, params={"path": str(ws)}).json()
        assert [(e["name"], e["is_dir"], e["has_children"]) for e in body["entries"]] == [
            ("empty", True, False), ("proj", True, True), ("notes.txt", False, False)]
        # The workspace root has no parent in Docker.
        assert body["parent"] is None

    def test_subfolder_parent_is_the_workspace(self, owner, docker, tmp_path):
        ws = (tmp_path / "workspace").resolve()
        body = owner.get(BROWSE, params={"path": str(ws / "proj")}).json()
        assert body["parent"] == str(ws)
        assert [e["name"] for e in body["entries"]] == ["src", "README.md"]

    @pytest.mark.parametrize("rel", ["../outside", "proj/../../outside"])
    def test_outside_the_workspace_is_400(self, owner, docker, tmp_path, rel):
        r = owner.get(BROWSE, params={"path": str(tmp_path / "workspace" / rel)})
        assert r.status_code == 400
        assert r.json()["detail"] == "Browsing outside the workspace is not allowed"

    def test_symlink_out_of_the_workspace_is_400(self, owner, docker, tmp_path):
        link = tmp_path / "workspace" / "escape"
        link.symlink_to(tmp_path / "outside")
        r = owner.get(BROWSE, params={"path": str(link)})
        assert r.status_code == 400

    def test_missing_path_is_404(self, owner, docker, tmp_path):
        missing = str(tmp_path / "workspace" / "nope")
        r = owner.get(BROWSE, params={"path": missing})
        assert r.status_code == 404
        assert r.json()["detail"] == f"Path not found: {missing}"

    def test_file_is_400(self, owner, docker, tmp_path):
        f = str(tmp_path / "workspace" / "notes.txt")
        r = owner.get(BROWSE, params={"path": f})
        assert r.status_code == 400
        assert r.json()["detail"] == f"Path is not a directory: {f}"


class TestBrowseLocal:
    def test_start_is_home_and_root(self, owner, local):
        body = owner.get(BROWSE).json()
        assert body["is_local_mode"] is True
        assert [e["name"] for e in body["entries"]] == ["Home", "/"]

    def test_can_leave_the_workspace(self, owner, local, tmp_path):
        body = owner.get(BROWSE, params={"path": str(tmp_path / "outside")}).json()
        assert body["path"] == str((tmp_path / "outside").resolve())
        assert body["parent"] == str(tmp_path.resolve())


class TestDiagnostics:
    def test_whole_stack_needs_the_owner(self, owner):
        r = owner.get("/api/v1/diagnostics", headers=AUTH, params={"whole_stack": "true"})
        assert r.status_code == 403
        assert r.json()["detail"] == "Whole-stack diagnostics require owner access"

    def test_whole_stack_reports_unavailable_collectors(self, owner, monkeypatch):
        from app.core import fluid_memory, process_metrics, resource_monitor

        async def metrics():
            return {"pool_streaming": 2, "browser_active_calls": 0, "browser_viewers": 1}

        def boom(*a, **k):
            raise OSError("no /proc")

        monkeypatch.setattr(resource_monitor, "_collect_metrics", metrics)
        monkeypatch.setattr(resource_monitor, "_check_warnings", lambda m: ["hot"])
        monkeypatch.setattr(process_metrics, "collect_process_metrics", boom)
        monkeypatch.setattr(fluid_memory, "journal_diagnostics", boom)
        r = owner.get("/api/v1/diagnostics", params={"whole_stack": "true"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["warnings"] == ["hot"]
        m = body["metrics"]
        assert m["whole_stack"] == {"unavailable": "OSError"}
        assert m["inference_journal"] == {"unavailable": "OSError"}
        assert m["activity_scope"]["local_concurrent_activity"] is True
        assert m["activity_scope"]["local_streaming_sessions"] == 2
        assert m["activity_scope"]["activity_label"] == "active"

    def test_whole_stack_sees_a_sibling_runner_through_the_shared_database(self, owner, monkeypatch):
        from app.core import fluid_memory, process_metrics, resource_monitor
        from app.db import runs

        async def metrics():
            return {"pool_streaming": 0, "browser_active_calls": 0, "browser_viewers": 0}

        monkeypatch.setattr(resource_monitor, "_collect_metrics", metrics)
        monkeypatch.setattr(resource_monitor, "_check_warnings", lambda m: [])
        monkeypatch.setattr(process_metrics, "collect_process_metrics", lambda enabled: {})
        monkeypatch.setattr(fluid_memory, "journal_diagnostics", dict)
        scope = owner.get("/api/v1/diagnostics", params={"whole_stack": "true"}).json()["metrics"]["activity_scope"]
        assert scope["activity_label"] == "no_known_session_activity"
        assert scope["whole_stack_idle_classified"] is True

        runs.claim_run("sibling-session", "another-install", 60)
        scope = owner.get("/api/v1/diagnostics", params={"whole_stack": "true"}).json()["metrics"]["activity_scope"]
        # This worker is quiet, but the installation is not idle.
        assert scope["local_concurrent_activity"] is False
        assert scope["cross_worker_activity"] is True
        assert scope["activity_label"] == "active"
        assert scope["whole_stack_idle_classified"] is False
        assert scope["shared_database"]["other_active_runners"] == 1
        assert "another-install" not in str(scope) and "sibling-session" not in str(scope)

    def test_api_key_gets_the_local_snapshot(self, owner, monkeypatch):
        from app.core import resource_monitor

        async def metrics():
            return {"pool_streaming": 0}

        monkeypatch.setattr(resource_monitor, "_collect_metrics", metrics)
        monkeypatch.setattr(resource_monitor, "_check_warnings", lambda m: [])
        r = owner.get("/api/v1/diagnostics", headers=AUTH)
        assert r.status_code == 200
        assert "whole_stack" not in r.json()["metrics"]


def test_db_maintenance_is_owner_only(owner, monkeypatch):
    monkeypatch.setattr(database, "run_database_maintenance", lambda: {"analyzed": True})
    assert owner.post("/api/v1/system/db-maintenance", headers=AUTH).status_code == 403
    r = owner.post("/api/v1/system/db-maintenance")
    assert r.status_code == 200
    assert r.json()["analyzed"] is True and "elapsed_seconds" in r.json()


def test_readiness_reports_a_broken_session_pool(owner, monkeypatch):
    import app.core.session_pool as session_pool

    def broken():
        raise RuntimeError("pool gone")

    monkeypatch.setattr(session_pool, "get_session_pool", broken)
    r = owner.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["checks"]["session_pool"] == "error"
    assert "session pool unavailable: pool gone" in body["reasons"]
