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

Whether a copy is switched is read from the checkout itself (its ``origin``
and push URLs, :func:`is_switched`), so every copy counts: registered project
folders and base checkouts the app cloned on demand. A switch that cannot
fetch is undone and retried later.

Nothing here deletes or rewrites files or branches. The only branch change is
a fast-forward of a clean base checkout that is strictly behind the new
remote, which never overwrites a local file.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
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
        wanted = normalize_remote_url(url)
        for row in _pinned_rows():  # a current pin beats an older merge
            if row["origin"] != derived and normalize_remote_url(pinned_remote(row)) == wanted:
                return row["origin"], True
        target = merged_into(projects_v2.get_project_v2_by_origin(derived))
        if target:
            return target, True
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


def _folder_key(folder: str) -> str:
    return os.path.normcase(os.path.realpath(folder))


def _own_folders_for(origin: str, *, lock: bool) -> List[str]:
    """Every folder on THIS worker that holds ``origin``: its registered
    project folders and its base checkouts (a base the app cloned on demand
    has no project row), once each. ``lock`` pins the project rows to
    ``origin`` so a remote change never moves them."""
    from app.core.checkout_manager import _resolve_checkout_path
    from app.core.project_setup import _me
    from app.core.project_utils import get_project_working_dir
    from app.db import checkouts as checkouts_db
    from app.db.projects_v2 import project_v2_id_for_origin

    folders: Dict[str, str] = {}
    for row in _own_rows_for(origin):
        if lock:
            row = _lock_to(row, origin)
        try:
            folder = get_project_working_dir(row)
        except (OSError, ValueError):
            continue
        folders.setdefault(_folder_key(folder), folder)
    pv2_id, me = project_v2_id_for_origin(origin), _me()
    for ck in checkouts_db.list_active_base_checkouts():
        if ck.get("project_id") != pv2_id or ck.get("runner_id") != me or not ck.get("path"):
            continue
        folder = _resolve_checkout_path(ck["path"])
        if os.path.isdir(folder):
            folders.setdefault(_folder_key(folder), folder)
    return list(folders.values())


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


def _config_all(folder: str, key: str) -> List[str]:
    result = _git(folder, ["config", "--get-all", key], timeout=10)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()] if result.returncode == 0 else []


def is_switched(folder: str, url: str) -> bool:
    """Whether every URL this checkout's ``origin`` fetches from and pushes to is ``url``."""
    from app.core.project_links import normalize_remote_url

    wanted = normalize_remote_url(url)
    urls = _config_all(folder, "remote.origin.url")
    return bool(urls) and all(normalize_remote_url(u) == wanted
                              for u in urls + _config_all(folder, "remote.origin.pushurl"))


def _ahead_behind(folder: str, branch: str) -> Optional[Tuple[int, int]]:
    result = _git(folder, ["rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"], timeout=15)
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    return (int(parts[0]), int(parts[1])) if len(parts) == 2 else None


def _restore(folder: str, urls: List[str], push_urls: List[str], added_upstream: bool) -> None:
    """Put ``origin`` back as it was before a switch that could not fetch."""
    if not urls:
        _git(folder, ["remote", "remove", "origin"])
    else:
        _git(folder, ["config", "--unset-all", "remote.origin.url"])
        _git(folder, ["config", "--unset-all", "remote.origin.pushurl"])
        for value in urls:
            _git(folder, ["config", "--add", "remote.origin.url", value])
        for push in push_urls:
            _git(folder, ["config", "--add", "remote.origin.pushurl", push])
    if added_upstream:
        _git(folder, ["remote", "remove", "upstream"])


def _apply_to_folder(folder: str, url: str, keep_upstream: bool) -> Dict[str, Any]:
    """Point one checkout's ``origin`` at ``url`` and fetch it. Never raises.

    The switch only sticks once the new repository fetched: otherwise
    ``origin`` is put back, so the copy keeps working and the next reconcile
    pass tries again (``is_switched`` is the only record of a switch).
    """
    from app.core.git_service import git_service
    from app.core.project_setup import classify_clone_failure

    out: Dict[str, Any] = {"ok": False}
    try:
        if not Path(folder).is_dir() or not git_service.is_git_repo(folder):
            out.update(status="no_git", error="This worker's copy is not a Git checkout.")
            return out
        current = _remote_url(folder, "origin")
        changed = not is_switched(folder, url)
        urls = _config_all(folder, "remote.origin.url") if changed else []
        push_urls = _config_all(folder, "remote.origin.pushurl") if changed else []
        added_upstream = False
        if changed and keep_upstream and current and _remote_url(folder, "upstream") is None:
            added_upstream = _git(folder, ["remote", "add", "upstream", current]).returncode == 0
            out["upstream"] = added_upstream or None
        if changed:
            if current is None:
                result = _git(folder, ["remote", "add", "origin", url])
            else:
                # Every old fetch URL and explicit push URL goes: either would
                # keep fetching from or pushing to the old repository.
                result = _git(folder, ["config", "--replace-all", "remote.origin.url", url])
                if result.returncode == 0 and push_urls:
                    result = _git(folder, ["config", "--unset-all", "remote.origin.pushurl"])
            if result.returncode != 0:
                _restore(folder, urls, push_urls, added_upstream)
                out.pop("upstream", None)
                out.update(status="error", error="Git refused to change this copy's remote.")
                return out
        out["changed"] = changed
        try:
            fetched = _git(folder, ["fetch", "--prune", "origin"], timeout=FETCH_TIMEOUT)
            fetch_error = fetched.stderr if fetched.returncode != 0 else None
        except Exception as exc:  # noqa: BLE001 - a timeout must undo the switch like any failure
            fetch_error = f"fetch did not finish: {type(exc).__name__} (timed out)"
        if fetch_error is not None:
            if changed:
                _restore(folder, urls, push_urls, added_upstream)
                out.pop("upstream", None)
            failure = classify_clone_failure(fetch_error, url)
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
                clean = not any(status.get(k) for k in ("staged", "modified", "untracked", "conflicts"))
                if ahead == 0 and behind and clean:
                    ff = _git(folder, ["merge", "--ff-only", "--no-overwrite-ignore", f"origin/{default}"],
                              timeout=60)
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


def _refresh_repo_records(origin: str) -> None:
    from app.api import git_helpers
    from app.core.project_utils import get_project_working_dir

    for row in _own_rows_for(origin):
        git_helpers._repo_record_cache.pop(row["id"], None)
        try:
            git_helpers.ensure_git_repository_record(row["id"], get_project_working_dir(row))
        except Exception:  # noqa: BLE001 - the next poll records it
            logger.debug("project remote: repository record refresh failed for %s", row["id"], exc_info=True)


RETRY_FAILED_AFTER = 1800  # seconds before the reconcile pass retries a failed switch
#: folder key -> (url, monotonic time) of the last switch that failed (this process only).
_failed: Dict[str, Tuple[str, float]] = {}
#: One switch per folder at a time (a request and a reconcile pass can overlap).
_folder_locks: Dict[str, threading.Lock] = {}
_folder_locks_guard = threading.Lock()


def _folder_lock(key: str) -> threading.Lock:
    with _folder_locks_guard:
        return _folder_locks.setdefault(key, threading.Lock())


def apply_local(origin: str, url: str, *, keep_upstream: bool, only_pending: bool = False,
                retry_failed: bool = True) -> Dict[str, Any]:
    """Apply a pinned remote to THIS worker's copies of ``origin`` (sync).

    Returns ``{"status": ..., ...}``: ``updated``/``already`` on success,
    ``no_copy`` when this worker has none, else an error status with a
    user-facing ``error``. ``only_pending`` skips copies already switched;
    ``retry_failed=False`` also skips copies that failed to switch to ``url``
    in the last :data:`RETRY_FAILED_AFTER` seconds.
    """
    folders = _own_folders_for(origin, lock=True)
    if not folders:
        return {"status": "no_copy", "ok": True}
    results = []
    for folder in folders:
        key = _folder_key(folder)
        with _folder_lock(key):
            if only_pending and is_switched(folder, url):
                results.append({"status": "already", "ok": True})
                continue
            last = _failed.get(key)
            if not retry_failed and last and last[0] == url and time.monotonic() - last[1] < RETRY_FAILED_AFTER:
                continue
            outcome = _apply_to_folder(folder, url, keep_upstream)
            if outcome.get("ok"):
                _failed.pop(key, None)
            else:
                _failed[key] = (url, time.monotonic())
        results.append(outcome)
    if not results:
        return {"status": "retry_later", "ok": False, "copies": len(folders)}
    if any(r.get("changed") for r in results):
        _refresh_repo_records(origin)
    first_error = next((r for r in results if not r.get("ok")), None)
    return {**(first_error or results[0]), "copies": len(folders)}


def fold_local(duplicate_origin: str, into_origin: str) -> Dict[str, Any]:
    """Relink this worker's copy of a merged duplicate to the kept project (sync).

    Skipped when this worker already has the kept project in another folder:
    one worker never gets two base folders for one project.
    """
    dup_rows = _own_rows_for(duplicate_origin)
    if _own_folders_for(into_origin, lock=False):
        return {"folded": 0, "skipped": len(dup_rows)}
    if not dup_rows:
        return _fold_base_checkout(duplicate_origin, into_origin)
    for row in dup_rows[:1]:
        _lock_to(row, into_origin)
    return {"folded": 1, "skipped": len(dup_rows) - 1}


def _fold_base_checkout(duplicate_origin: str, into_origin: str) -> Dict[str, Any]:
    """Register the duplicate's on-demand base checkout under the kept project
    too, so the kept project's changes reach it. The duplicate's row stays
    active: chats bound to it keep working (and can still move here), a path
    lookup resolves the folder to the kept project (``merged_into``), and the
    Projects page lists only the kept project."""
    from app.core.checkout_manager import _resolve_checkout_path
    from app.core.project_setup import _me
    from app.db import checkouts as checkouts_db
    from app.db.projects_v2 import project_v2_id_for_origin

    dup_id, me = project_v2_id_for_origin(duplicate_origin), _me()
    for ck in checkouts_db.list_active_base_checkouts():
        if ck.get("project_id") == dup_id and ck.get("runner_id") == me and ck.get("path"):
            path = _resolve_checkout_path(ck["path"])
            if os.path.isdir(path):
                checkouts_db.upsert_checkout(project_v2_id_for_origin(into_origin), me, path, status="active")
                return {"folded": 1}
    return {"folded": 0}


def reconcile_local() -> int:
    """Switch every copy on this worker that is not on its project's pinned
    remote yet, and fold merged duplicates (sync). Returns how many projects
    it changed; a copy that just failed waits :data:`RETRY_FAILED_AFTER`.
    """
    from app.core.project_setup import _me, _origin_of, _own_project_rows
    from app.db import checkouts as checkouts_db
    from app.db import projects_v2

    me = _me()

    def held() -> Dict[str, Dict[str, Any]]:
        origins = {_origin_of(row) for row in _own_project_rows()}
        base_ids = {ck["project_id"] for ck in checkouts_db.list_active_base_checkouts()
                    if ck.get("runner_id") == me}
        return {row["origin"]: row for row in projects_v2.list_projects_v2()
                if row.get("origin") in origins or row.get("id") in base_ids}

    by_origin = held()
    changed = 0
    for origin, pv2 in by_origin.items():
        target = merged_into(pv2)
        if target and projects_v2.get_project_v2_by_origin(target):
            changed += fold_local(origin, target).get("folded", 0)
    if changed:
        by_origin = held()
    for origin, pv2 in by_origin.items():
        url = pinned_remote(pv2)
        if not url or merged_into(pv2):
            continue
        if all(is_switched(folder, url) for folder in _own_folders_for(origin, lock=False)):
            continue
        outcome = apply_local(origin, url, keep_upstream=bool(_settings(pv2).get(KEEP_UPSTREAM_KEY)),
                              only_pending=True, retry_failed=False)
        changed += 1 if outcome.get("status") != "retry_later" else 0
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
    row has not arrived yet still applies it now. When this worker's synced
    row already holds a NEWER generation, that one wins: a late request never
    switches a copy back.
    """
    from app.db import projects_v2
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
        nonlocal url
        keep_upstream = bool(body.get("keep_upstream"))
        synced = projects_v2.get_project_v2_by_origin(origin)
        if pinned_remote(synced) and generation(synced) >= gen:
            url = pinned_remote(synced)
            keep_upstream = bool(_settings(synced).get(KEEP_UPSTREAM_KEY))
        if isinstance(duplicate, str) and duplicate and duplicate != origin:
            fold_local(duplicate, origin)
        return apply_local(origin, url, keep_upstream=keep_upstream, only_pending=True)

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
        dup_now = pinned_remote(dup)
        # Only a project still on that repository is a duplicate: one that
        # since moved to another repository is a live project of its own.
        if dup is not None and not merged_into(dup) and (dup_now is None or normalize_remote_url(dup_now) == wanted):
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
