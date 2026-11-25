"""Remove project records from this worker together with their chats.

Both "Remove project and chats" paths land here: ``DELETE /projects/{id}``
and the Workers tab's remove (``project_setup.remove_local``, run on the
worker that holds the copy). The chats are exactly ``projects.chat_in_removal``:
this worker's chats bound to a removed project row (a chat bound only by a
shared identity stays, since the identity does not prove its row). Each
goes through the ordinary chat delete (``app/api/sessions._delete_session``:
the ordered DB delete, browser close, sync fan-out, then worker, pool and
transcript teardown), re-checked under its lock. Files on disk stay,
worktree folders included.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, Optional

from fastapi import BackgroundTasks

from app.db.async_database import run_sync

_tasks: set = set()


def preview(project_ids: Iterable[str]) -> Dict[str, Any]:
    """What removing these project rows together does (sync): ``{"chats", "worktrees"}``.

    ``chats`` is how many chats are deleted; ``worktrees`` the worktree
    folders whose records go while the folders stay on disk.
    """
    from app.core.project_utils import get_worktree_working_dir
    from app.db.projects import project_removal_plan

    plan = project_removal_plan(list(project_ids))
    if not plan:
        return {"chats": 0, "worktrees": []}
    folders = []
    for row in plan["worktrees"]:
        try:
            folders.append(get_worktree_working_dir(row))
        except ValueError:  # a corrupt row without a path has no folder to keep
            continue
    return {"chats": len(plan["chats"]), "worktrees": sorted(set(folders))}


async def remove_projects(project_ids: Iterable[str],
                          background_tasks: Optional[BackgroundTasks] = None) -> Dict[str, Any]:
    """Delete the chats, then the project rows. Returns removed ids and chat count.

    Without ``background_tasks`` the slow per-chat teardown runs in a task of
    its own, after the rows are gone, as it does behind a DELETE response.
    """
    from app.api.sessions import _delete_session
    from app.db import database
    from app.db.projects import chat_in_removal, project_removal_plan

    ids = list(dict.fromkeys(project_ids))
    plan = await run_sync(project_removal_plan, ids)
    tasks = background_tasks if background_tasks is not None else BackgroundTasks()
    chats = 0
    try:
        if plan is not None:
            binding = plan["binding"]

            def still_ours(row: dict) -> bool:
                return chat_in_removal(row, binding)

            for row in plan["chats"]:
                if await _delete_session(row, tasks, matches=still_ours):
                    chats += 1
        removed = [pid for pid in ids if await run_sync(database.delete_project, pid)]
    finally:
        if background_tasks is None and tasks.tasks:
            task = asyncio.create_task(tasks())
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
    return {"removed": removed, "chats_removed": chats}
