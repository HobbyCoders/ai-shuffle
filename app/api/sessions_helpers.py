"""
Session helper functions, models, and shared state.

Extracted from sessions.py for maintainability.
"""

from typing import List, Optional

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field

import time

from app.db import database
from app.db.async_database import run_sync
from app.db.api_users import get_api_users_by_ids
from app.db.tags import get_tags_for_sessions
from app.db.background_tasks import get_running_task_presence
from app.db.sessions import get_fork_info_for_sessions
from app.db.projects import get_projects_by_ids
from app.db.projects_v2 import (
    get_projects_v2_by_ids,
    get_projects_v2_by_origins,
)
from app.api.auth import get_api_user_from_request

import logging
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Throttle for session sync — at most once per 60 s per project
# ---------------------------------------------------------------------------
_SESSION_SYNC_THROTTLE_S = 60.0
_last_sync_times: dict[str, float] = {}  # project_id -> monotonic timestamp


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class BatchDeleteRequest(BaseModel):
    """Request body for batch delete operation"""
    session_ids: List[str]


class SessionActiveUpdate(BaseModel):
    """Request body for setting a session's active status"""
    is_active: bool


class SessionImportResponse(BaseModel):
    """Response for session import"""
    session_id: str
    title: Optional[str]
    message_count: int
    status: str


class ImportJSONLRequest(BaseModel):
    """Request body for importing a JSONL session"""
    jsonl_path: str
    profile_id: Optional[str] = None
    project_id: Optional[str] = None


class ImportJSONLResponse(BaseModel):
    """Response for JSONL session import"""
    session_id: str
    sdk_session_id: str
    title: Optional[str]
    message_count: int
    status: str


class ForkSessionRequest(BaseModel):
    """Request body for forking a session.

    ``message_index`` is optional: omit it (or send ``null``) to fork the
    ENTIRE conversation — the sessions-board "Fork" action, which duplicates a
    whole session rather than branching from a specific message. When provided,
    the fork happens up to and including that 0-based display-message index.
    """
    message_index: Optional[int] = None


class ForkSessionResponse(BaseModel):
    """Response for session fork"""
    session_id: str
    title: Optional[str]
    parent_session_id: str
    fork_point_message_index: int
    message_count: int
    status: str


class AdoptRequest(BaseModel):
    """Request body for POST /sessions/{id}/adopt.

    Empty / omitted body is pin-to-here (today's Resume here). ``follow=true``
    is unpin: take ownership here and drop the sticky placement pin.
    """
    follow: bool = False


class AdoptSessionResponse(BaseModel):
    """Response for the cross-device 'Resume here' adopt action.

    ``adopted`` is True when ownership was re-pointed to this install; False
    when the session was already local (a no-op — the caller just opens it).
    ``status`` is ``adopted``, ``already_local``, or ``unpinned``.
    """
    session_id: str
    instance_id: str
    adopted: bool
    status: str


class MoveSessionRequest(BaseModel):
    """Request body for the push-direction 'Move to device' action.

    ``target_instance_id`` is the install id (``installs.id`` /
    ``sessions.instance_id``) that should own the session from here on.
    """
    target_instance_id: str = Field(..., min_length=1)


class MoveSessionResponse(BaseModel):
    """Response for the cross-device 'Move to device' action.

    ``moved`` is True when ownership was re-pointed to the target; False when the
    session already belonged to it (a no-op). ``instance_id`` is the owner AFTER
    the call. ``pending_live_run`` stays on the wire as ``false``: a live run
    409s instead of queueing the move (turn-end persist would otherwise restore
    the old disk's ``sdk_session_id``).
    """
    session_id: str
    instance_id: str
    moved: bool
    status: str
    pending_live_run: bool = False


class CreateWorktreeSessionRequest(BaseModel):
    """Request body for creating a session with a worktree"""
    # XOR with project_v2_id — exactly one is required (400 if both/neither).
    project_id: Optional[str] = None
    project_v2_id: Optional[str] = None
    branch_name: str
    base_branch: str = "main"
    profile_id: Optional[str] = None
    create_new_branch: bool = False  # False = use existing branch, True = create new branch
    # Optional first-prompt kickoff — when ``prompt`` is set the backend
    # persists the user message, migrates any pending uploads into the new
    # worktree, and submits a durable first turn, so the query runs server-side
    # regardless of WS state.
    prompt: Optional[str] = None
    # The composer's run picks (model, effort, permission mode, ...). With a
    # prompt they ride the first turn; without one they are stored on the new
    # session as its run binding, the same values that turn would have stored.
    overrides: Optional[dict] = None
    pending_upload_paths: Optional[List[str]] = None
    # Composer device pick. When set to another install's id the session is
    # created PINNED to it: no local checkout is materialized, a durable
    # ``ensure_worktree`` git op is queued for that install instead, and any
    # ``pending_upload_paths`` travel as upload blobs on the first turn. The
    # response's ``worktree_path`` is None for a remote target — the path does
    # not exist on this machine and never will.
    target_instance_id: Optional[str] = None


class WorktreeSessionResponse(BaseModel):
    """Response for worktree session creation"""
    session_id: str
    # Legacy worktree row id — NULL on the v2 branch-intake path (a NEW branch
    # session has no legacy worktree row; it records only sessions.branch and its
    # deterministic checkout is materialized lazily at spawn).
    worktree_id: Optional[str] = None
    branch_name: str
    base_branch: str
    # The materialized checkout path (v2) or the legacy worktree path.
    worktree_path: Optional[str] = None
    status: str
    # True when ``prompt`` was provided and the backend has already submitted
    # the first turn. Frontend uses this flag to skip sending a redundant WS
    # ``query`` message.
    query_started: bool = False
    upload_migration_errors: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

def parse_session_history_via_store(
    sdk_session_id: str, working_dir: str, *, use_cache: bool = True
) -> tuple[list, dict]:
    """Parse a session transcript from the Postgres session store (postgres_only).

    Synchronous (call via ``run_sync``). Builds a ``PgSessionStore`` and a sync
    ``store_loader`` that matches entries on ``session_id`` (+subpath) ALONE —
    the stored ``project_key`` is the on-disk folder name and diverges from any
    cwd-derived key, so we never reconstruct it (see
    ``app/core/pg_session_store.py``). Delegates to
    :func:`app.core.jsonl_parser.parse_session_history_from_store`, which runs
    the entries through the SAME transform as the JSONL path. Returns the same
    ``(messages, usage_data)`` shape as ``parse_session_history``.

    ``use_cache=False`` forces a fresh parse straight from the store entries,
    bypassing ``_store_parse_cache``. Used by the post-turn reseed to obtain an
    authoritative, ``uuid``-bearing transcript (the in-memory live-store
    messages carry no JSONL uuid, which breaks rewind anchoring).
    """
    from app.core.pg_session_store import (
        _load_entries_by_session_id_sync,
        _store_version_by_session_id_sync,
        lazy_backfill_session_from_jsonl,
    )
    from app.core.jsonl_parser import parse_session_history_from_store

    # Store miss → almost always a pre-cutover session whose JSONL was never
    # bulk-backfilled before the postgres_only flip. Heal the store from the
    # local JSONL before parsing so the chat renders its history instead of
    # empty. Guarded inside (count, summary tombstone, JSONL idle-mtime,
    # advisory lock) so it is a cheap no-op for new/live/stored sessions.
    count, _ = _store_version_by_session_id_sync(sdk_session_id)
    if count == 0:
        lazy_backfill_session_from_jsonl(sdk_session_id, working_dir)

    def _store_loader(session_id: str, subpath: Optional[str] = None) -> list:
        return _load_entries_by_session_id_sync(session_id, subpath)

    def _store_version_loader(session_id: str) -> tuple[int, int]:
        # Cheap (count, max_id) stamp so a cached parse from before a rewind on
        # THIS or another install (shared Postgres) is dropped on read rather
        # than served verbatim — the cross-install "rewind still broken" bug.
        return _store_version_by_session_id_sync(session_id)

    return parse_session_history_from_store(
        sdk_session_id,
        working_dir,
        _store_loader,
        use_cache=use_cache,
        store_version_loader=_store_version_loader,
    )


# ---------------------------------------------------------------------------
# Access control helpers
# ---------------------------------------------------------------------------

def resolve_api_user_profile(api_user: Optional[dict], requested: Optional[str]) -> Optional[str]:
    """Return the agent an API user may run, enforcing its assigned agents.

    ``api_user['profile_ids']`` (the ``api_user_profiles`` junction) is the
    restriction. Unrestricted callers (the owner passes ``None``; an API user
    with no assigned agents) get ``requested`` back unchanged. A restricted
    user asking for an agent outside the set gets 403; one that names no agent
    runs its only agent, or gets 400 listing the allowed ids when it has
    several.
    """
    allowed = (api_user or {}).get("profile_ids") or []
    if not allowed:
        return requested
    if requested:
        if requested not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This API key is not allowed to use agent '{requested}'.",
            )
        return requested
    if len(allowed) == 1:
        return allowed[0]
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Specify 'profile': this API key may use agents "
               + ", ".join(allowed) + ".",
    )


def api_user_may_use_profile(api_user: Optional[dict], profile_id: Optional[str]) -> bool:
    """True when ``api_user`` may use agent ``profile_id``.

    The owner (``None``) and an API user with no assigned agents are
    unrestricted; otherwise the agent must be in ``api_user['profile_ids']``.
    """
    allowed = (api_user or {}).get("profile_ids") or []
    return not allowed or profile_id in allowed


def api_user_profile_filter(api_user: Optional[dict], requested: Optional[str]) -> Optional[str]:
    """The profile filter for a restricted API user's session list/search.

    A requested agent outside the assigned set is 403; with no request, a
    single assigned agent becomes the filter (several leave it unset — the
    list is already limited to the caller's own sessions).
    """
    allowed = (api_user or {}).get("profile_ids") or []
    if not allowed:
        return requested
    if requested:
        if requested not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this profile",
            )
        return requested
    return allowed[0] if len(allowed) == 1 else None


_WORKER_OWNER_DEPTH = 8


def api_user_owns_session(api_user: dict, session: dict) -> bool:
    """True when ``session`` belongs to API user ``api_user``.

    A session belongs to the key that started it (``sessions.api_user_id``,
    the same column the session list filters on). Worker sessions an agent
    spawns (``is_worker``) are minted without one and belong to whoever owns
    the session that spawned them. Every other session with no
    ``api_user_id`` is the owner's, and a fork belongs to whoever forked it.
    """
    caller = api_user.get("id")
    if not caller:
        return False
    row: Optional[dict] = session
    for _ in range(_WORKER_OWNER_DEPTH):
        owner = row.get("api_user_id")
        if owner or not row.get("is_worker") or not row.get("parent_session_id"):
            return owner == caller
        row = database.get_session(row["parent_session_id"])
        if not row:
            return False
    return False


def check_session_access_for_api_user(api_user: Optional[dict], session: dict) -> None:
    """Enforce an API user's access to a session.

    Pure helper (no Request): ``api_user`` is None for the owner (full
    access). An API user may act only on its own sessions
    (:func:`api_user_owns_session`), and only while the session is inside its
    project and agent scope, so a project-limited key reaches a session with
    no project only when that session is its own. Raises
    HTTPException(403) on refusal. Extracted from :func:`check_session_access`
    so non-HTTP callers (the chat and sync WebSockets, the browser panel) use
    the exact same rule.
    """
    if not api_user:
        return  # Admin has full access

    if not api_user_owns_session(api_user, session):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this session"
        )

    # Check project restriction
    # (a projectless session passes only because it is the caller's own).
    if api_user.get("project_id") and session.get("project_id"):
        if api_user["project_id"] != session["project_id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this session"
            )

    # Check agent restriction (assigned agents, ``profile_ids``)
    if not api_user_may_use_profile(api_user, session.get("profile_id")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this session"
        )


def check_session_access(request: Request, session: dict) -> None:
    """Check that the request's API user owns ``session`` and has it in scope."""
    check_session_access_for_api_user(get_api_user_from_request(request), session)


async def require_session(request: Request, session_id: str) -> dict:
    """Fetch a session, verify it exists, and check access in one call.

    Replaces the repeated pattern of:
        existing = await run_sync(database.get_session, session_id)
        if not existing: raise HTTPException(404)
        check_session_access(request, existing)

    Returns the session dict if valid.
    Raises HTTPException(404) if not found, HTTPException(403) if access denied.
    """
    session = await run_sync(database.get_session, session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )
    check_session_access(request, session)
    return session


# ---------------------------------------------------------------------------
# Enrichment helpers
# ---------------------------------------------------------------------------

def is_pinned(session_row: dict) -> bool:
    """True when ``placement.where`` is an explicit ``runner:<id>`` pin.

    Computed at read (never persisted). NULL / anywhere / capability
    placements are not pins — those still auto-adopt.
    """
    from app.core.placement import _normalize_placement

    placement = _normalize_placement(session_row.get("placement"))
    where = (placement or {}).get("where") or ""
    return isinstance(where, str) and where.startswith("runner:")


def retain_sdk_session_id(
    session_row: dict, destination_instance_id: str
) -> Optional[str]:
    """Keep ``sdk_session_id`` iff it is still usable on the destination.

    Fluid ids live in shared ``fluid_sessions`` (resume works on any install).
    CLI JSONL lives on one disk: keep only when the destination is this
    install and the file exists. Otherwise return None (caller clears).
    """
    sdk = session_row.get("sdk_session_id") or None
    if not sdk:
        return None

    from app.core.provider.fluid_transcript import is_fluid_session

    if is_fluid_session(sdk):
        return sdk

    from app.core.config import settings

    if destination_instance_id != settings.effective_instance_id:
        return None

    from app.core.jsonl_parser import get_session_jsonl_path
    from app.core.message_resolver import resolve_working_dir

    try:
        cwd = resolve_working_dir(session_row)
    except Exception:  # noqa: BLE001 — missing cwd must not block the clear
        cwd = "/workspace"
    if get_session_jsonl_path(sdk, cwd):
        return sdk
    return None


def apply_sdk_session_keep(session_row: dict, destination_instance_id: str) -> None:
    """Clear ``sdk_session_id`` when :func:`retain_sdk_session_id` returns None.

    Logs the reason (``no_local_jsonl`` / ``cross_install_move`` / ``not_fluid``).
    DB transcript is the history that always survives.
    """
    sdk = session_row.get("sdk_session_id")
    if not sdk:
        return
    if retain_sdk_session_id(session_row, destination_instance_id):
        return

    from app.core.config import settings

    if destination_instance_id != settings.effective_instance_id:
        reason = "cross_install_move"
    else:
        reason = "no_local_jsonl"
    database.update_session(session_row["id"], sdk_session_id="")
    session_row["sdk_session_id"] = None
    logger.info(
        "Cleared sdk_session_id for session %s (reason=%s, not_fluid)",
        session_row.get("id"),
        reason,
    )


def is_session_degraded(
    session_row: dict,
    claimable_ids: set,
    installs_map: Optional[dict] = None,
) -> bool:
    """Derive the transient ``degraded`` state for one session.

    ``degraded`` is True IFF ALL of:
      1. the session has a QUEUED turn and no live run — i.e. its id is in
         ``claimable_ids`` (the ``turns.list_claimable_sessions`` candidate set,
         the same source of truth the cross-runner claim loop uses);
      2. ``resolve_placement(session_row).kind == "none"`` — no capable runner is
         online to execute it.

    Single oracle: ``placement.resolve_placement`` (never raw ``installs.online``
    or the legacy cloud/sync paths). Derive-on-read only — nothing is persisted.
    Any exception anywhere ⇒ False (this NEVER raises, so a listing can never
    500 on the derivation).

    ``installs_map`` (optional) is a preloaded install registry snapshot handed
    to the resolver. The enrichment pipeline has already read the registry for
    its device annotations, so passing it makes the whole page cost ONE registry
    read instead of two per degraded candidate. Omit it and the resolver reads
    the registry itself, exactly as before.
    """
    try:
        # Not a queued-with-no-live-run candidate ⇒ not degraded.
        if session_row.get("id") not in claimable_ids:
            return False

        from app.core.placement import resolve_placement

        return resolve_placement(session_row, installs_map=installs_map).kind == "none"
    except Exception:  # noqa: BLE001 — derivation must never break the listing
        logger.debug(
            "degraded derivation failed for %s",
            session_row.get("id"), exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# Batched lookups for the listing-enrichment pipeline
#
# The enrichment pipeline runs on EVERY sidebar poll, so every lookup it makes
# has to be batched: one query for the whole page, never one per row. The SQL
# now lives beside its siblings in the DB layer
# (``app.db.projects.get_projects_by_ids`` — which also owns the per-id
# fallback — and ``app.db.projects_v2.get_projects_v2_by_ids`` /
# ``get_projects_v2_by_origins``). Only the composition and the degrade-to-
# full-scan policy are here, because they are the LISTING's policy: the two
# targeted ``projects_v2`` reads are one logical lookup, so a failure in either
# falls back to the single old full scan rather than to two of them.
# ---------------------------------------------------------------------------

def _projects_v2_targeted(pv2_ids, origins) -> tuple[dict[str, dict], dict[str, str]]:
    """``(by_id, origin_to_id)`` for just the projects this page references.

    Replaces the full ``list_projects_v2()`` scan that ran on every listing:
    only the ``project_v2_id``s carried by the page's sessions and the origins
    of the page's legacy projects can ever be looked up, so only those are read.
    Falls back to that full scan if either targeted read fails, so behaviour is
    unchanged when the schema or driver disagrees.
    """
    by_id: dict[str, dict] = {}
    origin_to_id: dict[str, str] = {}
    try:
        by_id = dict(get_projects_v2_by_ids(pv2_ids))
        origin_to_id = {
            origin: row["id"]
            for origin, row in get_projects_v2_by_origins(origins).items()
            if row.get("id")
        }
        return by_id, origin_to_id
    except Exception:  # noqa: BLE001 — enrichment must never break the listing
        logger.debug(
            "Targeted projects_v2 lookup failed; falling back to full scan",
            exc_info=True,
        )
        by_id, origin_to_id = {}, {}

    # Fallback: the old full scan.
    try:
        from app.db import projects_v2 as projects_v2_db

        for row in projects_v2_db.list_projects_v2():
            by_id[row["id"]] = row
            if row.get("origin"):
                origin_to_id[row["origin"]] = row["id"]
    except Exception:  # noqa: BLE001
        logger.debug("Failed to load projects_v2 for enrichment", exc_info=True)
    return by_id, origin_to_id


def _annotate_cluster_fields(sessions: List[dict]) -> None:
    """Annotate each session (in place) with owning-device + project info.

    On a shared database every install's sessions are now visible together, so
    each row needs to say *which* device owns it, whether that device is online,
    and whether it can be controlled from here. We also resolve project names
    server-side: the frontend ``$projects`` map is instance-filtered, so a
    cross-install session's project would otherwise render as unknown.

    Cheap and fully batched — a fixed number of queries regardless of page
    size: one registry read (``get_installs_map``), ONE ``IN (...)`` read for
    every distinct legacy project (non-instance-filtered, so cross-install
    projects resolve too), targeted ``projects_v2`` reads for only the ids /
    origins this page references, and one claimable-sessions read.
    """
    if not sessions:
        return

    from app.core.config import settings
    from app.core.project_resolver import origin_for_legacy_project
    me = settings.effective_instance_id

    # Batch: installs registry (id -> row, each carrying an in-DB ``online``
    # flag). Control (send/interrupt) flows through the shared DB — the ``runs``
    # lease claim loop and durable ``pending_interactions`` — so it depends only
    # on the owning install being online, never on its ``base_url``.
    try:
        installs_map = database.get_installs_map()
    except Exception:  # noqa: BLE001 — annotation must never break the listing
        logger.debug("Failed to load installs registry for enrichment", exc_info=True)
        installs_map = {}

    # Batch: resolve project names + link groups with ONE non-instance-filtered
    # ``IN (...)`` read (was ``get_project`` per distinct project — an N+1 on
    # every sidebar poll) so projects owned by another install still resolve.
    # The link group lets the board fold same-repo clones from every install
    # into one logical project.
    project_names: dict[str, str] = {}
    project_link_groups: dict[str, Optional[str]] = {}
    # v2-projects: origin of each legacy project (link_group_id or synth
    # ``local:<id>``), derived from the SAME already-fetched project row — no
    # extra DB call. Used to self-heal ``origin``/``project_v2_id`` for sessions
    # whose ``project_v2_id`` column is still NULL (created before the seed).
    project_origins: dict[str, str] = {}
    for pid, proj in get_projects_by_ids(
        s.get("project_id") for s in sessions
    ).items():
        project_names[pid] = proj.get("name") or proj.get("path") or pid
        project_link_groups[pid] = proj.get("link_group_id")
        project_origins[pid] = origin_for_legacy_project(proj)

    # v2-projects: resolve each session's origin-keyed project + origin token
    # with TARGETED reads for just the ids/origins this page references (never a
    # per-session DB call, and never a full ``list_projects_v2`` table scan
    # per request). ``origin`` is 1:1 with ``link_group_id`` for grouped
    # projects, so the board folds identically.
    pv2_by_id, pv2_origin_to_id = _projects_v2_targeted(
        (s.get("project_v2_id") for s in sessions),
        project_origins.values(),
    )

    # Degraded derivation. Read the queued-with-no-live-run candidate set
    # ONCE (batched — never a per-session query); each degraded derivation then
    # just tests membership + resolves placement.
    claimable_ids: set = set()
    try:
        from app.db.turns import list_claimable_sessions

        # Targeted anti-join over THIS page's ids only — the no-argument form is
        # a full ``turns ⋈ runs`` DISTINCT scan, and this runs on every listing.
        # Same predicates, just a WHERE on ``t.session_id``, so the result is
        # exactly the page's subset of the full candidate set.
        claimable_ids = set(
            list_claimable_sessions(
                session_ids=[s["id"] for s in sessions if s.get("id")]
            )
        )
    except Exception:  # noqa: BLE001 — annotation must never break the listing
        logger.debug(
            "Failed to load claimable sessions for degraded derivation",
            exc_info=True,
        )
        claimable_ids = set()

    my_install = installs_map.get(me)
    try:
        from app.network import runners as _runners
        my_device = _runners.self_device_id()
    except Exception:  # noqa: BLE001
        my_device = None
    from app.network.session_runtime import for_sessions, STATES
    runtime = for_sessions([s["id"] for s in sessions if s.get("owner_device_id") != my_device])
    for session in sessions:
        inst = session.get("instance_id")
        owner_device = session.get("owner_device_id")
        if owner_device and owner_device != my_device and (not inst or inst == me):
            # A mirrored session from another network device.
            inst = owner_device
        is_remote = bool(inst) and inst != me and inst != my_device
        if owner_device and my_device and owner_device == my_device:
            # This device owns the session. A session handed back here can
            # still carry the other worker's instance_id; treating it as
            # remote read an empty runtime summary and showed "Unknown".
            is_remote = False
        session["is_remote"] = is_remote
        session["is_pinned"] = is_pinned(session)

        # Pass the registry snapshot read above: resolve_placement would
        # otherwise re-read it (twice) per degraded candidate.
        session["degraded"] = is_session_degraded(
            session, claimable_ids, installs_map=installs_map
        )

        if is_remote:
            install = installs_map.get(inst)
            online = bool(
                install and install.get("online") and install.get("approved", True)
            )
            session["device_name"] = install.get("device_name") if install else None
            session["device_online"] = online
            # Controllable whenever the owning install is online: send/interrupt
            # ride the shared DB (runs lease + pending_interactions), which needs
            # no ``base_url``. Offline owner → history stays readable but read-only.
            session["controllable"] = bool(online and install)
            summary = runtime.get(session["id"], {})
            state = summary.get("state")
            session["remote_runtime_state"] = (
                "offline" if not online else
                state if summary.get("owner_device_id") == owner_device and state in STATES else "unknown"
            )
        else:
            # Local or legacy (NULL instance_id): always controllable from here.
            session["device_name"] = my_install.get("device_name") if my_install else None
            session["device_online"] = True
            session["controllable"] = True

        pid = session.get("project_id")
        if pid and pid in project_names:
            session["project_name"] = project_names[pid]
        if pid and project_link_groups.get(pid):
            session["link_group_id"] = project_link_groups[pid]

        # v2-projects: emit project_v2_id + origin. Authoritative source is the
        # session's own ``project_v2_id`` column; fall back to the legacy project's
        # origin for self-heal-pending rows. Both resolved from the batched
        # ``pv2_by_id`` / ``pv2_origin_to_id`` maps — no per-session query.
        # Unconditional since PR-B (the ``v2_projects`` kill switch was removed).
        pv2_id = session.get("project_v2_id")
        origin = None
        if pv2_id and pv2_id in pv2_by_id:
            origin = pv2_by_id[pv2_id].get("origin")
        else:
            legacy_origin = project_origins.get(pid) if pid else None
            if legacy_origin and legacy_origin in pv2_origin_to_id:
                pv2_id = pv2_origin_to_id[legacy_origin]
                origin = legacy_origin
        if pv2_id:
            session["project_v2_id"] = pv2_id
            if not session.get("project_name"):
                session["project_name"] = pv2_by_id.get(pv2_id, {}).get("name")
        if origin:
            session["origin"] = origin


def enrich_sessions_with_tags(sessions: List[dict]) -> List[dict]:
    """Add tags array, has_forks field, and api_user_name to each session.

    Uses batch DB queries (2 total) instead of per-session queries to avoid
    O(N) round-trips that slow down with session count.
    """
    if not sessions:
        return sessions

    session_ids = [s["id"] for s in sessions]

    # Batch-lookup API user names — ONE ``IN (...)`` query for the whole page.
    # (This was a ``get_api_user`` call per distinct user id: an N+1 on the
    # hottest endpoint in the app.)
    api_user_ids = [s["api_user_id"] for s in sessions if s.get("api_user_id")]
    api_user_names: dict[str, str] = {}
    if api_user_ids:
        try:
            users = get_api_users_by_ids(api_user_ids)
        except Exception:  # noqa: BLE001 — enrichment must never break the listing
            logger.debug("Batched api_user lookup failed", exc_info=True)
            users = {}
        api_user_names = {
            uid: (user.get("name") or user.get("username") or uid)
            for uid, user in users.items()
        }

    # Batch: tags for all sessions (1 query instead of N)
    tags_by_session = get_tags_for_sessions(session_ids)

    # Batch: fork info for all sessions (2 queries instead of N)
    fork_info_by_session = get_fork_info_for_sessions(session_ids)
    presence = get_running_task_presence(session_ids)

    for session in sessions:
        sid = session["id"]
        session["tags"] = tags_by_session.get(sid, [])
        session["has_running_tasks"] = presence.get(sid, {}).get("has_running_tasks", False)
        fork_info = fork_info_by_session.get(sid)
        session["has_forks"] = fork_info["has_forks"] if fork_info else False
        if session.get("api_user_id"):
            session["api_user_name"] = api_user_names.get(session["api_user_id"])

    # Annotate owning device + project (cross-install sharing).
    _annotate_cluster_fields(sessions)
    return sessions


def enrich_search_results_with_tags(results: list) -> list:
    """Add tags array and has_forks field to each search result.

    Uses batch DB queries (2 total) instead of per-result queries to avoid
    O(N) round-trips that slow down with result count.
    """
    if not results:
        return results

    session_ids = [r["id"] for r in results]

    # Batch: tags for all results (1 query instead of N)
    tags_by_session = get_tags_for_sessions(session_ids)

    # Batch: fork info for all results (2 queries instead of N)
    fork_info_by_session = get_fork_info_for_sessions(session_ids)
    presence = get_running_task_presence(session_ids)

    for result in results:
        sid = result["id"]
        result["tags"] = tags_by_session.get(sid, [])
        result["has_running_tasks"] = presence.get(sid, {}).get("has_running_tasks", False)
        fork_info = fork_info_by_session.get(sid)
        result["has_forks"] = fork_info["has_forks"] if fork_info else False

    # Annotate owning device + project (cross-install sharing).
    _annotate_cluster_fields(results)
    return results


# ---------------------------------------------------------------------------
# One-thread-hop listing helpers
#
# The listing query and the whole enrichment pipeline are both synchronous
# psycopg2 work. Running them as two ``run_sync`` calls costs two thread hops
# per request; running the enrichment on the event loop would block EVERY
# concurrent request for the duration of 6-10 queries. These
# wrappers let a handler do the lot in ONE ``run_sync``.
# ---------------------------------------------------------------------------

def fetch_and_enrich_sessions(fetch, **kwargs):
    """Run ``fetch(**kwargs)`` and enrich its sessions — one thread hop.

    ``fetch`` is a synchronous listing function (``database.get_sessions``,
    ``get_sessions_paginated``, ``get_sessions_cursor``); it may return a bare
    list or a tuple whose FIRST element is the list. The return value is passed
    through unchanged (enrichment mutates the rows in place).
    """
    result = fetch(**kwargs)
    sessions = result[0] if isinstance(result, tuple) else result
    enrich_sessions_with_tags(sessions)
    return result


def fetch_and_enrich_search_results(fetch, **kwargs):
    """``fetch_and_enrich_sessions`` for the search endpoints."""
    result = fetch(**kwargs)
    results = result[0] if isinstance(result, tuple) else result
    enrich_search_results_with_tags(results)
    return result


# ---------------------------------------------------------------------------
# Shared filter logic
# ---------------------------------------------------------------------------

async def _apply_session_filters(
    request: Request,
    project_id: Optional[str],
    profile_id: Optional[str],
    api_user_id: Optional[str],
    admin_only: bool,
    api_users_only: bool,
    sync_claude_sessions: bool,
) -> tuple:
    """Apply API user restrictions and sync Claude sessions.

    Shared logic for list_sessions / list_sessions_paginated / list_sessions_cursor:
    1. Force project/profile/api_user_id from API user restrictions
    2. Auto-sync Claude Code sessions for the project (disk scan offloaded
       to a worker thread so it never blocks the event loop)
    3. Build filter_api_user_id and filter_api_users_only from the flags

    Returns (project_id, profile_id, filter_api_user_id, filter_api_users_only).
    """
    api_user = get_api_user_from_request(request)

    # Force API user restrictions
    if api_user:
        if api_user.get("project_id"):
            project_id = api_user["project_id"]
        profile_id = api_user_profile_filter(api_user, profile_id)
        # API users can only see their own sessions
        api_user_id = api_user["id"]
        admin_only = False
        api_users_only = False

    # Auto-sync Claude Code sessions (only for specific project, not global).
    # Throttled to at most once per 60 s per project — disk scan + N DB
    # lookups is too expensive to run on every list request.
    if sync_claude_sessions and project_id:
        now = time.monotonic()
        last = _last_sync_times.get(project_id, 0.0)
        if now - last >= _SESSION_SYNC_THROTTLE_S:
            try:
                from app.core.session_sync import session_sync_service
                _last_sync_times[project_id] = now
                # Offload the disk scan + N DB lookups to a worker thread.
                # Run inline (as before) it blocked the event loop for the full
                # scan, freezing EVERY concurrent request — the observed "all
                # endpoints return at the same elapsed time" stall. Throttle is
                # stamped before the await so a concurrent list request inside
                # the same 60 s window still skips the scan.
                sync_result = await run_sync(
                    session_sync_service.sync_project_sessions,
                    project_id=project_id,
                    profile_id=profile_id or "default",
                )
                if sync_result["imported_count"] > 0:
                    logger.debug(
                        f"Auto-imported {sync_result['imported_count']} Claude Code sessions "
                        f"for project {project_id}"
                    )
            except Exception as e:
                # Don't fail the request if sync fails - just log and continue
                logger.warning(f"Failed to auto-sync Claude Code sessions: {e}")

    # Determine api_user_id filter value:
    # - admin_only=True  -> filter for sessions with api_user_id IS NULL
    # - api_users_only=True -> filter for sessions with api_user_id IS NOT NULL
    # - api_user_id set  -> filter for that specific user
    # - otherwise        -> show all sessions
    filter_api_user_id = None
    filter_api_users_only = False
    if admin_only:
        filter_api_user_id = ""  # Empty string signals "IS NULL" in database.get_sessions
    elif api_users_only:
        filter_api_users_only = True
    elif api_user_id:
        filter_api_user_id = api_user_id

    return project_id, profile_id, filter_api_user_id, filter_api_users_only


# ---------------------------------------------------------------------------
# Transcript payload shaping
#
# A raw transcript carries untruncated tool inputs (a Write call carries the
# whole file) and inline base64 screenshot data URIs — multi-megabyte JSON per
# open and per legacy ``/live`` poll. The helpers below add, in the API layer:
#   * opt-in keyset pagination over the message list (newest window first,
#     cursor for older) with a generous default cap so existing clients are
#     unaffected;
#   * a display cap on tool INPUTS, mirroring the 2000-char cap the parser
#     already applies to tool OUTPUTS, with a truncation indicator;
#   * substitution of an inline screenshot data URI by its content-addressed
#     artifact URL when those bytes are already in the artifact store (the
#     exact form the SSE path emits, which the frontend already renders).
# ---------------------------------------------------------------------------

# Messages returned when the caller does not ask for a specific window. Chosen
# well above any normal transcript so the default response is byte-identical
# for ordinary sessions; it exists to bound the pathological ones.
DEFAULT_MESSAGE_PAGE_LIMIT = 2000
MAX_MESSAGE_PAGE_LIMIT = 10000

# Per-string display cap for tool INPUTS — same budget the parser applies to
# tool outputs (``jsonl_parser.MAX_DISPLAY_OUTPUT_SIZE``), kept as an
# independent constant so the two can diverge without a cross-module import.
MAX_TOOL_INPUT_CHARS = 2000

# Guard against pathological nesting when walking a tool-input structure.
_MAX_TOOL_INPUT_DEPTH = 6


# Appended to a capped string; also the marker that makes capping idempotent.
_TRUNCATION_MARKER = "[... truncated"


def _truncate_display_text(value: str) -> str:
    """Cap one string for display, with an explicit marker (parser's format)."""
    return (
        f"{value[:MAX_TOOL_INPUT_CHARS]}\n\n"
        f"{_TRUNCATION_MARKER} - {len(value) // 1024}KB total]"
    )


def _already_capped(value: str) -> bool:
    """True when ``value`` is a string this module already truncated.

    Keeps capping idempotent: a value that survived one pass (the cap plus its
    marker is itself longer than the cap) must not be truncated again, which
    would stack markers and lie about the original size.
    """
    return value[MAX_TOOL_INPUT_CHARS:].startswith(f"\n\n{_TRUNCATION_MARKER}")


def _cap_value(value, depth: int = 0):
    """Recursively cap long strings inside a tool-input value.

    Returns ``(new_value, truncated)``. The input is never mutated: a copy is
    made only on the branches that actually contain an over-long string, so an
    untruncated payload keeps its original object identity.
    """
    if isinstance(value, str):
        if len(value) > MAX_TOOL_INPUT_CHARS and not _already_capped(value):
            return _truncate_display_text(value), True
        return value, False
    if depth >= _MAX_TOOL_INPUT_DEPTH:
        return value, False
    if isinstance(value, dict):
        capped: dict = {}
        truncated = False
        for key, item in value.items():
            new_item, item_truncated = _cap_value(item, depth + 1)
            capped[key] = new_item
            truncated = truncated or item_truncated
        return (capped, True) if truncated else (value, False)
    if isinstance(value, list):
        capped_list = []
        truncated = False
        for item in value:
            new_item, item_truncated = _cap_value(item, depth + 1)
            capped_list.append(new_item)
            truncated = truncated or item_truncated
        return (capped_list, True) if truncated else (value, False)
    return value, False


def cap_tool_input(tool_input):
    """Cap a message's ``toolInput`` for display.

    Returns ``(tool_input, truncated)`` — the same object when nothing needed
    capping, so unaffected messages are untouched.
    """
    if not isinstance(tool_input, (dict, list, str)):
        return tool_input, False
    return _cap_value(tool_input)


def screenshot_artifact_url(data_uri) -> Optional[str]:
    """``/api/v1/artifacts/<sha256>`` for an inline screenshot already stored.

    The turn recorder content-addresses screenshot bytes into the artifact
    store as they stream (``app.core.artifact_ingest``), and the SSE path emits
    exactly this URL — which the frontend already renders. So whenever the
    bytes are on disk here we can hand a REST client the URL instead of a
    multi-megabyte base64 data URI. Returns None when the screenshot is not a
    data URI or its bytes are not in the store (then the data URI is kept, and
    the transcript still renders).
    """
    try:
        from app.core.artifact_ingest import content_hash, parse_screenshot_data_uri
        from app.core.artifact_store import artifact_path

        parsed = parse_screenshot_data_uri(data_uri)
        if not parsed:
            return None
        digest = content_hash(parsed[0])
        if artifact_path(digest) is None:
            return None
        return f"/api/v1/artifacts/{digest}"
    except Exception:  # noqa: BLE001 — never break a transcript read
        logger.debug("screenshot artifact lookup failed", exc_info=True)
        return None


def compact_messages_for_display(messages: List[dict]) -> None:
    """Cap tool inputs and de-inline stored screenshots, in place.

    Applied to the messages actually being SENT (i.e. after pagination), so the
    cost is proportional to the response, not to the transcript.
    """
    for msg in messages:
        if not isinstance(msg, dict):
            continue

        # Tool input: camelCase is the field the frontend reads; the detail
        # endpoint also emits the snake_case alias pointing at the SAME object,
        # so both are rewritten from ONE capped copy (never capped twice).
        raw_input = msg.get("toolInput")
        input_key_present = "toolInput" in msg
        if raw_input is None:
            raw_input = msg.get("tool_input")
        capped, truncated = cap_tool_input(raw_input)
        if truncated:
            if input_key_present:
                msg["toolInput"] = capped
            if "tool_input" in msg:
                msg["tool_input"] = capped
            metadata = msg.get("metadata")
            metadata = dict(metadata) if isinstance(metadata, dict) else {}
            metadata["toolInputTruncated"] = True
            metadata["tool_input_truncated"] = True
            msg["metadata"] = metadata

        # Screenshot: prefer the artifact URL over the inline base64 payload.
        screenshot = msg.get("toolScreenshot")
        if isinstance(screenshot, str) and screenshot.startswith("data:"):
            url = screenshot_artifact_url(screenshot)
            if url:
                msg["toolScreenshot"] = url


def resolve_message_page_limit(limit) -> int:
    """Clamp a caller-supplied message limit into the allowed range.

    Anything that is not a plain int (None, or an unevaluated FastAPI ``Query``
    default when the endpoint function is called directly, as several tests and
    internal callers do) means "unspecified" and takes the default.
    """
    if not isinstance(limit, int) or isinstance(limit, bool):
        return DEFAULT_MESSAGE_PAGE_LIMIT
    return max(1, min(limit, MAX_MESSAGE_PAGE_LIMIT))


def paginate_messages(
    messages: List[dict],
    limit: Optional[int] = None,
    before_seq: Optional[int] = None,
) -> tuple[List[dict], dict]:
    """Keyset window over an oldest→newest transcript, newest page first.

    ``seq`` is a message's 0-based position in the full transcript (transcripts
    are append-only, so a position is a stable cursor). With no ``before_seq``
    the NEWEST ``limit`` messages are returned — what a client opening a session
    wants — and ``next_before_seq`` walks backwards through older pages.

    Returns ``(page, pagination)`` where ``pagination`` carries ``total``,
    ``returned``, ``limit``, ``first_seq``/``last_seq``, ``has_more`` (older
    messages exist) and ``next_before_seq`` (the cursor for them).
    """
    page_limit = resolve_message_page_limit(limit)
    # Same leniency as the limit: a non-int cursor means "from the end".
    if not isinstance(before_seq, int) or isinstance(before_seq, bool):
        before_seq = None
    total = len(messages)
    end = total if before_seq is None else max(0, min(before_seq, total))
    start = max(0, end - page_limit)
    page = messages[start:end]
    return page, {
        "total": total,
        "returned": len(page),
        "limit": page_limit,
        "first_seq": start if page else None,
        "last_seq": end - 1 if page else None,
        "has_more": start > 0,
        "next_before_seq": start if start > 0 else None,
        "has_more_after": end < total,
    }


def finalize_transcript(
    session_id: str,
    messages: List[dict],
    *,
    limit: Optional[int] = None,
    before_seq: Optional[int] = None,
) -> tuple[List[dict], dict]:
    """Attach cost/user metadata, paginate, and compact — ONE thread hop.

    Every step here is synchronous DB or CPU work that must stay off the event
    loop. Cost attachment walks the WHOLE transcript (turn indices
    must line up with ``usage_log`` rows), so it runs before the window is
    taken; the display caps then apply only to the messages being sent.
    """
    from app.core.cost_utils import attach_cost_metadata, attach_user_message_metadata

    attach_cost_metadata(messages, session_id)
    # Store/JSONL transcripts drop the DB row's user-message metadata
    # (e.g. automation_id/automation_run_id) — merge it back.
    attach_user_message_metadata(messages, session_id)

    page, pagination = paginate_messages(messages, limit, before_seq)
    compact_messages_for_display(page)
    return page, pagination


def build_detail_messages(session_id: str, jsonl_messages: List[dict]) -> List[dict]:
    """Transform parsed transcript entries into ``GET /sessions/{id}`` messages.

    Moved verbatim out of the endpoint so the whole transform (which also
    resolves per-message profiles from the DB) runs in a worker thread instead
    of on the event loop.

    Per-message profile resolution: JSONL/store entries carry no DB row id, so
    each user message's profile is read from the DB user rows by matching
    content and consuming the match in order (cached to avoid re-loading all DB
    messages). Popping left-to-right assigns the correct profile even when the
    profile changed mid-session and the same prompt text repeats. (This must
    reach the response: the SessionMessage model carries profile_id/profileId,
    which the frontend reads to label each bubble.)
    """
    from app.core.cost_utils import build_profile_queues

    profile_queues = build_profile_queues(session_id)
    messages: List[dict] = []

    # Use camelCase for frontend compatibility (toolName, toolInput, toolId)
    current_turn_profile = None
    for i, m in enumerate(jsonl_messages):
        # A user row reads its OWN profile from the ordered per-content queue
        # built from the DB user rows; that profile then carries to the turn's
        # assistant/thinking rows (which the transcript stores without a
        # profile) so each bubble keeps the profile that produced it instead of
        # falling back to the session's CURRENT profile when the user switches
        # selection.
        if m.get("role") == "user":
            queue = profile_queues.get(m.get("content", ""))
            current_turn_profile = queue.popleft() if queue else None
        msg_profile_id = current_turn_profile
        # Timestamps live in metadata (not Pydantic datetime fields); the
        # frontend reads them directly from metadata for display.
        msg_data = {
            "id": m.get("id", i),
            "role": m.get("role", "user"),
            "content": m.get("content", ""),
            "type": m.get("type"),  # Critical for tool_use/tool_result rendering
            "subtype": m.get("subtype"),  # For system messages (e.g., local_command)
            "systemSubtype": m.get("systemSubtype"),  # Frontend-compatible system subtype
            "systemData": m.get("systemData"),  # Structured data for system messages
            "toolName": m.get("toolName"),  # camelCase for frontend
            "toolInput": m.get("toolInput"),  # camelCase for frontend
            "toolId": m.get("toolId"),
            "toolResult": m.get("toolResult"),  # Tool output grouped with tool_use
            "toolStatus": m.get("toolStatus"),  # Status: running, complete, error
            "toolScreenshot": m.get("toolScreenshot"),  # Inline screenshot (Chrome/computer-use, Read of image)
            "tool_name": m.get("toolName"),  # Also include snake_case for compatibility
            "tool_input": m.get("toolInput"),  # Also include snake_case for compatibility
            "metadata": m.get("metadata"),
            "profile_id": msg_profile_id,
            "profileId": msg_profile_id,
            "created_at": None  # Let Pydantic use default; timestamp is in metadata
        }
        # Include subagent-specific fields if present
        if m.get("type") == "subagent":
            msg_data["agentId"] = m.get("agentId")
            msg_data["agentType"] = m.get("agentType")
            msg_data["agentDescription"] = m.get("agentDescription")
            msg_data["agentStatus"] = m.get("agentStatus")
            msg_data["agentChildren"] = m.get("agentChildren")
        messages.append(msg_data)
    return messages


# Optional fields the live wire shape carries when present (system, tool,
# subagent, metadata).
_OPTIONAL_LIVE_FIELDS = (
    "uuid",  # Raw JSONL entry uuid — lets the UI rewind by uuid directly
    "subtype", "systemSubtype", "systemData",
    "toolName", "toolId", "toolInput", "toolResult", "toolStatus",
    "toolScreenshot",  # inline screenshot (Chrome/computer-use, Read of image)
    "agentId", "agentType", "agentDescription", "agentPrompt",
    "agentStatus", "agentChildren", "agentUsage",
    "metadata",
)


def build_live_messages(session_id: str, jsonl_messages: List[dict]) -> List[dict]:
    """Transform parsed transcript entries into ``GET /sessions/{id}/live`` messages.

    Moved verbatim out of the endpoint (see ``build_detail_messages``) so the
    transform AND the profile-id resolution it triggers run off the event loop.
    """
    from app.core.cost_utils import attach_turn_profile_ids

    messages: List[dict] = []
    for m in jsonl_messages:
        msg = {
            "id": m.get("id", ""),
            "role": m.get("role", "user"),
            "type": m.get("type", "text"),
            "content": m.get("content", ""),
            "streaming": False,
        }
        for field in _OPTIONAL_LIVE_FIELDS:
            value = m.get(field)
            if value is not None:
                msg[field] = value
        messages.append(msg)

    # Per-message profile resolution — same ordered-queue merge as
    # GET /{id}: JSONL entries carry no DB row id, so each user message
    # consumes its content's queue built from the DB rows, and that profile
    # carries to the turn's assistant/thinking rows so each bubble keeps the
    # profile that produced it (rather than re-labelling to the session's
    # CURRENT profile on a switch).
    attach_turn_profile_ids(session_id, messages)
    return messages
