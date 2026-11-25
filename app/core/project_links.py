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
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.project_utils import get_project_working_dir
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


# A host name, or a bracketed IPv6 address (``[2001:db8::1]``).
_HOST = r"(\[[0-9A-Za-z:.%]+\]|[^\s/:?#@\[\]\\]+)"
_SCHEME_REMOTE_RE = re.compile(
    r"^(https?|ssh|git\+ssh|ssh\+git|git)://(?:([^/?#\s]*)@)?" + _HOST + r"(?::(\d*))?(/[^?#]*)?$",
    re.IGNORECASE)
_SCP_REMOTE_RE = re.compile(r"^(?:([^/\s]*)@)?" + _HOST + r":(?!//)(.+)$")
# The SSH login forges use: ``git@host:owner/repo`` is ``https://host/owner/repo``.
_FORGE_LOGIN = "git"
_USERINFO_RE = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/?#\s]*@", re.IGNORECASE)
# Each scheme's own default port: only that one is dropped.
_DEFAULT_PORT = {"https": "443", "http": "80", "ssh": "22", "git": "9418"}


def canonical_remote(url: Optional[str]) -> str:
    """One spelling for the ways of writing one network remote.

    - HTTPS and HTTP become ``https://host[:port]/path``. A port is dropped
      only when it is the URL's own scheme default (443 for HTTPS, 80 for
      HTTP), so ``https://host:80/…`` stays its own remote.
    - The forge form, scp-style with the login ``git`` and a relative path
      (``git@host:owner/repo``, as GitHub, GitLab, Bitbucket, Gitea and
      Forgejo use), is the same repository as ``https://host/owner/repo``.
    - Any other SSH form keeps its own spelling, as ``ssh://`` with the scp
      and URL forms agreeing. A path relative to the login's home (another
      login, no login, or a ``~`` path) keeps the login, since it picks the
      home: ``alice@host:repos/app`` is ``ssh://alice@host/~/repos/app``. An
      absolute path (``git@host:/srv/repo``, ``ssh://git@host/srv/repo``)
      drops it. A non-default port is kept. ``git://`` stays ``git://``.
    - A password is always dropped, and the login everywhere but a home
      path; a bracketed IPv6 host is read as one host.

    Then :func:`normalize_remote_url` applies. A plain HTTPS URL comes out
    exactly as :func:`normalize_remote_url` always made it, so its identity
    never changes. Local paths and ``file://`` URLs are only normalized.
    """
    text = (url or "").strip()
    if not text:
        return ""
    match = _SCHEME_REMOTE_RE.match(text)
    if match:
        scheme, userinfo, host, port, path = match.groups()
        scheme = scheme.lower()
        kind = "ssh" if "ssh" in scheme else scheme
        port = port or ""
        keep = f":{port}" if port and port != _DEFAULT_PORT[kind] else ""
        path = path or ""
        if kind == "http":
            kind = "https"  # one repository whichever of the two it is fetched over
        if kind == "ssh" and path.startswith("/~"):
            return normalize_remote_url(f"ssh://{_login(userinfo)}{host}{keep}{path}")
        return normalize_remote_url(f"{kind}://{host}{keep}{path}")
    scp = _SCP_REMOTE_RE.match(text)
    if scp and len(scp.group(2)) > 1:  # never a Windows drive letter
        userinfo, host, path = scp.groups()
        if path.startswith("/"):
            return normalize_remote_url(f"ssh://{host}{path}")
        login = _login(userinfo)
        if login == f"{_FORGE_LOGIN}@" and not path.startswith("~"):
            return normalize_remote_url(f"https://{host}/{path}")
        home = path if path.startswith("~") else f"~/{path}"
        return normalize_remote_url(f"ssh://{login}{host}/{home}")
    return normalize_remote_url(_USERINFO_RE.sub(r"\1", text))


def _login(userinfo: Optional[str]) -> str:
    """``alice@`` from ``alice`` or ``alice:secret``; empty without a login."""
    login = (userinfo or "").split(":", 1)[0]
    return f"{login}@" if login else ""


def _link_id(normalized: str) -> Optional[str]:
    if not normalized:
        return None
    return "git:" + hashlib.sha256(normalized.encode()).hexdigest()[:16]


def auto_link_group_id(remote_url: Optional[str]) -> Optional[str]:
    """Deterministic link-group id for a git remote, identical on every install
    and for every spelling of it (:func:`canonical_remote`).

    Returns ``None`` when there's no usable remote — a local-only project keeps
    its own row and is not auto-grouped.
    """
    return _link_id(canonical_remote(remote_url))


def legacy_link_group_id(remote_url: Optional[str]) -> Optional[str]:
    """The id older versions derived from this exact spelling (no canonical
    form): what a project first registered under SSH, say, is still keyed by."""
    return _link_id(normalize_remote_url(remote_url))


def remote_link_ids(remote_url: Optional[str]) -> set:
    """Every identity ``remote_url`` names: its id and its spelling's older id."""
    return {gid for gid in (auto_link_group_id(remote_url), legacy_link_group_id(remote_url)) if gid}


def same_remote(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two remote URLs are one repository, in any spelling."""
    left = canonical_remote(a)
    return bool(left) and left == canonical_remote(b)


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

    A project already linked to an identity that still names this remote (its
    id in any spelling, or the remote the project was pointed at) keeps it:
    another project's row for another spelling arriving later never moves the
    checkout. It moves only when its identity was merged into another one
    (**Change repository**) or a project was pointed at this exact remote, as
    before. Choosing by spelling is for a project with no such identity yet.
    """
    from app.core.project_remote import identity_for_remote

    try:
        project = database.get_project(project_id)
    except Exception:  # noqa: BLE001 — never break the caller's main flow
        logger.debug("maybe_auto_link_project: get_project failed for %s", project_id, exc_info=True)
        return None
    if not project:
        return None
    if is_link_locked(project):
        return project.get("link_group_id")
    found = _kept_identity(project.get("link_group_id"), remote_url) or identity_for_remote(remote_url)
    if not found:
        return None
    group_id, pin = found
    if project.get("link_group_id") == group_id:
        return group_id
    try:
        database.set_project_link_group(project_id, group_id)
        if pin:
            # A project pointed at this remote (or a duplicate merged into
            # one): keep the row there even if the folder's remote changes.
            merged = dict(project.get("settings") or {})
            merged[LINK_LOCKED_KEY] = True
            database.update_project(project_id, settings_dict=merged)
    except Exception:  # noqa: BLE001
        logger.debug("maybe_auto_link_project: set failed for %s", project_id, exc_info=True)
        return project.get("link_group_id")
    return group_id


def _kept_identity(current: Optional[str], remote_url: Optional[str]) -> Optional[Tuple[str, bool]]:
    """``(origin, locked)`` for a project already linked to ``current``, or None
    to choose by the remote.

    ``current`` is kept while it still names ``remote_url``; it gives way to a
    project pointed at this exact remote, and an explicit merge of it is
    followed.
    """
    from app.core.project_remote import merged_into, pinned_remote, remote_names_origin
    from app.db import projects_v2

    if not current or not remote_url:
        return None
    try:
        if not remote_names_origin(remote_url, current):
            return None
        wanted = normalize_remote_url(remote_url)
        for row in projects_v2.list_projects_v2():  # a project pointed at this remote, as before
            pinned = pinned_remote(row)
            if pinned and row["origin"] != current and normalize_remote_url(pinned) == wanted:
                return None
        target = merged_into(projects_v2.get_project_v2_by_origin(current))
        return (target, True) if target else (current, False)
    except Exception:  # noqa: BLE001 - fall back to choosing by the remote
        logger.debug("maybe_auto_link_project: current identity check failed", exc_info=True)
        return None


def record_project_remote(project_id: str, remote_url: Optional[str],
                          default_branch: Optional[str] = None) -> bool:
    """Best-effort: store the project's remote on its replicated projects_v2 row.

    Called after :func:`maybe_auto_link_project` whenever the git repository
    record is ensured, so the row it writes is the project's CURRENT logical
    identity. The URL is stored without credentials. A worker that never had
    the project can then still clone it after every worker holding a copy
    went offline or dropped its record. Never raises into the caller.
    """
    from app.core.scrub import strip_url_userinfo

    clean = strip_url_userinfo((remote_url or "").strip()) or ""
    if not clean:
        return False
    try:
        from app.core.project_resolver import origin_for_legacy_project, resolve_project_v2
        from app.db import projects_v2

        project = database.get_project(project_id)
        if not project:
            return False
        origin = origin_for_legacy_project(project)
        if projects_v2.get_project_v2_by_origin(origin) is None:
            # Seed the logical row the same way first use does (with its base checkout).
            resolve_project_v2({"project_id": project_id})
        return projects_v2.record_remote(origin, clean, default_branch or None)
    except Exception:  # noqa: BLE001 - never break the caller's main flow
        logger.debug("record_project_remote failed for %s", project_id, exc_info=True)
        return False


def backfill_project_remotes() -> int:
    """Record every remote this database's git repository records know (sync).

    For rows that predate :func:`record_project_remote`. Cheap to repeat:
    :func:`app.db.projects_v2.record_remote` writes nothing when the row is
    already right. Returns how many rows changed.
    """
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT gr.project_id AS project_id, gr.remote_url AS remote_url, "
            "gr.default_branch AS default_branch FROM git_repositories gr "
            "JOIN projects p ON p.id = gr.project_id "
            "WHERE gr.remote_url IS NOT NULL AND gr.remote_url <> ''"
        )
        rows = cursor.fetchall()
    return sum(1 for row in rows
               if record_project_remote(row["project_id"], row["remote_url"], row["default_branch"]))


# ---------------------------------------------------------------------------
# Cross-install project resolution
#
# On a shared database a project's ``path`` is machine-specific, so mapping a
# stored project row onto whatever clone THIS install actually has is a shared
# concern: automations resolve the row to run against, and cross-device session
# adopt / worktree recreate rebind onto the local clone of the same repo. Both
# reuse the ``link_group_id`` grouping (one row per install for one logical
# repo, derived from the normalized git remote).
# ---------------------------------------------------------------------------

def _link_group_siblings(project: Dict[str, Any]) -> List[Dict[str, Any]]:
    """All rows sharing the project's link group (one per install), or []."""
    link_group_id = project.get("link_group_id")
    return database.get_projects_in_link_group(link_group_id) if link_group_id else []


def _local_candidate(
    project: Dict[str, Any], siblings: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The row THIS install can actually run, given a project and its siblings.

    Own row first, then this install's link-group sibling. As a last resort,
    the stored row itself when its path exists on this machine — covering
    legacy rows without an ``instance_id``, regenerated instance ids, and
    shared filesystems, all of which ran fine before instance tracking.
    """
    me = settings.effective_instance_id
    if project.get("instance_id") == me:
        return project
    for sibling in siblings:
        if sibling.get("instance_id") == me:
            return sibling
    try:
        if Path(get_project_working_dir(project)).is_dir():
            return project
    except (OSError, ValueError):
        pass
    return None


def resolve_runnable_local_project(
    project_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """``(stored_project, local_project_or_None)`` for a project id.

    Maps a project id onto the row runnable on THIS install (own row, then this
    install's link-group sibling, then the stored row when its path exists here),
    but the returned ``local`` is only non-None when its working directory is an
    existing directory ON THIS MACHINE — the own-row / sibling branches trust the
    ``instance_id`` match without touching the filesystem (right for election but
    wrong for a rebind about to run git against the path), so this adds a final
    ``is_dir()`` guard. Surviving caller: ``worktree_manager.recreate_worktree``
    (the worktree-recreate endpoint, Phase-4 worktree machinery) — cwd/adopt
    resolution moved to the origin resolver in PR-B.

    ``stored`` is the raw project row (or None if the id is unknown), useful for
    building an actionable "not on this device" message.
    """
    stored = database.get_project(project_id)
    if not stored:
        return None, None
    local = _local_candidate(stored, _link_group_siblings(stored))
    if local is not None:
        try:
            if not Path(get_project_working_dir(local)).is_dir():
                local = None
        except (OSError, ValueError):
            local = None
    return stored, local
