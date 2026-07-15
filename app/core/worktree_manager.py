"""
Worktree Manager

Manages worktree lifecycle independently from chat sessions.

Worktrees are persistent resources that can be used by multiple sessions
over time. The relationship is tracked via sessions.worktree_id.

Key design principles:
- Worktrees are independent of sessions (can exist without a session)
- Multiple sessions can use the same worktree (sequentially, not concurrently)
- Worktree validity is determined by checking if the folder exists on disk

Path pattern:
  All projects: {project_dir}/.claude/worktrees/{branch_sanitized}-{short_hash}
  (Worktrees live inside the project's own .claude folder, git-ignored via
  .git/info/exclude. The absolute path is stored in the DB.)
"""

import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

from app.core.config import settings
from app.core.git_cache import _git_cache
from app.core.git_service import git_service
from app.core.project_utils import (
    get_project_working_dir, get_worktree_working_dir,
    get_worktree_working_dir_as_path
)
from app.db import checkouts as checkouts_db
from app.db import database
from app.db.base import WorktreeStatus

logger = logging.getLogger(__name__)


class WorktreeError(Exception):
    """Exception raised for worktree creation/management errors with user-friendly messages."""
    pass


def _external_worktree_branch_name(git_wt: Dict[str, Any]) -> str:
    """
    Branch name to store when importing an external worktree (issue #568).

    A detached-HEAD worktree has no branch — git's porcelain output carries only
    the HEAD SHA, and list_worktrees fills ``branch`` with None (not a missing
    key), so a plain ``.get("branch", "unknown")`` still yields None and the
    insert violates the ``branch_name NOT NULL`` schema constraint. Derive a
    sentinel from the short SHA instead so the worktree imports cleanly.
    """
    branch = git_wt.get("branch")
    if branch:
        return branch
    head = (git_wt.get("head") or "").strip()
    return f"detached-{head[:12]}" if head else "unknown"


# ---------------------------------------------------------------------------
# Cross-device rebind fetch de-duplication (PR #493 follow-up #1)
#
# A rebind that must recover a branch surviving only on origin needs fresh
# remote-tracking refs, which means a ``git fetch`` — a slow, network-bound
# operation (historically up to ~60s). ``resolve_rebound_branch_plan`` (called by
# ``recreate_worktree``) fetches once and caches the result here for a short
# window, so a burst of rebind attempts against the same clone reuses those refs
# instead of blocking on another fetch. Keyed by the resolved local-clone
# directory.
#
# Backed by the shared ``git_cache`` TTL store (the same per-key cache
# git_service / git_branch_ops / git_diff_ops use, instrumented by
# resource_monitor) under ``fetch:{main_dir}`` keys, rather than a private dict —
# so all git-side TTL state lives in one place.
# ---------------------------------------------------------------------------
_FETCH_TTL_SECONDS = 60.0
_FETCH_CACHE_PREFIX = "fetch:"


def _fetch_cache_key(main_dir: str) -> str:
    return f"{_FETCH_CACHE_PREFIX}{main_dir}"


def note_repo_fetched(main_dir: str) -> None:
    """Record that origin refs for ``main_dir`` were just fetched."""
    _git_cache.put(_fetch_cache_key(main_dir), True)


def _repo_recently_fetched(main_dir: str) -> bool:
    return _git_cache.get(_fetch_cache_key(main_dir), _FETCH_TTL_SECONDS) is not None


def reset_fetch_cache() -> None:
    """Drop all cached fetch timestamps (test hook).

    Only the ``fetch:`` keys are invalidated so we never disturb the other git
    result categories sharing ``_git_cache``.
    """
    _git_cache.invalidate_prefix(_FETCH_CACHE_PREFIX)


def fetch_repo_cached(git_service_obj, main_dir: str, *, force: bool = False) -> bool:
    """Fetch origin for ``main_dir`` unless a fetch was cached within the TTL.

    ``force=True`` always fetches — used at adopt time, where freshness matters
    for the "unpushed branch" advisory and the added latency is off the send hot
    path. The send path passes ``force=False`` so a fetch primed at adopt time is
    reused rather than repeated. A successful fetch (either way) refreshes the
    cache timestamp. Returns ``True`` when a fetch was actually performed.
    """
    if not force and _repo_recently_fetched(main_dir):
        return False
    git_service_obj.fetch(main_dir)
    note_repo_fetched(main_dir)
    return True


def resolve_rebound_branch_plan(
    git_service_obj, main_dir: str, branch_name: str
) -> Tuple[bool, Optional[str]]:
    """Decide how to materialize ``branch_name`` on a sibling clone after a
    cross-device rebind.

    A rebind lands the session on THIS install's clone of the same repo, whose
    local refs differ from the device that created the branch. We must NEVER
    fabricate a fresh branch off the default here — that would silently present
    an empty branch as the user's unpushed work. So the decision is:

    * branch exists locally → reuse it as-is → ``(create_new=False, base=None)``
    * else present on origin → create from the pushed tip →
      ``(create_new=True, base=branch_name)`` — ``add_worktree(new_branch=True,
      base_branch=branch_name)`` prefers ``origin/{base}`` internally, landing
      the branch on the pushed tip.
    * else → raise ``WorktreeError`` (loud, actionable).

    Returns ``(create_new_branch, base_branch)`` for the caller's
    ``create_worktree`` / ``add_worktree`` call. Shared by ``recreate_worktree``
    (worktree-row rebind) and query_options' no-row rebind path so the two stay
    in lockstep.
    """
    branches = git_service_obj.list_branches(main_dir, include_remote=False)
    local_branch_names = [b["name"] for b in branches if not b["is_remote"]]
    if branch_name in local_branch_names:
        return False, None

    # Reuse a fetch primed at adopt time when it's still fresh, so this decision
    # doesn't block the send path on another network fetch (PR #493 follow-up #1).
    fetched = fetch_repo_cached(git_service_obj, main_dir)
    remote_names = [
        b["name"]
        for b in git_service_obj.list_branches(main_dir, include_remote=True)
    ]
    if f"origin/{branch_name}" in remote_names:
        return True, branch_name

    # The branch is absent on the refs we just consulted. If those refs came from
    # the CACHE (no fetch actually ran), they may be stale: the adopt flow primes
    # the cache and warns the user to "push it from the other device", so the very
    # next send — within the 60s TTL — would otherwise see stale refs and raise in
    # the exact remediation flow we recommended. Force one fresh fetch and re-list
    # before concluding the branch is unrecoverable (PR #493 follow-up #1).
    if not fetched:
        fetch_repo_cached(git_service_obj, main_dir, force=True)
        remote_names = [
            b["name"]
            for b in git_service_obj.list_branches(main_dir, include_remote=True)
        ]
        if f"origin/{branch_name}" in remote_names:
            return True, branch_name

    raise WorktreeError(
        f"Branch '{branch_name}' doesn't exist in the local clone or on "
        f"origin — it may only exist on the device that created this "
        f"session. Push it from there (git push -u origin {branch_name}) "
        f"or start a new session."
    )


class WorktreeManager:
    """
    Manages worktree lifecycle independently from chat sessions.

    Worktrees are persistent resources that can be used by multiple sessions
    over time. Sessions track their worktree via the sessions.worktree_id field.

    Key operations:
    - create_worktree(): Creates a standalone worktree (no session)
    - create_worktree_session(): Creates worktree + session together (legacy support)
    - get_available_worktrees(): Gets worktrees available for new sessions
    - is_worktree_valid(): Checks if worktree folder exists on disk

    Path pattern: {project_dir}/.claude/worktrees/{branch_sanitized}-{short_hash}
    (Worktrees are nested inside the project's .claude folder, git-ignored)
    """

    def __init__(self):
        self.git_service = git_service

    def _sanitize_branch_name(self, branch: str) -> str:
        """Sanitize branch name for use in filesystem path"""
        # Replace common problematic characters
        return branch.replace("/", "-").replace("\\", "-").replace(":", "-").replace("*", "-")

    def _get_worktree_path(self, project_path: str, branch: str) -> Path:
        """
        Get full path for a worktree - nested inside the project's own
        ``.claude/worktrees/`` directory, mirroring where Claude Code keeps
        its agent worktrees.

        Uses a short hash of the project path to ensure uniqueness, plus a
        kebab-case sanitized branch name (the same naming convention used
        before). The ``.claude/worktrees/`` directory is created if needed and
        the nested location is added to the repo's ``.git/info/exclude`` so it
        never pollutes the user's git status.

        Example:
            {project_dir}/.claude/worktrees/feature-auth-a7b3
        """
        sanitized = self._sanitize_branch_name(branch)

        # Generate a short hash from the project path for uniqueness
        path_hash = hashlib.md5(project_path.encode()).hexdigest()[:4]
        base_name = f"{sanitized}-{path_hash}"

        # Place worktrees inside the project's own .claude/worktrees/ folder.
        main_dir = get_project_working_dir(project_path)
        worktrees_dir = Path(main_dir) / ".claude" / "worktrees"
        worktrees_dir.mkdir(parents=True, exist_ok=True)

        # Ensure the nested worktree dir is ignored by the main repo so the
        # git worktrees we create here don't show up as untracked files.
        self._ensure_worktrees_ignored(main_dir)

        base_path = worktrees_dir / base_name

        # If base path doesn't exist, use it
        if not base_path.exists():
            return base_path

        # Otherwise, find a unique suffix
        counter = 2
        while True:
            candidate = worktrees_dir / f"{base_name}-{counter}"
            if not candidate.exists():
                return candidate
            counter += 1
            if counter > 100:
                raise WorktreeError(f"Too many worktrees for branch '{branch}'")

    def _ensure_worktrees_ignored(self, main_dir: str) -> None:
        """
        Make sure ``.claude/worktrees/`` is git-ignored within the project's
        repository via ``.git/info/exclude``.

        We deliberately use ``.git/info/exclude`` (a local, untracked ignore
        file) rather than the tracked ``.gitignore`` so we never modify files
        the user has committed. Idempotent: the entry is only appended once.
        """
        ignore_entry = ".claude/worktrees/"
        try:
            git_dir = Path(main_dir) / ".git"
            if git_dir.is_dir():
                # Normal repo: .git is a directory → write info/exclude there.
                info_dir = git_dir / "info"
            elif git_dir.is_file():
                # The project path is itself a git worktree (its .git is a file
                # pointing at the real git dir). The shared exclude file lives in
                # the *common* git dir, which we resolve via rev-parse. Writing to
                # the worktree's own git dir would not cover this checkout's
                # untracked-file rules the same way.
                result = self.git_service._run_git(
                    main_dir, ["rev-parse", "--git-common-dir"], timeout=5
                )
                if result.returncode != 0 or not result.stdout.strip():
                    logger.warning(
                        f"Could not resolve --git-common-dir for {main_dir}: "
                        f"{result.stderr.strip()}"
                    )
                    return
                common_dir = result.stdout.strip()
                common_path = Path(common_dir)
                if not common_path.is_absolute():
                    # rev-parse may return a path relative to main_dir.
                    common_path = (Path(main_dir) / common_path).resolve()
                info_dir = common_path / "info"
            else:
                # No .git at all — nothing we can do.
                return

            info_dir.mkdir(parents=True, exist_ok=True)
            exclude_file = info_dir / "exclude"

            existing = ""
            if exclude_file.exists():
                existing = exclude_file.read_text(encoding="utf-8", errors="ignore")
            # Already present (match the bare entry on its own line)
            if any(line.strip() == ignore_entry for line in existing.splitlines()):
                return

            prefix = "" if (not existing or existing.endswith("\n")) else "\n"
            with exclude_file.open("a", encoding="utf-8") as f:
                f.write(f"{prefix}{ignore_entry}\n")
            logger.debug(f"Added '{ignore_entry}' to {exclude_file}")
        except Exception as e:
            # Non-fatal: worst case the worktree dir shows up as untracked.
            logger.warning(f"Failed to update git info/exclude for {main_dir}: {e}")

    def _get_relative_worktree_path_from_full(self, full_path: Path, project_path: str = "") -> str:
        """
        Get the worktree path to store in the database.

        New worktrees live under the project's ``.claude/worktrees/`` folder,
        which is not relative to ``workspace_dir``, so we store the full
        absolute path. ``get_worktree_working_dir()`` resolves absolute paths
        directly while remaining backward compatible with older rows that
        stored just a folder name relative to ``workspace_dir``.
        """
        return str(full_path)

    def _ensure_repository_record(self, project_id: str, project_path: str) -> Optional[Dict]:
        """
        Ensure a git_repositories record exists for this project.
        Creates one if needed.

        Returns:
            Repository record dict or None if not a git repo
        """
        working_dir = get_project_working_dir(project_path)

        if not self.git_service.is_git_repo(working_dir):
            logger.warning(f"Project {project_id} is not a git repository")
            return None

        repo = database.get_git_repository_by_project(project_id)
        if repo:
            return repo

        # Create a new record
        repo_id = f"repo-{uuid.uuid4().hex[:12]}"
        remote_url = self.git_service.get_remote_url(working_dir)
        default_branch = self.git_service.get_default_branch(working_dir)

        # Try to extract GitHub repo name from URL
        github_repo_name = None
        if remote_url and "github.com" in remote_url:
            try:
                if remote_url.startswith("git@"):
                    github_repo_name = remote_url.split(":")[1].replace(".git", "")
                else:
                    parts = remote_url.replace(".git", "").split("github.com/")
                    if len(parts) > 1:
                        github_repo_name = parts[1]
            except Exception:
                pass

        repo = database.create_git_repository(
            repository_id=repo_id,
            project_id=project_id,
            remote_url=remote_url,
            default_branch=default_branch,
            github_repo_name=github_repo_name
        )
        logger.debug(f"Created git repository record for project {project_id}: {repo_id}")
        return repo

    def sync_worktrees_for_project(self, project_id: str) -> Dict[str, Any]:
        """
        Sync database worktree records with actual state on disk.

        This should be called when a chat card loads for a git project to ensure
        the database reflects reality (worktrees may have been deleted outside the app).

        Args:
            project_id: The project to sync worktrees for

        Returns:
            Dict with sync results: cleaned_up (list of deleted worktree ids), active (count)
        """
        project = database.get_project(project_id)
        if not project:
            logger.warning(f"Project not found for sync: {project_id}")
            return {"cleaned_up": [], "active": 0, "error": "Project not found"}

        repo = database.get_git_repository_by_project(project_id)
        if not repo:
            # Not a git repo, nothing to sync
            return {"cleaned_up": [], "active": 0, "is_git_repo": False}

        # Get all active worktrees from database
        active_worktrees = database.get_active_worktrees_for_repository(repo["id"])

        cleaned_up = []
        still_active = 0

        main_dir = get_project_working_dir(project)

        # Collect branches that need cleanup (path doesn't exist on disk)
        branches_to_cleanup = set()
        for wt in active_worktrees:
            worktree_full_path = get_worktree_working_dir_as_path(wt)

            if not worktree_full_path.exists():
                # Stale record - worktree was deleted outside the app
                branch_name = wt["branch_name"]
                logger.debug(f"Cleaning up stale worktree record: {branch_name} (path: {worktree_full_path})")
                database.update_worktree(wt["id"], status=WorktreeStatus.DELETED)

                if branch_name:
                    branches_to_cleanup.add(branch_name)

                cleaned_up.append({
                    "id": wt["id"],
                    "branch_name": branch_name,
                    "reason": "path_not_found",
                    "branch_deleted": False
                })
            else:
                still_active += 1

        # Prune stale git worktree entries BEFORE attempting branch deletion.
        # Git refuses to delete branches that are still referenced by worktree
        # tracking entries, even when the worktree directory no longer exists.
        if branches_to_cleanup:
            self.git_service.prune_worktrees(main_dir)

            default_branch = repo.get("default_branch", "main")
            # Deduplicate: only attempt each branch deletion once
            for branch_name in branches_to_cleanup:
                if branch_name == default_branch:
                    continue
                try:
                    branches = self.git_service.list_branches(main_dir, include_remote=False)
                    local_branch_names = [b["name"] for b in branches if not b["is_remote"]]
                    if branch_name in local_branch_names:
                        success = self.git_service.delete_branch(main_dir, branch_name, force=True)
                        if success:
                            logger.debug(f"Deleted orphaned branch: {branch_name}")
                            # Mark branch_deleted on all records for this branch
                            for entry in cleaned_up:
                                if entry["branch_name"] == branch_name:
                                    entry["branch_deleted"] = True
                        else:
                            logger.warning(f"Failed to delete orphaned branch: {branch_name}")
                except Exception as e:
                    logger.warning(f"Error cleaning up orphaned branch {branch_name}: {e}")

        if cleaned_up:
            logger.debug(f"Cleaned up {len(cleaned_up)} stale worktree records for project {project_id}")

        # Import external worktrees (created outside the app, e.g. via git CLI)
        imported = 0
        try:
            git_worktrees = self.git_service.list_worktrees(main_dir)
            # Normalize paths for comparison (git uses forward slashes, Python Path
            # uses backslashes on Windows — they must match for deduplication)
            db_paths = {
                os.path.normpath(get_worktree_working_dir(wt))
                for wt in active_worktrees
                if wt["id"] not in {c["id"] for c in cleaned_up}
            }
            # V2 placement checkouts are tracked in the ``checkouts`` table and
            # must never be imported as legacy ``worktrees`` rows. Build the set
            # of active checkout paths + this runner's checkout root once.
            v2_paths = {
                os.path.normpath(p) for p in checkouts_db.list_all_active_checkout_paths()
            }
            v2_checkout_root = os.path.normpath(
                str(Path(settings.effective_workspace_dir) / "checkouts")
            )
            for git_wt in git_worktrees:
                if git_wt.get("is_main"):
                    continue
                git_path = git_wt.get("path", "")
                if not git_path:
                    continue
                # Only import worktrees that actually exist on disk
                if not Path(git_path).exists():
                    continue
                normalized_git_path = os.path.normpath(git_path)
                # Skip V2 placement checkouts: they are owned by the ``checkouts``
                # table and must never become legacy ``worktrees`` rows. The
                # root-prefix check also covers the mid-materialization race where
                # the dir exists before its checkouts row commits.
                if (
                    normalized_git_path in v2_paths
                    or normalized_git_path == v2_checkout_root
                    or normalized_git_path.startswith(v2_checkout_root + os.sep)
                ):
                    continue
                if normalized_git_path not in db_paths:
                    try:
                        # Store the absolute path so it resolves consistently
                        # regardless of where the worktree lives on disk (e.g.
                        # nested under .claude/worktrees/, not workspace_dir).
                        stored_path = os.path.abspath(git_path)
                        branch_name = _external_worktree_branch_name(git_wt)
                        worktree_id = f"wt-{uuid.uuid4().hex[:12]}"
                        database.create_worktree(
                            worktree_id=worktree_id,
                            repository_id=repo["id"],
                            branch_name=branch_name,
                            worktree_path=stored_path,
                            session_id=None,
                            base_branch=None,
                            status=WorktreeStatus.ACTIVE
                        )
                        logger.debug(f"Imported external worktree: {stored_path} (branch: {branch_name})")
                        imported += 1
                    except Exception as e:
                        logger.warning(f"Failed to import external worktree {git_path}: {e}")
        except Exception as e:
            logger.warning(f"Failed to scan for external worktrees: {e}")

        return {
            "cleaned_up": cleaned_up,
            "active": still_active + imported,
            "imported": imported,
            "is_git_repo": True
        }

    def is_worktree_valid(self, worktree: Dict) -> bool:
        """
        Check if a worktree is valid on disk.

        Availability is exactly two rules:
        1. The folder exists, AND
        2. It contains a git repo (``.git`` present).

        ``.git`` may be a FILE (standard linked worktree pointing at the main
        repo) OR a DIRECTORY (a regular checkout) — both count as valid.
        """
        if not worktree:
            return False
        worktree_path = get_worktree_working_dir_as_path(worktree)
        if not worktree_path.exists():
            return False
        # A valid git repo: .git present as a file (standard worktree) OR a directory.
        return (worktree_path / ".git").exists()

    def get_available_worktrees(self, project_id: str) -> List[Dict]:
        """
        Get all valid worktrees for a project that can be used for new sessions.

        Returns worktrees with their current state:
        - exists: Whether the folder exists on disk
        - sessions: List of sessions that have used this worktree
        - active_session: Currently active session (if any)

        Args:
            project_id: The project ID

        Returns:
            List of worktree dicts with enriched info
        """
        repo = database.get_git_repository_by_project(project_id)
        if not repo:
            return []

        worktrees = database.get_active_worktrees_for_repository(repo["id"])
        result = []

        for wt in worktrees:
            # Check if worktree is valid (not just if folder exists)
            try:
                wt["exists"] = self.is_worktree_valid(wt)
            except (ValueError, TypeError, KeyError) as e:
                logger.warning(f"Worktree {wt.get('id', '?')} has invalid path: {e}")
                wt["exists"] = False

            if not wt["exists"]:
                # Invalid worktree (missing, empty, or corrupted) - skip it
                continue

            # Get all sessions for this worktree
            sessions = database.get_sessions_for_worktree(wt["id"])
            wt["sessions"] = sessions
            wt["session_count"] = len(sessions)

            # Find active session (if any)
            active_sessions = [s for s in sessions if s.get("status") == WorktreeStatus.ACTIVE]
            wt["active_session"] = active_sessions[0] if active_sessions else None

            result.append(wt)

        return result

    def _attach_new_session_to_worktree(
        self,
        worktree: Dict,
        project_id: str,
        project: Dict,
        profile_id: Optional[str],
        branch_name: str,
        base_branch: Optional[str],
    ) -> Tuple[Dict, Dict]:
        """
        Create a new chat session attached to an already-existing worktree.

        Used by the idempotent retry path in create_worktree_session when a
        previous request already created the git worktree + DB row but the
        frontend lost the response. Instead of erroring with
        "branch already exists", we simply create a fresh session on top of
        the existing worktree — the user observes the same end state.
        """
        # Resolve profile id the same way create_worktree_session does
        if not profile_id:
            project_settings = project.get("settings", {})
            profile_id = project_settings.get("default_profile_id")
            if not profile_id:
                profiles = database.get_all_profiles()
                if profiles:
                    profile_id = profiles[0]["id"]
                else:
                    raise WorktreeError("No profiles available. Please create a profile first.")

        session_id = f"ses-{uuid.uuid4().hex[:12]}"
        try:
            session = database.create_session(
                session_id=session_id,
                profile_id=profile_id,
                project_id=project_id,
                title=None,
                worktree_id=worktree["id"],
                worktree_branch=branch_name,
                worktree_path=worktree["worktree_path"],
            )
        except Exception as e:
            logger.error(f"Failed to create session on reused worktree {worktree['id']}: {e}")
            raise WorktreeError(f"Failed to create chat session: {str(e)}")

        logger.debug(
            f"Attached new session {session_id} to existing worktree {worktree['id']} "
            f"for branch {branch_name}"
        )
        return worktree, session

    def get_worktree_by_session(self, session_id: str) -> Optional[Dict]:
        """Get worktree associated with a session"""
        return database.get_worktree_by_session(session_id)

    def get_worktrees_for_project(self, project_id: str) -> List[Dict]:
        """Get all worktrees for a project with session info.

        Uses the new sessions.worktree_id relationship model.
        Uses a single query to avoid N+1 pattern.
        Returns worktrees enriched with:
        - exists: whether worktree folder exists on disk
        - sessions: list of all sessions for this worktree
        - session_count: total number of sessions
        - active_session: currently active session (if any)
        """
        repo = database.get_git_repository_by_project(project_id)
        if not repo:
            return []

        # Use optimized single-query function to avoid N+1 pattern
        worktrees = database.get_worktrees_with_sessions(repo["id"])

        for wt in worktrees:
            # Check if worktree is valid (not just if folder exists).
            # Catch corrupt records (NULL worktree_path) so one bad row
            # doesn't crash the entire list endpoint with a 500.
            try:
                wt["exists"] = self.is_worktree_valid(wt)
            except (ValueError, TypeError, KeyError) as e:
                logger.warning(f"Worktree {wt.get('id', '?')} has invalid path, marking as non-existent: {e}")
                wt["exists"] = False

            # Legacy support: also check deprecated worktrees.session_id if no sessions found
            if not wt["sessions"] and wt.get("session_id"):
                session = database.get_session(wt["session_id"])
                if session:
                    wt["sessions"] = [{
                        "id": session["id"],
                        "title": session["title"],
                        "status": session["status"],
                        "updated_at": session["updated_at"]
                    }]
                    wt["session_count"] = 1
                    if session.get("status") == "active":
                        wt["active_session"] = wt["sessions"][0]

        return worktrees

    def get_worktree_details(self, worktree_id: str) -> Optional[Dict]:
        """Get detailed worktree information with session history."""
        worktree = database.get_worktree(worktree_id)
        if not worktree:
            return None

        # Check if worktree is valid (not just if folder exists)
        try:
            worktree_abs_path = get_worktree_working_dir(worktree)
            worktree["exists"] = self.is_worktree_valid(worktree)
        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f"Worktree {worktree_id} has invalid path: {e}")
            worktree_abs_path = None
            worktree["exists"] = False

        if worktree["exists"] and worktree_abs_path:
            worktree["git_status"] = self.git_service.get_status(worktree_abs_path)

        # Get all sessions for this worktree (new model)
        sessions = database.get_sessions_for_worktree(worktree_id)
        worktree["sessions"] = [{
            "id": s["id"],
            "title": s["title"],
            "status": s["status"],
            "updated_at": s["updated_at"]
        } for s in sessions]
        worktree["session_count"] = len(sessions)

        # Find active session
        active_sessions = [s for s in sessions if s.get("status") == WorktreeStatus.ACTIVE]
        worktree["active_session"] = {
            "id": active_sessions[0]["id"],
            "title": active_sessions[0]["title"],
            "status": active_sessions[0]["status"],
            "updated_at": active_sessions[0]["updated_at"]
        } if active_sessions else None

        # Legacy support: check deprecated worktrees.session_id
        if not worktree["sessions"] and worktree.get("session_id"):
            session = database.get_session(worktree["session_id"])
            if session:
                worktree["sessions"] = [{
                    "id": session["id"],
                    "title": session["title"],
                    "status": session["status"],
                    "updated_at": session["updated_at"]
                }]
                worktree["session_count"] = 1
                if session.get("status") == WorktreeStatus.ACTIVE:
                    worktree["active_session"] = worktree["sessions"][0]

        return worktree

    async def terminate_worktree_sessions(self, worktree_id: str) -> list[str]:
        """
        Terminate all SDK sessions associated with a worktree.

        Finds all sessions linked to the worktree via DB, removes them from
        the session pool (which gracefully kills the SDK subprocess), and
        tears down their task-registry/Monitor-shell state.

        This MUST be called before `git worktree remove` — otherwise the
        subprocess holds a CWD lock on the worktree folder and removal fails
        (especially on Windows).

        Args:
            worktree_id: The worktree whose sessions should be terminated.

        Returns:
            List of session IDs that were terminated.
        """
        from app.core.session_pool import get_session_pool
        from app.core import streaming_status

        sessions = database.get_sessions_for_worktree(worktree_id)
        if not sessions:
            return []

        pool = get_session_pool()
        terminated = []

        for session in sessions:
            session_id = session["id"]

            # Remove from session pool — this gracefully disconnects the SDK
            # subprocess (SIGTERM → wait → SIGKILL)
            state = await pool.remove(session_id)
            if state:
                logger.info(
                    f"Terminated SDK session {session_id} for worktree {worktree_id}"
                )
                terminated.append(session_id)

            # Durable end-of-turn writes if this session was mid-turn (busy
            # truth from the durable runs table — the stale run lease itself
            # self-heals via TTL).
            if streaming_status.has_active_run(session_id):
                from app.core.turn_finalize import persist_turn_end_state
                await persist_turn_end_state(session_id)
                logger.info(
                    f"Ended streaming for session {session_id} (worktree cleanup)"
                )

            # Reap the session's background work: the SDK client is gone, so
            # tracked tasks can never complete — flip them to killed (surfaced
            # on the taskbar), drop the registry bucket, and reap any Monitor
            # shells the session spawned.
            try:
                from app.core.task_registry import task_registry
                for rec in task_registry.get_active(session_id):
                    task_registry.complete_task(session_id, rec["id"], "killed")
                task_registry.clear_session(session_id)
            except Exception:
                logger.debug("worktree-cleanup task reap failed", exc_info=True)
            try:
                from app.mcp.builtin_tools import reap_session_background_shells
                reap_session_background_shells(session_id)
            except Exception:
                logger.debug(
                    "worktree-cleanup shell reap scheduling failed", exc_info=True
                )

        if terminated:
            logger.info(
                f"Terminated {len(terminated)} session(s) for worktree {worktree_id}: "
                f"{terminated}"
            )

        return terminated

    async def cleanup_worktree(self, worktree_id: str, keep_branch: bool = True) -> bool:
        """
        Remove a worktree and optionally delete its branch.

        Terminates any SDK sessions whose subprocess is running inside the
        worktree directory before removing it, ensuring no CWD locks block
        the git worktree removal.

        Args:
            worktree_id: The worktree to remove
            keep_branch: If True, keep the branch after removing worktree

        Returns:
            True if successful
        """
        worktree = database.get_worktree(worktree_id)
        if not worktree:
            logger.warning(f"Worktree not found: {worktree_id}")
            return False

        # Get repository to find main directory
        repo = database.get_git_repository(worktree["repository_id"])
        if not repo:
            logger.warning(f"Repository not found for worktree: {worktree_id}")
            # Still try to clean up the database record
            database.delete_worktree(worktree_id)
            return True

        # Get project to find main directory
        project = database.get_project(repo["project_id"])
        if not project:
            logger.warning(f"Project not found for repository: {repo['id']}")
            database.delete_worktree(worktree_id)
            return True

        # Terminate SDK sessions BEFORE removing the worktree folder.
        # Their subprocess CWD is inside the worktree — if we don't kill
        # them first, `git worktree remove` fails (Windows CWD lock) or
        # leaves a zombie subprocess (Linux).
        terminated = await self.terminate_worktree_sessions(worktree_id)
        if terminated:
            logger.info(
                f"Terminated {len(terminated)} session(s) before removing "
                f"worktree {worktree_id}"
            )

        main_dir = get_project_working_dir(project)
        worktree_abs_path = get_worktree_working_dir(worktree)

        # Remove git worktree
        if Path(worktree_abs_path).exists():
            success = self.git_service.remove_worktree(main_dir, worktree_abs_path, force=True)
            if not success:
                logger.warning(f"Failed to remove git worktree at {worktree_abs_path}")
                # Continue to mark as removed in database

        # Optionally delete the branch
        if not keep_branch:
            branch_name = worktree["branch_name"]
            # Don't delete if it's the default branch
            default_branch = repo.get("default_branch", "main")
            if branch_name != default_branch:
                self.git_service.delete_branch(main_dir, branch_name, force=True)
                logger.debug(f"Deleted branch {branch_name}")

        # Mark as completed (preserve record for session reopen)
        database.update_worktree(worktree_id, status=WorktreeStatus.COMPLETED)

        logger.debug(f"Cleaned up worktree {worktree_id} (marked COMPLETED)")
        return True

    def _find_git_worktree_for_branch(
        self, main_dir: str, branch_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Find a live (non-main) git worktree that already has ``branch_name``
        checked out on this device.

        Git enforces that a branch can be checked out by at most one worktree,
        so when ``git worktree add`` would fail with "already used by worktree
        at X" the branch is in fact alive at X — we can adopt that path rather
        than failing. This is common in a shared-Postgres cluster: another
        device may mark the DB record ORPHANED/DELETED because the dir is
        missing on *that* disk, but it's healthy on the device resuming here.

        Returns the matching ``git_service.list_worktrees`` dict (keys: path,
        branch, is_main, is_bare, is_detached) whose directory exists on disk
        and contains a ``.git`` entry, or ``None`` if no adoptable match.
        """
        try:
            existing = self.git_service.list_worktrees(main_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not list worktrees for {main_dir}: {e}")
            return None

        for wt in existing:
            if wt.get("is_main") or wt.get("is_bare"):
                continue
            # list_worktrees returns the short branch name (refs/heads/ stripped).
            if wt.get("branch") != branch_name:
                continue
            raw_path = wt.get("path")
            if not raw_path:
                continue
            candidate = Path(raw_path)
            # Reuse the is_worktree_valid notion: folder exists AND has .git.
            if candidate.exists() and (candidate / ".git").exists():
                return wt
        return None

    def _adopt_existing_worktree(
        self,
        worktree_id: str,
        branch_name: str,
        adopted: Dict[str, Any],
        project: Dict[str, Any],
        prior_status: Any,
        repository_id: Optional[str] = None,
        rebind_project_id: Optional[str] = None,
        rebind_session_id: Optional[str] = None,
    ) -> Dict:
        """
        Adopt an on-disk git worktree (found via
        ``_find_git_worktree_for_branch``) into the DB record instead of
        re-adding it. Flips the record to ACTIVE with the adopted path and
        returns the updated record (same shape ``recreate_worktree`` returns).

        ``repository_id`` / ``rebind_project_id`` / ``rebind_session_id`` are set
        only on the cross-device rebind path: the worktree row is re-pointed at
        this install's clone of the repo, and ONLY the resumed session
        (``rebind_session_id``) is moved onto the local project row — never its
        sibling forks, which stay bound to their home device (PR #493 follow-up
        #2).
        """
        adopt_path = Path(adopted["path"])
        stored_path = self._get_relative_worktree_path_from_full(
            adopt_path, project["path"]
        )
        updated = database.update_worktree(
            worktree_id,
            status=WorktreeStatus.ACTIVE,
            worktree_path=stored_path,
            branch_name=branch_name,
            repository_id=repository_id,
        )
        self._repoint_rebound_session(rebind_session_id, rebind_project_id)
        logger.info(
            f"Adopted existing on-disk worktree for branch '{branch_name}' "
            f"(worktree_id={worktree_id}, path={adopt_path}, "
            f"prior status was {prior_status}) — skipped git add"
        )
        return updated

    def _repoint_rebound_session(
        self, session_id: Optional[str], project_id: Optional[str]
    ) -> None:
        """Move ONLY the resumed session onto this install's local project row.

        A cross-device rebind physically relocates the worktree onto this
        install's clone, but the shared DB is authoritative for every install:
        re-pointing *all* sessions on the worktree (as the original #493 code did)
        would corrupt sibling forks the user isn't resuming here — flipping their
        project id to a row that is foreign on THEIR home device. So we scope the
        move to the single session being resumed. When no session id is known
        (e.g. the manual ``reopen`` endpoint), we re-point nothing rather than
        clobber siblings (PR #493 follow-up #2).
        """
        if session_id and project_id:
            database.set_session_project(session_id, project_id)

    def recreate_worktree(
        self, worktree_id: str, rebind_session_id: Optional[str] = None
    ) -> Dict:
        """
        Recreate a worktree from its branch (last git state) so the user can
        resume the session — regardless of the record's prior status.

        Worktree sessions are never a dead end: this recreates from ANY status
        (COMPLETED, DELETED, ORPHANED, or ACTIVE-but-invalid-on-disk). It reuses
        the existing branch if it still exists locally, otherwise creates a new
        branch off the default branch. The existing DB record is flipped back to
        ACTIVE with the new path.

        The only fast path: if the record is ACTIVE and still valid on disk, the
        existing worktree is returned unchanged.

        Args:
            worktree_id: The worktree record to recreate
            rebind_session_id: On the cross-device rebind path, the single session
                being resumed. Only that session is moved onto this install's
                local project row; sibling forks are left untouched (follow-up #2).

        Returns:
            Updated worktree dict

        Raises:
            WorktreeError: If worktree not found or git recreation fails
        """
        worktree = database.get_worktree(worktree_id)
        if not worktree:
            raise WorktreeError(f"Worktree not found: {worktree_id}")

        if worktree["status"] == WorktreeStatus.ACTIVE:
            # Already active — check if valid on disk
            if self.is_worktree_valid(worktree):
                return worktree
            # Active but missing on disk — proceed with recreation

        # Recreate regardless of prior status (COMPLETED, DELETED, ORPHANED, or
        # ACTIVE-but-invalid). A worktree session must always be reopenable.

        # Get repository and project
        repo = database.get_git_repository(worktree["repository_id"])
        if not repo:
            raise WorktreeError(f"Repository not found for worktree: {worktree_id}")

        project = database.get_project(repo["project_id"])
        if not project:
            raise WorktreeError(f"Project not found for repository: {repo['id']}")

        main_dir = get_project_working_dir(project)
        branch_name = worktree["branch_name"]

        # Cross-device rebind: on a shared database the worktree's project row
        # can belong to another (offline) machine, so its stored path isn't a git
        # repo here. Rather than let git fail with "fatal: not a git repository",
        # rebind onto THIS install's clone of the same repo (matched via the
        # project link group) and recreate the worktree locally.
        rebound = False
        rebound_repo_id: Optional[str] = None
        rebound_project_id: Optional[str] = None
        if not (Path(main_dir).is_dir() and self.git_service.is_git_repo(main_dir)):
            from app.core.project_links import resolve_runnable_local_project

            _stored, local_project = resolve_runnable_local_project(project["id"])
            local_dir = (
                get_project_working_dir(local_project) if local_project else None
            )
            if (
                local_project is not None
                and local_project["id"] != project["id"]
                and local_dir is not None
                and self.git_service.is_git_repo(local_dir)
            ):
                local_repo = self._ensure_repository_record(
                    local_project["id"], local_project["path"]
                )
                if not local_repo:
                    raise WorktreeError(
                        f"Local clone of the repository at '{local_dir}' is not a "
                        f"git repository — cannot rebind worktree {worktree_id}."
                    )
                logger.info(
                    "Rebound worktree %s from project %s (%s) to local project "
                    "%s (%s) via link group %s",
                    worktree_id,
                    project["id"],
                    main_dir,
                    local_project["id"],
                    local_dir,
                    project.get("link_group_id"),
                )
                project = local_project
                repo = local_repo
                main_dir = local_dir
                rebound = True
                rebound_repo_id = local_repo["id"]
                rebound_project_id = local_project["id"]
            else:
                raise WorktreeError(
                    f"This session's project lives at '{main_dir}', which doesn't "
                    f"exist on this device, and no local clone of the same "
                    f"repository was found. Clone the repository here and add it as "
                    f"a project, or resume on the original device."
                )

        default_branch = repo.get("default_branch", "main")

        # Determine if we need to create a new branch or reuse existing.
        if rebound:
            # On the rebind path we must NOT fabricate a fresh branch off the
            # default — that would silently present an empty branch as the user's
            # unpushed work. The shared helper reuses a local branch, recovers a
            # pushed one from origin, or raises loudly.
            create_new, base_branch = resolve_rebound_branch_plan(
                self.git_service, main_dir, branch_name
            )
        else:
            branches = self.git_service.list_branches(main_dir, include_remote=False)
            local_branch_names = [b["name"] for b in branches if not b["is_remote"]]
            branch_exists = branch_name in local_branch_names
            if branch_exists:
                create_new = False
                base_branch = None
            else:
                create_new = True
                # If creating new branch, base it on the default branch (which has merged changes)
                base_branch = default_branch

        # Generate new worktree path (old path was deleted)
        worktree_path = self._get_worktree_path(project["path"], branch_name)
        relative_worktree_path = self._get_relative_worktree_path_from_full(
            worktree_path, project["path"]
        )

        logger.debug(
            f"Recreating worktree {worktree_id} at {worktree_path} "
            f"(branch={branch_name}, create_new={create_new})"
        )

        # Prune stale worktree entries so git doesn't complain about existing refs
        self.git_service.prune_worktrees(main_dir)

        # Adopt-if-already-checked-out: prune does NOT remove an entry whose
        # directory still exists on disk, so re-adding the same branch would
        # fail ("already used by worktree at X"). If the branch is already live
        # in git on this device, adopt that path instead of re-adding — this
        # makes recreate idempotent and is the common shared-DB-cluster case
        # (the record was flipped ORPHANED/DELETED by another device, but the
        # worktree is healthy locally).
        adopted = self._find_git_worktree_for_branch(main_dir, branch_name)
        if adopted is not None:
            return self._adopt_existing_worktree(
                worktree_id, branch_name, adopted, project, prior_status=worktree["status"],
                repository_id=rebound_repo_id, rebind_project_id=rebound_project_id,
                rebind_session_id=rebind_session_id,
            )

        # Create git worktree
        success, error_msg = self.git_service.add_worktree(
            main_dir=main_dir,
            path=str(worktree_path),
            branch=branch_name,
            new_branch=create_new,
            base_branch=base_branch
        )

        if not success:
            # Belt-and-suspenders: a concurrent/stale entry can race past the
            # adopt scan above. If git reports the branch is already used by an
            # existing worktree, re-scan and adopt it rather than dead-ending.
            if error_msg and "already used by worktree" in error_msg.lower():
                adopted = self._find_git_worktree_for_branch(main_dir, branch_name)
                if adopted is not None:
                    return self._adopt_existing_worktree(
                        worktree_id, branch_name, adopted, project,
                        prior_status=worktree["status"],
                        repository_id=rebound_repo_id, rebind_project_id=rebound_project_id,
                        rebind_session_id=rebind_session_id,
                    )
            raise WorktreeError(f"Git failed to recreate worktree: {error_msg or 'Unknown error'}")

        # Verify the worktree directory was actually created on disk
        if not worktree_path.is_dir():
            raise WorktreeError(
                f"Git reported success but worktree directory not found: {worktree_path}"
            )
        logger.debug(f"Verified worktree directory exists: {worktree_path}")

        # Update existing DB record back to ACTIVE with new path (and, on a
        # rebind, the local repository id + re-pointed sessions).
        updated = database.update_worktree(
            worktree_id,
            status=WorktreeStatus.ACTIVE,
            worktree_path=relative_worktree_path,
            branch_name=branch_name,
            repository_id=rebound_repo_id,
        )
        self._repoint_rebound_session(rebind_session_id, rebound_project_id)

        # NOTE: sdk_session_id is intentionally preserved on reopen.
        # JSONL history lives in ~/.claude/projects/ (not in the worktree dir),
        # so the SDK can resume the conversation with full history intact.

        logger.info(
            f"Recreated worktree {worktree_id} at {worktree_path} "
            f"for branch {branch_name} (prior status was {worktree['status']})"
        )
        return updated

    def sync_worktrees(self, project_id: str) -> Dict[str, Any]:
        """
        Sync database worktrees with actual git worktrees.
        Marks orphaned worktrees and cleans up stale records.

        Returns:
            Dict with sync results
        """
        result = {
            "synced": 0,
            "orphaned": 0,
            "cleaned_up": 0,
            "errors": []
        }

        # Get project and repository
        project = database.get_project(project_id)
        if not project:
            result["errors"].append(f"Project not found: {project_id}")
            return result

        repo = database.get_git_repository_by_project(project_id)
        if not repo:
            result["errors"].append(f"No git repository for project: {project_id}")
            return result

        main_dir = get_project_working_dir(project)

        # Prune stale git worktree entries first — this removes git's internal
        # tracking for worktrees whose directories no longer exist on disk.
        self.git_service.prune_worktrees(main_dir)

        # Get actual worktrees from git (after pruning)
        git_worktrees = self.git_service.list_worktrees(main_dir)
        # Normalize paths for comparison (git uses forward slashes,
        # Python Path uses backslashes on Windows)
        git_worktree_paths = set()
        # Also index real git worktrees by their basename (folder name) so we can
        # tolerate older DB rows. See the FIX 3 note in the orphan check below.
        git_worktree_basenames = set()
        for wt in git_worktrees:
            if not wt.get("is_main"):
                normalized = os.path.normpath(wt.get("path", ""))
                git_worktree_paths.add(normalized)
                git_worktree_basenames.add(os.path.basename(normalized))

        # Get database worktrees
        db_worktrees = database.get_worktrees_for_repository(repo["id"])

        for db_wt in db_worktrees:
            worktree_abs_path = os.path.normpath(get_worktree_working_dir(db_wt))

            if worktree_abs_path in git_worktree_paths:
                # Worktree exists in both git and database
                result["synced"] += 1
            elif (
                db_wt["status"] == WorktreeStatus.ACTIVE
                and os.path.basename(os.path.normpath(db_wt.get("worktree_path") or ""))
                in git_worktree_basenames
            ):
                # Edge case (pre-existing latent inconsistency): older worktree
                # rows for absolute-path projects were stored as a *bare folder
                # name* but physically created in the project's PARENT dir. Such a
                # row now resolves to ``workspace_dir/<name>`` (nonexistent), so a
                # naive check would mark a VALID active worktree as ORPHANED and
                # re-import it on every sync. If a real git worktree with the same
                # folder name still exists, treat the row as synced rather than
                # aggressively orphaning it. This is self-healing and avoids churn;
                # we intentionally do NOT attempt a risky path migration here.
                result["synced"] += 1
            elif db_wt["status"] == WorktreeStatus.ACTIVE:
                # Worktree is in database but not on filesystem - mark as orphaned
                logger.warning(f"Worktree {db_wt['id']} is orphaned (path doesn't exist)")
                database.update_worktree(db_wt["id"], status=WorktreeStatus.ORPHANED)
                result["orphaned"] += 1
            elif db_wt["status"] == WorktreeStatus.COMPLETED:
                # Completed worktrees are preserved for session reopen — skip
                result["synced"] += 1
            elif db_wt["status"] in [WorktreeStatus.DELETED, WorktreeStatus.ORPHANED]:
                # Clean up stale records
                database.delete_worktree(db_wt["id"])
                result["cleaned_up"] += 1

        # Import external worktrees (created outside the app) into our database
        db_paths = {
            os.path.normpath(get_worktree_working_dir(wt))
            for wt in db_worktrees
        }
        # Also dedup by bare folder name so older rows (stored as a bare name and
        # resolving to a nonexistent ``workspace_dir/<name>``) don't get
        # re-imported as duplicates every sync. See the FIX 3 note above.
        db_basenames = {
            os.path.basename(os.path.normpath(wt.get("worktree_path") or ""))
            for wt in db_worktrees
            if wt.get("worktree_path")
        }
        # V2 placement checkouts are tracked in the ``checkouts`` table and must
        # never be imported as legacy ``worktrees`` rows. Build the set of active
        # checkout paths + this runner's checkout root once.
        v2_paths = {
            os.path.normpath(p) for p in checkouts_db.list_all_active_checkout_paths()
        }
        v2_checkout_root = os.path.normpath(
            str(Path(settings.effective_workspace_dir) / "checkouts")
        )
        for git_wt in git_worktrees:
            if git_wt.get("is_main"):
                continue  # Skip main worktree

            git_path = git_wt.get("path", "")
            if not git_path:
                continue
            # Only import worktrees that actually exist on disk
            if not Path(git_path).exists():
                continue
            normalized_git_path = os.path.normpath(git_path)
            # Skip V2 placement checkouts: they are owned by the ``checkouts``
            # table and must never become legacy ``worktrees`` rows. The
            # root-prefix check also covers the mid-materialization race where the
            # dir exists before its checkouts row commits.
            if (
                normalized_git_path in v2_paths
                or normalized_git_path == v2_checkout_root
                or normalized_git_path.startswith(v2_checkout_root + os.sep)
            ):
                continue
            if (
                normalized_git_path not in db_paths
                and os.path.basename(normalized_git_path) not in db_basenames
            ):
                # External worktree found - import it
                try:
                    # Store the absolute path so it resolves consistently
                    # regardless of where the worktree lives on disk (e.g.
                    # nested under .claude/worktrees/, not workspace_dir). A
                    # bare folder name would resolve against workspace_dir and
                    # never match the real git path, causing duplicate
                    # re-imports and false ORPHANED markings. Mirrors the import
                    # logic in sync_worktrees_for_project().
                    stored_path = os.path.abspath(git_path)
                    branch_name = _external_worktree_branch_name(git_wt)

                    # Create database record for this external worktree
                    worktree_id = f"wt-{uuid.uuid4().hex[:12]}"
                    database.create_worktree(
                        worktree_id=worktree_id,
                        repository_id=repo["id"],
                        branch_name=branch_name,
                        worktree_path=stored_path,
                        session_id=None,
                        base_branch=None,
                        status=WorktreeStatus.ACTIVE
                    )
                    logger.debug(f"Imported external worktree: {stored_path} (branch: {branch_name})")
                    result["imported"] = result.get("imported", 0) + 1
                except Exception as e:
                    logger.warning(f"Failed to import external worktree {git_path}: {e}")
                    result["errors"].append(f"Failed to import worktree at {git_path}: {str(e)}")

        logger.debug(f"Sync complete for project {project_id}: {result}")
        return result


# Singleton instance
worktree_manager = WorktreeManager()
