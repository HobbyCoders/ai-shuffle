"""Where a change is live: each paired worker's revision against origin/main.

Read-only. Answers "is PR #N / commit X running on Unraid yet?" without the
owner checking every worker by hand. Each worker reports the revision it runs,
its app version and update channel; a local ai-shuffle git checkout turns that
into commits behind origin/main and "contains the change". Nothing here
raises: a worker, the git checkout or GitHub that cannot answer degrades to
``None`` plus a ``reason``.

Android loads its UI from the worker it connects to, so only a change under
``android/`` needs a new Play build.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

logger = logging.getLogger(__name__)

_APP_ROOT = Path(__file__).resolve().parents[2]
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_DEFAULT_REPO = "HobbyCoders/ai-shuffle"
MAIN = "origin/main"
# One `git fetch origin main` per checkout per FETCH_TTL seconds.
FETCH_TTL = 120
_fetched: Dict[str, float] = {}
_OFFER_PHASES = ("available", "downloading", "ready")


class EdgePublicationRefusal(TypedDict):
    """Explicit gate evidence for the same short revision as ``origin_main``."""

    revision: str
    reason: str


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, timeout: int = 15) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _out(repo: Path, *args: str, timeout: int = 15) -> Optional[str]:
    result = _git(repo, *args, timeout=timeout)
    return result.stdout.strip() if result is not None and result.returncode == 0 else None


# A registered project's origin must BE the ai-shuffle repository (any owner,
# so a fork works), not merely mention it the way the fixed candidates may.
_AI_SHUFFLE_ORIGIN = re.compile(r"[/:][^/:]+/ai-shuffle(?:\.git)?/?$", re.IGNORECASE)


def _project_checkouts() -> List[Path]:
    """Registered projects' paths on this worker (the desktop app keeps its
    ai-shuffle project outside the workspace folder, #1815)."""
    try:
        from app.core.project_utils import get_project_working_dir
        from app.db import database
        return [Path(get_project_working_dir(p["path"]))
                for p in database.get_all_projects() or [] if p.get("path")]
    except Exception:  # noqa: BLE001 — no database is no candidates, not an error
        logger.debug("deploy status: project lookup failed", exc_info=True)
        return []


def find_repo() -> Optional[Path]:
    """A local ai-shuffle checkout with an origin remote, or None.

    ``SHUFFLE_SOURCE_REPO``, the app's own root and ``<workspace>/ai-shuffle``
    first, then any registered project whose origin is the ai-shuffle repo.
    """
    candidates: List[Path] = []
    if os.environ.get("SHUFFLE_SOURCE_REPO"):
        candidates.append(Path(os.environ["SHUFFLE_SOURCE_REPO"]))
    candidates.append(_APP_ROOT)
    try:
        from app.core.config import settings
        if settings.workspace_dir:
            candidates.append(Path(settings.workspace_dir) / "ai-shuffle")
    except Exception:  # noqa: BLE001
        pass
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        url = _out(candidate, "remote", "get-url", "origin", timeout=5)
        if url and "ai-shuffle" in url.lower():
            top = _out(candidate, "rev-parse", "--show-toplevel", timeout=5)
            return Path(top) if top else candidate
    seen = {str(c) for c in candidates}
    for candidate in _project_checkouts():
        if str(candidate) in seen or not candidate.is_dir():
            continue
        seen.add(str(candidate))
        url = _out(candidate, "remote", "get-url", "origin", timeout=5)
        if url and _AI_SHUFFLE_ORIGIN.search(url.strip()):
            top = _out(candidate, "rev-parse", "--show-toplevel", timeout=5)
            return Path(top) if top else candidate
    return None


def _refresh(repo: Path) -> None:
    key = str(repo)
    if time.monotonic() - _fetched.get(key, -FETCH_TTL) < FETCH_TTL:
        return
    _fetched[key] = time.monotonic()
    result = _git(repo, "fetch", "--quiet", "origin", "main", timeout=30)
    if result is None or result.returncode != 0:
        logger.debug("deploy status: git fetch failed: %s", result.stderr if result else "not run")


def _resolve(repo: Path, rev: Optional[str]) -> Optional[str]:
    if not rev:
        return None
    return _out(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")


def _behind(repo: Path, full: str) -> Optional[int]:
    value = _out(repo, "rev-list", "--count", f"{full}..{MAIN}")
    return int(value) if value and value.isdigit() else None


def _contains(repo: Path, target: str, rev: Optional[str]) -> Optional[bool]:
    """``git merge-base --is-ancestor``: True, False, or None when git cannot say."""
    if not rev:
        return None
    result = _git(repo, "merge-base", "--is-ancestor", target, rev)
    if result is None or result.returncode not in (0, 1):
        return None
    return result.returncode == 0


def _files(repo: Path, sha: str) -> Optional[List[str]]:
    # -m --first-parent gives a merge commit's whole PR diff; squash commits are plain.
    value = _out(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--first-parent", "--root", sha)
    return [line for line in value.splitlines() if line] if value is not None else None


def _pr_merge_commit(repo: Path, pr: int) -> Dict[str, Any]:
    """PR -> merge commit via gh, falling back to the squash subject "(#N)" on main."""
    try:
        from app.core.github_service import github_service
        slug = github_service.get_repo_from_remote(str(repo)) or _DEFAULT_REPO
        result = github_service._run_gh(["pr", "view", str(pr), "--repo", slug, "--json", "state,mergeCommit"],
                                        timeout=20)
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout or "{}")
            sha = (data.get("mergeCommit") or {}).get("oid")
            if data.get("state") != "MERGED" or not sha:
                return {"sha": None, "reason": f"PR #{pr} is {str(data.get('state') or 'unknown').lower()}, not merged"}
            return {"sha": sha}
    except Exception:  # noqa: BLE001 — gh is optional; git can still answer
        logger.debug("deploy status: gh pr view failed", exc_info=True)
    sha = _out(repo, "log", MAIN, "--fixed-strings", f"--grep=(#{pr})", "-1", "--format=%H")
    if sha:
        return {"sha": sha}
    return {"sha": None, "reason": f"PR #{pr} not found on {MAIN} (gh unavailable or not merged)"}


# scripts/ci/edge_publication_gate.py annotates a refusal that only the owner
# can clear (the full suites, or a combined main tree, need owner-dispatched
# CI on main) with this title and the exact `gh workflow run` commands (#1940).
# The gate job itself stays green.
EDGE_REFUSAL_TITLE = "Edge publication refused"
# Unrelated PR CI completions re-run the gate on the same main revision; bound
# how many of its runs are read for the annotation.
EDGE_GATE_RUNS = 10
_edge_refusals: Dict[str, tuple] = {}


def _gh_api(endpoint: str) -> Any:
    from app.core.github_service import github_service
    result = github_service._run_gh(["api", endpoint], timeout=20)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or f"gh api {endpoint} failed")
    return json.loads(result.stdout or "null")


def edge_refusal(repo: Path, revision: str) -> Optional[str]:
    """Why the edge gate refused to publish ``revision`` and what unblocks it.

    Read from the gate job's annotation. None when the gate passed for this
    revision, never annotated it, or GitHub cannot be reached.
    """
    cached = _edge_refusals.get(revision)
    if cached and time.monotonic() - cached[0] < FETCH_TTL:
        return cached[1]
    try:
        from app.core.github_service import github_service
        slug = github_service.get_repo_from_remote(str(repo)) or _DEFAULT_REPO
        listing = _gh_api(f"repos/{slug}/commits/{revision}/check-runs?per_page=100&filter=all")
        runs = [run for run in (listing or {}).get("check_runs") or []
                if (run.get("app") or {}).get("slug") == "github-actions"]
        gates = sorted((run for run in runs if run.get("name") == "gate"),
                       key=lambda run: run.get("id") or 0, reverse=True)
        suites = {(run.get("check_suite") or {}).get("id") for run in gates}
        value = None
        # The publish job runs only after the gate passed: the refusal is obsolete.
        if not any(run.get("name") == "publish" and run.get("conclusion") == "success"
                   and (run.get("check_suite") or {}).get("id") in suites for run in runs):
            for run in [run for run in gates if (run.get("output") or {}).get("annotations_count")][:EDGE_GATE_RUNS]:
                notes = _gh_api(f"repos/{slug}/check-runs/{run['id']}/annotations") or []
                message = next((note.get("message") for note in notes if isinstance(note, dict)
                                and note.get("title") == EDGE_REFUSAL_TITLE and note.get("message")), None)
                if message:
                    value = message.strip().replace(":\n", ": ").replace("\n", "; ")
                    break
    except Exception:  # noqa: BLE001 — gh is optional; the action still stands without it
        logger.debug("deploy status: edge gate lookup failed", exc_info=True)
        return None
    _edge_refusals[revision] = (time.monotonic(), value)
    return value


def android_build(files: Optional[List[str]]) -> Dict[str, Any]:
    if files is None:
        return {"build_needed": None, "reason": "changed files unknown"}
    if any(path.startswith("android/") for path in files):
        return {"build_needed": True, "reason": "touches android/; publish a new Play build"}
    return {"build_needed": False, "reason": "no build needed; the phone loads its UI from the worker"}


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------

def _same(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a and b and (a.startswith(b) or b.startswith(a)))


def _revision(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text if _SHA.match(text) else None


def _offered(state: Dict[str, Any], running: Optional[str]) -> Optional[str]:
    """The revision the worker's updater has found but not installed."""
    offered = _revision(state.get("version"))
    if state.get("phase") in _OFFER_PHASES and offered and not _same(offered, running):
        return offered
    return None


def _local_row(listing: Dict[str, Any]) -> Dict[str, Any]:
    from app.core import application_updates as updates
    from app.core.config import settings
    from app.core.file_logging import source_revision

    me = next((row for row in listing.get("runners", []) if row.get("is_self")), None)
    row = {"device_id": listing.get("self_device_id") or "local",
           "label": (me or {}).get("device_name") or "Current worker", "is_self": True, "online": True,
           "revision": _revision(source_revision()), "app_version": settings.version, "channel": None,
           "offered": None, "reason": None}
    try:
        state = updates.status()
        row["channel"] = state.get("channel")
        row["offered"] = _offered(state, row["revision"])
    except Exception:  # noqa: BLE001
        logger.debug("deploy status: local updater status failed", exc_info=True)
    row["channel"] = row["channel"] or (os.environ.get("UPDATE_CHANNEL") or "").strip().lower() or None
    if not row["revision"]:
        row["reason"] = "this worker does not report a source revision"
    return row


async def _peer_row(peer: Dict[str, Any]) -> Dict[str, Any]:
    row = {"device_id": peer["device_id"], "label": peer.get("device_name") or peer["device_id"],
           "is_self": False, "online": bool(peer.get("online")), "revision": None,
           "app_version": peer.get("version"), "channel": None, "offered": None, "reason": None}
    if not row["online"]:
        row["reason"] = "offline"
        return row
    try:
        from app.core import update_workers
        state = await update_workers._peer_status_for(peer["device_id"], fresh=False)
    except Exception:  # noqa: BLE001
        state = {"phase": "unknown"}
    row["revision"] = _revision(state.get("revision")) or _revision(state.get("running_version"))
    row["app_version"] = state.get("app_version") or row["app_version"]
    row["channel"] = state.get("channel")
    row["offered"] = _offered(state, row["revision"])
    if not row["revision"]:
        row["reason"] = ("status unavailable" if state.get("phase") == "unknown"
                         else "worker does not report its revision (it predates revision reporting; update it)")
    return row


async def collect_workers() -> List[Dict[str, Any]]:
    """This worker first, then every other worker on the network."""
    from app.db.async_database import run_sync
    try:
        from app.network import runners
        listing = await run_sync(runners.directory)
    except Exception:  # noqa: BLE001
        logger.debug("deploy status: worker directory failed", exc_info=True)
        listing = {"runners": [], "self_device_id": None}
    rows = [await run_sync(_local_row, listing)]
    peers = [row for row in listing.get("runners", []) if not row.get("is_self")]
    rows.extend(await asyncio.gather(*(_peer_row(peer) for peer in peers)))
    return rows


# ---------------------------------------------------------------------------
# assessment
# ---------------------------------------------------------------------------

def _not_live_action(row: Dict[str, Any], offered_contains: Optional[bool]) -> str:
    if row["offered"] and offered_contains is not False:
        return "update this worker"
    if row["offered"]:
        return "publish edge first (merged after the latest published build)"
    if row["channel"] == "stable":
        return "waits for a stable release (worker follows stable)"
    return "check for updates; publish edge first if none is offered"


def assess(rows: List[Dict[str, Any]], *, pr: Optional[int] = None, commit: Optional[str] = None,
           repo: Optional[Path] = None, refresh: bool = True) -> Dict[str, Any]:
    """Blocking: git per worker, change resolution and the summary line."""
    repo_reason = None
    if repo is None:
        repo = find_repo()
    if repo is None:
        repo_reason = "no local ai-shuffle git checkout on this worker"
    elif refresh:
        _refresh(repo)
    main = _resolve(repo, MAIN) if repo else None
    if repo and not main:
        repo_reason = f"{MAIN} is not available in the local checkout"

    change: Optional[Dict[str, Any]] = None
    target = None
    if pr is not None or commit:
        change = {"pr": pr, "commit": None, "short": None, "reason": None}
        if repo_reason:
            change["reason"] = repo_reason
        elif pr is not None:
            found = _pr_merge_commit(repo, pr)
            target = _resolve(repo, found.get("sha")) if found.get("sha") else None
            if not target:
                change["reason"] = found.get("reason") or "merge commit not in the local checkout"
        elif not _SHA.match(commit):
            change["reason"] = "commit must be a hex SHA (7-40 characters)"
        else:
            target = _resolve(repo, commit)
            if not target:
                change["reason"] = f"commit {commit} not found in the local checkout"
        if target:
            change.update(commit=target, short=target[:9])

    workers = []
    for row in rows:
        full = _resolve(repo, row["revision"]) if repo and not repo_reason and row["revision"] else None
        item = {"label": row["label"], "device_id": row["device_id"], "is_self": row["is_self"],
                "online": row["online"], "revision": full or row["revision"],
                "short": (full or row["revision"] or "")[:9] or None, "app_version": row["app_version"],
                "channel": row["channel"], "behind": None, "contains": None, "action": "unknown",
                "reason": row["reason"], "edge_publication_refusal": None}
        if full:
            item["behind"] = _behind(repo, full)
        elif row["revision"] and not item["reason"]:
            item["reason"] = repo_reason or "revision not found in the local checkout (fetch may be stale)"
        if target and full:
            item["contains"] = _contains(repo, target, full)

        if not row["online"]:
            item["action"] = "offline"
        elif target:
            if item["contains"] is True:
                item["action"] = "up to date"
            elif item["contains"] is False:
                offered_contains = _contains(repo, target, _resolve(repo, row["offered"])) if row["offered"] else None
                item["action"] = _not_live_action(row, offered_contains)
        elif item["behind"] == 0:
            item["action"] = "up to date"
        elif item["behind"]:
            action = _not_live_action(row, None)
            item["action"] = f"{item['behind']} behind; {action}"
        workers.append(item)

    # Edge publishes only origin/main: say why the gate refused it and how to unblock it.
    if main and any("publish edge first" in item["action"] for item in workers):
        refusal = edge_refusal(repo, main)
        if refusal:
            for item in workers:
                if "publish edge first" in item["action"]:
                    evidence: EdgePublicationRefusal = {"revision": main[:9], "reason": refusal}
                    item["edge_publication_refusal"] = evidence
                    item["action"] += f". Edge refused {main[:9]}: {refusal}"

    android = None
    if change is not None:
        android = android_build(_files(repo, target) if target else None)

    marks = []
    for item in workers:
        if not item["online"]:
            value = None  # a last-known revision may be stale; don't claim either way
        else:
            value = item["contains"] if change is not None else (item["behind"] == 0 if item["behind"] is not None else None)
        marks.append(f"{item['label']} {'✓' if value else '✗' if value is False else '?'}")
    if android is not None:
        needed = android["build_needed"]
        marks.append("Android: " + ("Play build needed" if needed else "no build needed" if needed is False else "unknown"))
    return {"origin_main": main[:9] if main else None, "reason": repo_reason, "change": change,
            "android": android, "workers": workers, "summary": "Live on: " + " · ".join(marks)}


async def deploy_status(pr: Optional[int] = None, commit: Optional[str] = None) -> Dict[str, Any]:
    """The full report. Never raises."""
    from app.db.async_database import run_sync
    try:
        rows = await collect_workers()
        return await run_sync(assess, rows, pr=pr, commit=commit)
    except Exception as exc:  # noqa: BLE001 — informational
        logger.warning("deploy status failed", exc_info=True)
        return {"origin_main": None, "reason": f"deploy status failed: {exc}", "change": None,
                "android": None, "workers": [], "summary": "Live on: unknown"}
