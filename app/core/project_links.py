"""Project link-grouping helpers.

On a shared database several installs each clone the same git repository,
producing one ``projects`` row per install for what is really one logical
project. ``link_group_id`` ties those rows together so the sessions board shows
a single group for the repo regardless of which device owns each session.

A row gets a link group in one of two ways:

1. **Auto, by git remote** — deterministic and needs no cross-install
   coordination: every install computes the *same* id from the normalized
   remote URL (``"git:" + sha256(normalized)[:16]``). This is the default and is
   refreshed whenever the git_repositories record is ensured.

2. **Manual** — an admin explicitly links/unlinks two project rows from the UI.
   A manual link sets ``settings.link_group_locked = True`` so the auto path
   stops overwriting it.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional

from app.db import database

logger = logging.getLogger(__name__)

# settings flag that pins a manually-chosen link group against the auto path.
LINK_LOCKED_KEY = "link_group_locked"


def normalize_remote_url(url: Optional[str]) -> str:
    """Normalize a git remote URL for comparison/hashing.

    Strips a single trailing ``.git`` and any trailing slash and lower-cases the
    result so ``https://github.com/Owner/Repo.git`` and
    ``https://github.com/owner/repo`` produce the same id.
    """
    if not url:
        return ""
    normalized = url.strip().rstrip("/")
    if normalized.lower().endswith(".git"):
        normalized = normalized[: -len(".git")]
    return normalized.lower()


def auto_link_group_id(remote_url: Optional[str]) -> Optional[str]:
    """Deterministic link-group id for a git remote, identical on every install.

    Returns ``None`` when there's no usable remote — a local-only project keeps
    its own row and is not auto-grouped.
    """
    normalized = normalize_remote_url(remote_url)
    if not normalized:
        return None
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:16]
    return f"git:{digest}"


def is_link_locked(project: Optional[Dict[str, Any]]) -> bool:
    """True when an admin manually pinned this project's link group."""
    if not project:
        return False
    settings_dict = project.get("settings") or {}
    if not isinstance(settings_dict, dict):
        return False
    return bool(settings_dict.get(LINK_LOCKED_KEY))


def maybe_auto_link_project(project_id: str, remote_url: Optional[str]) -> Optional[str]:
    """Set ``link_group_id`` from the git remote unless manually locked.

    Idempotent and cheap to call repeatedly (e.g. whenever the git repository
    record is ensured): it no-ops when the project is missing, manually locked,
    has no usable remote, or already carries the derived id. Returns the
    project's effective link_group_id (possibly unchanged) or ``None``.
    """
    group_id = auto_link_group_id(remote_url)
    if not group_id:
        return None
    try:
        project = database.get_project(project_id)
    except Exception:  # noqa: BLE001 — never break the caller's main flow
        logger.debug("maybe_auto_link_project: get_project failed for %s", project_id, exc_info=True)
        return None
    if not project:
        return None
    if is_link_locked(project):
        return project.get("link_group_id")
    if project.get("link_group_id") == group_id:
        return group_id
    try:
        database.set_project_link_group(project_id, group_id)
    except Exception:  # noqa: BLE001
        logger.debug("maybe_auto_link_project: set failed for %s", project_id, exc_info=True)
        return project.get("link_group_id")
    return group_id
