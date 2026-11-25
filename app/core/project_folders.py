"""Retarget a worker's base project registration without touching its files."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from fastapi import HTTPException
from psycopg2.errors import UniqueViolation

from app.core.config import settings
from app.core.git_service import git_service
from app.core.project_links import auto_link_group_id, normalize_remote_url
from app.core.project_utils import get_project_working_dir, is_absolute_project_path
from app.db import projects as projects_db

STALE_FOLDER = 'The project folder changed. Close this editor and reopen it.'


def revision(row: dict) -> str:
    """Opaque edit token, including row identity and the workspace for relative paths."""
    return hashlib.sha256(repr((row['id'], row['created_at'], row['updated_at'],
                               get_project_working_dir(row))).encode()).hexdigest()


def retarget(project: dict, folder: str, expected_revision: str, **metadata) -> dict:
    """Run only on the target worker. A registration is not a filesystem move."""
    from app.api.projects import normalize_docker_path, refuse_nested_repository, validate_project_path
    from app.core.project_resolver import origin_for_legacy_project

    if project.get('instance_id') != settings.effective_instance_id:
        raise HTTPException(409, 'This folder is not registered exclusively to this worker.')
    if revision(project) != expected_revision:
        raise HTTPException(409, STALE_FOLDER)
    folder = folder.strip()
    if not folder or '\x00' in folder:
        raise HTTPException(400, 'Enter an existing folder path on this worker.')
    if is_absolute_project_path(folder) and not Path(folder).is_absolute():
        raise HTTPException(400, 'Enter a folder path in this worker’s path format.')
    old = Path(get_project_working_dir(project))
    try:
        destination = validate_project_path(folder, allow_create=False)
        old_resolved = old.resolve()
        if destination != old_resolved and destination == settings.effective_workspace_dir.resolve():
            raise HTTPException(400, 'Choose a folder, not the workspace itself.')
        # The shared validator permits missing relative directories (project creation).
        if not destination.is_dir():
            raise HTTPException(400, 'That folder does not exist on this worker.')
        if destination != old_resolved:
            refuse_nested_repository(destination)
            _check_repository(project, old, destination, origin_for_legacy_project(project))
            for other in projects_db.get_all_projects():
                if other['id'] != project['id'] and Path(get_project_working_dir(other)).resolve() == destination:
                    raise HTTPException(409, 'That folder is already registered to another project on this worker.')
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise HTTPException(400, 'This worker could not read that folder or its Git metadata.') from exc

    try:
        return projects_db.retarget_folder(
            project, old_path=str(old), new_path=str(destination),
            stored_path=normalize_docker_path(str(destination)), same_folder=destination == old_resolved,
            **metadata,
        )
    except projects_db.FolderConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except UniqueViolation as exc:
        raise HTTPException(409, 'That folder is already registered as a checkout.') from exc


def _check_repository(project: dict, old: Path, destination: Path, origin: str) -> None:
    """Do not silently replace a repo with an unrelated checkout or a linked worktree."""
    is_repo = git_service.is_git_repo(str(destination))
    remote = git_service.get_remote_url(str(destination)) if is_repo else None
    old_is_repo = old.is_dir() and git_service.is_git_repo(str(old))
    old_remote = git_service.get_remote_url(str(old)) if old_is_repo else None
    if origin.startswith('git:'):
        from app.core.project_remote import remote_names_origin

        if not remote_names_origin(remote, origin):
            raise HTTPException(400, 'Choose a checkout of the same Git repository.')
    elif old_remote and normalize_remote_url(remote or '') != normalize_remote_url(old_remote):
        raise HTTPException(400, 'Choose a checkout of the same Git repository.')
    elif old_is_repo and not is_repo:
        raise HTTPException(400, 'Choose a Git repository folder for this project.')
    elif remote and not project.get('link_group_id') and auto_link_group_id(remote):
        # An unlinked registration can be auto-linked on subsequent Git/catalog
        # reads. Do not accidentally change its logical identity through a move.
        from app.core.project_links import is_link_locked

        if not is_link_locked(project):
            raise HTTPException(400, 'Choose a folder that will not change this project’s Git identity.')
    if is_repo:
        git_dir = git_service._run_git(str(destination), ['rev-parse', '--absolute-git-dir'],
                                       timeout=5, check=True).stdout.strip()
        common_dir = git_service._run_git(str(destination), ['rev-parse', '--git-common-dir'],
                                          timeout=5, check=True).stdout.strip()
        if Path(git_dir).resolve() != (destination / common_dir).resolve():
            raise HTTPException(400, 'Choose the main checkout, not a linked Git worktree.')
