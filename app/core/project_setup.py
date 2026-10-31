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
   a folder that is already a clone of the same repository, or creates an empty
   folder for a folder project. Then it registers a normal project row (B's
   ``instance_id``), the git repository record and the base checkout, so the
   catalog, eligibility, ``ensure_checkout`` and the composer's runner picker
   see B as able to run the project with no special case.
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

# Stable error codes (the UI keys its actions off these).
GITHUB_AUTH = "github_auth"
NETWORK = "network"
FOLDER_EXISTS = "folder_exists"
NOT_FOUND = "not_found"
OFFLINE = "offline"
UNSUPPORTED = "unsupported"
INTERNAL = "internal"
CANCELLED = "cancelled"

_OP_ID_RE = re.compile(r"^psetup-[0-9a-f]{32}$")
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


def new_op_id() -> str:
    return OP_PREFIX + uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def display_remote(url: Optional[str]) -> Optional[str]:
    """``github.com/owner/repo`` for any remote spelling; never credentials."""
    from app.core.scrub import strip_url_userinfo

    if not url:
        return None
    text = strip_url_userinfo(url.strip()) or ""
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://(.*)$", text)
    if match:
        text = match.group(1)
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


def _repo_info_by_origin() -> Dict[str, Dict[str, Any]]:
    """``{origin: {clone_url, default_branch}}`` from git records this DB holds."""
    from app.core.project_links import auto_link_group_id
    from app.core.scrub import strip_url_userinfo
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT gr.remote_url AS remote_url, gr.default_branch AS default_branch, "
            "p.id AS id, p.link_group_id AS link_group_id "
            "FROM git_repositories gr JOIN projects p ON p.id = gr.project_id "
            "WHERE gr.remote_url IS NOT NULL AND gr.remote_url <> ''"
        )
        rows = cursor.fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        url = strip_url_userinfo(row["remote_url"])
        if not url:
            continue
        info = {"clone_url": url, "default_branch": row["default_branch"] or None}
        out.setdefault(row["link_group_id"] or ("local:" + str(row["id"])), info)
        derived = auto_link_group_id(url)
        if derived:
            out.setdefault(derived, info)
    return out


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


def _validate_clone_url(url: str, origin: str) -> str:
    from app.core.git_service import classify_clone_url
    from app.core.project_links import auto_link_group_id
    from app.core.scrub import strip_url_userinfo
    from app.api.common import is_path_served

    clean = strip_url_userinfo((url or "").strip()) or ""
    kind, detail = classify_clone_url(clean)
    if kind == "invalid":
        raise ValueError(detail or "Invalid clone URL")
    if kind == "local":
        # A path on this worker's disk: only from where the owner may read
        # files here (the workspace or a registered project), as /git/clone.
        path = Path(detail or "")
        if not path.is_absolute() or not is_path_served(path, settings.effective_workspace_dir):
            raise ValueError("A local clone source must be inside the workspace or a project folder")
    if origin.startswith("git:") and auto_link_group_id(clean) != origin:
        raise ValueError("The remote does not match this project")
    return clean


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

    me = _me()
    device = _own_device_id()
    for row in git_ops_db.list_ops_for_runner(me, SETUP_OP, limit=50):
        if row.get("project_v2_id") == pv2_id and row["status"] in ("queued", "running"):
            return {"id": row["id"], "status": row["status"], "runner_id": device}

    params = {"origin": origin, "name": name, "kind": kind, "mode": mode,
              "clone_url": None, "requested_by": requested_by}
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
    stored, full = resolve_folder(str(spec.get("folder") or ""))
    if mode == "adopt" and not full.is_dir():
        raise ValueError(f"There is no folder at {full} to use")
    if projects_v2.get_project_v2(pv2_id) is None:
        # The identity replicates, but the request can outrun it.
        projects_v2.upsert_project(origin, name=name)
    row = git_ops_db.insert_op(me, SETUP_OP, project_v2_id=pv2_id,
                               params={**params, "folder": stored, "path": str(full)}, op_id=op_id)
    _kick_runner()
    return {"id": row["id"], "status": row["status"], "runner_id": device}


def _kick_runner() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # off the loop; the next claim pass picks it up
    from app.core import git_ops_runner

    git_ops_runner.kick()


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
    """Newest non-cancelled setup op per project on ``runner_id``."""
    from app.db import git_ops as git_ops_db

    out: Dict[str, Dict[str, Any]] = {}
    for row in git_ops_db.list_ops_for_runner(runner_id, SETUP_OP):
        pv2 = row.get("project_v2_id")
        if not pv2 or pv2 in out:
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
    from app.db import projects_v2

    detail: Dict[str, Any] = {"folder": None, "branch": None, "dirty": None, "changed_files": None}
    pv2 = projects_v2.get_project_v2(project_v2_id)
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
    base = next((ck for ck in checkouts_db.list_checkouts(project_v2_id)
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


def remove_local(project_v2_id: str) -> Dict[str, Any]:
    """Delete THIS worker's project record(s) for the logical project (sync).

    Same as deleting the project here: the record (and its chats) go, the
    files stay. Raises ``LookupError`` when this worker has no record.
    """
    from app.db import database, projects_v2
    from app.network import engine

    pv2 = projects_v2.get_project_v2(project_v2_id)
    if pv2 is None:
        raise LookupError("Unknown project")
    me = _me()
    removed = []
    for row in _own_project_rows():
        if _origin_of(row) != pv2.get("origin"):
            continue
        # An unowned legacy row is every worker's on a shared database.
        if row.get("instance_id") != me and engine.context() is None:
            continue
        database.delete_project(row["id"])
        removed.append(row["id"])
    if not removed:
        raise LookupError("This worker has no record of the project")
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Target side: the executor (runs in a thread, from git_ops_runner)
# ---------------------------------------------------------------------------

def _same_repo(path: Path, clone_url: Optional[str], origin: str) -> bool:
    from app.core.git_service import git_service
    from app.core.project_links import auto_link_group_id, normalize_remote_url

    if not path.is_dir() or not git_service.is_git_repo(str(path)):
        return False
    remote = git_service.get_remote_url(str(path))
    if not remote:
        return False
    if origin.startswith("git:"):
        return auto_link_group_id(remote) == origin
    return bool(clone_url) and normalize_remote_url(remote) == normalize_remote_url(clone_url)


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
    _notify_threadsafe(job)


def _clone(job: _Job, clone_url: str, final_path: Path) -> None:
    """Clone into a temporary sibling, then rename it to ``final_path``."""
    from fluid.tools.child_env import scrub_child_env
    from app.core.git_service import _IS_WINDOWS, _kill_git_tree, _quiet_git_env, git_service
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
    command = ["git", "-c", f"http.lowSpeedLimit={LOW_SPEED_LIMIT}", "-c", f"http.lowSpeedTime={LOW_SPEED_TIME}",
               "clone", "--progress", "--", clone_url, temp]
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
             if _IS_WINDOWS else {"start_new_session": True})
    tail: List[str] = []
    try:
        with _clone_slots:
            with job.lock:
                if job.cancelled:
                    raise SetupError(CANCELLED, "Setup was cancelled.")
                proc = subprocess.Popen(command, cwd=str(parent), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, **group)
                job.proc = proc
            try:
                buffer = ""
                assert proc.stderr is not None
                while True:
                    chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else proc.stderr.read(4096)
                    if not chunk:
                        break
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
        with job.lock:
            if job.cancelled:
                raise SetupError(CANCELLED, "Setup was cancelled.")
            job.finalizing = True
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


def execute_setup(op_row: Dict[str, Any]) -> Dict[str, Any]:
    """Run one claimed ``setup_project`` op on THIS worker (sync, in a thread).

    Returns the op's result; raises :class:`SetupError` with a stable code.
    Never deletes or overwrites a folder it did not create.
    """
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
    adopted = False
    if kind == "git":
        if path.exists() or path.is_symlink():
            if _same_repo(path, clone_url, origin):
                adopted = True
            elif mode == "auto" and _is_empty_dir(path):
                _clone(job, clone_url, path)
            else:
                raise SetupError(FOLDER_EXISTS, f"{path} already exists and isn't a copy of this project. "
                                                "Choose another folder.")
        elif mode == "adopt":
            raise SetupError(NOT_FOUND, f"There is no folder at {path} to use.")
        else:
            _clone(job, clone_url, path)
    elif kind == "folder":
        if path.exists() or path.is_symlink():
            if not path.is_dir() or path.is_symlink():
                raise SetupError(FOLDER_EXISTS, f"{path} already exists. Choose another folder.")
            if mode != "adopt" and not _is_empty_dir(path):
                raise SetupError(FOLDER_EXISTS, f"{path} already exists and isn't empty. Choose another "
                                                "folder, or use the folder already there.")
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
                                        folder=params["folder"], path=path, kind=kind)
    return {"project_id": project_id, "adopted": adopted, "progress": 100}


def _unique_name(name: str) -> str:
    taken = {str(row.get("name") or "").strip().casefold() for row in _own_project_rows()}
    candidate, n = name, 2
    while candidate.strip().casefold() in taken:
        candidate = f"{name} {n}"
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
                           path: Path, kind: str) -> str:
    """Register the folder as this worker's copy of the logical project (sync).

    A normal project row (this worker's ``instance_id``) through the same
    creation path as adding a project here, linked to the logical project's
    origin (auto-linked from the remote for a ``git:`` origin, a locked manual
    link otherwise), the git repository record, and the base checkout the
    catalog and ``ensure_checkout`` read.
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
    project = next((row for row in _own_project_rows()
                    if _safe_resolve(row) == resolved), None)
    if project is None:
        try:
            project = create_project_record_sync(ProjectCreate(
                id=_unique_id(path.name or name), name=_unique_name(name), path=folder))
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
        result = await run_sync(start_local, spec, op_id=str(body.get("op_id") or ""), requested_by=from_device)
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
        result = await run_sync(remove_local, str(body.get("project_v2_id") or ""))
        _after_local_remove()
        return result
    raise ValueError("Unknown setup action")


def _after_local_remove() -> None:
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

