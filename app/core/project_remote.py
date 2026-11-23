"""Point a project at another Git repository on every worker, keeping the project.

A project's identity (``projects_v2.origin``) starts as the hash of its Git
remote (``git:…``), so changing the remote in one checkout used to give that
worker a NEW project while every other worker kept the old one: a duplicate in
every picker, and an agent per worker to switch each copy by hand.

Changing the repository is now one request (``change_repository``):

1. The new remote is written on the project's replicated ``projects_v2`` row
   (``settings.remote_url``) and **pinned** there (``remote_pinned``) with a
   new ``remote_generation``. The origin does not change, so the project,
   its chats and its per-worker checkouts stay the same project.
2. Every worker with a copy applies it to its own checkout
   (:func:`apply_local`): ``origin`` is pointed at the new URL (the old one is
   kept as ``upstream`` when asked, for a fork), then fetched with that
   worker's own credentials. Online workers are told at once (the
   ``project-setup`` control action ``apply-remote``); a worker that was
   offline applies it from the synced row on its next reconcile pass
   (:func:`reconcile_local`).
3. Anything that would otherwise derive an identity from the new URL
   (auto-linking, adding a folder, setting the project up elsewhere) finds the
   pinned project first (:func:`identity_for_remote`), so no duplicate appears.
4. A project that already exists for the new remote (the duplicate a manual
   switch left behind) is marked ``merged_into`` the kept project; each worker
   relinks its copy of it to the kept project.

Nothing here deletes or rewrites files or branches. The only branch change is
a fast-forward of a clean base checkout that is strictly behind the new
remote.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

PINNED_KEY = "remote_pinned"
GENERATION_KEY = "remote_generation"
KEEP_UPSTREAM_KEY = "remote_keep_upstream"
PREVIOUS_KEY = "previous_remote_url"
MERGED_INTO_KEY = "merged_into"
#: Local (never synced) ``projects.settings`` key: what this worker last did.
APPLIED_KEY = "remote_applied"

APPLY_ACTION = "apply-remote"
LS_REMOTE_TIMEOUT = 30
FETCH_TIMEOUT = 180


class RepositoryChangeError(ValueError):
    """A change request this worker refuses; ``str()`` is user-facing."""


# ---------------------------------------------------------------------------
# Reading the pinned remote
# ---------------------------------------------------------------------------

def _settings(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    value = (row or {}).get("settings")
    return value if isinstance(value, dict) else {}


def pinned_remote(pv2: Optional[Dict[str, Any]]) -> Optional[str]:
    """The remote an owner pinned on this project, else None."""
    s = _settings(pv2)
    url = s.get("remote_url")
    return url if s.get(PINNED_KEY) is True and isinstance(url, str) and url else None


def generation(pv2: Optional[Dict[str, Any]]) -> int:
    value = _settings(pv2).get(GENERATION_KEY)
    return value if isinstance(value, int) and value > 0 else 0


def merged_into(pv2: Optional[Dict[str, Any]]) -> Optional[str]:
    value = _settings(pv2).get(MERGED_INTO_KEY)
    return value if isinstance(value, str) and value and value != (pv2 or {}).get("origin") else None


def remote_names_origin(url: Optional[str], origin: str, pv2: Optional[Dict[str, Any]] = None) -> bool:
    """Whether ``url`` is this identity's repository.

    A ``git:`` origin is the hash of the remote it was first seen with; once an
    owner points the project at another repository, the pinned remote names it
    too. Pass ``pv2`` when it is already loaded.
    """
    from app.core.project_links import auto_link_group_id, normalize_remote_url

    if not url or not origin:
        return False
    if origin.startswith("git:") and auto_link_group_id(url) == origin:
        return True
    if pv2 is None:
        from app.db import projects_v2

        pv2 = projects_v2.get_project_v2_by_origin(origin)
    pinned = pinned_remote(pv2) if pv2 and pv2.get("origin") == origin else None
    return bool(pinned) and normalize_remote_url(pinned) == normalize_remote_url(url)


def _pinned_rows() -> List[Dict[str, Any]]:
    from app.db import projects_v2

    return [row for row in projects_v2.list_projects_v2() if pinned_remote(row)]


def identity_for_remote(url: Optional[str]) -> Optional[Tuple[str, bool]]:
    """``(origin, locked)`` a checkout with remote ``url`` belongs to, or None.

    The remote's own ``git:`` id unless a project was pointed at this remote
    (then that project, ``locked`` so auto-linking keeps it there) or the
    remote's own project was merged into another one.
    """
    from app.core.project_links import auto_link_group_id, normalize_remote_url
    from app.db import projects_v2

    derived = auto_link_group_id(url)
    if not derived:
        return None
    try:
        own = projects_v2.get_project_v2_by_origin(derived)
        target = merged_into(own)
        if target:
            return target, True
        wanted = normalize_remote_url(url)
        for row in _pinned_rows():
            if row["origin"] != derived and normalize_remote_url(pinned_remote(row)) == wanted:
                return row["origin"], True
    except Exception:  # noqa: BLE001 - fall back to the remote's own identity
        logger.debug("identity_for_remote: lookup failed", exc_info=True)
    return derived, False


def clone_url_for(pv2: Optional[Dict[str, Any]]) -> Optional[str]:
    """The pinned remote of a project (what a new copy clones), else None."""
    return pinned_remote(pv2)


# ---------------------------------------------------------------------------
# This worker: apply the pinned remote to its own checkouts
# ---------------------------------------------------------------------------

def _git(folder: str, args: List[str], timeout: int = 30):
    from app.core.git_service import git_service

    return git_service._run_git(folder, args, timeout=timeout)


def _remote_url(folder: str, name: str) -> Optional[str]:
    result = _git(folder, ["remote", "get-url", name], timeout=10)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _own_rows_for(origin: str) -> List[Dict[str, Any]]:
    from app.core.project_setup import _origin_of, _own_project_rows

    return [row for row in _own_project_rows() if _origin_of(row) == origin]


def _record_applied(project_id: str, outcome: Dict[str, Any]) -> None:
    from app.db import database

    row = database.get_project(project_id)
    if not row:
        return
    merged = dict(row.get("settings") or {})
    merged[APPLIED_KEY] = outcome
    database.update_project(project_id, settings_dict=merged)


def _lock_to(row: Dict[str, Any], origin: str) -> Dict[str, Any]:
    """Pin this worker's row to ``origin`` so a remote change never moves it."""
    from app.core.project_links import LINK_LOCKED_KEY
    from app.db import database

    current = dict(row.get("settings") or {})
    if current.get(LINK_LOCKED_KEY) is not True:
        current[LINK_LOCKED_KEY] = True
        database.update_project(row["id"], settings_dict=current)
    if row.get("link_group_id") != origin:
        database.set_project_link_group(row["id"], origin)
    return database.get_project(row["id"]) or row


def _ahead_behind(folder: str, branch: str) -> Optional[Tuple[int, int]]:
    result = _git(folder, ["rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"], timeout=15)
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    return (int(parts[0]), int(parts[1])) if len(parts) == 2 else None


def _apply_to_folder(folder: str, url: str, keep_upstream: bool) -> Dict[str, Any]:
    """Point one checkout's ``origin`` at ``url`` and fetch it. Never raises."""
    from app.core.git_service import git_service
    from app.core.project_links import normalize_remote_url
    from app.core.project_setup import classify_clone_failure

    out: Dict[str, Any] = {"ok": False}
    try:
        if not Path(folder).is_dir() or not git_service.is_git_repo(folder):
            out.update(status="no_git", error="This worker's copy is not a Git checkout.")
            return out
        current = _remote_url(folder, "origin")
        changed = not current or normalize_remote_url(current) != normalize_remote_url(url)
        if changed and keep_upstream and current and _remote_url(folder, "upstream") is None:
            _git(folder, ["remote", "add", "upstream", current])
            out["upstream"] = True
        if current is None:
            result = _git(folder, ["remote", "add", "origin", url])
        elif changed:
            result = _git(folder, ["remote", "set-url", "origin", url])
        else:
            result = None
        if result is not None and result.returncode != 0:
            out.update(status="error", error="Git refused to change this copy's remote.")
            return out
        out["changed"] = changed
        fetched = _git(folder, ["fetch", "--prune", "origin"], timeout=FETCH_TIMEOUT)
        if fetched.returncode != 0:
            failure = classify_clone_failure(fetched.stderr, url)
            out.update(status="fetch_failed", error=str(failure), error_code=failure.error_code)
            return out
        _git(folder, ["remote", "set-head", "origin", "--auto"], timeout=30)
        default = git_service.get_default_branch(folder)
        status = git_service.get_status(folder)
        branch = status.get("current_branch")
        out.update(ok=True, status="updated" if changed else "already", default_branch=default, branch=branch)
        if default and branch == default:
            counts = _ahead_behind(folder, default)
            if counts is None:
                out["base"] = "unrelated"
            else:
                ahead, behind = counts
                clean = not any(status.get(k) for k in ("staged", "modified", "conflicts"))
                if ahead == 0 and behind and clean:
                    ff = _git(folder, ["merge", "--ff-only", f"origin/{default}"], timeout=60)
                    out["base"] = "fast_forwarded" if ff.returncode == 0 else "behind"
                else:
                    out["base"] = ("same" if not ahead and not behind else
                                   "behind" if not ahead else "ahead" if not behind else "diverged")
                out["ahead"], out["behind"] = ahead, behind
        git_service.invalidate_status_cache(folder)
    except Exception as exc:  # noqa: BLE001 - reported per worker, never raised
        logger.warning("project remote: applying to %s failed", folder, exc_info=True)
        out.update(status="error", error=f"Couldn't update this copy: {type(exc).__name__}")
    return out


def _refresh_repo_record(project_id: str, folder: str) -> None:
    from app.api import git_helpers

    git_helpers._repo_record_cache.pop(project_id, None)
    try:
        git_helpers.ensure_git_repository_record(project_id, folder)
    except Exception:  # noqa: BLE001 - the next poll records it
        logger.debug("project remote: repository record refresh failed for %s", project_id, exc_info=True)


def apply_local(origin: str, url: str, gen: int, *, keep_upstream: bool) -> Dict[str, Any]:
    """Apply a pinned remote to THIS worker's copies of ``origin`` (sync).

    Returns ``{"status": ..., ...}``: ``updated``/``already`` on success,
    ``no_copy`` when this worker has none, else an error status with a
    user-facing ``error``.
    """
    from app.core.project_utils import get_project_working_dir

    rows = _own_rows_for(origin)
    if not rows:
        return {"status": "no_copy", "ok": True}
    results = []
    for row in rows:
        row = _lock_to(row, origin)
        try:
            folder = get_project_working_dir(row)
        except (OSError, ValueError):
            results.append({"status": "error", "ok": False, "error": "This worker's project folder is unreadable."})
            continue
        outcome = _apply_to_folder(folder, url, keep_upstream)
        outcome.update(generation=gen, at=time.time(), project_id=row["id"])
        _record_applied(row["id"], outcome)
        if outcome.get("ok"):
            _refresh_repo_record(row["id"], folder)
        results.append(outcome)
    first_error = next((r for r in results if not r.get("ok")), None)
    return {**(first_error or results[0]), "copies": len(results)}


def fold_local(duplicate_origin: str, into_origin: str) -> Dict[str, Any]:
    """Relink this worker's copy of a merged duplicate to the kept project (sync).

    Skipped when this worker already has the kept project in another folder:
    one worker never gets two base folders for one project.
    """
    dup_rows = _own_rows_for(duplicate_origin)
    if not dup_rows:
        return {"folded": 0}
    if _own_rows_for(into_origin):
        return {"folded": 0, "skipped": len(dup_rows)}
    for row in dup_rows[:1]:
        _lock_to(row, into_origin)
    return {"folded": 1, "skipped": len(dup_rows) - 1}


RETRY_FAILED_AFTER = 1800  # seconds before the reconcile pass retries a failed apply


def _applied_generation(row: Dict[str, Any], *, retry_failed: bool = True) -> int:
    """The generation this copy already applied; a failure counts as applied
    for :data:`RETRY_FAILED_AFTER` (never with ``retry_failed=False``)."""
    last = _settings(row).get(APPLIED_KEY) or {}
    value = last.get("generation")
    if not isinstance(value, int):
        return 0
    if last.get("ok"):
        return value
    at = last.get("at")
    recent = isinstance(at, (int, float)) and time.time() - at < RETRY_FAILED_AFTER
    return value if retry_failed and recent else value - 1


def reconcile_local() -> int:
    """Apply every pinned remote and merge this worker has not applied yet (sync).

    Cheap when nothing changed: two small table reads. Returns how many
    copies it changed.
    """
    from app.core.project_setup import _origin_of, _own_project_rows
    from app.db import projects_v2

    rows = _own_project_rows()
    if not rows:
        return 0
    by_origin = projects_v2.get_projects_v2_by_origins({_origin_of(row) for row in rows})
    changed = 0
    for origin, pv2 in by_origin.items():
        target = merged_into(pv2)
        if target and projects_v2.get_project_v2_by_origin(target):
            changed += fold_local(origin, target).get("folded", 0)
    if changed:
        rows = _own_project_rows()
        by_origin = projects_v2.get_projects_v2_by_origins({_origin_of(row) for row in rows})
    for row in rows:
        pv2 = by_origin.get(_origin_of(row))
        url, gen = pinned_remote(pv2), generation(pv2)
        if not url or gen <= _applied_generation(row):
            continue
        apply_local(pv2["origin"], url, gen, keep_upstream=bool(_settings(pv2).get(KEEP_UPSTREAM_KEY)))
        changed += 1
    return changed


_reconcile_task: Optional["asyncio.Task"] = None


def reconcile_soon() -> None:
    """Run :func:`reconcile_local` off the event loop; one pass at a time."""
    global _reconcile_task
    if _reconcile_task is not None and not _reconcile_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def run() -> None:
        from app.db.async_database import run_sync

        try:
            if await run_sync(reconcile_local):
                from app.core.project_setup import _after_local_registration_change

                _after_local_registration_change()
        except Exception:  # noqa: BLE001 - retried on the next pass
            logger.warning("project remote: reconcile pass failed", exc_info=True)

    _reconcile_task = loop.create_task(run())


async def handle_apply(body: Dict[str, Any]) -> Dict[str, Any]:
    """Control action ``apply-remote``: apply what the requester just pinned.

    The request carries the pinned remote so a peer whose copy of the synced
    row has not arrived yet still applies it now; it only applies a generation
    newer than the one it already applied.
    """
    from app.db.async_database import run_sync

    origin = body.get("origin")
    url = body.get("remote_url")
    gen = body.get("generation")
    duplicate = body.get("merged_origin")
    if not isinstance(origin, str) or not 0 < len(origin) <= 300:
        raise ValueError("Invalid project")
    if not isinstance(url, str) or not 0 < len(url) <= 2048 or not isinstance(gen, int):
        raise ValueError("Invalid repository change")
    _check_url_shape(url)

    def run() -> Dict[str, Any]:
        if isinstance(duplicate, str) and duplicate and duplicate != origin:
            fold_local(duplicate, origin)
        rows = _own_rows_for(origin)
        if rows and all(_applied_generation(row, retry_failed=False) >= gen for row in rows):
            last = _settings(rows[0]).get(APPLIED_KEY) or {}
            return {**last, "copies": len(rows)}
        return apply_local(origin, url, gen, keep_upstream=bool(body.get("keep_upstream")))

    return await run_sync(run)


# ---------------------------------------------------------------------------
# Requester: validate, pin, apply here, tell every worker
# ---------------------------------------------------------------------------

def _check_url_shape(url: str) -> str:
    from app.api.common import is_path_served
    from app.core.git_service import classify_clone_url
    from app.core.scrub import strip_url_userinfo

    clean = strip_url_userinfo((url or "").strip()) or ""
    kind, detail = classify_clone_url(clean)
    if kind == "invalid":
        raise RepositoryChangeError(detail or "Use an HTTPS or SSH repository URL.")
    if kind == "local":
        path = Path(detail or "")
        if not path.is_absolute() or not is_path_served(path, settings.effective_workspace_dir):
            raise RepositoryChangeError("A local repository must be an absolute path inside the workspace.")
    return clean


def _probe(url: str) -> Optional[str]:
    """Check this worker can read ``url``; return its default branch (sync)."""
    from app.core.project_setup import classify_clone_failure

    cwd = str(settings.effective_workspace_dir)
    result = _git(cwd, ["ls-remote", "--symref", "--", url, "HEAD"], timeout=LS_REMOTE_TIMEOUT)
    if result.returncode != 0:
        raise RepositoryChangeError(f"Can't use that repository from this worker: {classify_clone_failure(result.stderr, url)}")
    for line in result.stdout.splitlines():
        if line.startswith("ref: refs/heads/") and line.rstrip().endswith("HEAD"):
            return line[len("ref: refs/heads/"):].split()[0]
    return None


def current_remote(pv2: Dict[str, Any]) -> Optional[str]:
    """The repository the project uses now: pinned, else what the workers report."""
    from app.core.project_setup import _repo_info_by_origin

    return pinned_remote(pv2) or (_repo_info_by_origin().get(pv2.get("origin") or "") or {}).get("clone_url")


def prepare_change(pv2_id: str, url: str, *, keep_upstream: bool) -> Dict[str, Any]:
    """Validate and pin the new remote on the synced project row (sync).

    Returns what to apply. Raises :class:`RepositoryChangeError`.
    """
    from app.core.project_links import auto_link_group_id, normalize_remote_url
    from app.core.project_resolver import resolve_catalog_project
    from app.db import projects_v2

    pv2 = resolve_catalog_project(pv2_id)
    if not pv2:
        raise RepositoryChangeError("Project not found.")
    origin = pv2["origin"]
    clean = _check_url_shape(url)
    if merged_into(pv2):
        raise RepositoryChangeError("This project was merged into another one. Change that project instead.")
    previous = current_remote(pv2)
    wanted = normalize_remote_url(clean)
    for row in _pinned_rows():
        if row["origin"] != origin and normalize_remote_url(pinned_remote(row)) == wanted:
            raise RepositoryChangeError(f"The project “{row.get('name')}” already uses that repository.")
    default_branch = _probe(clean)
    duplicate = None
    derived = auto_link_group_id(clean)
    if derived and derived != origin:
        dup = projects_v2.get_project_v2_by_origin(derived)
        if dup is not None:
            duplicate = derived
            projects_v2.mark_merged(derived, origin)
    gen = generation(pv2) + 1
    projects_v2.pin_remote(
        origin, clean, default_branch=default_branch, generation=gen,
        keep_upstream=keep_upstream,
        previous=previous if previous and normalize_remote_url(previous) != wanted else None,
    )
    return {"project_v2_id": pv2["id"], "origin": origin, "name": pv2.get("name"), "remote_url": clean,
            "generation": gen, "keep_upstream": keep_upstream, "merged_origin": duplicate,
            "previous_remote_url": previous, "default_branch": default_branch}


def _public_result(outcome: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("status", "ok", "error", "error_code", "branch", "default_branch", "base", "ahead", "behind",
            "upstream", "copies")
    return {key: outcome.get(key) for key in keys if outcome.get(key) is not None}


async def change_repository(pv2_id: str, url: str, *, keep_upstream: bool = False) -> Dict[str, Any]:
    """Point a project at ``url`` on every worker. Returns per-worker results."""
    from app.api import project_workers
    from app.core import project_setup as setup
    from app.db.async_database import run_sync
    from app.network import control, engine

    plan = await run_sync(prepare_change, pv2_id, url, keep_upstream=keep_upstream)
    body = {"action": APPLY_ACTION, **{k: plan[k] for k in ("origin", "remote_url", "generation",
                                                            "keep_upstream", "merged_origin")}}
    me = project_workers._self_install_id()
    installs = await run_sync(project_workers._approved_workers)
    networked = engine.context() is not None

    async def one(install: Dict[str, Any]) -> Dict[str, Any]:
        entry = {"install_id": install["id"], "device_name": install.get("device_name"),
                 "is_self": install["id"] == me}
        if entry["is_self"]:
            outcome = await handle_apply(body)
        elif not networked:
            outcome = {"status": "pending", "ok": True}  # shared database: its reconcile pass applies it
        elif not install.get("online"):
            outcome = {"status": "offline", "ok": True}
        else:
            try:
                outcome = await control.call(install["id"], setup.ACTION, body, timeout=FETCH_TIMEOUT + 30)
            except control.ControlRefused as exc:
                if setup._UNKNOWN_ACTION in str(exc) or "Unknown setup action" in str(exc):
                    outcome = {"status": "unsupported", "ok": False,
                               "error": f"Update {install.get('device_name') or 'that worker'} so it can change "
                                        "repositories; it applies the change after updating."}
                else:
                    outcome = {"status": "error", "ok": False, "error": str(exc)[:300]}
            except control.RunnerUnreachable:
                outcome = {"status": "offline", "ok": True}
            except Exception as exc:  # noqa: BLE001 - one worker never fails the whole change
                outcome = {"status": "error", "ok": False, "error": type(exc).__name__}
        return {**entry, **_public_result(outcome)}

    workers = await asyncio.gather(*(one(install) for install in installs))
    setup._after_local_registration_change()
    return {
        "project_v2_id": plan["project_v2_id"], "name": plan["name"],
        "repository": setup.display_remote(plan["remote_url"]), "remote_url": plan["remote_url"],
        "previous_repository": setup.display_remote(plan["previous_remote_url"]),
        "kept_as_upstream": bool(keep_upstream and plan["previous_remote_url"]),
        "default_branch": plan["default_branch"],
        "merged_duplicate": plan["merged_origin"] is not None,
        "workers": [w for w in workers if w.get("status") != "no_copy"],
        "workers_without_copy": [w["device_name"] for w in workers if w.get("status") == "no_copy"],
    }
