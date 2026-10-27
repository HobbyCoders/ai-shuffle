"""Owner-only checkout reclamation. All Git/filesystem work runs off the loop.

Only registered, local, app-managed placements are candidates. Unknown safety
facts retain work; a closed PR alone is NOT evidence that work was integrated.
The agent WorktreeReport remains strictly read-only; it shows this module's
dry-run verdicts (:func:`plan_preview`), so the two never disagree.

Besides integrated checkouts, a pass also removes: worker checkouts a reviewer
left on a pushed commit (their own branch untouched), local branches with no
worktree that are proven integrated or pushed with a finished PR, and, after
a week of idleness, abandoned worker checkouts, snapshotted first under
``refs/fluid-archive/<branch>`` so nothing is lost.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fluid.tools.child_env import scrub_child_env

from app.core import worktree_report as wr
from app.core.config import settings
from app.core.git_service import git_service
from app.core.git_worktree_ops import forget_stale_worktree_metadata, repo_worktree_lock

logger = logging.getLogger(__name__)
GRACE_SECONDS = 24 * 3600
# Every pass visits every local project; each stays bounded by MAX_ITEMS and
# PASS_SECONDS. A pass costs about one PR lookup per candidate.
INTERVAL_SECONDS = 2 * 3600
MAX_ITEMS = 50
PASS_SECONDS = 120
# Abandoned worker scratch is archived only after both the worker and its
# parent have been idle this long.
ARCHIVE_IDLE_SECONDS = 7 * 24 * 3600
ARCHIVE_REF_PREFIX = "refs/fluid-archive/"
ARCHIVE_IDENTITY = {"GIT_AUTHOR_NAME": "AI Shuffle", "GIT_AUTHOR_EMAIL": "noreply@hobbycoders.com",
                    "GIT_COMMITTER_NAME": "AI Shuffle", "GIT_COMMITTER_EMAIL": "noreply@hobbycoders.com"}
_pass_lock = threading.Lock()


def _local_sources():
    # The report's union supplies canonical and legacy aliases. Reject foreign
    # legacy projects even when a shared DB happens to contain a matching path.
    from app.db import checkouts, database

    sources = []
    for source in wr._project_repos(active_only=True):
        aliases = []
        for alias in source["aliases"]:
            legacy = database.get_project(alias)
            if legacy and legacy.get("instance_id") not in (None, "", settings.effective_instance_id):
                continue
            aliases.append(alias)
        if aliases and Path(source["path"], ".git").is_dir():
            canonical = {r["id"]: r for alias in aliases for r in checkouts.list_checkouts(alias)}
            registered = []
            for item in source["registered"]:
                ids = []
                created_times = []
                for registry_id in item["registry_ids"]:
                    row = canonical.get(registry_id)
                    if row:
                        if row["runner_id"] == settings.effective_instance_id and row.get("status") != "archived":
                            ids.append(registry_id)
                            created_times.append(row.get("created_at"))
                    else:
                        legacy = database.get_worktree(registry_id)
                        if legacy and legacy.get("status") != "completed":
                            owner_repo = database.get_git_repository(legacy["repository_id"])
                            if owner_repo and owner_repo["project_id"] in aliases:
                                ids.append(registry_id)
                                created_times.append(legacy.get("created_at"))
                if ids:
                    registered.append({**item, "registry_ids": ids, "created_times": created_times})
            sources.append({**source, "aliases": aliases, "registered": registered})
    return sources


def _project_sessions(source):
    from app.core.git_activity import active_git_sessions
    from app.db.base import get_db
    from app.db.sessions import list_sessions_for_cleanup

    rows = {}
    for alias in source["aliases"]:
        for row in list_sessions_for_cleanup(alias):
            rows[row["id"]] = row
    ids = sorted({i for registered in source["registered"] for i in registered["registry_ids"]})
    if ids:
        placeholders = ",".join("%s" for _ in ids)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT * FROM sessions WHERE worktree_id IN ({placeholders}) "
                           f"OR id IN (SELECT session_id FROM worktrees WHERE id IN ({placeholders}))", ids + ids)
            for row in cursor.fetchall():
                row = dict(row)
                rows[row["id"]] = row
    finished = {}
    if rows:
        placeholders = ",".join("%s" for _ in rows)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT session_id, MAX(ended_at) AS finished_at FROM turns "
                           f"WHERE session_id IN ({placeholders}) AND ended_at IS NOT NULL GROUP BY session_id", list(rows))
            finished = {r["session_id"]: r["finished_at"] for r in cursor.fetchall()}
    busy = {s["id"] for s in active_git_sessions(list(rows.values()))}
    return [{**s, "running": s["id"] in busy, "finished_at": finished.get(s["id"])} for s in rows.values()]


def _context(source):
    from app.core.github_service import github_service

    repo = source["path"]
    if not wr._is_checkout_root(repo):
        raise ValueError("base checkout unavailable")
    listing = wr._git(repo, "worktree", "list", "--porcelain")
    if listing is None:
        raise ValueError("worktree listing unavailable")
    items = {wr._path_key(i["path"]): i for i in wr.parse_worktree_list(listing)}
    default = (wr._git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "").strip()
    branch = default.removeprefix("origin/") if default else git_service._get_default_branch_cli(repo)
    if not default and wr._git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}^{{commit}}"):
        # Without origin/HEAD, prove against the remote-tracking default, not
        # a local branch that may be behind what was merged.
        default = f"origin/{branch}"
    ref = default or f"refs/heads/{branch}"
    default_sha = (wr._git(repo, "rev-parse", "--verify", ref + "^{commit}") or "").strip()
    if not default_sha:
        raise ValueError("default branch unavailable")
    remotes = wr._git(repo, "remote")
    if remotes is None:
        raise ValueError("remote configuration unavailable")
    remote = wr._git(repo, "remote", "get-url", "origin") if "origin" in remotes.splitlines() else ""
    if remote is None:
        raise ValueError("origin URL unavailable; PR status unknown")
    github_repo = github_service.get_repo_from_remote(repo) if remote else None
    if "github.com" in remote and not github_repo:
        raise ValueError("PR repository could not be resolved")
    return {"items": items, "default_branch": branch, "default_sha": default_sha,
            "default_label": default or branch,
            "sessions": _project_sessions(source), "github_repo": github_repo}


def _branch_head(repo, branch):
    if not branch:
        return None
    ref = f"refs/heads/{branch}"
    probe = git_service._run_git(repo, ["show-ref", "--verify", "--quiet", ref], timeout=10)
    if probe.returncode == 1:
        return None  # positively absent, not an unreadable ref
    if probe.returncode != 0:
        raise ValueError("branch existence could not be verified")
    head = (wr._git(repo, "rev-parse", "--verify", ref + "^{commit}") or "").strip()
    if not head:
        raise ValueError("branch HEAD unavailable")
    return head


def _proof(repo, context, head, prs=()):
    # The shared integration proof (ancestry, merge-tree, patch equivalence,
    # exact merged PR head). It never touches the index or working tree;
    # conflicts and Git errors fail closed.
    return wr.integration_proof(repo, context["default_sha"], head, prs,
                                label=context.get("default_label") or context["default_branch"],
                                base_branch=context["default_branch"])


def _worker_session_id(branch):
    """The worker id of a ``chore/task-<id>`` (or legacy ``task/<id>``) branch."""
    from app.core.worker_checkout import LEGACY_WORKER_BRANCH_PREFIX, WORKER_BRANCH_PREFIX

    for prefix in (WORKER_BRANCH_PREFIX, LEGACY_WORKER_BRANCH_PREFIX):
        if branch and branch.startswith(prefix) and len(branch) > len(prefix):
            return branch[len(prefix):]
    return None


def _on_remote(repo, sha):
    """Whether ``sha`` is reachable from any remote-tracking ref (it was pushed)."""
    found = wr._git(repo, "for-each-ref", "--count=1", "--format=%(refname)", "--contains", sha, "refs/remotes")
    return bool(found and found.strip())


def _unpublished_history(repo, path, default, exempt=()):
    """Commits this checkout's HEAD has pointed at (its HEAD reflog) that no
    remote-tracking ref and not ``default`` contain; None when unreadable.

    A reviewer who committed on a detached HEAD and then checked out a pushed
    commit leaves that commit reachable only from the reflog. ``exempt`` holds
    commits accounted for otherwise (the current HEAD, proven on GitHub, and
    the worker's own starting commit).
    """
    listing = wr._git(path, "log", "-g", "--format=%H", "HEAD")
    if listing is None:
        return None
    exempt = [sha for sha in exempt if sha]
    shas = [sha for sha in dict.fromkeys(listing.split()) if sha not in exempt]
    if not shas:
        return []
    # Exempt commits' ancestors are accounted for with them.
    args = ["rev-list", *shas, "--not", "--remotes", *exempt]
    if default:
        args.append(default)
    left = wr._git(repo, *args, timeout=60)
    return None if left is None else left.split()


def _untouched_branch(repo, branch):
    """The tip of a worker branch nobody committed to (its reflog holds only its creation)."""
    head = _branch_head(repo, branch)
    if not head:
        return None
    reflog = wr._git(repo, "reflog", "show", "--format=%H", f"refs/heads/{branch}")
    return head if reflog is not None and reflog.split() == [head] else None


def _stranded_reviewer(repo, path, branch, context, pulls=None):
    """A worker checkout whose HEAD was moved (detached or ``gh pr checkout``)
    onto a commit that exists elsewhere, while its own branch is untouched.

    "Elsewhere": a remote-tracking ref contains HEAD, it is proven integrated,
    or GitHub lists it in a PR (open or merged: either way it is on GitHub).
    ``pulls`` is the apply-time snapshot; without it the lookup runs here.
    Returns ``{"head", "branch_sha", "how"}`` or None. Only ``chore/task-*``
    (and legacy ``task/*``) worker checkouts qualify; the caller still requires
    a clean tree and only disposable ignored files.
    """
    if not _worker_session_id(branch):
        return None
    branch_sha = _untouched_branch(repo, branch)
    head = (wr._git(path, "rev-parse", "--verify", "HEAD^{commit}") or "").strip()
    if not branch_sha or not head:
        return None
    if head == branch_sha:
        how = "HEAD is still the worker's starting commit"
    elif _on_remote(repo, head):
        how = "HEAD is a pushed commit"
    else:
        on_github = [pr for pr in (_commit_pulls(context, head) if pulls is None else pulls)
                     if getattr(pr, "contains_commit", None) == head]
        proof = _proof(repo, context, head, on_github)
        if proof:
            how = f"HEAD is integrated ({proof})"
        elif on_github:
            how = f"HEAD is a commit of PR #{getattr(on_github[0], 'number', '?')} on GitHub"
        else:
            return None
    # Every other commit HEAD visited must be published too (a detached commit
    # followed by checking out a PR commit leaves it only in the reflog).
    # The worker's own starting commit (its untouched branch tip) is exempt,
    # exactly as when an untouched worker checkout is released at finish.
    left = _unpublished_history(repo, path, context["default_sha"], exempt=(head, branch_sha))
    if left is None or left:
        return None
    return {"head": head, "branch_sha": branch_sha, "how": how}


def _activity_age(session):
    """Seconds since a session's last update or finished turn; None if unknown."""
    try:
        stamps = [_activity_time(v) for v in (session.get("updated_at"), session.get("finished_at")) if v]
    except (ValueError, TypeError):
        return None
    return time.time() - max(stamps) if stamps else None


def _session_activity(session_id):
    """A session row with ``finished_at`` and ``running``, or None if it is gone."""
    from app.core.git_activity import active_git_sessions
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE id = %s", (session_id,))
        row = cursor.fetchone()
        if not row:
            return None
        cursor.execute("SELECT MAX(ended_at) AS finished_at FROM turns WHERE session_id = %s AND ended_at IS NOT NULL",
                       (session_id,))
        finished = cursor.fetchone()
    row = dict(row)
    row["finished_at"] = dict(finished).get("finished_at") if finished else None
    row["running"] = bool(active_git_sessions([row]))
    return row


def _abandon_blocker(context, branch, sessions):
    """Why a worker checkout is not (yet) abandoned scratch, or None when it is.

    Abandoned: the worker session and its parent are both idle for
    ARCHIVE_IDLE_SECONDS, neither is running and no bound session is active.
    Open PRs are refused earlier by the planner.
    """
    worker_id = _worker_session_id(branch)
    if not worker_id:
        return "not a worker checkout"
    worker = next((s for s in context["sessions"] if s.get("id") == worker_id), None)
    if worker is None:
        return "its worker session was not found"
    if any(s.get("is_active") or s.get("running") for s in [worker, *sessions]):
        return "its worker session is active"
    days = ARCHIVE_IDLE_SECONDS // 86400
    age = _activity_age(worker) if worker.get("finished_at") else None
    if age is None:
        return "worker activity time unavailable"
    if age < ARCHIVE_IDLE_SECONDS:
        return f"worker last active {int(age // 86400)} day(s) ago; archived after {days} idle days"
    parent_id = worker.get("parent_session_id")
    parent = _session_activity(parent_id) if parent_id else None
    if parent:
        if parent.get("running"):
            return "its parent session is running"
        parent_age = _activity_age(parent)
        if parent_age is None:
            return "parent session activity time unavailable"
        if parent_age < ARCHIVE_IDLE_SECONDS:
            return f"parent session last active {int(parent_age // 86400)} day(s) ago; archived after {days} idle days"
    return None


def _bound_sessions(context, registered, branch):
    target = Path(registered["path"]).resolve()
    branches = {b for b in (branch, registered.get("branch")) if b}
    ids = set(registered["registry_ids"])
    bound = []
    for session in context["sessions"]:
        logical = {b for b in (session.get("branch"), session.get("worktree_branch")) if b}
        cwd = session.get("worktree_path")
        by_path = bool(cwd and Path(wr._absolute(cwd)).is_relative_to(target))
        unknown_active = (session.get("is_active") or session.get("running")) and not logical and not cwd
        if logical & branches or session.get("worktree_id") in ids or by_path or unknown_active:
            bound.append(session)
    return bound


#: Report-only cache of GitHub answers: (kind, repository, key) -> (time, answer).
#: The sweeper never reads it; removal decisions always use a fresh lookup.
REPORT_CACHE_SECONDS = 600
_report_cache = {}
_report_cache_lock = threading.Lock()


def _cached(context, key, fetch):
    if not context.get("report_cache"):
        return fetch()
    now = time.monotonic()
    with _report_cache_lock:
        hit = _report_cache.get(key)
        if hit and now - hit[0] < REPORT_CACHE_SECONDS:
            return hit[1]
    answer = fetch()
    with _report_cache_lock:
        if len(_report_cache) > 2000:
            for stale in [k for k, (t, _) in _report_cache.items() if now - t >= REPORT_CACHE_SECONDS]:
                del _report_cache[stale]
        _report_cache[key] = (now, answer)
    return answer


def _pulls(context, branch):
    from app.core.github_service import github_service

    if branch and context["github_repo"]:
        return _cached(context, ("branch", context["github_repo"], branch),
                       lambda: github_service.list_pulls(context["github_repo"], state="all", head=branch, limit=100))
    return []


def _open_pr_claimed(sessions, prs):
    """Whether a bound session's cached ``pr_state`` of open still stands.

    The session row caches PR state and can lag a merge. GitHub's answer for
    that PR number (from this plan's lookup) overrides it; a claim GitHub's
    lookup cannot confirm or refute keeps the checkout.
    """
    states = {getattr(pr, "number", None): str(pr.state).lower() for pr in prs}
    for session in sessions:
        if str(session.get("pr_state") or "").lower() != "open":
            continue
        if states.get(session.get("pr_number")) in (None, "open"):
            return True
    return False


def _commit_pulls(context, sha):
    """PRs GitHub associates with this commit (merged PR that introduced it, or
    open PRs containing it). Network: never call under repo_worktree_lock."""
    from app.core.github_service import github_service

    if sha and context["github_repo"]:
        return _cached(context, ("commit", context["github_repo"], sha),
                       lambda: github_service.list_commit_pulls(context["github_repo"], sha))
    return []


def _activity_time(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()


def _session_grace_reason(sessions):
    for session in sessions:
        if not session.get("finished_at"):
            return "bound session has no finished turn; dispatch may still be pending"
        try:
            last = max(_activity_time(session.get("updated_at")), _activity_time(session["finished_at"]))
            if time.time() - last < GRACE_SECONDS:
                return "bound session has activity within the 24-hour freshness guard"
        except (ValueError, TypeError):
            return "bound session activity time unavailable"
    return None


def _checkout_grace_reason(target, registered):
    # A provisioned checkout can exist before its first session is bound. Do
    # not mistake that dispatch window (or an unused owner checkout) for old
    # merged work. Either a new registration or a new directory restarts grace.
    try:
        stamps = [target.stat().st_mtime]
        stamps.extend(_activity_time(value) for value in registered.get("created_times", []))
        if not all(math.isfinite(stamp) for stamp in stamps):
            return "checkout creation time unavailable"
        if time.time() - max(stamps) < GRACE_SECONDS:
            return "no commits of its own; checkout within the 24-hour freshness guard"
    except (OSError, ValueError, TypeError):
        return "checkout creation time unavailable"
    return None


#: Dependency folders; their links are detached by the removal path, never followed.
DEPENDENCY_DIRS = frozenset({"node_modules", "venv", ".venv"})
#: Tool caches that every run regenerates.
CACHE_DIRS = frozenset({".pytest_cache", "__pycache__", ".svelte-kit", ".ruff_cache", ".mypy_cache",
                        ".hypothesis", ".turbo", ".parcel-cache"})
CACHE_FILE_NAMES = frozenset({".eslintcache"})
CACHE_FILE_SUFFIXES = frozenset({".pyc", ".tsbuildinfo"})
#: JavaScript build output: disposable only directly beside a package.json.
JS_BUILD_DIRS = frozenset({"build", "dist"})


def _is_link(target):
    # A Windows junction is a directory link too: never count its target
    # as this checkout's disposable content.
    return target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction())


def _js_build_output(target):
    manifest = target.parent / "package.json"
    return target.name in JS_BUILD_DIRS and manifest.is_file() and not _is_link(manifest)


def _describe_kept(root, kept, limit=5):
    """``uploads/ (4 files), notes.txt``: what removal would destroy."""
    def files(target, budget=1000):
        count = 0
        for _, _, names in os.walk(target):  # never follows directory links
            count += len(names)
            if count >= budget:
                return f"{budget}+"
        return str(count)

    parts = []
    for relative in sorted(kept)[:limit]:
        target = Path(root) / relative
        name = relative.as_posix()
        if target.is_dir() and not _is_link(target):
            count = files(target)
            parts.append(f"{name}/ ({count} file{'' if count == '1' else 's'})")
        else:
            parts.append(name + (" (link)" if _is_link(target) else ""))
    if len(kept) > limit:
        parts.append(f"and {len(kept) - limit} more")
    return ", ".join(parts)


def _ignored_reason(path):
    """None when every ignored file is disposable, else a reason naming the rest.

    Disposable: dependency folders, known tool caches, ``*.pyc`` and the
    ``build/`` or ``dist/`` folder directly beside a ``package.json``. Anything
    else ignored (``uploads/``, ``mockups/``, ``.env``) keeps the checkout.
    """
    ignored = wr._git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    if ignored is None:
        return "ignored files could not be checked"
    paths = [p for p in ignored.split("\0") if p]
    kept = []

    def visit(relative):
        if DEPENDENCY_DIRS.intersection(relative.parts):
            return
        target = Path(path) / relative
        # A cache that ignores its own contents (.pytest_cache writes a
        # ".gitignore" of "*") is listed file by file. Git never lists paths
        # through a link, so a cache ancestor here is a real directory.
        for depth, part in enumerate(relative.parts[:-1], 1):
            if part in CACHE_DIRS and not _is_link(Path(path).joinpath(*relative.parts[:depth])):
                return
        if _is_link(target):
            kept.append(relative)
            return
        if target.is_dir():
            if target.name in CACHE_DIRS or _js_build_output(target):
                return
            # --directory also collapses *unignored* parents containing only
            # ignored children. Descend those, but never an unknown ignored dir
            # (e.g. a root dist/), even if all its children happen to be caches.
            result = subprocess.run(["git", "-C", str(path), "check-ignore", "-q", "--", str(relative)],
                                    capture_output=True, timeout=15, env=scrub_child_env())
            if result.returncode != 1:
                kept.append(relative)
                return
            for child in target.iterdir():
                visit(child.relative_to(path))
            return
        if target.is_file() and (target.suffix in CACHE_FILE_SUFFIXES or target.name in CACHE_FILE_NAMES):
            return
        kept.append(relative)

    try:
        for item in paths:
            visit(Path(item.rstrip("/")))
        if not kept:
            return None
        return "holds ignored files: " + _describe_kept(path, kept)
    except (OSError, subprocess.SubprocessError):
        return "ignored files could not be checked"


def _plan(source, registered, context, *, pulls=None):

    path, repo = registered["path"], source["path"]
    row = {"path": path, "registry_ids": registered["registry_ids"],
           "branch": registered.get("branch"), "head_sha": None, "branch_sha": None, "proof": None,
           "decision": "keep", "reason": "safety unknown", "applied": False}

    def keep(reason):
        return {**row, "reason": reason}

    target = Path(os.path.abspath(path))
    base = Path(repo).resolve()
    roots = [base / name / "worktrees" for name in (".fluid", ".claude")]
    # resolve() alone would follow a junction root into someone else's checkout.
    if target == base or not any(root in target.parents for root in roots):
        return keep("not an app-managed branch checkout")
    for part in (target, *target.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            return keep("checkout path contains a link or junction")
    item = context["items"].get(wr._path_key(path))
    if item and item.get("locked"):
        return keep("worktree locked")
    missing = not target.exists()
    reviewer = None
    dirty = 0
    nested = []
    if not missing:
        if not item or not wr._is_checkout_root(path):
            return keep("not a linked worktree of this repository")
        actual = (wr._git(path, "symbolic-ref", "--short", "HEAD") or "").strip() or None
        if actual != registered.get("branch") or actual != item.get("branch"):
            # A reviewer moved a worker checkout onto a PR commit. Its own
            # branch is untouched and HEAD exists elsewhere: nothing local-only.
            reviewer = _stranded_reviewer(repo, path, registered.get("branch"), context, pulls)
            if not reviewer:
                reason = (f"checkout is on {actual or 'a detached HEAD'}, not its registered branch "
                          f"{registered.get('branch') or '(none)'}")
                if _worker_session_id(registered.get("branch")):
                    reason += "; its own branch has commits, or that HEAD is not pushed or integrated"
                return keep(reason)
        row["branch"] = registered.get("branch") if reviewer else actual
        status = wr._git(path, "status", "--porcelain", "--untracked-files=all")
        if status is None:
            return keep("git status unavailable")
        dirty = len([line for line in status.splitlines() if line.strip()])
        nested = _nested_repositories(path, status)
        # Only an abandoned worker checkout on its own branch may continue
        # dirty: it can be archived (snapshot first), never plainly removed.
        if dirty and (reviewer or not _worker_session_id(row["branch"])):
            return keep(f"uncommitted changes ({dirty} file{'s' if dirty != 1 else ''})")
        ignored_reason = _ignored_reason(path)
        if ignored_reason:
            return keep(ignored_reason)
    branch = row["branch"]
    if branch and branch.casefold() == context["default_branch"].casefold():
        return keep("default branch protected")
    sessions = _bound_sessions(context, registered, branch)
    if any(s.get("is_active") or s.get("running") for s in sessions):
        return keep("bound to an active or running session")
    grace_reason = _session_grace_reason(sessions)
    if grace_reason:
        return keep(grace_reason)
    # Look up PRs outside repo_worktree_lock; apply supplies this snapshot while
    # rechecking the matching HEAD, local Git state and liveness under the lock.
    prs = _pulls(context, branch) if pulls is None else pulls
    if len(prs) >= 100:
        return keep("PR history truncated; status unknown")
    # Head-branch PRs here; PRs GitHub ties to the exact commit come below.
    branch_prs = [pr for pr in prs if not getattr(pr, "contains_commit", None)]
    if any(pr.state.lower() == "open" for pr in branch_prs) or _open_pr_claimed(sessions, branch_prs):
        return keep("open PR head branch")
    row["pr_states"] = {pr.number: str(pr.state).lower() for pr in prs if getattr(pr, "number", None)}
    branch_head = _branch_head(repo, branch)
    if reviewer:
        # Re-proven under every plan (and again under the apply lock).
        if branch_head != reviewer["branch_sha"]:
            return keep("worker branch moved during the check")
        row.update(head_sha=reviewer["head"], branch_sha=branch_head)
        checkout_grace = _checkout_grace_reason(target, registered)
        if checkout_grace:
            return keep(checkout_grace)
        return {**row, "decision": "remove",
                "reason": f"reviewer left this worker checkout on another commit; its own branch has no commits "
                          f"and {reviewer['how']}"}
    head = ((wr._git(path, "rev-parse", "HEAD") or "").strip() if not missing
            else branch_head or (item or {}).get("head"))
    row["head_sha"] = head
    row["branch_sha"] = branch_head
    if pulls is None and head:
        # What GitHub knows about this exact commit (apply gets it in its snapshot).
        prs = [*prs, *_commit_pulls(context, head)]
    if any(pr.state.lower() == "open" and getattr(pr, "contains_commit", None) == head for pr in prs):
        return keep("an open PR contains this commit")
    # Missing directories with no remaining branch/head have only stale metadata.
    proof = None if dirty else (head and _proof(repo, context, head, prs))
    row["proof"] = proof or None
    if not (proof or (missing and not branch_head and not head)):
        reason = (f"uncommitted changes ({dirty} file{'s' if dirty != 1 else ''})" if dirty
                  else "unmerged or unpushed work; integration not proven")
        if missing or not _worker_session_id(branch):
            return keep(reason)
        # Abandoned worker scratch is archived (snapshot, then removed), never
        # dropped. Chat checkouts and named branches never take this path.
        blocker = _abandon_blocker(context, branch, sessions)
        if blocker:
            return keep(f"{reason}; not archived: {blocker}")
        if not branch_head or head != branch_head:
            return keep("branch moved away from checkout HEAD")
        if nested:
            # A snapshot would hold only a gitlink: the nested repository's own
            # commits and edits would be lost with the folder.
            return keep(f"{reason}; not archived: holds a nested Git repository: {', '.join(nested[:5])}")
        return {**row, "decision": "archive",
                "reason": f"{reason}; abandoned worker checkout (worker and parent idle "
                          f"{ARCHIVE_IDLE_SECONDS // 86400}+ days): snapshot kept at {ARCHIVE_REF_PREFIX}{branch}"}
    if branch_head and head != branch_head:
        return keep("branch moved away from checkout HEAD")
    reflog = wr._git(repo, "reflog", "show", "--format=%H", f"refs/heads/{branch}") if branch else None
    no_own_commits = bool(head == context["default_sha"] and reflog and reflog.splitlines()[-1].strip() == head)
    if no_own_commits and not missing:
        checkout_grace = _checkout_grace_reason(target, registered)
        if checkout_grace:
            return keep(checkout_grace)
    reason = "no commits of its own" if no_own_commits else "integration proven; safe to reclaim"
    return {**row, "decision": "forget" if missing else "remove", "reason": reason}


def _stamp_head(source, row):
    from app.db import checkouts

    if row["head_sha"]:
        stamped = set()
        for alias in source["aliases"]:
            updated = checkouts.set_head_sha_for_path(row["path"], row["head_sha"], project_id=alias,
                                                     runner_id=settings.effective_instance_id)
            if updated:
                stamped.add(updated["id"])
        expected = {i for i in row["registry_ids"] if i.startswith("ck-")}
        if not expected <= stamped:
            raise ValueError("checkout restoration HEAD could not be persisted")


def _archive(source, row):
    from app.db import checkouts, database
    from app.db.base import WorktreeStatus

    for alias in source["aliases"]:
        checkouts.archive_checkout_by_path(alias, row["path"], runner_id=settings.effective_instance_id)
    for registry_id in row["registry_ids"]:
        if database.get_worktree(registry_id):
            database.update_worktree(registry_id, status=WorktreeStatus.COMPLETED)


def _invalidate(source, path):
    from app.api.git_helpers import invalidate_working_dir_cache
    from app.api.git_worktrees import _invalidate_worktree_status_cache
    from app.core.git_cache import _git_cache
    from app.core.git_service import git_service

    invalidate_working_dir_cache()
    for alias in source["aliases"]:
        _invalidate_worktree_status_cache(alias)
    for prefix in ("status:", "branches:", "ahead_behind:", "worktrees:"):
        _git_cache.invalidate_prefix(prefix)
    git_service.invalidate_status_cache(source["path"])
    git_service.invalidate_status_cache(path)


def _prepare(source, registered, preview, automatic):
    # Network lookup is deliberately outside the repository lock, and BEFORE
    # releasing users. A stale plan must not evict an engine or stop its work.
    lookup_context = _context(source)
    pulls = _pulls(lookup_context, registered.get("branch"))
    # The plan also asked GitHub about the exact commit.
    if preview.get("decision") != "keep" and preview.get("head_sha"):
        pulls = [*pulls, *_commit_pulls(lookup_context, preview["head_sha"])]
    context = _context(source)  # the network round trip may have outlived a new dispatch/run
    snapshot = {"repository": lookup_context["github_repo"], "pulls": pulls}
    row = _plan(source, registered, context, pulls=pulls)
    if context["github_repo"] != snapshot["repository"]:
        row = {**row, "decision": "keep", "reason": "PR repository changed during lookup"}
    if _changed(row, preview):
        row = {**row, "decision": "keep", "reason": "safety changed: " + row["reason"]}
    elif automatic and not _old_activity(source, registered, row, time.time()):
        row = {**row, "decision": "keep", "reason": "session activity changed during automatic grace check"}
    return row, snapshot


def _changed(row, preview):
    """A fresh plan that no longer matches what was previewed (never escalate)."""
    return (row["decision"] == "keep" or row["decision"] != preview["decision"]
            or row["head_sha"] != preview["head_sha"]
            or row.get("branch_sha") != preview.get("branch_sha"))


def _delete_branch(repo, branch, sha):
    """Compare-and-delete ``refs/heads/<branch>``: refused unless it still points at ``sha``."""
    if not branch or not sha:
        return False
    deleted = git_service._run_git(repo, ["update-ref", "-d", f"refs/heads/{branch}", sha], timeout=10)
    if deleted.returncode != 0:
        return False
    # Its tracking configuration goes with it, as ``git branch -d`` does.
    git_service._run_git(repo, ["config", "--remove-section", f"branch.{branch}"], timeout=10)
    return True


def _nested_repositories(path, status):
    """Untracked folders (``?? name/`` in status) that are Git repositories."""
    found = []
    for line in (status or "").splitlines():
        if line.startswith("?? ") and line.rstrip().endswith("/"):
            name = line[3:].strip().strip('"')
            if (Path(path) / name / ".git").exists():
                found.append(name)
    return found


def _gitlinks(path, treeish):
    listing = wr._git(path, "ls-tree", "-r", "-z", treeish)
    if listing is None:
        raise ValueError("snapshot tree could not be listed")
    links = set()
    for entry in listing.split("\0"):
        meta, _, name = entry.partition("\t")
        if meta.startswith("160000 "):
            links.add(name)
    return links


def _worktree_tree(path, head):
    """The tree of ``head`` plus every tracked change and unignored untracked
    file in the checkout, built in a private index (the checkout's own index,
    HEAD and refs are untouched)."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="fluid-archive-") as scratch:
        env = {**scrub_child_env(), "GIT_INDEX_FILE": str(Path(scratch) / "index")}

        def run(*args):
            result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=120, env=env)
            if result.returncode != 0:
                raise ValueError(f"snapshot step failed: git {args[0]}")
            return result.stdout.strip()

        run("read-tree", head)
        run("add", "-A", "--", ".")
        tree = run("write-tree")
    # ``git add`` records a nested repository (or a submodule) as a gitlink
    # only; its commits and files would not be in the snapshot. Never archive
    # a checkout holding one.
    links = _gitlinks(path, tree)
    if links:
        raise ValueError("snapshot would hold only a gitlink for nested Git repository: "
                         + ", ".join(sorted(links)[:5]))
    return tree


def _snapshot(path, head, branch):
    """A commit holding the checkout's uncommitted work on top of ``head``
    (``head`` itself when there is none). No ref is written."""
    tree = _worktree_tree(path, head)
    if tree == (wr._git(path, "rev-parse", "--verify", head + "^{tree}") or "").strip():
        return head
    env = {**scrub_child_env(), **ARCHIVE_IDENTITY}
    result = subprocess.run(["git", "-C", str(path), "commit-tree", tree, "-p", head, "-m",
                             f"Archived uncommitted work of {branch}"],
                            capture_output=True, text=True, timeout=30, env=env)
    snapshot = result.stdout.strip()
    if result.returncode != 0 or not snapshot:
        raise ValueError("snapshot commit could not be written")
    return snapshot


def _archive_checkout(repo, row, before_remove=None):
    """Snapshot an abandoned worker checkout to refs/fluid-archive/<branch>,
    verify it, then remove the checkout (forced: the snapshot holds its work).

    ``before_remove(snapshot)`` runs after verification, before removal (the
    restore stamp). Returns the snapshot SHA. A failure before removal drops
    the archive ref and raises, leaving the checkout as it was. Once forced
    removal has started the ref is never dropped, whatever the outcome: files
    may already be gone and the snapshot is then their only copy.
    """
    path, branch, head = row["path"], row["branch"], row["head_sha"]
    ref = ARCHIVE_REF_PREFIX + branch
    snapshot = _snapshot(path, head, branch)
    created = git_service._run_git(repo, ["update-ref", ref, snapshot, ""], timeout=10)
    if created.returncode != 0:
        existing = (wr._git(repo, "rev-parse", "--verify", "--quiet", ref) or "").strip()
        if existing != snapshot:
            raise ValueError(f"{ref} already holds another snapshot; checkout kept")
    try:
        # Verify the stored snapshot is a commit on HEAD that holds exactly
        # what the checkout holds right now, immediately before removal.
        stored = (wr._git(repo, "rev-parse", "--verify", ref + "^{commit}") or "").strip()
        parent_ok = snapshot == head or (wr._git(repo, "rev-parse", "--verify", snapshot + "^1") or "").strip() == head
        current = (wr._git(path, "rev-parse", "--verify", "HEAD") or "").strip()
        tree = (wr._git(repo, "rev-parse", "--verify", snapshot + "^{tree}") or "").strip()
        if not (stored == snapshot and parent_ok and current == head and tree == _worktree_tree(path, head)):
            raise ValueError("snapshot verification failed; checkout kept")
        # Only a verified snapshot is recorded as the tip to restore.
        if before_remove:
            before_remove(snapshot)
    except BaseException:
        git_service._run_git(repo, ["update-ref", "-d", ref, snapshot], timeout=10)
        raise
    # From here on the archive ref stays, even if removal fails part-way.
    if not git_service.remove_worktree(repo, path, True):
        raise ValueError(f"archived checkout removal failed; its snapshot stays at {ref}")
    return snapshot


def _apply(source, registered, preview, *, automatic=False, pulls):
    repo = source["path"]
    with repo_worktree_lock(repo):
        # Re-verify after release. No network I/O is allowed under this lock.
        context = _context(source)
        if context["github_repo"] != pulls["repository"]:
            return {**preview, "decision": "keep", "reason": "PR repository changed after lookup"}
        row = _plan(source, registered, context, pulls=pulls["pulls"])
        if _changed(row, preview):
            return {**row, "decision": "keep", "reason": "safety changed: " + row["reason"]}
        if automatic and not _old_activity(source, registered, row, time.time()):
            return {**row, "decision": "keep", "reason": "session activity changed during automatic grace check"}
        try:
            users = _bound_sessions({"sessions": _project_sessions(source)}, registered, row["branch"])
            if any(s.get("is_active") or s.get("running") for s in users) or _session_grace_reason(users):
                return {**row, "decision": "keep", "reason": "session activity changed immediately before removal"}
            if row["decision"] == "archive":
                # The resume path recreates the branch at the snapshot.
                snapshot = _archive_checkout(repo, row, lambda sha: _stamp_head(source, {**row, "head_sha": sha}))
                row = {**row, "archive_ref": ARCHIVE_REF_PREFIX + row["branch"], "snapshot_sha": snapshot}
            else:
                # Persist restoration information BEFORE either Git resource goes.
                # Failure here aborts removal rather than leaving an unrestorable row.
                _stamp_head(source, row)
                users = _bound_sessions({"sessions": _project_sessions(source)}, registered, row["branch"])
                if any(s.get("is_active") or s.get("running") for s in users) or _session_grace_reason(users):
                    return {**row, "decision": "keep", "reason": "session activity changed immediately before removal"}
                if row["decision"] == "forget":
                    forget_stale_worktree_metadata(repo, row["path"])
                elif not git_service.remove_worktree(repo, row["path"], False):
                    raise ValueError("safe non-forced worktree removal refused")
            # remove_worktree severs runtime links BEFORE Git recurses. Without
            # --force, Git is the last-line guard for files written after status.
            expected = row.get("branch_sha") or row["head_sha"]
            branch_head = _branch_head(repo, row["branch"])
            if branch_head:
                if branch_head != expected:
                    raise ValueError("branch changed during removal; branch retained")
                if not _delete_branch(repo, row["branch"], expected):
                    raise ValueError("merged branch removal failed; retry metadata cleanup")
            _archive(source, row)
            return {**row, "applied": True}
        finally:
            _invalidate(source, row["path"])


def _old_activity(source, registered, row, now):
    sessions = _bound_sessions(_context(source), registered, row["branch"])
    for session in sessions:
        try:
            value = session.get("updated_at")
            stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if now - stamp.timestamp() < GRACE_SECONDS:
                return False
        except (ValueError, TypeError):
            return False
    return True


def _candidates(source):
    # Dedupe legacy/canonical records without ever including unregistered Git
    # worktrees. Report-only tools can still list external worktrees.
    registered = {}
    for item in source["registered"]:
        key = wr._path_key(item["path"])
        if key in registered:
            if registered[key].get("branch") != item.get("branch"):
                registered[key]["ambiguous"] = True
            registered[key]["registry_ids"] = sorted(set(registered[key]["registry_ids"] + item["registry_ids"]))
        else:
            registered[key] = {**item}
    return sorted(registered.values(), key=lambda i: i["path"])


def _ambiguous_row(candidate):
    return {"path": candidate["path"], "registry_ids": candidate["registry_ids"],
            "branch": candidate.get("branch"), "head_sha": None, "branch_sha": None, "proof": None,
            "decision": "keep", "reason": "ambiguous checkout registrations", "applied": False}


def _observe(observations, key, row, old_activity, now):
    """Automatic grace: a candidate must stay eligible, at the same HEAD and
    with quiet sessions, for GRACE_SECONDS before it is applied."""
    previous = observations.get(key, {})
    if row["decision"] == "keep":
        observations.pop(key, None)
        return row
    if previous.get("head") != row["head_sha"] or not previous.get("since"):
        previous = {"head": row["head_sha"], "since": now}
        observations[key] = previous
    if not old_activity:
        previous["since"] = now
    if now - previous["since"] < GRACE_SECONDS or not old_activity:
        return {**row, "decision": "keep", "reason": "automatic 24-hour integration/activity grace period"}
    return row


async def reclaim(project_id, *, dry_run=True, limit=MAX_ITEMS, offset=0, observations=None):
    """Shared manual/automatic entry point; bounded, fresh decisions per item.

    ``observations`` is supplied ONLY by the automatic pass. First-observed
    eligibility gives a conservative, persisted merge grace even for squash
    merges with old author dates and no recorded GitHub merge timestamp.
    """
    if not _pass_lock.acquire(blocking=False):
        raise ValueError("another reclaim pass is in progress")
    try:
        sources = await asyncio.to_thread(_local_sources)
        source = next((s for s in sources if project_id in s["aliases"]), None)
        if not source:
            raise LookupError("local project base checkout not found")
        candidates = _candidates(source)
        items = []
        started = time.monotonic()
        context = await asyncio.to_thread(_context, source)
        for candidate in candidates[offset:offset + min(max(limit, 1), 200)]:
            if time.monotonic() - started >= PASS_SECONDS:
                break
            row = _ambiguous_row(candidate)
            try:
                if not candidate.get("ambiguous"):
                    row = await asyncio.to_thread(_plan, source, candidate, context)
                if observations is not None:
                    now = time.time()
                    old_activity = (row["decision"] == "keep"
                                    or await asyncio.to_thread(_old_activity, source, candidate, row, now))
                    row = _observe(observations, wr._path_key(candidate["path"]), row, old_activity, now)
                if not dry_run and row["decision"] != "keep":
                    from app.api.git_operations import _release_checkout_sessions

                    row, pulls = await asyncio.to_thread(_prepare, source, candidate, row, observations is not None)
                    if row["decision"] != "keep":
                        if await _release_checkout_sessions(project_id, row["path"], None, idle_only=True):
                            row = await asyncio.to_thread(_apply, source, candidate, row,
                                                          automatic=observations is not None, pulls=pulls)
                        else:
                            row = {**row, "decision": "keep", "reason": "checkout users are busy or could not be released"}
            except Exception:
                logger.warning("Checkout reclaim item failed", exc_info=True)
                row = {**row, "decision": "keep", "reason": "safety check or reclaim failed; refresh preview before retry", "error": True}
            items.append(row)
        reclaimed = sum(i["applied"] for i in items)
        # Applied rows disappear from the next candidate list. Do not skip the
        # next page as registrations shrink under an automatic/bulk pass.
        next_offset = offset + len(items) - reclaimed
        remaining = len(candidates) - reclaimed
        return {"project_id": project_id, "dry_run": dry_run, "items": items,
                "total": len(candidates), "next_offset": next_offset if next_offset < remaining else None,
                "reclaimable": sum(i["decision"] != "keep" for i in items),
                "reclaimed": reclaimed}
    finally:
        _pass_lock.release()


# --- Local branches with no worktree -----------------------------------------
#
# ``gh pr checkout`` leftovers (pr702, pr702b) and branches of reclaimed
# checkouts pile up: nothing else removes a branch that has no worktree. Only
# branches the app or ``gh pr checkout`` name are candidates; a hand-made
# branch is never touched. A candidate goes only when it is checked out
# nowhere, is not the default branch, no active/running session or open PR
# uses it, and its tip is proven integrated or is a pushed branch tip that a
# merged or closed PR has at exactly that commit.

#: Worker (chore/task-*, legacy task/*), chat (chore/chat-*) and
#: ``gh pr checkout -b`` style names (pr702, pr702b, pr-702).
APP_BRANCH_PATTERN = re.compile(r"(?:chore/task-|task/|chore/chat-)[^/].*|pr\d+[a-z]?|pr-\d+")


def _app_branch(name):
    return bool(APP_BRANCH_PATTERN.fullmatch(name or ""))


def _local_branches(repo):
    listing = wr._git(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads")
    if listing is None:
        raise ValueError("branch listing unavailable")
    branches = []
    for line in listing.splitlines():
        sha, _, ref = line.strip().partition(" ")
        if ref.startswith("refs/heads/"):
            branches.append((ref.removeprefix("refs/heads/"), sha))
    return sorted(branches)


def _remote_refs_at(repo, sha):
    listing = wr._git(repo, "for-each-ref", "--format=%(refname)", "--points-at", sha, "refs/remotes")
    return [ref for ref in (listing or "").split() if not ref.endswith("/HEAD")]


def _branch_heads(repo, name, sha):
    """Head branch names a PR for this local branch can have: its own name,
    its upstream's name (``gh pr checkout`` names the local branch pr702b) and
    the remote branches at its tip."""
    heads = {name}
    merge = (wr._git(repo, "config", "--get", f"branch.{name}.merge") or "").strip()
    if merge.startswith("refs/heads/"):
        heads.add(merge.removeprefix("refs/heads/"))
    for ref in _remote_refs_at(repo, sha):
        parts = ref.split("/", 3)  # refs/remotes/<remote>/<branch>
        if len(parts) == 4:
            heads.add(parts[3])
    return sorted(heads)


def _branch_pulls(context, repo, name, sha):
    pulls = []
    for head in _branch_heads(repo, name, sha):
        if head.casefold() != context["default_branch"].casefold():
            pulls.extend(_pulls(context, head))
    return [*pulls, *_commit_pulls(context, sha)]


def _branch_sessions(context, name):
    bound = []
    for session in context["sessions"]:
        logical = {b for b in (session.get("branch"), session.get("worktree_branch")) if b}
        unknown_active = ((session.get("is_active") or session.get("running"))
                          and not logical and not session.get("worktree_path"))
        if name in logical or unknown_active:
            bound.append(session)
    return bound


def _plan_branch(source, context, name, sha, *, pulls=None):
    repo = source["path"]
    row = {"branch": name, "head_sha": sha, "decision": "keep", "reason": "safety unknown", "applied": False}

    def keep(reason):
        return {**row, "reason": reason}

    if not _app_branch(name):
        return keep("not a branch the app or gh pr checkout created")
    if any(item.get("branch") == name for item in context["items"].values()):
        return keep("checked out in a worktree")
    if any(item.get("branch") == name for item in source["registered"]):
        # A reviewer may have detached its checkout: the worktree sweep owns it.
        return keep("bound to a registered checkout")
    if name.casefold() == context["default_branch"].casefold():
        return keep("default branch protected")
    sessions = _branch_sessions(context, name)
    if any(s.get("is_active") or s.get("running") for s in sessions):
        return keep("bound to an active or running session")
    grace_reason = _session_grace_reason(sessions)
    if grace_reason:
        return keep(grace_reason)
    prs = _branch_pulls(context, repo, name, sha) if pulls is None else pulls
    if len(prs) >= 100:
        return keep("PR history truncated; status unknown")
    if _open_pr_claimed(sessions, prs):
        return keep("open PR head branch")
    if any(pr.state.lower() == "open" for pr in prs):
        return keep("open PR head branch or an open PR contains its tip")
    proof = _proof(repo, context, sha, prs)
    if proof:
        return {**row, "decision": "delete", "reason": f"integrated: {proof}"}
    remote = _remote_refs_at(repo, sha)
    names = {ref.split("/", 3)[3] for ref in remote if len(ref.split("/", 3)) == 4}
    # The PR itself must hold this exact tip: its recorded head, or GitHub
    # listing the commit in it. A possibly stale remote ref alone is not enough.
    finished = [pr for pr in prs if str(pr.state).lower() in ("merged", "closed")
                and (getattr(pr, "head_sha", None) == sha or getattr(pr, "contains_commit", None) == sha)]
    if remote and names and finished:
        pr = finished[0]
        return {**row, "decision": "delete",
                "reason": f"tip is pushed ({remote[0].removeprefix('refs/remotes/')}); "
                          f"PR #{getattr(pr, 'number', '?')} {str(pr.state).lower()}"}
    if remote:
        return keep("pushed, but no merged or closed PR for it; integration not proven")
    return keep("unmerged and not pushed; integration not proven")


def _apply_branch(source, name, preview, pulls):
    repo = source["path"]
    with repo_worktree_lock(repo):
        # Worktree creation takes this lock too: "checked out nowhere" holds.
        context = _context(source)
        if context["github_repo"] != pulls["repository"]:
            return {**preview, "decision": "keep", "reason": "PR repository changed after lookup"}
        current = _branch_head(repo, name)
        if current != preview["head_sha"]:
            return {**preview, "decision": "keep", "reason": "branch moved during the check"}
        row = _plan_branch(source, context, name, current, pulls=pulls["pulls"])
        if row["decision"] != "delete":
            return {**row, "decision": "keep", "reason": "safety changed: " + row["reason"]}
        if not _delete_branch(repo, name, current):
            raise ValueError("compare-and-delete refused; branch kept")
        _invalidate(source, repo)
        return {**row, "applied": True}


def _prepare_branch(source, name, preview):
    lookup_context = _context(source)
    prs = _branch_pulls(lookup_context, source["path"], name, preview["head_sha"])
    context = _context(source)
    snapshot = {"repository": lookup_context["github_repo"], "pulls": prs}
    row = _plan_branch(source, context, name, preview["head_sha"], pulls=prs)
    if context["github_repo"] != snapshot["repository"] or row["decision"] != "delete":
        row = {**row, "decision": "keep", "reason": "safety changed: " + row["reason"]}
    return row, snapshot


def _branch_key(source, name):
    return f"branch:{wr._path_key(source['path'])}:{name}"


async def reclaim_branches(project_id, *, dry_run=True, limit=MAX_ITEMS, offset=0, observations=None):
    """Delete local branches that have no worktree and hold nothing unique.

    Bounded like :func:`reclaim`; ``observations`` (automatic pass only) adds
    the same 24-hour grace. Every delete is a compare-and-delete.
    """
    if not _pass_lock.acquire(blocking=False):
        raise ValueError("another reclaim pass is in progress")
    try:
        sources = await asyncio.to_thread(_local_sources)
        source = next((s for s in sources if project_id in s["aliases"]), None)
        if not source:
            raise LookupError("local project base checkout not found")
        context = await asyncio.to_thread(_context, source)
        checked_out = {i.get("branch") for i in context["items"].values() if i.get("branch")}
        branches = [(name, sha) for name, sha in await asyncio.to_thread(_local_branches, source["path"])
                    if _app_branch(name) and name not in checked_out
                    and name.casefold() != context["default_branch"].casefold()]
        items = []
        started = time.monotonic()
        for name, sha in branches[offset:offset + min(max(limit, 1), 200)]:
            if time.monotonic() - started >= PASS_SECONDS:
                break
            row = {"branch": name, "head_sha": sha, "decision": "keep", "reason": "safety unknown", "applied": False}
            try:
                row = await asyncio.to_thread(_plan_branch, source, context, name, sha)
                if observations is not None:
                    row = _observe(observations, _branch_key(source, name), row, True, time.time())
                if not dry_run and row["decision"] == "delete":
                    row, pulls = await asyncio.to_thread(_prepare_branch, source, name, row)
                    if row["decision"] == "delete":
                        row = await asyncio.to_thread(_apply_branch, source, name, row, pulls)
            except Exception:
                logger.warning("Branch reclaim item failed", exc_info=True)
                row = {**row, "decision": "keep", "reason": "safety check or delete failed", "error": True}
            items.append(row)
        deleted = sum(i["applied"] for i in items)
        next_offset = offset + len(items) - deleted
        remaining = len(branches) - deleted
        return {"project_id": project_id, "dry_run": dry_run, "items": items, "total": len(branches),
                "next_offset": next_offset if next_offset < remaining else None,
                "deletable": sum(i["decision"] != "keep" for i in items), "deleted": deleted}
    finally:
        _pass_lock.release()


# --- Automatic pass -------------------------------------------------------------


def _state_path():
    return Path(settings.effective_data_dir) / "checkout-reclaim.json"


def _load_state():
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state must be an object")
        index, observed = state.get("project_index", 0), state.get("observations", {})
        offsets, branch_offsets = state.get("offsets", {}), state.get("branch_offsets", {})
        if type(index) is not int or index < 0 or not isinstance(observed, dict):
            raise ValueError("invalid rotation state")
        for table in (offsets, branch_offsets):
            if not isinstance(table, dict) or any(not isinstance(k, str) or type(v) is not int or v < 0
                                                  for k, v in table.items()):
                raise ValueError("invalid offsets")
        for key, value in observed.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ValueError("invalid observation")
            since = value.get("since")
            if type(since) not in (int, float) or not math.isfinite(since) or since < 0:
                raise ValueError("invalid observation time")
            head = value.get("head")
            if head is not None and (not isinstance(head, str) or len(head) not in (40, 64)
                                     or any(c not in "0123456789abcdef" for c in head)):
                raise ValueError("invalid observation HEAD")
        return state
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        logger.warning("Checkout reclaim: invalid/unreadable state; resetting conservative grace")
        return {}


def _save_state(state):
    from tempfile import NamedTemporaryFile

    state_path = _state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=state_path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(state, stream)
    try:
        os.replace(temporary, state_path)
    finally:
        temporary.unlink(missing_ok=True)


async def automatic_pass():
    """Every local project per pass, each bounded by MAX_ITEMS and PASS_SECONDS."""
    if os.environ.get("AI_SHUFFLE_PREVIEW") == "1":
        return
    if not settings.worktree_auto_reclaim_enabled:
        logger.info("Checkout reclaim: disabled")
        return
    state = await asyncio.to_thread(_load_state)
    sources = await asyncio.to_thread(_local_sources)
    if not sources:
        logger.info("Checkout reclaim: no local projects")
        return
    observations = state.setdefault("observations", {})
    offsets = state.setdefault("offsets", {})
    branch_offsets = state.setdefault("branch_offsets", {})
    totals = {"inspected": 0, "eligible": 0, "reclaimed": 0, "branches_deleted": 0}
    for source in sources:
        project_id = source["aliases"][0]
        try:
            result = await reclaim(project_id, dry_run=False, offset=offsets.get(project_id, 0),
                                   observations=observations)
            offsets[project_id] = result["next_offset"] or 0
            totals["inspected"] += len(result["items"])
            totals["eligible"] += result["reclaimable"]
            totals["reclaimed"] += result["reclaimed"]
            branches = await reclaim_branches(project_id, dry_run=False, offset=branch_offsets.get(project_id, 0),
                                              observations=observations)
            branch_offsets[project_id] = branches["next_offset"] or 0
            totals["branches_deleted"] += branches["deleted"]
        except Exception:
            # One project's failure (or a manual pass holding the lock) must
            # not starve the others.
            logger.warning("Checkout reclaim: project %s skipped this pass", project_id, exc_info=True)
    state.pop("project_index", None)
    # Bound persisted metadata too; absence merely restarts a conservative grace.
    now = time.time()
    state["observations"] = {k: v for k, v in observations.items() if now - v["since"] < 30 * GRACE_SECONDS}
    await asyncio.to_thread(_save_state, state)
    logger.info("Checkout reclaim: projects=%d inspected=%d eligible=%d reclaimed=%d branches_deleted=%d",
                len(sources), totals["inspected"], totals["eligible"], totals["reclaimed"],
                totals["branches_deleted"])


# --- Report preview -------------------------------------------------------------


def _when(observed, row, now):
    if not settings.worktree_auto_reclaim_enabled:
        return "would be, but automatic cleanup is turned off on this worker"
    if observed and observed.get("head") == row["head_sha"] and observed.get("since"):
        due = observed["since"] + GRACE_SECONDS
        if due <= now:
            return "on the next automatic pass (every 2 hours)"
        stamp = datetime.fromtimestamp(due, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return f"after the 24h grace, around {stamp}"
    return "after the 24h grace that starts at the next automatic pass"


def describe_cleanup(row, observed=None, now=None):
    """One line: what automatic cleanup does with this checkout, and when."""
    now = time.time() if now is None else now
    decision = row["decision"]
    if decision == "keep":
        return "kept: " + row["reason"]
    verb = {"remove": "removed", "forget": "forgotten (its folder is already gone)",
            "archive": f"archived to {ARCHIVE_REF_PREFIX}{row.get('branch')} and removed"}.get(decision, decision)
    detail = row.get("proof") or row["reason"]
    return f"{verb} automatically {_when(observed, row, now)} ({detail})"


def _report_facts(source, candidate, context, row):
    """For a row the planner kept before proving anything: its HEAD, the
    integration proof (GitHub's commit-to-PR answer included) and GitHub's
    state of any PR a bound session still claims is open. Report only."""
    repo, path, branch = source["path"], candidate["path"], row.get("branch")
    head = row.get("head_sha")
    if not head:
        head = ((wr._git(path, "rev-parse", "--verify", "HEAD") or "").strip() if wr._is_checkout_root(path)
                else _branch_head(repo, branch) if branch else None)
    if not head:
        return row
    row = {**row, "head_sha": head}
    sessions = _bound_sessions(context, candidate, branch)
    if branch and row.get("pr_states") is None and any(
            str(s.get("pr_state") or "").lower() == "open" for s in sessions):
        row["pr_states"] = {pr.number: str(pr.state).lower() for pr in _pulls(context, branch)}
    proof = _proof(repo, context, head)
    if not proof:
        proof = wr.integration_proof(repo, context["default_sha"], head, _commit_pulls(context, head), local=False,
                                     base_branch=context["default_branch"])
    row["proof"] = proof
    return row


NOT_CHECKED = "kept: not checked (time limit); run the report again"


def plan_preview(source, deadline=None):
    """Blocking and read-only: the sweeper's dry-run verdict per registered
    checkout of one local project, keyed by path key.

    ``source`` is an entry of :func:`_local_sources`. ``deadline`` is a
    ``time.monotonic()`` value; checkouts not started by then are reported as
    not checked. GitHub answers come from a 10-minute report-only cache.
    Raises ValueError when the project cannot be inspected (the report says
    so instead).
    """
    from concurrent.futures import ThreadPoolExecutor

    context = {**_context(source), "report_cache": True}
    observations = _load_state().get("observations", {})
    now = time.time()

    def one(candidate):
        key = wr._path_key(candidate["path"])
        if deadline is not None and time.monotonic() >= deadline:
            return key, {**_ambiguous_row(candidate), "reason": "not checked (time limit)",
                         "not_checked": True, "cleanup": NOT_CHECKED}
        try:
            row = _ambiguous_row(candidate) if candidate.get("ambiguous") else _plan(source, candidate, context)
        except Exception as exc:  # noqa: BLE001 — a report row, not a removal
            return key, {**_ambiguous_row(candidate), "cleanup": f"kept: safety check failed ({type(exc).__name__})"}
        cleanup = describe_cleanup(row, observations.get(key), now)
        if row["decision"] == "keep" and not row.get("proof") and (deadline is None or time.monotonic() < deadline):
            try:
                row = _report_facts(source, candidate, context, row)
            except Exception:  # noqa: BLE001 — facts are optional; the verdict stands
                logger.debug("WorktreeReport: extra facts unavailable for %s", candidate["path"], exc_info=True)
        return key, {**row, "cleanup": cleanup}

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="worktree-report") as pool:
        return dict(pool.map(one, _candidates(source)))


def archived_refs(repo):
    """Branch names snapshotted under refs/fluid-archive/ in this repository."""
    listing = wr._git(repo, "for-each-ref", "--format=%(refname)", ARCHIVE_REF_PREFIX.rstrip("/"))
    return [ref.removeprefix(ARCHIVE_REF_PREFIX) for ref in (listing or "").split()]


async def periodic_reclaim():
    if os.environ.get("AI_SHUFFLE_PREVIEW") == "1":
        return
    await asyncio.sleep(60)
    while True:
        try:
            await automatic_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Checkout reclaim: pass failed; retaining checkouts", exc_info=True)
        await asyncio.sleep(INTERVAL_SECONDS)
