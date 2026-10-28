"""Project registration tools for the app-config MCP server."""

import re

from fastapi import HTTPException
from pydantic import ValidationError

from app.mcp.config_helpers import _error, _tool_result


def _create_project_tools(is_admin: bool) -> list:
    from app.api.projects import create_project_record
    from app.core.models import ProjectCreate
    from app.core.project_utils import get_project_working_dir
    from app.db import database
    from app.db.async_database import run_sync
    from app.mcp.inproc import tool

    @tool(
        "listProjects",
        "List projects on this worker with their IDs, names and folder paths.",
        {"type": "object", "properties": {}},
    )
    async def list_projects(args: dict) -> dict:
        projects = await run_sync(database.get_all_projects)
        return _tool_result({
            "projects": [
                {"id": p["id"], "name": p["name"], "path": get_project_working_dir(p)}
                for p in projects
            ],
            "count": len(projects),
        })

    @tool(
        "createProject",
        "Add an AI Shuffle project at a folder path, creating the folder if needed. "
        "List projects first to avoid duplicates. Git clone URLs are not supported.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name (1-100 chars)."},
                "id": {
                    "type": "string",
                    "description": "Optional ID: lowercase letters, digits and hyphens (1-50 chars). Defaults to the name in kebab-case.",
                },
                "path": {
                    "type": "string",
                    "description": "Folder path on this worker. Docker paths must stay in the workspace. Defaults to workspace/<id>.",
                },
                "description": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    async def create_project(args: dict) -> dict:
        if not is_admin:
            return _error("App configuration tools require admin access.")

        name = args.get("name", "")
        project_id = args.get("id")
        if project_id is None and isinstance(name, str):
            project_id = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
            project_id = re.sub(r"-+", "-", project_id).strip("-")[:50]
        try:
            request = ProjectCreate(
                id=project_id,
                name=name.strip() if isinstance(name, str) else name,
                path=args.get("path"),
                description=args.get("description"),
            )
            created = await create_project_record(request)
        except HTTPException as exc:
            return _error(str(exc.detail))
        except (ValidationError, OSError) as exc:
            return _error(str(exc))
        return _tool_result({
            "success": True,
            "project_id": created["id"],
            "name": created["name"],
            "path": get_project_working_dir(created),
        })

    return [list_projects, create_project]
