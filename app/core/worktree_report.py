"""Which app-managed worktrees can be reclaimed — a report, never a deletion.

111 stale worktrees once piled up before anyone noticed. This lists each linked
worktree of every registered project on this worker with what decides whether
it is safe to remove: the sessions bound to it (and whether any is running),
its branch's PR state, uncommitted changes and unpushed commits. ``reclaimable``
is True only when nothing would be lost and nothing is using it.

Deletion stays with the app's own removal path (it detaches dependency links
first); agents must never remove worktrees themselves.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DONE_PR_STATES = {"merged", "closed"}


def _git(repo: str, *args: str, timeout: int = 15) -> Optional[str]:
    try:
        result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def parse_worktree_list(porcelain: str) -> List[Dict[str, Any]]:
    """``git worktree list --porcelain`` → ``[{path, head, branch, locked, prunable}]``."""
    items: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for line in porcelain.splitlines() + [""]:
        if not line.strip():
            if current:
                items.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value, "head": None, "branch": None, "locked": False, "prunable": False}
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "locked":
            current["locked"] = True
        elif key == "prunable":
            current["prunable"] = True
    return items


def assess(item: Dict[str, Any], sessions: List[Dict[str, Any]], dirty: Optional[int],
           ahead: Optional[int], merged_into_default: Optional[bool]) -> Dict[str, Any]:
    """Pure: decide ``reclaimable`` and the reason for one worktree."""
    running = [s for s in sessions if s.get("running")]
    pr_states = {str(s.get("pr_state") or "").lower() for s in sessions if s.get("pr_state")}
    pr_done = bool(pr_states) and pr_states <= _DONE_PR_STATES
    reasons: List[str] = []
    if running:
        reasons.append("a session is working in it")
    if item.get("locked"):
        reasons.append("locked")
    if dirty is None or dirty:
        reasons.append("uncommitted changes" if dirty else "status unknown")
    if not (pr_done or merged_into_default):
        reasons.append("PR still open" if "open" in pr_states else "branch not merged and no closed PR")
    if ahead and not pr_done and not merged_into_default:
        reasons.append(f"{ahead} unpushed commit(s)")
    return {
        "path": item["path"], "branch": item.get("branch"), "prunable": item.get("prunable", False),
        "sessions": [{"id": s["id"], "title": s.get("title"), "running": bool(s.get("running")),
                      "pr_state": s.get("pr_state"), "pr_number": s.get("pr_number")} for s in sessions],
        "dirty_files": dirty, "unpushed_commits": ahead, "merged_into_default": merged_into_default,
        "reclaimable": not reasons, "reason": "safe to remove" if not reasons else "; ".join(reasons),
    }


def _absolute(path: str) -> str:
    """Workspace-relative paths (how projects and sessions store them) made absolute."""
    from app.core.project_utils import get_project_working_dir

    return str(Path(get_project_working_dir(path)).resolve())


def _sessions_by_path() -> Dict[str, List[Dict[str, Any]]]:
    from app.core import streaming_status
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, worktree_path, pr_state, pr_number FROM sessions "
                       "WHERE worktree_path IS NOT NULL AND worktree_path <> ''")
        rows = cursor.fetchall()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        row = dict(row)
        row["running"] = streaming_status.has_active_run(row["id"])
        out.setdefault(_absolute(row["worktree_path"]), []).append(row)
    return out


def _project_repos() -> List[Dict[str, str]]:
    from app.db import database

    repos = []
    for project in database.get_all_projects() or []:
        path = _absolute(project["path"]) if project.get("path") else None
        if path and Path(path).is_dir() and _git(path, "rev-parse", "--git-dir", timeout=5) is not None:
            repos.append({"project": project.get("name") or project.get("id"),
                          "project_id": project.get("id"), "path": path})
    return repos


def report(project: Optional[str] = None) -> Dict[str, Any]:
    """Blocking. Every linked worktree of each registered project; never raises per repo."""
    sessions = _sessions_by_path()
    projects = []
    for repo in _project_repos():
        if project and project.casefold() not in {str(repo["project"]).casefold(),
                                                  str(repo.get("project_id") or "").casefold(),
                                                  repo["path"].casefold()}:
            continue
        listing = _git(repo["path"], "worktree", "list", "--porcelain")
        if listing is None:
            projects.append({**repo, "error": "git worktree list failed", "worktrees": []})
            continue
        default = (_git(repo["path"], "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "origin/main").strip()
        rows = []
        for item in parse_worktree_list(listing)[1:]:  # the first entry is the main checkout
            path = item["path"]
            if item.get("prunable") or not Path(path).is_dir():
                rows.append(assess(item, sessions.get(str(Path(path).resolve()), []), 0, 0, True)
                            | {"reason": "directory missing (git can prune the record)"})
                continue
            status = _git(path, "status", "--porcelain")
            dirty = len([line for line in status.splitlines() if line.strip()]) if status is not None else None
            ahead_out = _git(path, "rev-list", "--count", f"{default}..HEAD")
            ahead = int(ahead_out.strip()) if ahead_out and ahead_out.strip().isdigit() else None
            merged = None
            if item.get("head"):
                probe = subprocess.run(["git", "-C", repo["path"], "merge-base", "--is-ancestor", item["head"], default],
                                       capture_output=True, timeout=15)
                merged = probe.returncode == 0 if probe.returncode in (0, 1) else None
            rows.append(assess(item, sessions.get(str(Path(path).resolve()), []), dirty, ahead, merged))
        projects.append({**repo, "worktrees": rows,
                         "reclaimable": sum(1 for row in rows if row["reclaimable"]), "total": len(rows)})
    total = sum(p.get("total", 0) for p in projects)
    reclaimable = sum(p.get("reclaimable", 0) for p in projects)
    return {"projects": projects, "total": total, "reclaimable": reclaimable,
            "summary": f"{reclaimable} of {total} worktrees can be removed safely",
            "note": "Report only. Remove worktrees through the app, never with git worktree remove or rm -rf."}
