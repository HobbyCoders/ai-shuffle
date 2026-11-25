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


# The one spelling joined to another: scp ``git@host:path``, the login exactly
# ``git`` (the forges' SSH login), the path relative (neither ``/`` nor ``~``).
_FORGE_SCP_RE = re.compile(r"^git@([^\s/:@\[\]\\]+):(?![/~])(\S+)$")


def canonical_remote(url: Optional[str]) -> str:
    """The form a remote's identity is hashed from.

    One spelling is rewritten: scp ``git@host:owner/repo`` (the login exactly
    ``git``, a path neither absolute nor ``~``), which GitHub, GitLab,
    Bitbucket, Gitea and Forgejo all serve as ``https://host/owner/repo``,
    becomes that URL. Every other remote is :func:`normalize_remote_url`
    unchanged, so its identity is exactly the one it always had.
    """
    forge = _FORGE_SCP_RE.match((url or "").strip())
    if forge:
        return normalize_remote_url(f"https://{forge.group(1)}/{forge.group(2)}")
    return normalize_remote_url(url)


# ``scheme://`` then the authority, which runs to the first ``/``.
_SCHEME_AUTHORITY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/]*)(.*)$", re.DOTALL)
_SSH_SCHEMES = {"ssh://", "git+ssh://", "ssh+git://"}
# What may be left of an authority once its userinfo is gone: a host (or a
# bracketed IPv6 address) and an optional port.
_HOST_PORT_RE = re.compile(r"(?:\[[0-9A-Fa-f:.%]+\]|[^\s@:/\[\]]+)(?::\d*)?")
_SSH_LOGIN_RE = re.compile(r"[^\s@:/\[\]]+")


def strip_remote_userinfo(url: Optional[str]) -> Optional[str]:
    """``url`` with no credential in it, or ``None`` when that can't be done safely.

    In a ``scheme://`` URL everything in the authority up to its last ``@`` is
    dropped, except an SSH login with no ``:`` (a login is not a secret, and two
    logins are two identities). What is left of the authority must be a host
    and port, and the rest may hold no ``@``, else ``None``. The scp form
    (``login@host:path``) can't carry a password and is kept, unless what
    precedes its ``@`` holds a ``:``.
    """
    text = (url or "").strip()
    if not text:
        return None
    match = _SCHEME_AUTHORITY_RE.match(text)
    if match:
        scheme, authority, rest = match.groups()
        login = ""
        if "@" in authority:
            userinfo, _, authority = authority.rpartition("@")
            if scheme.lower() in _SSH_SCHEMES and _SSH_LOGIN_RE.fullmatch(userinfo):
                login = f"{userinfo}@"
        if (authority and not _HOST_PORT_RE.fullmatch(authority)) or "@" in rest:
            return None
        return f"{scheme}{login}{authority}{rest}"
    if ":" in text.partition("@")[0] and "@" in text:
        return None
    return text


def remote_key(url: Optional[str]) -> str:
    """A comparison key for a remote that never holds a credential, or ``""``
    when one can't be made safely; the New project dialog's ``remoteKey``.

    :func:`strip_remote_userinfo`, then :func:`canonical_remote`, so two
    remotes with one key are one identity.
    """
    return canonical_remote(strip_remote_userinfo(url))


# ``https://host/path``, the path relative: the other spelling of ``git@host:path``.
_FORGE_HTTPS_RE = re.compile(r"^https://([^\s/:@\[\]\\]+)/(?![/~])(\S+)$")


# Public forges that serve one repository at ``git@host:owner/repo`` and
# ``https://host/owner/repo`` alike, with how many path segments a repository
# has there (GitLab nests groups). A self-hosted forge may serve HTTPS under a
# prefix (``/code/``), so its other spelling is never guessed.
_PUBLIC_FORGE_DEPTH = {"github.com": (2, 2), "gitlab.com": (2, None), "bitbucket.org": (2, 2),
                       "codeberg.org": (2, 2)}
# A segment the two transports read the same way: no escapes (HTTP decodes
# ``%64`` and scp does not), no ``~``, nothing empty, not ``.`` or ``..``.
_PLAIN_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")


def joined_counterpart(url: Optional[str]) -> Optional[str]:
    """The other spelling of a joined remote on a public forge
    (``git@github.com:owner/repo`` <-> ``https://github.com/owner/repo``),
    else ``None``. There both name one repository, so a worker that can sign
    in over only one of them can still clone it. Any other host, or a path
    the two transports could read differently, has none."""
    text = (url or "").strip()
    forge = _FORGE_SCP_RE.match(text)
    if forge:
        host, path = forge.group(1), forge.group(2)
        other = f"https://{host}/{path}"
    else:
        https = _FORGE_HTTPS_RE.match(text)
        if not https:
            return None
        host, path = https.group(1), https.group(2)
        other = f"git@{host}:{path}"
    depth = _PUBLIC_FORGE_DEPTH.get(host)
    if depth is None:
        return None
    segments = path.split("/")
    least, most = depth
    if len(segments) < least or (most is not None and len(segments) > most):
        return None
    if not all(_PLAIN_SEGMENT_RE.fullmatch(s) and s not in (".", "..") for s in segments):
        return None
    return other if same_remote(other, text) else None


def _link_id(normalized: str) -> Optional[str]:
    if not normalized:
        return None
    return "git:" + hashlib.sha256(normalized.encode()).hexdigest()[:16]


def auto_link_group_id(remote_url: Optional[str]) -> Optional[str]:
    """Deterministic link-group id for a git remote, identical on every install,
    and one for ``git@host:owner/repo`` and ``https://host/owner/repo``
    (:func:`canonical_remote`).

    Returns ``None`` when there's no usable remote — a local-only project keeps
    its own row and is not auto-grouped.
    """
    return _link_id(canonical_remote(remote_url))


def legacy_link_group_id(remote_url: Optional[str]) -> Optional[str]:
    """The id older versions derived from this exact spelling: what a project
    first registered as ``git@host:owner/repo`` is still keyed by. The same as
    :func:`auto_link_group_id` for every other remote."""
    return _link_id(normalize_remote_url(remote_url))


def remote_link_ids(remote_url: Optional[str]) -> set:
    """Every identity ``remote_url`` names: its id and its spelling's older id
    (two only for ``git@host:owner/repo``)."""
    return {gid for gid in (auto_link_group_id(remote_url), legacy_link_group_id(remote_url)) if gid}


def same_remote(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two remote URLs are one identity (:func:`canonical_remote`)."""
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
    own id or older id, or the remote it was pointed at) gets its answer from
    :func:`_kept_identity` and never from the remote's spelling, so another
    project's row arriving later never moves the checkout. Choosing by the
    remote (:func:`~app.core.project_remote.identity_for_remote`) is for a
    project with no such identity yet.
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
    when ``current`` doesn't name ``remote_url`` (choose by the remote then).

    Once ``current`` names it, this is the answer: where an explicit merge of
    ``current`` (**Change repository**) leads, else a different live project
    pointed at this exact remote (as before), else ``current``.
    """
    from app.core.project_remote import exact_pins, is_live, merge_target, remote_names_origin

    if not current or not remote_url:
        return None
    try:
        if not remote_names_origin(remote_url, current):
            return None
    except Exception:  # noqa: BLE001 - not recognised: choose by the remote
        logger.debug("maybe_auto_link_project: current identity check failed", exc_info=True)
        return None
    try:
        target = merge_target(current)
        if target:
            return target, True
        for row in exact_pins(remote_url):
            if row["origin"] != current and is_live(row):
                return row["origin"], True
    except Exception:  # noqa: BLE001 - recognised: keep it
        logger.debug("maybe_auto_link_project: merge or pin lookup failed", exc_info=True)
    return current, False


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
