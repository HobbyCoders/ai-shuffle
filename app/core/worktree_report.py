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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid.tools.child_env import scrub_child_env

logger = logging.getLogger(__name__)

_DONE_PR_STATES = {"merged", "closed"}


def _git(repo: str, *args: str, timeout: int = 15) -> Optional[str]:
    try:
        result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, env=scrub_child_env())
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


def integration_proof(repo: str, default: str, head: Optional[str], pulls=(), *,
                      label: str = "the default branch", local: bool = True,
                      base_branch: Optional[str] = None) -> Optional[str]:
    """How ``head`` is proven integrated into ``default``, or None when it is not.

    The one proof the sweeper and the report share. Each step only reads Git:
    (a) ancestry; (b) ``merge-tree --write-tree`` yields default's own tree
    (a squash or rebase merge); (c) patch equivalence: ``git cherry`` lists no
    commit whose change is missing from default (catches squash merges that
    main later edited). ``git cherry`` skips merge commits, so this step is
    used only when ``head`` has no merge commit of its own; (d) a merged PR
    into ``base_branch`` (the default branch name) whose head is exactly
    ``head``, or that GitHub lists ``head`` as one of its commits (``pulls``
    entries with ``contains_commit``, from ``github_service.list_commit_pulls``).
    Without ``base_branch`` no PR counts. Errors and unsupported Git versions
    are "not proven", never "proven".
    """
    if not head or not default:
        return None
    if not local:
        return _pr_proof(head, pulls, base_branch)
    try:
        probe = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", head, default],
                               capture_output=True, timeout=15, env=scrub_child_env())
        if probe.returncode == 0:
            return f"contained in {label}"
    except (OSError, subprocess.SubprocessError):
        pass
    tree = _git(repo, "rev-parse", "--verify", default + "^{tree}")
    merged = _git(repo, "merge-tree", "--write-tree", default, head, timeout=30)
    if tree and merged and merged.strip() == tree.strip():
        return f"squash-merged (merging it changes nothing in {label})"
    # A merge commit can carry its own content (a conflict resolution) that
    # git cherry never compares: no patch-equivalence proof across merges.
    merges = _git(repo, "rev-list", "--merges", "--max-count=1", f"{default}..{head}", timeout=30)
    if merges is not None and not merges.strip():
        cherry = _git(repo, "cherry", default, head, timeout=60)
        lines = [line for line in (cherry or "").splitlines() if line.strip()]
        if cherry is not None and lines and all(line.startswith("- ") for line in lines):
            return f"every commit's change is already in {label}"
    return _pr_proof(head, pulls, base_branch)


def _pr_proof(head: str, pulls, base_branch: Optional[str]) -> Optional[str]:
    if not base_branch:
        return None
    for pr in pulls or ():
        if str(getattr(pr, "state", "")).lower() != "merged":
            continue
        if str(getattr(pr, "base_branch", "") or "").casefold() != base_branch.casefold():
            continue  # merged into another branch is not integrated
        number = getattr(pr, "number", None)
        name = f"PR #{number}" if number else "a PR"
        if getattr(pr, "head_sha", None) == head:
            return f"{name} merged at this commit"
        if getattr(pr, "contains_commit", None) == head:
            return f"{name} merged with this commit in it"
    return None


def assess(item: Dict[str, Any], sessions: List[Dict[str, Any]], dirty: Optional[int],
           ahead: Optional[int], merged_into_default, pushed: Optional[bool] = None) -> Dict[str, Any]:
    """Pure: decide ``reclaimable`` and the reason for one worktree.

    ``merged_into_default`` is the integration proof (a description, or True)
    when the branch is proven integrated; such a branch is never described as
    unmerged or as holding unpushed work. ``pushed`` is True when a
    remote-tracking ref contains the commits ahead of the default branch.
    """
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
        reasons.append(f"{ahead} commit(s) not in the default branch, all pushed" if pushed
                       else f"{ahead} unpushed commit(s)")
    reason = "; ".join(reasons) if reasons else "safe to remove"
    if merged_into_default and isinstance(merged_into_default, str):
        reason += f" (integrated: {merged_into_default})"
    return {
        "path": item["path"], "branch": item.get("branch"), "prunable": item.get("prunable", False),
        "sessions": [{"id": s["id"], "title": s.get("title"), "running": bool(s.get("running")),
                      "pr_state": s.get("pr_state"), "pr_number": s.get("pr_number")} for s in sessions],
        "dirty_files": dirty, "unpushed_commits": None if merged_into_default else ahead,
        "merged_into_default": merged_into_default,
        "reclaimable": not reasons, "reason": reason,
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
                       "worktree_path, worktree_id, instance_id, owner_device_id, pr_state, pr_number "
                       "FROM sessions WHERE project_v2_id IS NOT NULL OR worktree_id IS NOT NULL "
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
        if not path and row.get("worktree_id"):
            from app.db import database
            worktree = database.get_worktree(row["worktree_id"])
            path = worktree.get("worktree_path") if worktree else None
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


_RETIRED_CHECKOUT_STATUSES = {"archived", "removed", "deleted"}
_RETIRED_WORKTREE_STATUSES = {"completed", "deleted"}


def choose_base_checkout(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The base row (branch NULL) of one project on one runner that is real.

    A moved project leaves several base rows behind, some pointing at folders
    that no longer exist. Prefer a row whose folder exists and is a Git checkout
    root, then an active row, then the one containing the most registered
    branch checkouts, then the oldest (created_at, id): a deterministic choice.
    """
    bases = [c for c in rows if not c.get("branch") and c.get("path")]
    if len(bases) < 2:
        return bases[0] if bases else None
    branch_paths = [Path(_absolute(c["path"])) for c in rows if c.get("branch") and c.get("path")]

    def rank(row):
        path = _absolute(row["path"])
        contained = sum(1 for p in branch_paths if p.is_relative_to(path))
        return (not _is_checkout_root(path), (row.get("status") or "active") != "active",
                -contained, str(row.get("created_at") or ""), str(row.get("id") or ""))

    return min(bases, key=rank)


def _project_repos(*, active_only: bool = False) -> List[Dict[str, Any]]:
    """Union of local legacy registries and canonical runner checkouts.

    ``active_only`` leaves out retired history (archived checkout rows and
    completed legacy worktrees), as the sweeper does.
    """
    from app.core.config import settings
    from app.db import checkouts, database, projects_v2

    repos: dict[str, dict[str, Any]] = {}
    for project in database.get_all_projects() or []:
        if not project.get("path"):
            continue
        path = _absolute(project["path"])
        repo = database.get_git_repository_by_project(project["id"])
        registered = database.get_worktrees_for_repository(repo["id"]) if repo else []
        entry = repos.setdefault(_path_key(path), {
            "project": project.get("name") or project["id"], "project_id": project["id"],
            "path": path, "aliases": [], "registered": [],
        })
        entry["aliases"].append(project["id"])
        entry["registered"].extend(
            {"path": _absolute(w["worktree_path"]), "branch": w["branch_name"], "registry_ids": [w["id"]]}
            for w in registered if w.get("worktree_path")
            and not (active_only and w.get("status") in _RETIRED_WORKTREE_STATUSES)
        )
    for project in projects_v2.list_projects_v2():
        local = [c for c in checkouts.list_checkouts(project["id"])
                 if c["runner_id"] == settings.effective_instance_id and c.get("path")]
        if not local:
            continue
        base = choose_base_checkout(local) or local[0]
        path = _absolute(base["path"])
        entry = repos.setdefault(_path_key(path), {
            "project": project.get("name") or project["id"], "project_id": project["id"],
            "path": path, "aliases": [], "registered": [],
        })
        entry["aliases"].append(project["id"])
        entry["registered"].extend(
            {"path": _absolute(c["path"]), "branch": c["branch"], "registry_ids": [c["id"]]}
            for c in local if c.get("branch")
            and not (active_only and c.get("status") in _RETIRED_CHECKOUT_STATUSES)
        )
    return list(repos.values())


#: The whole report, planner included, stops inspecting rows after this long.
REPORT_SECONDS = 25


def _planner(project_sources, source, deadline=None):
    """The sweeper's dry-run verdicts for this project, or an error string."""
    from app.core import worktree_reclaim

    local = project_sources.get(_path_key(source["path"]))
    if local is None:
        return {}, "automatic cleanup does not cover this project on this worker (no local Git base checkout)"
    try:
        return worktree_reclaim.plan_preview(local, deadline), None
    except Exception as exc:  # noqa: BLE001 — reported, never fatal to the report
        return {}, f"automatic cleanup cannot inspect this project: {exc}"


def _compact(row: Dict[str, Any], verbose: bool) -> Dict[str, Any]:
    """Drop empty and default fields: the default report must stay small.

    ``dirty_files`` and ``unpushed_commits`` stay when unknown (None): that is
    information, not an empty value.
    """
    if verbose:
        return row
    keep = {"path", "branch", "reclaimable", "reason", "cleanup"}
    out = {k: v for k, v in row.items()
           if k in keep or (k in ("dirty_files", "unpushed_commits") and v is None and row.get("state") != "missing")
           or (k != "registry_ids" and v not in (None, False, [], "", 0) and not (k == "state" and v == "valid"))}
    if out.get("sessions"):
        out["sessions"] = [{k: v for k, v in s.items() if v not in (None, False, "")} for s in out["sessions"]]
    return out


def report(project: Optional[str] = None, *, include_retired: bool = False) -> Dict[str, Any]:
    """Blocking. Include registry-only paths; never inspect an enclosing repo as a worktree.

    Each row's ``cleanup`` is the automatic sweeper's own dry-run verdict
    (``worktree_reclaim.plan_preview``): when it will be removed, or exactly
    why it is kept. Retired history (archived/completed registrations) is left
    out unless ``include_retired``; registrations whose folder and branch are
    both gone are folded into ``history_folded``.
    """
    deadline = time.monotonic() + REPORT_SECONDS
    sessions = _sessions_by_path()
    projects = []
    project_sources: Dict[str, Any] = {}
    try:
        from app.core import worktree_reclaim

        project_sources = {_path_key(s["path"]): s for s in worktree_reclaim._local_sources()}
    except Exception:  # noqa: BLE001 — each project then reports the gap
        logger.warning("WorktreeReport: automatic cleanup sources unavailable", exc_info=True)
    for source in _project_repos(active_only=not include_retired):
        repo = {k: v for k, v in source.items() if k not in {"registered", "aliases"}}
        names = [repo["project"], repo["project_id"], repo["path"], *source.get("aliases", [])]
        if project and project.casefold() not in {str(n).casefold() for n in names}:
            continue
        repo_valid = _is_checkout_root(repo["path"])
        listing = _git(repo["path"], "worktree", "list", "--porcelain") if repo_valid else None
        items = {_path_key(i["path"]): i for i in parse_worktree_list(listing or "")[1:]}
        registered_keys = set()
        for registered in source.get("registered", []):
            key = _path_key(registered["path"])
            registered_keys.add(key)
            item = items.setdefault(key, dict(registered))
            if registered.get("branch") and registered["branch"] != item.get("branch"):
                item.setdefault("bound_branches", []).append(registered["branch"])
            item.setdefault("registry_ids", []).extend(
                i for i in registered.get("registry_ids", []) if i not in item.get("registry_ids", [])
            )
        planned, planner_error = _planner(project_sources, source, deadline) if repo_valid else ({}, None)
        default = ((_git(repo["path"], "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
                    if repo_valid else None) or "origin/main").strip()
        rows = []
        folded = 0
        for key, item in items.items():
            path = item["path"]
            plan = planned.get(key)
            if time.monotonic() >= deadline or (plan and plan.get("not_checked")):
                rows.append({"path": path, "branch": item.get("branch"), "reclaimable": False,
                             "reason": "not checked (time limit); run the report again",
                             "cleanup": "kept: not checked (time limit); run the report again"})
                continue
            state, error, binding_warning = "valid", None, None
            root_valid = _is_checkout_root(path)
            if not Path(path).exists():
                state, error = "missing", "directory missing; needs cleanup"
            elif not root_valid:
                state, error = "invalid", "not its own Git working tree; directory preserved; needs cleanup"
            else:
                actual = (_git(path, "symbolic-ref", "--short", "HEAD") or "").strip() or None
                bound = {b for b in [item.get("branch"), *item.get("bound_branches", [])] if b}
                mismatches = sorted(b for b in bound if b != actual)
                if mismatches:
                    state = "branch_mismatch"
                    binding_warning = (
                        f"Checkout is on {actual or 'a detached HEAD'!s}, not the bound branch "
                        f"{', '.join(mismatches)}; counts describe the actual checkout."
                    )
                    error = binding_warning
                item["branch"] = actual
            status = _git(path, "status", "--porcelain") if root_valid else None
            # Missing folders have no files to lose. Invalid folders may still
            # hold uncommitted work: never call those safe based on the parent repo.
            dirty = (len([line for line in status.splitlines() if line.strip()])
                     if status is not None else 0 if state == "missing" else None)
            # Read HEAD/comparison refs at the verified physical root, not the
            # bound branch or the parent project's checkout after a switch.
            head = ((_git(path, "rev-parse", "HEAD") or "").strip() if root_valid
                    else item.get("head") if state == "missing" else None)
            if not head and state == "missing" and item.get("branch") and repo_valid:
                head = (_git(repo["path"], "rev-parse", "--verify", f"refs/heads/{item['branch']}") or "").strip()
            row_sessions = sessions.get(key, [])
            if plan and plan.get("pr_states"):
                # GitHub's answer from the sweeper's lookup beats a session's cached PR state.
                row_sessions = [{**s, "pr_state": plan["pr_states"].get(s.get("pr_number"), s.get("pr_state"))}
                                for s in row_sessions]
            if (state == "missing" and repo_valid and not head and not row_sessions and not item.get("locked")
                    and not include_retired):
                folded += 1  # folder and branch both gone: history, not a worktree
                continue
            comparison_repo = path if root_valid else repo["path"]
            can_compare = root_valid or (repo_valid and state == "missing")
            ahead_out = (_git(comparison_repo, "rev-list", "--count", f"{default}..{head}")
                         if head and can_compare else None)
            ahead = int(ahead_out.strip()) if ahead_out and ahead_out.strip().isdigit() else None
            merged = None
            if head and can_compare:
                # Reuse the sweeper's proof for this exact HEAD (it can also use
                # an exact merged PR head); compute it only when the planner
                # stopped before proving anything.
                if plan and plan.get("head_sha") == head:
                    merged = plan.get("proof") or False  # the planner already ran the proof
                else:
                    merged = integration_proof(comparison_repo, default, head, label=default) or False
            pushed = bool(ahead and not merged and _git(
                comparison_repo, "for-each-ref", "--count=1", "--format=%(refname)", "--contains", head, "refs/remotes"))
            row = assess(item, row_sessions, dirty, ahead, merged, pushed)
            row.update(state=state, registry_ids=item.get("registry_ids", []), binding_warning=binding_warning,
                       directory_preserved=state in {"invalid", "branch_mismatch"})
            if state in {"invalid", "branch_mismatch"}:
                row["reclaimable"] = False
            if error:
                row["reason"] = error if row["reason"] == "safe to remove" else error + "; " + row["reason"]
            if plan:
                row["cleanup"] = plan["cleanup"]
            elif key not in registered_keys:
                row["cleanup"] = "kept: not registered with the app; automatic cleanup never touches it"
            else:
                row["cleanup"] = "kept: " + (planner_error or "automatic cleanup could not inspect it")
            rows.append(_compact(row, include_retired))
        archived = _archived_summary(repo["path"]) if repo_valid else None
        projects.append({**repo, "worktrees": rows,
                         **({"error": "git worktree list failed"} if listing is None else {}),
                         **({"cleanup_error": planner_error} if planner_error else {}),
                         **({"history_folded": folded} if folded else {}),
                         **({"archived": archived} if archived else {}),
                         "reclaimable": sum(1 for row in rows if row["reclaimable"]),
                         "removed_automatically": sum(1 for row in rows if not row["cleanup"].startswith("kept")),
                         "total": len(rows)})
    total = sum(p.get("total", 0) for p in projects)
    reclaimable = sum(p.get("reclaimable", 0) for p in projects)
    automatic = sum(p.get("removed_automatically", 0) for p in projects)
    folded = sum(p.get("history_folded", 0) for p in projects)
    summary = (f"{total} worktrees: {automatic} will be removed by automatic cleanup; "
               f"{reclaimable} hold nothing that would be lost")
    if folded:
        summary += f"; {folded} retired registrations (folder and branch both gone) folded into counts"
    return {"projects": projects, "total": total, "reclaimable": reclaimable,
            "removed_automatically": automatic, "summary": summary,
            "note": "Report only. Automatic cleanup removes the rows whose cleanup says so; remove anything else "
                    "through the app, never with git worktree remove or rm -rf."}


def _archived_summary(repo: str) -> Optional[Dict[str, Any]]:
    """Abandoned worker checkouts snapshotted by automatic cleanup: a count and how to restore one."""
    from app.core import worktree_reclaim

    names = worktree_reclaim.archived_refs(repo)
    if not names:
        return None
    prefix = worktree_reclaim.ARCHIVE_REF_PREFIX
    return {"count": len(names),
            "list": f"git -C {repo} for-each-ref {prefix.rstrip('/')}",
            "restore": f"git -C {repo} branch <branch> {prefix}<branch>"}
