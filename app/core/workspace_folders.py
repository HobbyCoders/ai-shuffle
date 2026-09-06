"""Per-chat workspace projects, resolved on the runner instead of storing paths."""

import json
from typing import Any


def normalize_workspace_project_ids(value: Any) -> list[str]:
    # Postgres returns JSONB as a list; the SQLite test shim returns JSON text.
    if isinstance(value, str):
        value = json.loads(value)
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 50:
        raise ValueError("Workspace folders must be a list of up to 50 projects")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("Workspace folders must contain project IDs")
    return list(dict.fromkeys(item.strip() for item in value))


def workspace_directories(value: Any, main_project_id: str | None = None, api_user_id: str | None = None) -> list[str]:
    from app.core.project_resolver import resolve_existing_cwd
    from app.db import database

    ids = normalize_workspace_project_ids(value)
    api_user = database.get_api_user(api_user_id) if ids and api_user_id else None
    paths = []
    for project_id in ids:
        if api_user_id and (not api_user or (api_user.get("project_id") and api_user["project_id"] != project_id)):
            raise ValueError("Access denied to workspace project")
        if project_id == main_project_id:
            continue
        project = database.get_project(project_id)
        if not project:
            raise ValueError(f"Workspace project '{project_id}' is no longer available. Remove it from Workspace folders.")
        # Use the same origin/checkout resolver as the main project so another
        # device never receives a path belonging to the original machine.
        path = resolve_existing_cwd({"project_id": project_id})
        if not path:
            raise ValueError(f"Workspace project '{project['name']}' has no checkout on this device.")
        if path not in paths:
            paths.append(path)
    return paths
