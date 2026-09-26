"""``GET /agents/{id}/subagents/all`` enforces project access (security S-2).

The composer autocomplete route used to check only the API user's profile
list. A project-scoped API user could pass another project's id and read that
project's agent prompts, and the read also ran the legacy ``.claude/agents``
migration, writing files into the foreign project.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api import agents as agents_mod
from app.api.auth import require_admin, require_auth


def _client(api_user):
    app = FastAPI()

    @app.middleware("http")
    async def _as_api_user(request: Request, call_next):
        request.state.api_user = api_user
        return await call_next(request)

    app.include_router(agents_mod.router)
    app.dependency_overrides[require_auth] = lambda: "t"
    app.dependency_overrides[require_admin] = lambda: "t"
    return TestClient(app)


def _project(tmp_path, name):
    root = tmp_path / name
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".claude" / "agents" / "secret-agent.md").write_text(
        "---\nname: secret-agent\ndescription: internal\n---\nTOP SECRET PROMPT\n"
    )
    return root


def _get(tmp_path, api_user, project_id, root, *, worktree=None, repo_id="repo-mine"):
    db = MagicMock()
    db.get_profile.return_value = {"id": "p", "name": "P", "config": {}}
    db.get_project.return_value = {"id": project_id, "path": project_id, "name": project_id}
    db.get_worktree.return_value = worktree
    db.get_git_repository_by_project.return_value = {"id": repo_id}
    params = {"project_id": project_id}
    if worktree:
        params["worktree_id"] = worktree["id"]
    with patch.object(agents_mod, "database", db), \
         patch("app.api.git_worktrees.database", db), \
         patch.object(agents_mod, "get_project_working_dir_as_path", return_value=root), \
         patch.object(agents_mod, "get_worktree_working_dir_as_path", return_value=tmp_path / "wt"), \
         patch.object(agents_mod, "get_user_agent_service") as users, \
         patch.object(agents_mod, "get_plugin_service") as plugins:
        users.return_value.get_agents.return_value = []
        plugins.return_value.get_enabled_plugins.return_value = []
        plugins.return_value.list_plugins.return_value = []
        return _client(api_user).get("/api/v1/agents/p/subagents/all", params=params)


def test_scoped_api_user_cannot_read_or_migrate_a_foreign_project(tmp_path):
    other = _project(tmp_path, "other-project")
    response = _get(tmp_path, {"id": "u1", "project_id": "my-project"}, "other-project", other)
    assert response.status_code == 403
    assert "TOP SECRET PROMPT" not in response.text
    assert not (other / ".fluid").exists()


def test_scoped_api_user_still_reads_its_own_project(tmp_path):
    mine = _project(tmp_path, "my-project")
    response = _get(tmp_path, {"id": "u1", "project_id": "my-project"}, "my-project", mine)
    assert response.status_code == 200
    assert "TOP SECRET PROMPT" in response.text


def test_a_worktree_of_another_project_is_ignored(tmp_path):
    mine = _project(tmp_path, "my-project")
    foreign_wt = {"id": "wt-x", "repository_id": "repo-other"}
    with patch.object(agents_mod, "get_project_agent_service",
                      wraps=agents_mod.get_project_agent_service) as svc:
        response = _get(tmp_path, None, "my-project", mine, worktree=foreign_wt)
    assert response.status_code == 200
    # The project's own base path was read, never the foreign worktree's.
    assert svc.call_args.args[0] == mine
