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
        cursor.execute("SELECT id, title, project_id, project_v2_id, branch, worktree_branch, "
                       "worktree_path, instance_id, owner_device_id, pr_state, pr_number "
                       "FROM sessions WHERE project_v2_id IS NOT NULL "
                       "OR (worktree_path IS NOT NULL AND worktree_path <> '')")
        rows = cursor.fetchall()
    out: Dict[str, List[Dict[str, Any]]] = {}
    from app.core.config import settings
    from app.core.project_resolver import _local_checkout
    from app.network.control import owner_of

    for row in rows:
        row = dict(row)
        if owner_of(row) is not None:
            continue
        # Metadata reads must not invoke the resolver's session/project
        # self-healing writes. Honor the legacy branch without backfilling it.
        branch = row.get("branch")
        if not branch or not str(branch).strip():
            branch = row.get("worktree_branch")
        checkout = (_local_checkout(row["project_v2_id"], settings.effective_instance_id,
                                    branch=branch) if row.get("project_v2_id") else None)
        path = checkout.get("path") if checkout else row.get("worktree_path")
        if not path:
            continue
        row["running"] = streaming_status.has_active_run(row["id"])
        out.setdefault(_path_key(path), []).append(row)
    return out


def _path_key(path: str) -> str:
    import os

    return os.path.normcase(_absolute(path))


def _is_checkout_root(path: str) -> bool:
    # Git walks upward from an ordinary directory; do not mistake its parent
    # repository for the registered checkout.
    if not Path(path).is_dir() or not (Path(path) / ".git").exists():
        return False
    root = _git(path, "rev-parse", "--show-toplevel", timeout=5)
    return bool(root and root.strip() and _path_key(root.strip()) == _path_key(path))


def _project_repos() -> List[Dict[str, Any]]:
    """Union of local legacy registries and canonical runner checkouts."""
    from app.core.config import settings
    from app.db import checkouts, database, projects_v2

    repos: dict[str, dict[str, Any]] = {}
    for project in database.get_all_projects() or []:
        if not project.get("path"):
            continue
        path = _absolute(project["path"])
        repo = database.get_git_repository_by_project(project["id"])
        registered = database.get_worktrees_for_repository(repo["id"]) if repo else []
        repos[_path_key(path)] = {
            "project": project.get("name") or project["id"], "project_id": project["id"],
            "path": path, "aliases": [project["id"]],
            "registered": [{"path": _absolute(w["worktree_path"]), "branch": w["branch_name"],
                            "registry_ids": [w["id"]]} for w in registered if w.get("worktree_path")],
        }
    for project in projects_v2.list_projects_v2():
        local = [c for c in checkouts.list_checkouts(project["id"])
                 if c["runner_id"] == settings.effective_instance_id and c.get("path")]
        if not local:
            continue
        base = next((c for c in local if not c.get("branch")), local[0])
        path = _absolute(base["path"])
        entry = repos.setdefault(_path_key(path), {
            "project": project.get("name") or project["id"], "project_id": project["id"],
            "path": path, "aliases": [], "registered": [],
        })
        entry["aliases"].append(project["id"])
        entry["registered"].extend(
            {"path": _absolute(c["path"]), "branch": c["branch"], "registry_ids": [c["id"]]}
            for c in local if c.get("branch")
        )
    return list(repos.values())


def report(project: Optional[str] = None) -> Dict[str, Any]:
    """Blocking. Include registry-only paths; never inspect an enclosing repo as a worktree."""
    from app.core.checkout_manager import WorktreeError, _require_branch_binding

    sessions = _sessions_by_path()
    projects = []
    for source in _project_repos():
        repo = {k: v for k, v in source.items() if k not in {"registered", "aliases"}}
        names = [repo["project"], repo["project_id"], repo["path"], *source.get("aliases", [])]
        if project and project.casefold() not in {str(n).casefold() for n in names}:
            continue
        repo_valid = _is_checkout_root(repo["path"])
        listing = _git(repo["path"], "worktree", "list", "--porcelain") if repo_valid else None
        items = {_path_key(i["path"]): i for i in parse_worktree_list(listing or "")[1:]}
        for registered in source.get("registered", []):
            key = _path_key(registered["path"])
            item = items.setdefault(key, dict(registered))
            if registered.get("branch") and registered["branch"] != item.get("branch"):
                item.setdefault("bound_branches", []).append(registered["branch"])
            item.setdefault("registry_ids", []).extend(
                i for i in registered.get("registry_ids", []) if i not in item.get("registry_ids", [])
            )
        default = ((_git(repo["path"], "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
                    if repo_valid else None) or "origin/main").strip()
        rows = []
        for key, item in items.items():
            path = item["path"]
            state, error = "valid", None
            if not Path(path).is_dir():
                state, error = "missing", "directory missing; needs cleanup"
            else:
                try:
                    if item.get("branch"):
                        for branch in {item["branch"], *item.get("bound_branches", [])}:
                            _require_branch_binding(path, branch)
                    elif not _is_checkout_root(path):
                        raise WorktreeError("not its own Git working tree")
                except WorktreeError as exc:
                    state, error = "invalid", f"{exc}; needs cleanup"
            status = _git(path, "status", "--porcelain") if state == "valid" else None
            # Missing folders have no files to lose. Invalid folders may still
            # hold uncommitted work: never call those safe based on the parent repo.
            dirty = (len([line for line in status.splitlines() if line.strip()])
                     if status is not None else 0 if state == "missing" else None)
            head = item.get("head")
            if not head and item.get("branch") and repo_valid:
                head = (_git(repo["path"], "rev-parse", "--verify", f"refs/heads/{item['branch']}") or "").strip()
            ahead_out = (_git(repo["path"], "rev-list", "--count", f"{default}..{head}")
                         if head and repo_valid else None)
            ahead = int(ahead_out.strip()) if ahead_out and ahead_out.strip().isdigit() else None
            merged = None
            if head and repo_valid:
                try:
                    probe = subprocess.run(["git", "-C", repo["path"], "merge-base", "--is-ancestor", head, default],
                                           capture_output=True, timeout=15)
                    merged = probe.returncode == 0 if probe.returncode in (0, 1) else None
                except (OSError, subprocess.SubprocessError):
                    pass
            row = assess(item, sessions.get(key, []), dirty, ahead, merged)
            row.update(state=state, registry_ids=item.get("registry_ids", []))
            if error:
                row["reason"] = error + "; " + row["reason"]
            rows.append(row)
        projects.append({**repo, "worktrees": rows,
                         **({"error": "git worktree list failed"} if listing is None else {}),
                         "reclaimable": sum(1 for row in rows if row["reclaimable"]), "total": len(rows)})
    total = sum(p.get("total", 0) for p in projects)
    reclaimable = sum(p.get("reclaimable", 0) for p in projects)
    return {"projects": projects, "total": total, "reclaimable": reclaimable,
            "summary": f"{reclaimable} of {total} worktrees can be removed safely",
            "note": "Report only. Remove worktrees through the app, never with git worktree remove or rm -rf."}
