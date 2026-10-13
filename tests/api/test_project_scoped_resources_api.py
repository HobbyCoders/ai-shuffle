"""Project-scoped plugins, agents and memory routes, end to end.

``/api/v1/projects/{id}/plugins``, ``/agents`` and ``/memory`` act on files
inside one project directory. Every route checks the project exists (404) and
that a project-limited API key only reaches its own project (403); the error
branches map the services' ``ValueError`` onto 400/404. The real services run
against a project in ``tmp_path``; the user-global Fluid plugin home is pointed
at ``tmp_path`` too so nothing touches the developer's ``~/.fluid``.
"""
import json

import pytest

from app.api.auth import hash_api_key
from app.db import database

KEY = "aih_project_resources_key"
AUTH = {"Authorization": f"Bearer {KEY}"}
PID = "proj-res"


@pytest.fixture
def project(client, tmp_path, monkeypatch):
    """One project at an absolute path, plus a key limited to another project."""
    from app.api import auth
    from app.core import plugin_service, project_agent_service, project_memory_service

    # Requests without a key are the owner on this machine.
    monkeypatch.setattr(auth.owner_auth, "local_access",
                        lambda request: "authorization" not in request.headers)
    client.headers["Origin"] = "http://testserver"
    monkeypatch.setattr(plugin_service, "_get_fluid_dir", lambda: tmp_path / "fluid-home")
    monkeypatch.setattr(plugin_service, "_plugin_services", {})
    monkeypatch.setattr(project_agent_service, "_agent_services", {})
    monkeypatch.setattr(project_memory_service, "_memory_services", {})

    root = tmp_path / "proj"
    root.mkdir()
    database.create_project(PID, "Res", None, str(root))
    database.create_project("other-proj", "Other", None, str(tmp_path / "other"))
    database.create_api_user("u-other", "Other key", hash_api_key(KEY), project_id="other-proj")
    return root


def _plugin(root, name, scope_dir=".fluid/plugins", hooks=None, readme="# T\n\nDoes things"):
    d = root / scope_dir / name
    d.mkdir(parents=True)
    (d / "README.md").write_text(readme)
    if hooks is not None:
        (d / "hooks").mkdir()
        (d / "hooks" / "hooks.json").write_text(json.dumps(hooks))
    return d


# ---------------------------------------------------------------------------
# Access: every route answers 404 for a missing project, 403 for a foreign key
# ---------------------------------------------------------------------------

ROUTES = [
    ("get", "/plugins/installed", None),
    ("get", "/plugins/available", None),
    ("post", "/plugins/install", {"plugin_id": "p@m", "scope": "local"}),
    ("delete", "/plugins/p@project-local", None),
    ("post", "/plugins/p@project-local/enable", None),
    ("post", "/plugins/p@project-local/disable", None),
    ("get", "/plugins/hooks", None),
    ("get", "/agents", None),
    ("get", "/agents/writer", None),
    ("post", "/agents", {"name": "writer", "description": "d", "prompt": "p"}),
    ("put", "/agents/writer", {"description": "d"}),
    ("post", "/agents/writer/rename", {"new_name": "w2"}),
    ("delete", "/agents/writer", None),
    ("get", "/memory", None),
    ("get", "/memory/project", None),
    ("put", "/memory/project", {"content": "x"}),
    ("delete", "/memory/project", None),
]


@pytest.mark.parametrize("method,suffix,body", ROUTES)
def test_foreign_project_key_is_refused(client, project, method, suffix, body):
    kwargs = {"headers": AUTH}
    if body is not None:
        kwargs["json"] = body
    r = client.request(method.upper(), f"/api/v1/projects/{PID}{suffix}", **kwargs)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "Access denied to this project"


@pytest.mark.parametrize("method,suffix,body", ROUTES)
def test_missing_project_is_404(client, project, method, suffix, body):
    kwargs = {} if body is None else {"json": body}
    r = client.request(method.upper(), f"/api/v1/projects/nope{suffix}", **kwargs)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Project not found: nope"


def test_key_reaches_its_own_project(client, project, tmp_path):
    (tmp_path / "other").mkdir()
    r = client.get("/api/v1/projects/other-proj/memory", headers=AUTH)
    assert r.status_code == 200
    assert {f["location"] for f in r.json()} == {"project", "local"}


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------


class TestProjectPlugins:
    def test_installed_lists_both_scopes(self, client, project):
        _plugin(project, "alpha")
        _plugin(project, "beta", scope_dir="plugins")
        (project / ".fluid" / "plugins" / ".hidden").mkdir()
        (project / ".fluid" / "plugins" / "stray.txt").write_text("x")

        r = client.get(f"/api/v1/projects/{PID}/plugins/installed")
        assert r.status_code == 200
        by_id = {p["id"]: p for p in r.json()}
        assert set(by_id) == {"alpha@project-local", "beta@project-project"}
        assert by_id["alpha@project-local"]["scope"] == "local"
        assert by_id["alpha@project-local"]["description"] == "Does things"
        assert by_id["alpha@project-local"]["enabled"] is True

    def test_available_marks_installed_ones(self, client, project, monkeypatch):
        from app.core import plugin_service
        from app.core.plugin_models import AvailablePlugin

        _plugin(project, "alpha")
        seen = {}

        def fake_available(self, marketplace=None):
            seen["marketplace"] = marketplace
            return [
                AvailablePlugin(id="alpha@mk", name="alpha", marketplace="mk",
                                marketplace_path="/m/alpha", description="A"),
                AvailablePlugin(id="gamma@mk", name="gamma", marketplace="mk",
                                marketplace_path="/m/gamma", description="G"),
            ]

        monkeypatch.setattr(plugin_service.PluginService, "get_available_plugins", fake_available)
        r = client.get(f"/api/v1/projects/{PID}/plugins/available", params={"marketplace": "mk"})
        assert r.status_code == 200
        assert seen["marketplace"] == "mk"
        assert {p["name"]: p["installed"] for p in r.json()} == {"alpha": True, "gamma": False}

    def test_install_rejects_unknown_scope(self, client, project):
        r = client.post(f"/api/v1/projects/{PID}/plugins/install",
                        json={"plugin_id": "p@m", "scope": "user"})
        assert r.status_code == 400
        assert r.json()["detail"] == "Invalid scope: user. Must be 'local' or 'project'."

    @pytest.mark.parametrize("plugin_id,detail", [
        ("no-marketplace", "Invalid plugin ID format: no-marketplace"),
        ("p@missing-mk", "Marketplace not found: missing-mk"),
    ])
    def test_install_service_errors_are_400(self, client, project, plugin_id, detail):
        r = client.post(f"/api/v1/projects/{PID}/plugins/install",
                        json={"plugin_id": plugin_id, "scope": "project"})
        assert r.status_code == 400
        assert r.json()["detail"] == detail

    def test_install_requires_plugin_id(self, client, project):
        r = client.post(f"/api/v1/projects/{PID}/plugins/install", json={"scope": "local"})
        assert r.status_code == 422

    def test_install_returns_the_installed_plugin(self, client, project, monkeypatch):
        from app.core import plugin_service
        from app.core.plugin_models import PluginInfo

        calls = []

        def fake_install(self, plugin_id, scope):
            calls.append((plugin_id, scope))
            return PluginInfo(id="p@project-local", name="p", marketplace="project",
                              description="", version="2.0.0", scope="local",
                              install_path=str(project / ".fluid/plugins/p"), enabled=True)

        monkeypatch.setattr(plugin_service.PluginService, "install_plugin_to_project", fake_install)
        r = client.post(f"/api/v1/projects/{PID}/plugins/install", json={"plugin_id": "p@mk"})
        assert r.status_code == 201
        assert r.json()["version"] == "2.0.0"
        assert calls == [("p@mk", plugin_service.PluginScope.LOCAL)]

    def test_uninstall_removes_the_directory(self, client, project):
        d = _plugin(project, "alpha")
        r = client.delete(f"/api/v1/projects/{PID}/plugins/alpha@project-local")
        assert r.status_code == 204
        assert not d.exists()

    @pytest.mark.parametrize("plugin_id,detail", [
        ("ghost@project-local", "Plugin not found: ghost"),
        ("alpha@somewhere", "Invalid project plugin ID: alpha@somewhere"),
        ("no-at-sign", "Invalid plugin ID format: no-at-sign"),
    ])
    def test_uninstall_errors_are_404(self, client, project, plugin_id, detail):
        _plugin(project, "alpha")
        r = client.delete(f"/api/v1/projects/{PID}/plugins/{plugin_id}")
        assert r.status_code == 404
        assert r.json()["detail"] == detail

    def test_enable_and_disable_round_trip(self, client, project):
        _plugin(project, "alpha")
        base = f"/api/v1/projects/{PID}/plugins/alpha@project-local"

        assert client.post(f"{base}/disable").status_code == 204
        listed = client.get(f"/api/v1/projects/{PID}/plugins/installed").json()
        assert listed[0]["enabled"] is False
        stored = json.loads((project / ".fluid" / "settings.local.json").read_text())
        assert stored["enabledPlugins"] == {"alpha@project-local": False}

        assert client.post(f"{base}/enable").status_code == 204
        listed = client.get(f"/api/v1/projects/{PID}/plugins/installed").json()
        assert listed[0]["enabled"] is True

    @pytest.mark.parametrize("action", ["enable", "disable"])
    def test_toggle_unknown_plugin_is_404(self, client, project, action):
        r = client.post(f"/api/v1/projects/{PID}/plugins/ghost@project-local/{action}")
        assert r.status_code == 404
        assert r.json()["detail"] == "Plugin not found: ghost@project-local"
        assert not (project / ".fluid" / "settings.local.json").exists()

    def test_hooks_come_from_project_plugins(self, client, project):
        _plugin(project, "alpha", hooks={"hooks": {"PreToolUse": [], "Stop": []}})
        _plugin(project, "beta", hooks=[{"event": "PostToolUse", "type": "command",
                                         "toolMatcher": "Bash", "description": "lint"}])
        r = client.get(f"/api/v1/projects/{PID}/plugins/hooks")
        assert r.status_code == 200
        got = sorted((h["plugin_id"], h["event"], h["hook_type"], h["tool_matcher"]) for h in r.json())
        assert got == [
            ("alpha@project-local", "PreToolUse", "prompt", None),
            ("alpha@project-local", "Stop", "prompt", None),
            ("beta@project-local", "PostToolUse", "command", "Bash"),
        ]


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class TestProjectAgents:
    def _create(self, client, **over):
        body = {"name": "writer", "description": "Writes tests", "prompt": "You write tests.",
                "model": "sonnet", "tools": ["Read", "Bash"]}
        body.update(over)
        return client.post(f"/api/v1/projects/{PID}/agents", json=body)

    def test_create_list_get(self, client, project):
        r = self._create(client)
        assert r.status_code == 201
        assert r.json()["file_path"] == ".fluid/agents/writer.md"
        assert (project / ".fluid" / "agents" / "writer.md").is_file()

        listed = client.get(f"/api/v1/projects/{PID}/agents").json()
        assert [(a["name"], a["tools"]) for a in listed] == [("writer", ["Read", "Bash"])]
        assert "prompt" not in listed[0]

        detail = client.get(f"/api/v1/projects/{PID}/agents/writer").json()
        assert detail["prompt"] == "You write tests."
        assert detail["model"] == "sonnet"
        assert "You write tests." in detail["raw_content"]

    def test_create_duplicate_is_400(self, client, project):
        assert self._create(client).status_code == 201
        r = self._create(client)
        assert r.status_code == 400
        assert r.json()["detail"] == "Agent already exists: writer"

    @pytest.mark.parametrize("over", [
        {"name": "1starts-with-digit"},
        {"name": "has space"},
        {"name": "../escape"},
        {"name": "x" * 101},
        {"description": ""},
        {"prompt": ""},
        {"model": "m" * 51},
    ])
    def test_create_validation_is_422(self, client, project, over):
        r = self._create(client, **over)
        assert r.status_code == 422
        assert not (project / ".fluid" / "agents").exists()

    def test_get_missing_or_invalid_name_is_404(self, client, project):
        for name in ("ghost", "a.b", ".hidden"):
            r = client.get(f"/api/v1/projects/{PID}/agents/{name}")
            assert r.status_code == 404, name
            assert r.json()["detail"] == f"Agent not found: {name}"

    def test_update_changes_only_sent_fields(self, client, project):
        self._create(client)
        r = client.put(f"/api/v1/projects/{PID}/agents/writer",
                       json={"description": "New desc", "model": None})
        assert r.status_code == 200
        body = r.json()
        assert body["description"] == "New desc"
        assert body["model"] is None
        assert body["tools"] == ["Read", "Bash"]
        detail = client.get(f"/api/v1/projects/{PID}/agents/writer").json()
        assert detail["prompt"] == "You write tests."

    def test_update_missing_is_404(self, client, project):
        r = client.put(f"/api/v1/projects/{PID}/agents/ghost", json={"prompt": "p"})
        assert r.status_code == 404
        assert r.json()["detail"] == "Agent not found: ghost"

    def test_rename(self, client, project):
        self._create(client)
        r = client.post(f"/api/v1/projects/{PID}/agents/writer/rename", json={"new_name": "tester"})
        assert r.status_code == 200
        assert r.json()["name"] == "tester"
        assert not (project / ".fluid" / "agents" / "writer.md").exists()
        assert (project / ".fluid" / "agents" / "tester.md").is_file()

    def test_rename_errors(self, client, project):
        self._create(client)
        self._create(client, name="other")
        base = f"/api/v1/projects/{PID}/agents"
        r = client.post(f"{base}/writer/rename", json={"new_name": "other"})
        assert r.status_code == 400
        assert r.json()["detail"] == "Agent already exists: other"
        r = client.post(f"{base}/ghost/rename", json={"new_name": "fresh"})
        assert r.status_code == 400
        assert r.json()["detail"] == "Agent not found: ghost"
        r = client.post(f"{base}/writer/rename", json={"new_name": "bad name"})
        assert r.status_code == 422

    def test_delete(self, client, project):
        self._create(client)
        assert client.delete(f"/api/v1/projects/{PID}/agents/writer").status_code == 204
        r = client.delete(f"/api/v1/projects/{PID}/agents/writer")
        assert r.status_code == 404
        assert r.json()["detail"] == "Agent not found: writer"
        r = client.delete(f"/api/v1/projects/{PID}/agents/a.b")
        assert r.status_code == 404
        assert r.json()["detail"] == "Invalid agent name format: a.b"


# ---------------------------------------------------------------------------
# Memory (AGENTS.md)
# ---------------------------------------------------------------------------


class TestProjectMemory:
    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    def test_invalid_location_is_400(self, client, project, method):
        kwargs = {"json": {"content": "x"}} if method == "PUT" else {}
        r = client.request(method, f"/api/v1/projects/{PID}/memory/global", **kwargs)
        assert r.status_code == 400
        assert r.json()["detail"] == "Invalid location: 'global'. Must be one of: project, local"

    def test_list_reports_missing_files(self, client, project):
        r = client.get(f"/api/v1/projects/{PID}/memory")
        assert r.status_code == 200
        assert [(f["location"], f["exists"], f["file_path"]) for f in r.json()] == [
            ("project", False, "AGENTS.md"),
            ("local", False, ".agents/AGENTS.md"),
        ]

    def test_save_read_delete_local(self, client, project):
        url = f"/api/v1/projects/{PID}/memory/local"
        r = client.put(url, json={"content": "# Local\n"})
        assert r.status_code == 200
        assert r.json()["exists"] is True and r.json()["size"] == len("# Local\n")
        assert (project / ".agents" / "AGENTS.md").read_text() == "# Local\n"

        assert client.get(url).json()["content"] == "# Local\n"
        assert client.delete(url).status_code == 204
        assert not (project / ".agents" / "AGENTS.md").exists()

    def test_legacy_file_is_read_and_deleted_but_never_written(self, client, project):
        (project / "CLAUDE.md").write_text("legacy")
        url = f"/api/v1/projects/{PID}/memory/project"
        got = client.get(url).json()
        assert (got["file_path"], got["content"]) == ("CLAUDE.md", "legacy")

        saved = client.put(url, json={"content": "new"}).json()
        assert saved["file_path"] == "AGENTS.md"
        assert (project / "CLAUDE.md").read_text() == "legacy"

        assert client.delete(url).status_code == 204
        assert not (project / "AGENTS.md").exists()
        assert (project / "CLAUDE.md").exists()

    def test_delete_missing_is_404(self, client, project):
        r = client.delete(f"/api/v1/projects/{PID}/memory/project")
        assert r.status_code == 404
        assert "does not exist" in r.json()["detail"]

    def test_save_requires_content(self, client, project):
        r = client.put(f"/api/v1/projects/{PID}/memory/project", json={})
        assert r.status_code == 422

    @pytest.mark.parametrize("action", ["preview", "apply"])
    def test_import_is_owner_only(self, client, project, action):
        r = client.post(f"/api/v1/projects/other-proj/memory-import/{action}",
                        headers=AUTH, json={"provider": "claude"})
        assert r.status_code == 403

    @pytest.mark.parametrize("action", ["preview", "apply"])
    def test_import_unknown_provider_is_400(self, client, project, action):
        r = client.post(f"/api/v1/projects/{PID}/memory-import/{action}",
                        json={"provider": "gemini"})
        assert r.status_code == 400
        assert "gemini" in r.json()["detail"]

    def test_import_preview_and_apply(self, client, project, monkeypatch):
        from app.api import project_memory as mod

        calls = []
        monkeypatch.setattr(mod, "preview_memory_import",
                            lambda proj, provider: calls.append(("preview", proj["id"], provider))
                            or {"items": [], "ready_count": 0})
        monkeypatch.setattr(mod, "apply_memory_import",
                            lambda proj, provider, paths: calls.append(("apply", proj["id"], provider, paths))
                            or {"imported": paths or []})
        base = f"/api/v1/projects/{PID}/memory-import"
        assert client.post(f"{base}/preview", json={"provider": "codex"}).json() == {
            "items": [], "ready_count": 0}
        r = client.post(f"{base}/apply", json={"provider": "claude", "relative_paths": ["a.md"]})
        assert r.json() == {"imported": ["a.md"]}
        assert calls == [("preview", PID, "codex"), ("apply", PID, "claude", ["a.md"])]
