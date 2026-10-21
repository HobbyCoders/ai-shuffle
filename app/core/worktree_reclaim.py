"""Owner-only checkout reclamation. All Git/filesystem work runs off the loop.

Only registered, local, app-managed placements are candidates. Unknown safety
facts retain work; a closed PR alone is NOT evidence that work was integrated.
The agent WorktreeReport remains strictly read-only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.core import worktree_report as wr
from app.core.config import settings
from app.core.git_service import git_service
from app.core.git_worktree_ops import forget_stale_worktree_metadata, repo_worktree_lock

logger = logging.getLogger(__name__)
GRACE_SECONDS = 24 * 3600
INTERVAL_SECONDS = 6 * 3600
MAX_ITEMS = 50
PASS_SECONDS = 120
_pass_lock = threading.Lock()


def _local_sources():
    # The report's union supplies canonical and legacy aliases. Reject foreign
    # legacy projects even when a shared DB happens to contain a matching path.
    from app.db import checkouts, database

    sources = []
    for source in wr._project_repos():
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
                for registry_id in item["registry_ids"]:
                    row = canonical.get(registry_id)
                    if row:
                        if row["runner_id"] == settings.effective_instance_id and row.get("status") != "archived":
                            ids.append(registry_id)
                    else:
                        legacy = database.get_worktree(registry_id)
                        if legacy and legacy.get("status") != "completed":
                            owner_repo = database.get_git_repository(legacy["repository_id"])
                            if owner_repo and owner_repo["project_id"] in aliases:
                                ids.append(registry_id)
                if ids:
                    registered.append({**item, "registry_ids": ids})
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
    busy = {s["id"] for s in active_git_sessions(list(rows.values()))}
    return [{**s, "running": s["id"] in busy} for s in rows.values()]


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


def _contained(repo, default_sha, head):
    # A zero ahead count proves ancestry; merge-tree also catches squash/rebase
    # integrations, without touching the index or working tree. Conflicts and
    # Git versions without merge-tree --write-tree fail closed.
    ahead = wr._git(repo, "rev-list", "--count", f"{default_sha}..{head}")
    if ahead is not None and ahead.strip() == "0":
        return True
    tree = wr._git(repo, "rev-parse", default_sha + "^{tree}")
    merged = wr._git(repo, "merge-tree", "--write-tree", default_sha, head)
    return bool(tree and merged and merged.strip() == tree.strip())


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


def _plan(source, registered, context):
    from app.core.github_service import github_service

    path, repo = registered["path"], source["path"]
    row = {"path": path, "registry_ids": registered["registry_ids"],
           "branch": registered.get("branch"), "head_sha": None,
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
    if not missing:
        if not item or not wr._is_checkout_root(path):
            return keep("not a linked worktree of this repository")
        actual = (wr._git(path, "symbolic-ref", "--short", "HEAD") or "").strip() or None
        if actual != registered.get("branch") or actual != item.get("branch"):
            return keep("checkout branch differs from registration or Git listing")
        row["branch"] = actual
        status = wr._git(path, "status", "--porcelain", "--untracked-files=all")
        if status is None or status.strip():
            return keep("uncommitted changes or status unavailable")
    branch = row["branch"]
    if branch and branch.casefold() == context["default_branch"].casefold():
        return keep("default branch protected")
    sessions = _bound_sessions(context, registered, branch)
    if any(s.get("is_active") or s.get("running") for s in sessions):
        return keep("bound to an active or running session")
    if any(str(s.get("pr_state") or "").lower() == "open" for s in sessions):
        return keep("open PR head branch")
    prs = []
    if branch and context["github_repo"]:
        # Per-head lookup avoids a truncated global PR list hiding an open PR.
        prs = github_service.list_pulls(context["github_repo"], state="all", head=branch, limit=100)
        if len(prs) >= 100:
            return keep("PR history truncated; status unknown")
        if any(pr.state.lower() == "open" for pr in prs):
            return keep("open PR head branch")
    branch_head = _branch_head(repo, branch)
    head = ((wr._git(path, "rev-parse", "HEAD") or "").strip() if not missing
            else branch_head or (item or {}).get("head"))
    row["head_sha"] = head
    # Missing directories with no remaining branch/head have only stale metadata.
    integrated = bool(head and _contained(repo, context["default_sha"], head))
    exact_pr = bool(head and any(pr.state.lower() == "merged" and pr.head_sha == head for pr in prs))
    if not (integrated or exact_pr or (missing and not branch_head and not head)):
        return keep("unmerged or unpushed work; integration not proven")
    if branch_head and head != branch_head:
        return keep("branch moved away from checkout HEAD")
    return {**row, "decision": "forget" if missing else "remove", "reason": "safe to reclaim"}


def _archive(source, row):
    from app.db import checkouts, database
    from app.db.base import WorktreeStatus

    for alias in source["aliases"]:
        if row["head_sha"]:
            checkouts.set_head_sha_for_path(row["path"], row["head_sha"], project_id=alias,
                                           runner_id=settings.effective_instance_id)
        checkouts.archive_checkout_by_path(alias, row["path"], runner_id=settings.effective_instance_id)
    for registry_id in row["registry_ids"]:
        if database.get_worktree(registry_id):
            database.update_worktree(registry_id, status=WorktreeStatus.COMPLETED)


def _invalidate(source, path):
    from app.api.git_helpers import invalidate_working_dir_cache
    from app.api.git_worktrees import _invalidate_worktree_status_cache
    from app.core.git_cache import _git_cache, _native_reader

    invalidate_working_dir_cache()
    for alias in source["aliases"]:
        _invalidate_worktree_status_cache(alias)
    for prefix in ("status:", "branches:", "ahead_behind:", "worktrees:"):
        _git_cache.invalidate_prefix(prefix)
    if _native_reader is not None:
        _native_reader.invalidate_repo(source["path"])
        _native_reader.invalidate_repo(path)


def _apply(source, registered, preview, *, automatic=False):
    repo = source["path"]
    with repo_worktree_lock(repo):
        # Re-read Git, PR and session truth immediately before the mutation. An
        # old dry-run response is never a deletion authorization.
        row = _plan(source, registered, _context(source))
        if row["decision"] == "keep" or row["head_sha"] != preview["head_sha"]:
            return {**row, "decision": "keep", "reason": "safety changed: " + row["reason"]}
        if automatic and not _old_activity(source, registered, row, time.time()):
            return {**row, "decision": "keep", "reason": "session activity changed during automatic grace check"}
        try:
            if row["decision"] == "forget":
                forget_stale_worktree_metadata(repo, row["path"])
            elif not git_service.remove_worktree(repo, row["path"], True):
                raise ValueError("safe worktree removal failed")
            # remove_worktree performs sever_runtime_links BEFORE Git recurses.
            # Never use rmtree here, including as an error fallback.
            branch_head = _branch_head(repo, row["branch"])
            if branch_head:
                if branch_head != row["head_sha"]:
                    raise ValueError("branch changed during removal; branch retained")
                if not git_service.delete_branch(repo, row["branch"], True):
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
        candidates = sorted(registered.values(), key=lambda i: i["path"])
        items = []
        started = time.monotonic()
        context = await asyncio.to_thread(_context, source)
        for candidate in candidates[offset:offset + min(max(limit, 1), 200)]:
            if time.monotonic() - started >= PASS_SECONDS:
                break
            row = {"path": candidate["path"], "registry_ids": candidate["registry_ids"],
                   "branch": candidate.get("branch"), "head_sha": None,
                   "decision": "keep", "reason": "ambiguous checkout registrations", "applied": False}
            try:
                if not candidate.get("ambiguous"):
                    row = await asyncio.to_thread(_plan, source, candidate, context)
                if observations is not None:
                    key = wr._path_key(candidate["path"])
                    previous = observations.get(key, {})
                    now = time.time()
                    if row["decision"] == "keep":
                        observations.pop(key, None)
                    else:
                        if previous.get("head") != row["head_sha"] or not previous.get("since"):
                            previous = {"head": row["head_sha"], "since": now}
                            observations[key] = previous
                        old_activity = await asyncio.to_thread(_old_activity, source, candidate, row, now)
                        if not old_activity:
                            previous["since"] = now
                        if now - previous["since"] < GRACE_SECONDS or not old_activity:
                            row = {**row, "decision": "keep", "reason": "automatic 24-hour integration/activity grace period"}
                if not dry_run and row["decision"] != "keep":
                    from app.api.git_operations import _release_checkout_sessions

                    if await _release_checkout_sessions(project_id, row["path"], None):
                        row = await asyncio.to_thread(_apply, source, candidate, row, automatic=observations is not None)
                    else:
                        row = {**row, "decision": "keep", "reason": "checkout users could not be released"}
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


async def automatic_pass():
    """One project / at most 50 candidates per pass; rotate for eventual coverage."""
    if not settings.worktree_auto_reclaim_enabled:
        logger.info("Checkout reclaim: disabled")
        return
    state_path = Path(settings.effective_data_dir) / "checkout-reclaim.json"

    def load():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    state = await asyncio.to_thread(load)
    sources = await asyncio.to_thread(_local_sources)
    if not sources:
        logger.info("Checkout reclaim: no local projects")
        return
    index = int(state.get("project_index", 0)) % len(sources)
    project_id = sources[index]["aliases"][0]
    observations = state.setdefault("observations", {})
    result = await reclaim(project_id, dry_run=False, offset=int(state.get("offsets", {}).get(project_id, 0)),
                           observations=observations)
    state.setdefault("offsets", {})[project_id] = result["next_offset"] or 0
    state["project_index"] = (index + 1) % len(sources)
    # Bound persisted metadata too; absence merely restarts a conservative grace.
    now = time.time()
    state["observations"] = {k: v for k, v in observations.items() if now - v["since"] < 30 * GRACE_SECONDS}

    def save():
        from tempfile import NamedTemporaryFile

        state_path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=state_path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream)
        try:
            os.replace(temporary, state_path)
        finally:
            temporary.unlink(missing_ok=True)

    await asyncio.to_thread(save)
    logger.info("Checkout reclaim: inspected=%d eligible=%d reclaimed=%d", len(result["items"]),
                result["reclaimable"], result["reclaimed"])


async def periodic_reclaim():
    await asyncio.sleep(60)
    while True:
        try:
            await automatic_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Checkout reclaim: pass failed; retaining checkouts", exc_info=True)
        await asyncio.sleep(INTERVAL_SECONDS)
