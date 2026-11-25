"""Set a project up on another worker (or this one): clone it, or make its folder.

The flow, for a project the owner wants on worker B, asked from worker A's UI:

1. A sends B the ``project-setup`` control action (``start``) with what B needs
   to know about the logical project: its origin, name, kind, the folder to use
   and, for a Git project, the remote to clone (never with credentials).
2. B validates the folder with the same rules as adding a project there
   (``validate_project_path`` / ``normalize_docker_path`` / nested-repository
   refusal) and queues a durable ``git_ops`` row (op ``setup_project``). Its
   git-ops claim pass runs it in the background (``git_ops_runner``).
3. The op clones the remote with B's OWN credentials into a temporary sibling
   folder and renames it into place (never over an existing folder), or adopts
   a folder that is already a clone of the same repository; a folder project
   uses the folder already there as it is, or creates an empty one. Then it
   registers a normal project row (B's ``instance_id``, under the logical
   project's origin from the start), the git repository record and the base
   checkout, so the catalog, eligibility, ``ensure_checkout`` and the
   composer's runner picker see B as able to run the project with no special
   case.
4. On every state change B tells A (``projects-changed``) so A's open pages
   refresh; A reads the live state with the ``status`` action.

A target that is offline gets the request later: A keeps it durably
(``app.db.project_setup``) and its dispatch pass sends it when B is back.
Setting a project up on THIS worker goes through the same ``start`` path.

On a legacy shared database (no worker network) there is no control channel:
the ``git_ops`` table itself is shared, so A writes the queued op for B
directly and B's claim pass picks it up when it runs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

SETUP_OP = "setup_project"
ACTION = "project-setup"
OP_PREFIX = "psetup-"
DISPATCH_PASS = "project_setup_dispatch"
DISPATCH_INTERVAL_SECONDS = 15.0
PEER_TIMEOUT_SECONDS = 10.0
HEARTBEAT_SECONDS = 20.0
# A running setup whose process is gone (the worker restarted) is settled
# after this long without a heartbeat.
ZOMBIE_SECONDS = 90.0
# Abort a clone whose transfer stalls (bytes/second below the limit for the time).
LOW_SPEED_LIMIT = "1000"
LOW_SPEED_TIME = "300"
# ... a network clone that goes silent (an SSH server that accepts and then
# never answers is not covered by the HTTP limits above): no output this long ...
CLONE_IDLE_SECONDS = 300.0
# ... and, as a backstop for any clone, one still running after this long
# (generous enough for a big repository on a slow link).
CLONE_DEADLINE_SECONDS = 3600.0
# The transports a new project's first clone may use (GIT_ALLOW_PROTOCOL: it
# overrides every protocol.* setting in any gitconfig, and applies to the URL
# after insteadOf rewrites). No ``file``: a local folder is added with Folder.
NEW_PROJECT_PROTOCOLS = "https:http:ssh"
# Other spellings of a project's remote a setup may fall back to (git@ vs https).
MAX_ALTERNATE_URLS = 4

# Stable error codes (the UI keys its actions off these).
GITHUB_AUTH = "github_auth"
NETWORK = "network"
FOLDER_EXISTS = "folder_exists"
NOT_FOUND = "not_found"
OFFLINE = "offline"
UNSUPPORTED = "unsupported"
INTERNAL = "internal"
CANCELLED = "cancelled"
MOVED = "moved"

_OP_ID_RE = re.compile(r"^psetup-[0-9a-f]{32}$")
# Same rule as ``ProjectCreate.id``.
_PROJECT_ID_RE = re.compile(r"^[a-z0-9-]{1,50}$")
_UNKNOWN_ACTION = "Unknown control action"


class SetupError(RuntimeError):
    """A setup failure with a stable code; ``str()`` is the plain-English message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.error_code = code


class SetupConflict(RuntimeError):
    """The request cannot apply in the op's current state (already finished)."""


class Unsupported(RuntimeError):
    """The peer runs a version without the setup action."""


def unsupported_message(worker_name: Optional[str]) -> str:
    return f"Update {worker_name or 'that worker'} to set projects up from here"


def moved_message(pinned: str) -> str:
    return (f"The project for this repository now uses {display_remote(pinned) or 'another repository'}. "
            "Clone that repository instead.")


def new_op_id() -> str:
    return OP_PREFIX + uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def display_remote(url: Optional[str]) -> Optional[str]:
    """``github.com/owner/repo`` for any remote spelling; never credentials.

    For display and comparison only (never a clone URL): the login of a
    scheme-form SSH URL (``ssh://git@host/...``) is dropped like the scp form's.
    """
    from app.core.scrub import strip_url_userinfo

    if not url:
        return None
    text = strip_url_userinfo(url.strip()) or ""
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://(.*)$", text)
    if match:
        text = re.sub(r"^[^@/]*@", "", match.group(1))
    else:
        scp = re.match(r"^(?:[^@/]+@)?([^:/]+):(?!/)(.*)$", text)
        if scp and len(scp.group(1)) > 1:
            text = f"{scp.group(1)}/{scp.group(2)}"
    text = text.rstrip("/")
    if text.lower().endswith(".git"):
        text = text[:-4]
    return text or None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:50].strip("-")


def _folder_name(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    name = re.split(r"[\\/]+", str(path).rstrip("\\/"))[-1]
    return name or None


def _iso(value: Any) -> Optional[str]:
    if value is None or isinstance(value, str):
        return value
    return value.isoformat()


def _me() -> str:
    return settings.effective_instance_id


def _own_device_id() -> str:
    from app.network import engine

    ctx = engine.context()
    return ctx.device_id if ctx else _me()


def _origin_of(project: Dict[str, Any]) -> str:
    from app.core.project_resolver import origin_for_legacy_project

    return origin_for_legacy_project(project)


def _own_project_rows() -> List[Dict[str, Any]]:
    """This worker's legacy project rows (own ``instance_id``, else unowned legacy)."""
    from app.db import database

    return database.get_all_projects()


def _own_rows_by_origin() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    me = _me()
    for row in _own_project_rows():
        origin = _origin_of(row)
        if origin not in out or row.get("instance_id") == me:
            out[origin] = row
    return out


def _superseded_local_origins(rows: List[Dict[str, Any]]) -> set:
    """``local:<id>`` identities of rows since linked to a remote (#802)."""
    out = set()
    for row in rows:
        synthesized = "local:" + str(row.get("id"))
        if _origin_of(row) != synthesized:
            out.add(synthesized)
    return out


def clone_records(*, clean: bool = True) -> List[Dict[str, Any]]:
    """Every git record this database holds, ready for :func:`clone_candidates`.

    ``clean`` strips userinfo from each URL (what is sent to another worker
    or shown). ``own`` marks this runner's own records (its ``instance_id``,
    else an unowned legacy row, as :func:`_own_project_rows`): it cloned or
    added that spelling itself, so it holds that spelling's credentials.
    """
    from app.core.project_links import legacy_link_group_id, remote_link_ids
    from app.core.scrub import strip_url_userinfo
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT gr.remote_url AS remote_url, gr.default_branch AS default_branch, "
            "p.id AS id, p.link_group_id AS link_group_id, p.instance_id AS instance_id "
            "FROM git_repositories gr JOIN projects p ON p.id = gr.project_id "
            "WHERE gr.remote_url IS NOT NULL AND gr.remote_url <> ''"
        )
        rows = cursor.fetchall()
    me = _me()
    out: List[Dict[str, Any]] = []
    for row in rows:
        url = strip_url_userinfo(row["remote_url"]) if clean else row["remote_url"]
        if not url:
            continue
        out.append({"url": url, "default_branch": row["default_branch"] or None,
                    "own": row["instance_id"] in (None, "", me),
                    "linked": row["link_group_id"] or ("local:" + str(row["id"])),
                    "ids": remote_link_ids(url), "exact": legacy_link_group_id(url)})
    return out


def stored_candidate(pv2: Optional[Dict[str, Any]], *, clean: bool = True) -> Optional[Dict[str, Any]]:
    """The remote stored on a ``projects_v2`` row, for :func:`clone_candidates`."""
    from app.core.scrub import strip_url_userinfo
    from app.db import projects_v2

    url, branch = projects_v2.stored_remote(pv2)
    url = (strip_url_userinfo(url) if clean else url) if url else None
    if not url:
        return None
    settings_ = (pv2 or {}).get("settings") or {}
    pinned = isinstance(settings_, dict) and settings_.get(projects_v2.PINNED_KEY) is True
    return {"url": url, "default_branch": branch, "pinned": pinned}


def clone_candidates(origin: str, records: List[Dict[str, Any]],
                     stored: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The URLs to clone ``origin`` from, in the order to try them, no repeats.

    Each is ``{url, default_branch}``. A project the owner pointed at a
    repository clones exactly that one (``[pin]``). Otherwise: this runner's
    own records that name the origin (it holds that spelling's credentials),
    then the catalog row's stored remote, then other records that name it, a
    record in the origin's own spelling before one that names it only
    through the other spelling (git@ vs https). A ``git:`` origin never gets
    a URL that does not name it (``remote_link_ids``): a fork linked into the
    group is another repository. A ``local:`` or ``manual:`` origin, which no
    URL names, falls back to the records linked to it, as before.
    """
    from app.core.project_links import remote_link_ids

    if stored and stored.get("pinned"):
        return [{"url": stored["url"], "default_branch": stored.get("default_branch")}]
    is_git = origin.startswith("git:")
    naming = [r for r in records if origin in r["ids"]]
    ordered: List[Dict[str, Any]] = []
    ordered += [r for r in naming if r["own"] and r["exact"] == origin]
    ordered += [r for r in naming if r["own"] and r["exact"] != origin]
    if stored and (not is_git or origin in remote_link_ids(stored["url"])):
        ordered.append(stored)
    ordered += [r for r in naming if not r["own"] and r["exact"] == origin]
    ordered += [r for r in naming if not r["own"] and r["exact"] != origin]
    if not is_git:
        linked = [r for r in records if r["linked"] == origin and origin not in r["ids"]]
        ordered += [r for r in linked if r["own"]] + [r for r in linked if not r["own"]]
    out: List[Dict[str, Any]] = []
    seen = set()
    for entry in ordered:
        if entry["url"] in seen:
            continue
        seen.add(entry["url"])
        out.append({"url": entry["url"], "default_branch": entry.get("default_branch")})
    return out


def _repo_info_by_origin() -> Dict[str, Dict[str, Any]]:
    """``{origin: {clone_url, default_branch, alternate_urls}}`` from git
    records this DB holds and the remotes stored on the replicated
    ``projects_v2`` rows (which still know a project no local record has any
    more). ``clone_url`` is the first of :func:`clone_candidates`;
    ``alternate_urls`` the rest, tried in order when a clone fails."""
    from app.db.base import get_db, parse_jsonb

    records = clone_records()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT origin, settings FROM projects_v2")
        stored = {row["origin"]: stored_candidate({"settings": parse_jsonb(row["settings"])})
                  for row in cursor.fetchall()}
    origins = set(stored)
    for record in records:
        origins.add(record["linked"])
        origins.update(record["ids"])
    out: Dict[str, Dict[str, Any]] = {}
    for origin in origins:
        candidates = clone_candidates(origin, records, stored.get(origin))
        if candidates:
            out[origin] = {"clone_url": candidates[0]["url"],
                           "default_branch": candidates[0]["default_branch"],
                           "alternate_urls": [c["url"] for c in candidates[1:]]}
    return out


_remotes_backfilled = False


def ensure_repo_records() -> None:
    """Give this worker's Git projects their repository record (sync, best effort).

    A project added from a folder gets its record (and with it its remote
    identity) on the Git panel's first poll. Until then it would read as a
    plain folder here and to every peer, and a setup elsewhere would make an
    empty folder instead of cloning it. ``ensure_git_repository_record``
    caches each answer for a minute, including "not a Git folder". The first
    call in a process also stores every known remote on its replicated
    ``projects_v2`` row (rows recorded before that existed).
    """
    global _remotes_backfilled
    from app.api.git_helpers import ensure_git_repository_record
    from app.core.project_utils import get_project_working_dir
    from app.db.base import get_db

    try:
        rows = _own_project_rows()
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT project_id FROM git_repositories")
            recorded = {row["project_id"] for row in cursor.fetchall()}
        for row in rows:
            if row["id"] in recorded:
                continue
            try:
                folder = get_project_working_dir(row)
            except (OSError, ValueError):
                continue
            if os.path.isdir(folder):
                ensure_git_repository_record(row["id"], folder)
        if not _remotes_backfilled:
            from app.core.project_links import backfill_project_remotes

            backfill_project_remotes()
            _remotes_backfilled = True
    except Exception:  # noqa: BLE001 - reads must not fail on this
        logger.debug("project setup: repository record check failed", exc_info=True)


def _local_project_infos() -> Dict[str, Dict[str, Any]]:
    """``{project_v2_id: {name, folder_name, clone_url, default_branch}}`` for
    this worker's own projects: what a peer needs to set one up elsewhere."""
    from app.db.projects_v2 import project_v2_id_for_origin

    repos = _repo_info_by_origin()
    out: Dict[str, Dict[str, Any]] = {}
    for origin, row in _own_rows_by_origin().items():
        repo = repos.get(origin) or {}
        out[project_v2_id_for_origin(origin)] = {
            "name": row.get("name"),
            "folder_name": _folder_name(row.get("path")),
            "clone_url": repo.get("clone_url"),
            "default_branch": repo.get("default_branch"),
        }
    return out


# ---------------------------------------------------------------------------
# Target side: start / status / cancel / remove
# ---------------------------------------------------------------------------

def _http_detail(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    return str(detail) if detail else str(exc)


def resolve_folder(folder: str) -> Tuple[str, Path]:
    """Validate a folder name or path on THIS worker, as adding a project does.

    Returns ``(stored_form, absolute_path)``: in Docker mode the stored form is
    workspace-relative; in local mode an absolute path stays absolute. Raises
    ``ValueError`` with a user-facing message.
    """
    from fastapi import HTTPException
    from app.api.projects import normalize_docker_path, refuse_nested_repository, validate_project_path

    text = (folder or "").strip()
    if not text or len(text) > 1024 or "\x00" in text:
        raise ValueError("Choose a folder name for the project")
    try:
        stored = normalize_docker_path(text)
        if not stored:
            raise ValueError("The workspace folder itself can't be a project; choose a folder inside it")
        full = validate_project_path(stored, allow_create=True)
        refuse_nested_repository(full)
    except HTTPException as exc:
        raise ValueError(_http_detail(exc)) from exc
    return stored, full


def _is_recorded_remote(clean: str, origin: str) -> bool:
    """Whether ``clean`` is the remote the project's stored identity names.

    A ``git:`` origin IS the hash of its remote, so a URL that reproduces it is
    that remote. Any other origin can only name one through the remote stored
    on its replicated ``projects_v2`` row.
    """
    from app.core.project_links import same_remote
    from app.db import projects_v2

    from app.core.project_remote import remote_names_origin

    if origin.startswith("git:"):
        return remote_names_origin(clean, origin)
    stored, _branch = projects_v2.stored_remote(projects_v2.get_project_v2_by_origin(origin))
    return bool(stored) and same_remote(stored, clean)


def _validate_clone_url(url: str, origin: str) -> str:
    """The clone source for a setup, refused unless it is safe to clone here.

    The URL never comes from a person: the requesting worker takes it from the
    project's stored records (``project_workers._spec_for``), and only the
    owner can ask (``require_admin``; API keys never reach this). Remote helpers
    and other schemes stay refused. A local path or ``file://`` source is
    accepted when it is the project's own recorded remote (the base-checkout
    self-seed clones that same remote), or when it is inside the workspace or a
    project folder, as ``/git/clone`` allows the owner. ``/git/clone``'s own
    check for typed URLs is unchanged.
    """
    from app.core.git_service import classify_clone_url
    from app.core.scrub import strip_url_userinfo
    from app.api.common import is_path_served

    clean = strip_url_userinfo((url or "").strip()) or ""
    kind, detail = classify_clone_url(clean)
    if kind == "invalid":
        raise ValueError(detail or "Invalid clone URL")
    if kind == "local":
        path = Path(detail or "")
        if not path.is_absolute():
            raise ValueError("A local clone source must be an absolute path")
        if not _is_recorded_remote(clean, origin) and not is_path_served(path, settings.effective_workspace_dir):
            raise ValueError("A local clone source must be the project's own remote, "
                             "or inside the workspace or a project folder")
    from app.core.project_remote import remote_names_origin

    if origin.startswith("git:") and not remote_names_origin(clean, origin):
        raise ValueError("The remote does not match this project")
    return clean


def _valid_alternates(urls: Any, origin: str, first: Optional[str]) -> List[str]:
    """The other spellings a setup may clone if ``first`` is refused, each
    checked as :func:`_validate_clone_url` checks ``first``; one it refuses
    (another repository, an unsafe source) is dropped, never cloned."""
    out: List[str] = []
    if not isinstance(urls, list):
        return out
    for url in urls[:MAX_ALTERNATE_URLS]:
        if not isinstance(url, str) or len(url) > 2048:
            continue
        try:
            clean = _validate_clone_url(url, origin)
        except ValueError:
            logger.warning("project setup: dropped another clone URL for %s: it is not this project's remote",
                           origin)
            continue
        if clean and clean != first and clean not in out:
            out.append(clean)
    return out


def _registered_row(origin: str) -> Optional[Dict[str, Any]]:
    """This worker's project row for ``origin`` whose folder exists, else None."""
    from app.core.project_utils import get_project_working_dir_as_path

    row = _own_rows_by_origin().get(origin)
    if not row:
        return None
    try:
        return row if get_project_working_dir_as_path(row).is_dir() else None
    except (OSError, ValueError):
        return None


def start_local(spec: Dict[str, Any], *, op_id: str, requested_by: Optional[str]) -> Dict[str, Any]:
    """Queue a setup on THIS worker (sync). Idempotent per project while in flight.

    ``spec``: ``project_v2_id``, ``origin``, ``name``, ``kind`` (``git`` or
    ``folder``), ``clone_url`` (git), ``folder``, ``mode`` (``auto``/``adopt``).
    A new project cloned from the New project dialog (``POST /projects/clone``)
    also passes ``project_id`` (the id the owner asked for); whether a failure
    hides the identity again is the persisted ``PROVISIONAL_KEY`` mark on it.
    Raises ``ValueError`` for a request this worker refuses outright.
    """
    from app.db import git_ops as git_ops_db
    from app.db import projects_v2

    if not isinstance(op_id, str) or not _OP_ID_RE.match(op_id):
        raise ValueError("Invalid setup request ID")
    origin = str(spec.get("origin") or "")
    pv2_id = str(spec.get("project_v2_id") or "")
    if not origin or len(origin) > 300 or projects_v2.project_v2_id_for_origin(origin) != pv2_id:
        raise ValueError("The project identity does not match its origin")
    kind = spec.get("kind")
    if kind not in ("git", "folder"):
        raise ValueError("Unknown project kind")
    mode = spec.get("mode") or "auto"
    if mode not in ("auto", "adopt"):
        raise ValueError("Unknown setup mode")
    name = str(spec.get("name") or "").strip()[:100] or "Project"
    project_id = spec.get("project_id")
    if project_id is not None and (not isinstance(project_id, str) or not _PROJECT_ID_RE.match(project_id)):
        raise ValueError("Invalid project ID")

    me = _me()
    device = _own_device_id()
    for row in git_ops_db.list_ops_for_runner(me, SETUP_OP, limit=50):
        if row.get("project_v2_id") == pv2_id and row["status"] in ("queued", "running"):
            return {"id": row["id"], "status": row["status"], "runner_id": device}

    params = {"origin": origin, "name": name, "kind": kind, "mode": mode,
              "clone_url": None, "requested_by": requested_by}
    if project_id is not None:
        params["project_id"] = project_id
    existing = _registered_row(origin)
    if existing is not None:
        # Already set up here: record a finished op so the caller has an answer.
        row = git_ops_db.insert_op(me, SETUP_OP, project_v2_id=pv2_id,
                                   params={**params, "folder": None, "path": None}, op_id=op_id)
        if row["status"] == "queued" and git_ops_db.claim_op(row["id"], me):
            row = git_ops_db.complete_op(row["id"], {"project_id": existing["id"], "already": True,
                                                    "progress": 100}) or row
        return {"id": row["id"], "status": row["status"], "runner_id": device}

    if kind == "git":
        params["clone_url"] = _validate_clone_url(str(spec.get("clone_url") or ""), origin)
        params["alternate_urls"] = _valid_alternates(spec.get("alternate_urls"), origin, params["clone_url"])
    stored, full = resolve_folder(str(spec.get("folder") or ""))
    if mode == "adopt" and not full.is_dir():
        raise ValueError(f"There is no folder at {full} to use")
    if projects_v2.get_project_v2(pv2_id) is None:
        # The identity replicates, but the request can outrun it.
        projects_v2.upsert_project(origin, name=name)
    row = git_ops_db.insert_op(me, SETUP_OP, project_v2_id=pv2_id,
                               params={**params, "folder": stored, "path": str(full)}, op_id=op_id)
    return {"id": row["id"], "status": row["status"], "runner_id": device}


async def start_local_now(spec: Dict[str, Any], *, op_id: str,
                          requested_by: Optional[str]) -> Dict[str, Any]:
    """Queue a setup on THIS worker and start it at once.

    ``start_local`` runs in a worker thread (it reads the database and the
    disk), where there is no event loop to kick the claim pass from, so a
    queued setup used to wait for the next scheduled pass: up to the slow
    single-install cadence on a worker that had just paired. Kick from here,
    on the loop, after the row is written.
    """
    from app.db.async_database import run_sync

    result = await run_sync(start_local, spec, op_id=op_id, requested_by=requested_by)
    if result.get("status") == "queued":
        from app.core import git_ops_runner

        git_ops_runner.kick()
    return result


def _summarize_op(row: Dict[str, Any]) -> Dict[str, Any]:
    result = row.get("result") or {}
    status = row.get("status")
    error_code = result.get("error_code") if status == "error" else None
    error = result.get("error") if status == "error" else None
    if status == "error" and not error_code:
        # An older worker on a shared database failed the row it did not know.
        if str(error or "").startswith("unknown git op"):
            error_code, error = UNSUPPORTED, None
        else:
            error_code = INTERNAL
    progress = result.get("progress")
    if status == "done":
        progress = 100
    return {
        "op_id": row.get("id"),
        "project_v2_id": row.get("project_v2_id"),
        "status": status,
        "progress": progress if isinstance(progress, int) else None,
        "error_code": error_code,
        "error": error,
        "updated_at": _iso(row.get("updated_at")),
    }


def latest_ops(runner_id: str) -> Dict[str, Dict[str, Any]]:
    """Newest non-cancelled setup op per project on ``runner_id``.

    A ``running`` op whose heartbeat went stale (its runner stopped; its own
    restart sweep settles it) reads as no op: it never shows as setting up.
    """
    from app.db import git_ops as git_ops_db

    out: Dict[str, Dict[str, Any]] = {}
    for row in git_ops_db.list_ops_for_runner(runner_id, SETUP_OP):
        pv2 = row.get("project_v2_id")
        if not pv2 or pv2 in out:
            continue
        if git_ops_db._is_stale_running(row):
            out[pv2] = {}
            continue
        summary = _summarize_op(row)
        if summary["error_code"] == CANCELLED:
            out[pv2] = {}  # a cancel hides older outcomes, then the slot reads as no op
            continue
        out[pv2] = summary
    return {pv2: op for pv2, op in out.items() if op}


def _checkout_detail(project_v2_id: str, runner_id: str) -> Dict[str, Any]:
    """Folder, branch and change count of ``runner_id``'s copy (this DB's view)."""
    from app.core.git_service import git_service
    from app.core.project_utils import get_project_working_dir
    from app.db import checkouts as checkouts_db
    from app.core.project_resolver import resolve_catalog_project

    detail: Dict[str, Any] = {"folder": None, "branch": None, "dirty": None, "changed_files": None}
    pv2 = resolve_catalog_project(project_v2_id)
    if not pv2:
        return detail
    row = _own_rows_by_origin().get(pv2.get("origin") or "") if runner_id == _me() else None
    if row is None:
        return detail
    try:
        folder = get_project_working_dir(row)
    except (OSError, ValueError):
        return detail
    detail["folder"] = folder
    if row.get("instance_id") == runner_id:
        from app.core.project_folders import revision

        detail["folder_revision"] = revision(row)
    base = next((ck for ck in checkouts_db.list_checkouts(pv2["id"])
                 if ck.get("runner_id") == runner_id and not ck.get("branch")), None)
    if base is not None and base.get("dirty") is not None:
        detail["dirty"] = bool(base["dirty"])
    if os.path.isdir(folder) and git_service.is_git_repo(folder):
        status = git_service.get_status(folder)
        if status.get("is_git_repo"):
            detail["branch"] = status.get("current_branch")
            changed = set()
            for key in ("staged", "modified", "untracked", "conflicts"):
                for item in status.get(key) or []:
                    changed.add(item.get("path") if isinstance(item, dict) else str(item))
            detail["changed_files"] = len(changed)
            detail["dirty"] = bool(changed)
    return detail


def local_status(project_v2_id: Optional[str] = None) -> Dict[str, Any]:
    """What this worker tells a requester: catalog, setup ops, project infos, and
    with ``project_v2_id`` the folder/branch/changes of its copy (sync)."""
    from app.db import checkouts as checkouts_db

    from app.db.projects_v2 import project_v2_id_for_origin

    ensure_repo_records()
    me = _me()
    # A remote-less identity this worker since linked to its remote (#802)
    # keeps its base checkout but is no longer a separate project here.
    superseded = {project_v2_id_for_origin(origin)
                  for origin in _superseded_local_origins(_own_project_rows())}
    out: Dict[str, Any] = {
        "project_ids": [pid for pid in checkouts_db.eligible_base_project_ids(me) if pid not in superseded],
        "workspace": str(settings.effective_workspace_dir),
        "ops": list(latest_ops(me).values()),
        "projects": _local_project_infos(),
    }
    if project_v2_id:
        out["detail"] = _checkout_detail(project_v2_id, me)
        # The Workers tab's remove confirmation names these.
        removal = local_removal_preview(project_v2_id)
        out["detail"]["removal_chats"] = removal["chats"] if removal else None
        out["detail"]["removal_worktrees"] = removal["worktrees"] if removal else None
    return out


@dataclass
class _Job:
    op_id: str
    requested_by: Optional[str]
    loop: Optional[asyncio.AbstractEventLoop]
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancelled: bool = False
    finalizing: bool = False
    proc: Optional[subprocess.Popen] = None
    temp_dir: Optional[str] = None
    last_progress: Optional[int] = None
    last_notified: float = 0.0


# A fast clone crosses every 10% step within milliseconds; each step would be
# a control call to the requesting worker. Its pages poll as well, so at most
# one progress notice a second is enough (the finish always notifies).
PROGRESS_NOTIFY_SECONDS = 1.0

_jobs: Dict[str, _Job] = {}
_tasks: set = set()
_clone_slots = threading.BoundedSemaphore(2)


def cancel_local(op_id: str) -> Dict[str, Any]:
    """Cancel a queued or running setup on THIS worker (sync).

    Raises ``LookupError`` for an unknown op and :class:`SetupConflict` when it
    already finished.
    """
    from app.core.git_service import _kill_git_tree
    from app.db import git_ops as git_ops_db

    me = _me()
    row = git_ops_db.get_op(op_id)
    if row is None or row.get("op") != SETUP_OP or row.get("runner_id") != me:
        raise LookupError("Unknown setup")
    if git_ops_db.cancel_queued(op_id, me, "Setup was cancelled."):
        hide_failed_first_clone(row)
        return {"status": "cancelled"}
    job = _jobs.get(op_id)
    if job is not None:
        with job.lock:
            if not job.finalizing:
                job.cancelled = True
                proc = job.proc
            else:
                proc = None
                job = None
        if job is not None:
            if proc is not None and proc.poll() is None:
                _kill_git_tree(proc)
            return {"status": "cancelled"}
    raise SetupConflict("This setup already finished")


def _local_removal_ids(project_v2_id: str) -> List[str]:
    """THIS worker's project record ids for the logical project (sync)."""
    from app.db import projects_v2
    from app.network import engine

    pv2 = projects_v2.get_project_v2(project_v2_id)
    if pv2 is None:
        raise LookupError("Unknown project")
    ids = [row["id"] for row in _own_project_rows()
           if _origin_of(row) == pv2.get("origin") and _removable_here(row, engine.context())]
    if not ids:
        raise LookupError("This worker has no record of the project")
    return ids


def _removable_here(row: Dict[str, Any], network_context: Any) -> bool:
    """The Workers tab's rule for removing a project row on this worker.

    The row must belong to this worker; an unowned legacy row counts only
    while networked (``_local_removal_ids``). The removal checks it again on
    the locked row before deleting it.
    """
    owner = row.get("instance_id")
    return owner == _me() or (owner is None and network_context is not None)


def local_removal_preview(project_v2_id: str) -> Optional[Dict[str, Any]]:
    """What :func:`remove_local` would remove here, or None without a record (sync)."""
    from app.core.project_removal import preview

    try:
        return preview(_local_removal_ids(project_v2_id))
    except LookupError:
        return None


async def remove_local(project_v2_id: str) -> Dict[str, Any]:
    """Delete THIS worker's project record(s) for the logical project.

    Same as deleting the project here: the record and its chats on this
    worker go (``app/core/project_removal.py``), the files stay. Raises
    ``LookupError`` when this worker has no record.
    """
    from app.core.project_removal import remove_projects
    from app.db.async_database import run_sync
    from app.network import engine

    ids = await run_sync(_local_removal_ids, project_v2_id)
    networked = engine.context()
    return await remove_projects(ids, owned=lambda row: _removable_here(row, networked))


# ---------------------------------------------------------------------------
# Target side: the executor (runs in a thread, from git_ops_runner)
# ---------------------------------------------------------------------------

def _same_repo(path: Path, clone_url: Optional[str], origin: str) -> bool:
    from app.core.git_service import git_service
    from app.core.project_links import same_remote

    if not path.is_dir() or not git_service.is_git_repo(str(path)):
        return False
    remote = git_service.get_remote_url(str(path))
    if not remote:
        return False
    if origin.startswith("git:"):
        from app.core.project_remote import remote_names_origin

        return remote_names_origin(remote, origin)
    return bool(clone_url) and same_remote(remote, clone_url)


def _is_empty_dir(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            return path.is_dir() and not path.is_symlink() and next(entries, None) is None
    except OSError:
        return False


_PROGRESS_RE = re.compile(r"^(Receiving objects|Resolving deltas|Updating files|Checking out files):\s+(\d{1,3})%")
_PHASES = {"Receiving objects": (5, 75), "Resolving deltas": (80, 15),
           "Updating files": (95, 5), "Checking out files": (95, 5)}


def parse_progress(line: str) -> Optional[int]:
    """Overall percent for one ``git clone --progress`` line, else None."""
    match = _PROGRESS_RE.match(line.strip().removeprefix("remote: "))
    if not match:
        return None
    start, span = _PHASES[match.group(1)]
    return min(100, start + span * min(100, int(match.group(2))) // 100)


_AUTH_HINTS = ("could not read username", "could not read password", "authentication failed",
               "terminal prompts disabled", "permission denied (publickey", "invalid username or password",
               "returned error: 401", "returned error: 403", "password authentication was removed",
               "host key verification failed")
_NOT_FOUND_HINTS = ("repository not found", "not found", "does not appear to be a git repository",
                    "returned error: 404", "does not exist")
_NETWORK_HINTS = ("could not resolve host", "could not resolve hostname", "connection timed out",
                  "failed to connect", "network is unreachable", "connection refused",
                  "operation timed out", "early eof", "the remote end hung up", "rpc failed",
                  "ssl", "tls", "unable to access", "connection reset", "timed out",
                  "operation too slow")


def classify_clone_failure(stderr: str, clone_url: str, *,
                           github_signed_in=None) -> SetupError:
    """Map ``git clone`` output to a :class:`SetupError` without leaking it."""
    from app.core.scrub import redact_url_credentials

    text = (stderr or "").lower()
    on_github = "github.com" in (clone_url or "").lower()
    if re.search(r"transport '[^']*' not allowed", text):
        # NEW_PROJECT_PROTOCOLS refused what the source became (a folder here).
        return SetupError(INTERNAL, "This URL leads to a folder on this worker, not a network repository. "
                                    "To add a folder on this worker, use Folder.")
    if any(hint in text for hint in _AUTH_HINTS):
        return SetupError(GITHUB_AUTH, (
            "This worker isn't signed in to GitHub, or its account can't access this repository."
            if on_github else "This worker has no access to the repository (sign-in or SSH key)."))
    if any(hint in text for hint in _NOT_FOUND_HINTS):
        # GitHub answers "not found" for a private repository it will not show.
        signed_in = True
        if on_github:
            try:
                signed_in = bool(github_signed_in() if github_signed_in else _github_signed_in())
            except Exception:  # noqa: BLE001 - classification must not fail
                signed_in = True
        if on_github and not signed_in:
            return SetupError(GITHUB_AUTH, (
                "This worker isn't signed in to GitHub, so it can't see this repository "
                "(it may be private)."))
        return SetupError(NOT_FOUND, "The repository wasn't found. It may have been deleted, renamed, "
                                     "or this worker's account can't see it.")
    if any(hint in text for hint in _NETWORK_HINTS):
        return SetupError(NETWORK, "This worker couldn't reach the repository host. Check its network "
                                   "connection and try again.")
    last = next((line.strip() for line in reversed((stderr or "").splitlines()) if line.strip()), "")
    detail = redact_url_credentials(last)[:200] if last else "unknown error"
    return SetupError(INTERNAL, f"Git couldn't clone the repository: {detail}")


def _github_signed_in() -> bool:
    from app.core.auth import is_github_authenticated_cached

    return is_github_authenticated_cached()


def _report_progress(job: _Job, value: int) -> None:
    from app.db import git_ops as git_ops_db

    if job.last_progress is not None and value < job.last_progress + 10 and value < 100:
        return
    job.last_progress = value
    try:
        git_ops_db.set_running_result(job.op_id, {"progress": value, "temp_dir": job.temp_dir})
    except Exception:  # noqa: BLE001 - progress is display only
        logger.debug("project setup: progress write failed for %s", job.op_id, exc_info=True)
    now = time.monotonic()
    if now - job.last_notified < PROGRESS_NOTIFY_SECONDS:
        return
    job.last_notified = now
    _notify_threadsafe(job)


def _clone_command(clone_url: str, temp: str, *, network_only: bool = False) -> List[str]:
    # ``--no-local``: a source that names an existing folder (git looks for one
    # before it reads ``host:path`` as SSH) goes through the file transport,
    # which GIT_ALLOW_PROTOCOL then refuses, instead of a direct local copy.
    return ["git", "-c", f"http.lowSpeedLimit={LOW_SPEED_LIMIT}", "-c", f"http.lowSpeedTime={LOW_SPEED_TIME}",
            "clone", "--progress", *(["--no-local"] if network_only else []), "--", clone_url, temp]


def _clone(job: _Job, clone_url: str, final_path: Path, *, network_only: bool = False) -> None:
    """Clone into a temporary sibling, then rename it to ``final_path``.

    ``network_only`` (a new project's first clone, ``POST /projects/clone``):
    git may use only :data:`NEW_PROJECT_PROTOCOLS`, whatever the source turns
    into (an ``insteadOf`` rewrite, a folder named like ``host:path``).
    Setting up an existing project may copy from a local source as before.
    """
    from fluid.tools.child_env import scrub_child_env
    from app.core.git_service import _IS_WINDOWS, _kill_git_tree, _quiet_git_env, classify_clone_url, git_service
    from app.db import git_ops as git_ops_db

    parent = final_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp = tempfile.mkdtemp(prefix=f".{final_path.name}.setup-", dir=str(parent))
    with job.lock:
        job.temp_dir = temp
    try:
        git_ops_db.set_running_result(job.op_id, {"progress": None, "temp_dir": temp})
    except Exception:  # noqa: BLE001
        logger.debug("project setup: could not record temp dir for %s", job.op_id, exc_info=True)
    env = {**scrub_child_env(), "GIT_TERMINAL_PROMPT": "0"}
    _quiet_git_env(env)
    if network_only:
        env["GIT_ALLOW_PROTOCOL"] = NEW_PROJECT_PROTOCOLS
    command = _clone_command(clone_url, temp, network_only=network_only)
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
             if _IS_WINDOWS else {"start_new_session": True})
    tail: List[str] = []
    started = time.monotonic()
    last_output = [started]
    # Only a network clone reports steadily (``--progress`` covers receiving
    # and checkout); a local copy may be quiet while it works.
    watch_idle = classify_clone_url(clone_url)[0] == "remote"
    stopped: List[str] = []  # why the watchdog killed the clone: "idle" or "deadline"
    done = threading.Event()

    def watchdog(proc: subprocess.Popen) -> None:
        while not done.wait(min(5.0, CLONE_IDLE_SECONDS / 4, CLONE_DEADLINE_SECONDS / 4)):
            now = time.monotonic()
            reason = ("deadline" if now - started > CLONE_DEADLINE_SECONDS
                      else "idle" if watch_idle and now - last_output[0] > CLONE_IDLE_SECONDS else None)
            if reason:
                stopped.append(reason)
                _kill_git_tree(proc)
                return

    try:
        with _clone_slots:
            with job.lock:
                if job.cancelled:
                    raise SetupError(CANCELLED, "Setup was cancelled.")
                proc = subprocess.Popen(command, cwd=str(parent), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, **group)
                job.proc = proc
            threading.Thread(target=watchdog, args=(proc,), name=f"clone-watchdog-{job.op_id}",
                             daemon=True).start()
            try:
                buffer = ""
                assert proc.stderr is not None
                while True:
                    chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else proc.stderr.read(4096)
                    if not chunk:
                        break
                    last_output[0] = time.monotonic()
                    buffer += chunk.decode("utf-8", "replace")
                    *lines, buffer = re.split(r"[\r\n]", buffer)
                    for line in lines:
                        if not line.strip():
                            continue
                        tail = (tail + [line])[-40:]
                        value = parse_progress(line)
                        if value is not None:
                            _report_progress(job, value)
                if buffer.strip():
                    tail = (tail + [buffer])[-40:]
                returncode = proc.wait()
            except BaseException:
                _kill_git_tree(proc)
                raise
            finally:
                done.set()
        with job.lock:
            if job.cancelled:
                raise SetupError(CANCELLED, "Setup was cancelled.")
            job.finalizing = True
        if stopped == ["idle"]:
            raise SetupError(NETWORK, "The repository host stopped answering, so the clone was stopped. "
                                      "Check this worker's network connection and try again.")
        if stopped == ["deadline"]:
            raise SetupError(NETWORK, f"The clone was still running after {int(CLONE_DEADLINE_SECONDS // 60)} "
                                      "minutes, so it was stopped. Check this worker's network connection "
                                      "and try again.")
        if returncode != 0:
            raise classify_clone_failure("\n".join(tail), clone_url)
        git_service.invalidate_status_cache(temp)
        if final_path.exists() or final_path.is_symlink():
            if not _is_empty_dir(final_path):
                raise SetupError(FOLDER_EXISTS, f"{final_path} appeared while cloning. Choose another folder.")
            os.rmdir(final_path)
        os.rename(temp, final_path)
        temp = None
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        with job.lock:
            job.proc = None
            job.temp_dir = None


def remote_kind(url: Optional[str]) -> str:
    """``SSH`` / ``HTTPS`` / … for a log line: which spelling, never the URL."""
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*)://", url or "")
    if not match:
        return "SSH" if re.match(r"^[^/\\]+:", url or "") else "a local path"
    scheme = match.group(1).lower()
    return "SSH" if "ssh" in scheme else scheme.upper()


def retries_next_url(error: SetupError) -> bool:
    """Whether a refused clone may try the project's next URL: sign-in or not
    found, which another spelling (git@ vs https) can answer differently."""
    return error.error_code in (GITHUB_AUTH, NOT_FOUND)


def _clone_any(job: _Job, urls: List[str], final_path: Path, *, network_only: bool = False) -> str:
    """:func:`_clone` from the first of ``urls`` that this worker may read.

    Each is a spelling of the project's one repository; this worker may hold
    credentials for only one of them, so a refused clone tries the next.
    Any other failure (network, cancel, the folder) stops at once.
    """
    for index, url in enumerate(urls):
        try:
            _clone(job, url, final_path, network_only=network_only)
        except SetupError as exc:
            if index + 1 >= len(urls) or not retries_next_url(exc):
                raise
            logger.info("project setup: %s couldn't clone over %s (%s); trying another spelling",
                        job.op_id, remote_kind(url), exc.error_code)
            with job.lock:
                job.finalizing = False  # the next clone can still be cancelled
            continue
        if index:
            logger.info("project setup: %s cloned over %s", job.op_id, remote_kind(url))
        return url
    raise SetupError(INTERNAL, "The setup request has no clone URL")


def _is_this_project(origin: str, row: Dict[str, Any]) -> bool:
    """Whether the request's identity is this worker's project ``row``: its
    current identity, or a remote-less placeholder this worker has PROVABLY
    linked to it (its local first-link record, #802). A matching name or legacy
    id is not proof: another worker's ``local:demo`` is not our ``demo``."""
    from app.db import project_catalog_aliases
    from app.db.projects_v2 import project_v2_id_for_origin

    current = _origin_of(row)
    if origin == current:
        return True
    if not origin.startswith("local:"):
        return False
    return project_catalog_aliases.replacement_for(project_v2_id_for_origin(origin)) == \
        project_v2_id_for_origin(current)


def execute_setup(op_row: Dict[str, Any]) -> Dict[str, Any]:
    """Run one claimed ``setup_project`` op on THIS worker (sync, in a thread).

    Returns the op's result; raises :class:`SetupError` with a stable code.
    Never deletes or overwrites a folder it did not create. A plain folder
    project uses an existing folder as it is, unless that folder is this
    worker's own other project.

    A new project's first clone (``project_id``) registers nothing until the
    clone succeeded; when it fails or is cancelled a provisional identity is
    hidden again (:func:`hide_failed_first_clone`), so no half-made project is
    left on the Projects page and a retry starts clean.
    """
    try:
        result = _execute_setup(op_row)
    except BaseException:
        hide_failed_first_clone(op_row)
        raise
    params = op_row.get("params") or {}
    if params.get("origin"):
        from app.db import projects_v2

        try:
            # Registered here (or an existing row adopted): listed, whatever an
            # earlier removal or failed first clone left on the identity.
            projects_v2.mark_registered(params["origin"])
        except Exception:  # noqa: BLE001 - the project is registered; the mark only affects listing
            logger.warning("project setup: could not clear the hidden mark of %s", op_row.get("id"),
                           exc_info=True)
    return result


def setup_holder(project_v2_ids: List[str]) -> Optional[Dict[str, Any]]:
    """The runner on this database where an ordinary setup (the Workers tab)
    of one of these identities is waiting or running, as ``{runner_id}``;
    else None (sync).

    Only what this database holds: every runner's ops on it (a stale running
    one is left out). A setup handed to a paired worker with its own
    database is not known here. A new project's first clone is left out:
    its provisional mark says which op it is.
    """
    from app.db import git_ops as git_ops_db

    for row in git_ops_db.list_inflight_for_projects(SETUP_OP, [pid for pid in project_v2_ids if pid]):
        if not (row.get("params") or {}).get("project_id"):
            return {"runner_id": row.get("runner_id")}
    return None


def hide_failed_first_clone(op_row: Dict[str, Any]) -> None:
    """Hide a new project's identity again after its first clone ended without a project.

    Every terminal path of a first-clone op calls this: the executor, a cancel
    before it ran (on this worker or, on a shared database, another's), the
    restart sweep, and a request refused while starting. The identity is
    hidden only while this op's provisional mark is still on it
    (``projects_v2.settle_provisional``, one locked compare-and-set): never
    once a later attempt replaced the mark or a copy was registered (which
    clears it), nor while an ordinary setup of it (the Workers tab) waits or
    runs on this database (:func:`setup_holder`). Best effort: the op's own
    failure is what the caller needs.
    """
    from app.db import projects_v2

    params = op_row.get("params") or {}
    origin = params.get("origin")
    if not params.get("project_id") or not origin or not op_row.get("id"):
        return
    try:
        if setup_holder([projects_v2.project_v2_id_for_origin(origin)]) is not None:
            return  # wanted on a worker: its setup registers it or shows its failure
        projects_v2.settle_provisional(origin, op_row.get("runner_id") or _me(), op_row["id"])
    except Exception:  # noqa: BLE001
        logger.warning("project setup: could not hide failed new project %s", op_row.get("id"), exc_info=True)


def _execute_setup(op_row: Dict[str, Any]) -> Dict[str, Any]:
    from app.db import git_ops as git_ops_db

    op_id = op_row["id"]
    job = _jobs.get(op_id) or _jobs.setdefault(op_id, _Job(op_id, None, None))
    git_ops_db.touch_op(op_id)
    params = op_row.get("params") or {}
    pv2_id = op_row.get("project_v2_id") or ""
    origin = params.get("origin") or ""
    kind = params.get("kind")
    mode = params.get("mode") or "auto"
    clone_url = params.get("clone_url")
    # Other spellings to try if ``clone_url`` is refused: never for a new
    # project (it clones exactly what the owner entered) or a pinned one.
    alternates = [] if params.get("project_id") else _valid_alternates(params.get("alternate_urls"), origin,
                                                                       clone_url)
    if kind == "git" and origin:
        from app.core.project_remote import pinned_remote
        from app.db import projects_v2

        # A request queued before the project moved to another repository
        # still carries the old URL: a new copy clones the current one.
        pinned = pinned_remote(projects_v2.get_project_v2_by_origin(origin))
        if pinned:
            alternates = []
        if pinned and params.get("project_id"):
            # A new project clones exactly the repository the owner entered.
            from app.core.project_links import same_remote

            if not same_remote(pinned, clone_url):
                raise SetupError(MOVED, moved_message(pinned))
        elif pinned:
            try:
                clone_url = _validate_clone_url(pinned, origin)
            except ValueError as exc:
                raise SetupError(INTERNAL, str(exc)) from exc
    existing = _registered_row(origin) if origin else None
    if existing is not None:
        return {"project_id": existing["id"], "already": True, "progress": 100}
    if not params.get("folder"):
        raise SetupError(INTERNAL, "The setup request has no folder")
    path_text = params.get("path")
    if not path_text:
        # Queued straight into a shared database: validate the folder here.
        try:
            params["folder"], resolved = resolve_folder(params["folder"])
        except ValueError as exc:
            raise SetupError(INTERNAL, str(exc)) from exc
        path_text = str(resolved)
        params["path"] = path_text
    path = Path(path_text)
    registered = _own_row_at(path)
    if registered is not None and path.is_dir() and _is_this_project(origin, registered):
        # This worker's own copy already lives in that folder. Nothing to set up.
        return {"project_id": registered["id"], "already": True, "adopted": True, "progress": 100}
    if (kind == "folder" and registered is not None and _origin_of(registered) != origin
            and (mode != "adopt" or registered.get("link_group_id") not in (None, "", "local:" + str(registered["id"])))):
        # The folder is this worker's own other project: using it would relink
        # that project (its identity, its chats) to this one. Only an explicit
        # "use a folder already there" may take an unlinked one; never a linked one.
        raise SetupError(FOLDER_EXISTS, f"{path} is already the project {registered.get('name') or registered['id']} "
                                        "on this worker. Choose another folder.")
    adopted = False
    # Only a new project's first clone carries ``project_id`` (POST /projects/clone).
    network_only = bool(params.get("project_id"))
    if kind == "git":
        if path.exists() or path.is_symlink():
            if _same_repo(path, clone_url, origin):
                adopted = True
            elif mode == "auto" and _is_empty_dir(path):
                _clone_any(job, [clone_url, *alternates], path, network_only=network_only)
            else:
                raise SetupError(FOLDER_EXISTS, f"{path} already exists and isn't a copy of this project. "
                                                "Choose another folder.")
        elif mode == "adopt":
            raise SetupError(NOT_FOUND, f"There is no folder at {path} to use.")
        else:
            _clone_any(job, [clone_url, *alternates], path, network_only=network_only)
    elif kind == "folder":
        if path.exists() or path.is_symlink():
            if not path.is_dir() or path.is_symlink():
                raise SetupError(FOLDER_EXISTS, f"{path} already exists. Choose another folder.")
            # A plain folder project has no files to bring: the folder already
            # there is used as it is (never written to, emptied or replaced).
            adopted = True
        elif mode == "adopt":
            raise SetupError(NOT_FOUND, f"There is no folder at {path} to use.")
        else:
            path.mkdir(parents=True, exist_ok=False)
    else:
        raise SetupError(INTERNAL, "Unknown project kind")
    with job.lock:
        if job.cancelled and not job.finalizing:
            raise SetupError(CANCELLED, "Setup was cancelled.")
        job.finalizing = True
    project_id = register_local_project(pv2_id, origin=origin, name=params.get("name") or path.name,
                                        folder=params["folder"], path=path, kind=kind,
                                        project_id=params.get("project_id"))
    return {"project_id": project_id, "adopted": adopted, "progress": 100}


def _unique_name(name: str) -> str:
    """A name no project here has, within the project-name limit (100).

    The number that makes it unique takes room from the name, as in
    ``_unique_id``: a name over the limit is refused at registration, which
    happens after the folder has been created or cloned.
    """
    name = name[:100]
    taken = {str(row.get("name") or "").strip().casefold() for row in _own_project_rows()}
    candidate, n = name, 2
    while candidate.strip().casefold() in taken:
        suffix = f" {n}"
        candidate = name[:100 - len(suffix)] + suffix
        n += 1
    return candidate


def _unique_id(seed: str) -> str:
    from app.db import database

    base = _slug(seed) or "project"
    candidate, n = base, 2
    while database.get_project(candidate) is not None:
        suffix = f"-{n}"
        candidate = base[:50 - len(suffix)] + suffix
        n += 1
    return candidate


def register_local_project(project_v2_id: str, *, origin: str, name: str, folder: str,
                           path: Path, kind: str, project_id: Optional[str] = None) -> str:
    """Register the folder as this worker's copy of the logical project (sync).

    A normal project row (this worker's ``instance_id``) through the same
    creation path as adding a project here, linked to the logical project's
    origin (auto-linked from the remote for a ``git:`` origin, a locked manual
    link otherwise), the git repository record, and the base checkout the
    catalog and ``ensure_checkout`` read. ``project_id`` is the id and ``name``
    the exact name the owner chose for a new project (checked before the clone
    started); otherwise both are made unique here.
    """
    from app.api.git_helpers import ensure_git_repository_record
    from app.api.projects import create_project_record_sync
    from app.core.git_ops_runner import _record_checkout_state
    from app.core.models import ProjectCreate
    from app.core.project_links import LINK_LOCKED_KEY
    from app.core.project_utils import get_project_working_dir_as_path
    from app.db import checkouts as checkouts_db
    from app.db import database
    from fastapi import HTTPException

    resolved = path.resolve()
    project = _own_row_at(path)
    if project is None:
        try:
            # Created under the logical identity from the start: a row first
            # seeded as ``local:<id>`` and then linked left that superseded
            # identity behind as a second, stale project.
            chosen = ProjectCreate(id=project_id, name=name[:100], path=folder) if project_id else ProjectCreate(
                id=_unique_id(path.name or name), name=_unique_name(name), path=folder)
            project = create_project_record_sync(chosen, link_group_id=origin,
                                                 link_locked=not origin.startswith("git:"))
        except HTTPException as exc:
            raise SetupError(INTERNAL, f"Couldn't register the project: {_http_detail(exc)}") from exc
    project_id = project["id"]
    if not origin.startswith("git:") and project.get("link_group_id") != origin:
        merged = dict(project.get("settings") or {})
        merged[LINK_LOCKED_KEY] = True
        database.update_project(project_id, settings_dict=merged)
        database.set_project_link_group(project_id, origin)
    working_dir = str(get_project_working_dir_as_path(database.get_project(project_id)))
    if kind == "git":
        ensure_git_repository_record(project_id, working_dir)
    current = database.get_project(project_id) or {}
    if origin.startswith("git:") and current.get("link_group_id") != origin:
        database.set_project_link_group(project_id, origin)
    checkouts_db.upsert_checkout(project_v2_id, _me(), str(resolved), status="active")
    if kind == "git":
        _record_checkout_state(project_v2_id, str(resolved), None)
    return project_id


def _own_row_at(path: Path) -> Optional[Dict[str, Any]]:
    """This worker's project row whose folder is ``path`` (resolved), else None."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    return next((row for row in _own_project_rows() if _safe_resolve(row) == resolved), None)


def _safe_resolve(row: Dict[str, Any]) -> Optional[Path]:
    from app.core.project_utils import get_project_working_dir_as_path

    try:
        return get_project_working_dir_as_path(row).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


# ---------------------------------------------------------------------------
# Target side: lifecycle around the executor (event loop)
# ---------------------------------------------------------------------------

def spawn(op_row: Dict[str, Any]) -> None:
    """Run an already-claimed setup op in the background (not on the claim pass,
    so a long clone never holds up worktree ops)."""
    op_id = op_row["id"]
    loop = asyncio.get_running_loop()
    _jobs[op_id] = _Job(op_id, (op_row.get("params") or {}).get("requested_by"), loop)
    task = loop.create_task(_run(op_row), name=f"project-setup-{op_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run(op_row: Dict[str, Any]) -> None:
    from app.core import git_ops_runner
    from app.db import git_ops as git_ops_db

    op_id = op_row["id"]
    job = _jobs[op_id]
    _notify(job.requested_by)

    async def heartbeat():
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await asyncio.to_thread(git_ops_db.touch_op, op_id)

    beat = asyncio.create_task(heartbeat())
    try:
        await git_ops_runner.run_op(op_row)
    except asyncio.CancelledError:
        with job.lock:
            job.cancelled = True
            proc = job.proc
        if proc is not None:
            from app.core.git_service import _kill_git_tree
            await asyncio.to_thread(_kill_git_tree, proc)
        raise
    finally:
        beat.cancel()
        _jobs.pop(op_id, None)
    final = await asyncio.to_thread(git_ops_db.get_op, op_id)
    if final and final.get("status") == "done":
        from app.network.project_catalog import announce_soon
        announce_soon()
    _notify(job.requested_by)


def _notify_threadsafe(job: _Job) -> None:
    if job.loop is None:
        return
    try:
        job.loop.call_soon_threadsafe(_notify, job.requested_by)
    except RuntimeError:
        pass  # the loop closed (shutdown)


def _notify(requested_by: Optional[str]) -> None:
    """Refresh this worker's open pages and tell the requesting worker (best effort)."""
    emit_changed()
    if not requested_by or requested_by == _own_device_id():
        return
    from app.network import engine

    if engine.context() is None:
        return

    async def tell():
        from app.network import control
        from app.network.project_catalog import ACTION as CHANGED, ANNOUNCE_TIMEOUT
        try:
            async with asyncio.timeout(ANNOUNCE_TIMEOUT):
                await control.call(requested_by, CHANGED, {}, timeout=ANNOUNCE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - the requester also polls
            logger.debug("project setup: change not delivered to %s: %s", requested_by, type(exc).__name__)

    try:
        task = asyncio.get_running_loop().create_task(tell())
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def emit_changed() -> None:
    from app.core.global_events import global_events

    for kind in ("admin", "api_user", "account", "paired"):
        global_events.resources_changed("projects", user_type=kind)


async def cancel_all() -> None:
    """Shutdown: stop running clones (their temp folders are removed)."""
    for task in list(_tasks):
        task.cancel()


def settle_zombies() -> Optional[int]:
    """Fail setup ops left ``running`` by a previous process, removing their temp folder.

    Returns how many it settled, or None while a running row is too fresh to
    judge (a heartbeat may still come); the caller checks again later.
    """
    from app.db import git_ops as git_ops_db

    settled, undecided = 0, False
    for row in git_ops_db.list_ops_for_runner(_me(), SETUP_OP, limit=50):
        if row["status"] != "running" or row["id"] in _jobs:
            continue
        if _age_seconds(row.get("updated_at")) < ZOMBIE_SECONDS:
            undecided = True
            continue
        temp = (row.get("result") or {}).get("temp_dir")
        path = (row.get("params") or {}).get("path")
        if temp and path and Path(temp).parent == Path(path).parent \
                and Path(temp).name.startswith(f".{Path(path).name}.setup-"):
            shutil.rmtree(temp, ignore_errors=True)
        git_ops_db.fail_op(row["id"], "Setup stopped when the worker restarted. Try again.", code=INTERNAL)
        hide_failed_first_clone(row)
        settled += 1
    return None if undecided else settled


def _age_seconds(value: Any) -> float:
    from datetime import datetime, timezone
    from app.db.git_ops import _as_aware_utc

    when = _as_aware_utc(value)
    if when is None:
        return float("inf")
    return (datetime.now(timezone.utc) - when).total_seconds()


# ---------------------------------------------------------------------------
# Control action (owner side of the peer channel)
# ---------------------------------------------------------------------------

async def handle_control(body: Dict[str, Any], from_device: str) -> Dict[str, Any]:
    """``project-setup`` control action: ``start`` / ``status`` / ``cancel`` / ``remove``."""
    from app.db.async_database import run_sync
    from app.network import engine, git as network_git

    ctx = engine.context()
    if ctx is None:
        raise PermissionError("Worker is not enrolled")
    await run_sync(network_git.device_id, ctx.device_id)  # refuse after local revocation
    action = body.get("action")
    if action == "status":
        project_v2_id = body.get("project_v2_id")
        if project_v2_id is not None and not (isinstance(project_v2_id, str) and len(project_v2_id) <= 80):
            raise ValueError("Invalid project")
        return await run_sync(local_status, project_v2_id or None)
    if action == "start":
        spec = body.get("spec")
        if not isinstance(spec, dict):
            raise ValueError("spec is required")
        result = await start_local_now(spec, op_id=str(body.get("op_id") or ""),
                                       requested_by=from_device)
        emit_changed()
        return result
    if action == "cancel":
        try:
            result = await run_sync(cancel_local, str(body.get("op_id") or ""))
        except SetupConflict as exc:
            raise PermissionError(str(exc)) from exc
        emit_changed()
        return result
    if action == "remove":
        result = await remove_local(str(body.get("project_v2_id") or ""))
        _after_local_registration_change()
        return result
    if action in ("browse-folder", "change-folder"):
        result = await run_sync(folder_action, action, body)
        if action == "change-folder" and "error" not in result:
            _after_local_registration_change()  # announce the updated local catalog
            from app.core.project_remote import reconcile_soon

            reconcile_soon()  # a copy of the project's previous repository switches now
        return result
    if action == "apply-remote":
        from app.core.project_remote import handle_apply

        result = await handle_apply(body)
        _after_local_registration_change()
        return result
    raise ValueError("Unknown setup action")


def folder_action(action: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Target-side folder operations for local owners and authenticated peers."""
    from fastapi import HTTPException

    from app.api.system import browse_directories
    from app.core.project_folders import retarget
    from app.core.project_resolver import resolve_catalog_project

    project_id = body.get("project_v2_id")
    path = body.get("folder")
    expected = body.get("expected_revision")
    if not isinstance(project_id, str) or not 0 < len(project_id) <= 80:
        return {"error": "Invalid project", "status": 400}
    if path is not None and (not isinstance(path, str) or len(path) > 4096 or "\x00" in path):
        return {"error": "Invalid folder path", "status": 400}
    pv2 = resolve_catalog_project(project_id)
    row = _own_rows_by_origin().get(pv2["origin"]) if pv2 else None
    if not row or row.get("instance_id") != _me():
        return {"error": "This worker has no worker-specific folder for this project.", "status": 409}
    try:
        if action == "browse-folder":
            return browse_directories(path).model_dump()
        if not isinstance(expected, str) or len(expected) != 64 or path is None:
            return {"error": "A folder and its current revision are required.", "status": 400}
        op = latest_ops(_me()).get(pv2["id"])
        if op and op["status"] in ("queued", "running"):
            return {"error": "Wait for project setup to finish on this worker.", "status": 409}
        result = retarget(row, path, expected)
    except HTTPException as exc:
        return {"error": exc.detail, "status": exc.status_code}
    except (OSError, RuntimeError, ValueError):
        return {"error": "This worker could not read that folder.", "status": 400}
    from app.core.project_folders import revision
    from app.core.project_utils import get_project_working_dir

    return {"folder": get_project_working_dir(result), "folder_revision": revision(result)}


def _after_local_registration_change() -> None:
    from app.network.project_catalog import announce_soon

    announce_soon()
    emit_changed()


# ---------------------------------------------------------------------------
# Requester side: offline dispatch
# ---------------------------------------------------------------------------

_pending_targets: Optional[set] = None
_zombies_checked = False


def note_peer_contact(device_id: str) -> None:
    """A peer reached us: dispatch its waiting setups now instead of next pass."""
    if _pending_targets is None or device_id not in _pending_targets:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    from app.core import turn_claim

    turn_claim.kick(DISPATCH_PASS)


def note_pending(target_id: str) -> None:
    if _pending_targets is not None:
        _pending_targets.add(target_id)


async def dispatch_once() -> None:
    """Send waiting (``queued_offline``) requests to targets that are back online."""
    global _pending_targets, _zombies_checked
    from app.db import project_setup as requests_db
    from app.db.async_database import run_sync
    from app.network import engine, transport

    if not _remotes_backfilled:
        # Soon after start, so a worker that then goes offline has already
        # stored its projects' remotes where every peer can read them.
        await run_sync(ensure_repo_records)
    if not _zombies_checked:
        try:
            _zombies_checked = await run_sync(settle_zombies) is not None
        except Exception:  # noqa: BLE001
            logger.debug("project setup: zombie sweep failed", exc_info=True)
    if _pending_targets is not None and not _pending_targets:
        return None
    if engine.context() is None:
        return None
    waiting = await run_sync(requests_db.list_requests, status=requests_db.REQUEST_QUEUED_OFFLINE)
    _pending_targets = {row["target_id"] for row in waiting}
    online = transport.online_devices()
    for row in reversed(waiting):  # oldest first
        if row["target_id"] not in online:
            continue
        await _dispatch_request(row)
    still = await run_sync(requests_db.list_requests, status=requests_db.REQUEST_QUEUED_OFFLINE)
    _pending_targets = {row["target_id"] for row in still}
    return None


async def _dispatch_request(row: Dict[str, Any]) -> None:
    from app.db import project_setup as requests_db
    from app.db.async_database import run_sync
    from app.network import control

    params = row.get("params") or {}
    target = row["target_id"]
    try:
        result = await control.call(target, ACTION, {"action": "start", "op_id": row["id"],
                                                     "spec": params.get("spec") or {}},
                                    timeout=PEER_TIMEOUT_SECONDS)
    except control.MaybeApplied:
        result = {"id": row["id"]}
    except control.RunnerUnreachable:
        return
    except control.ControlRefused as exc:
        if _UNKNOWN_ACTION in str(exc):
            code, message = UNSUPPORTED, unsupported_message(params.get("worker_name"))
        else:
            code, message = INTERNAL, str(exc)[:300]
        await run_sync(requests_db.set_request_status, row["id"], requests_db.REQUEST_ERROR,
                       expect=requests_db.REQUEST_QUEUED_OFFLINE, error_code=code, error=message)
        emit_changed()
        return
    except Exception:  # noqa: BLE001 - retried on the next pass
        logger.warning("project setup: dispatch of %s to %s failed", row["id"], target, exc_info=True)
        return
    remote_id = str(result.get("id") or row["id"])
    if remote_id != row["id"]:
        # The target already had one running for this project: track that one.
        await run_sync(requests_db.set_request_status, row["id"], requests_db.REQUEST_CANCELLED,
                       expect=requests_db.REQUEST_QUEUED_OFFLINE)
        await run_sync(requests_db.insert_request, remote_id, row["project_v2_id"], target, params,
                       requests_db.REQUEST_DISPATCHED)
    else:
        await run_sync(requests_db.set_request_status, row["id"], requests_db.REQUEST_DISPATCHED,
                       expect=requests_db.REQUEST_QUEUED_OFFLINE)
    emit_changed()


def start_dispatch_pass() -> None:
    from app.core import turn_claim

    turn_claim.register_pass(DISPATCH_PASS, dispatch_once, DISPATCH_INTERVAL_SECONDS, first_delay=5.0)


def stop_dispatch_pass() -> None:
    from app.core import turn_claim

    turn_claim.unregister_pass(DISPATCH_PASS)

