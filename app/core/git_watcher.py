"""
Git File Watcher Service

Three-layer event system for real-time git state monitoring:

1. **Git Hooks** (via GitHookManager) — Precise event-driven detection of
   commits, checkouts, merges, rewrites from any source (CLI, IDE, etc.).
   Bypasses the debounce for instant updates.

2. **Watchdog filesystem watcher** — Real-time detection of working tree
   changes (staging, file edits) that don't trigger hooks.
   500ms trailing-edge debounce to batch rapid operations.

3. **Programmatic triggers** (via trigger_immediate_update) — API mutation
   endpoints call this after git operations complete, bypassing debounce
   for instant feedback.

Key design:
- One watchdog Observer per active project (ref-counted by WebSocket connections)
- Repo-to-project reverse lookup for hook callback resolution
- Deduplication: 1-second suppression window after hook events prevents
  double-broadcast when both hook and filesystem fire for the same operation
- Cross-platform: ReadDirectoryChangesW on Windows, inotify on Linux
"""

import asyncio
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

logger = logging.getLogger(__name__)

# Debounce interval: git operations cause many rapid file changes
DEBOUNCE_SECONDS = 0.5

# After a hook event fires, suppress watchdog events for the same
# categories for this duration to prevent double-broadcast.
HOOK_SUPPRESSION_SECONDS = 1.0

# File patterns mapped to the cache categories they invalidate
_FILE_CATEGORY_MAP = {
    "index": {"status", "diff_stats"},
    "HEAD": {"status", "branches"},
    "MERGE_HEAD": {"status"},
    "REBASE_HEAD": {"status"},
    "COMMIT_EDITMSG": {"status", "diff_stats"},
    "packed-refs": {"branches", "status"},
}

# Hook names mapped to cache categories (higher precision than filesystem events)
_HOOK_CATEGORY_MAP = {
    "post-commit": {"status", "diff_stats", "branches"},
    "post-checkout": {"status", "branches", "diff_stats"},
    "post-merge": {"status", "diff_stats", "branches"},
    "post-rewrite": {"status", "diff_stats", "branches"},
}


def _classify_change(rel_path: str) -> Set[str]:
    """Map a changed file path (relative to .git/) to cache categories."""
    # Normalize path separators
    rel_path = rel_path.replace("\\", "/")

    # Direct file matches
    basename = rel_path.split("/")[-1]
    if basename in _FILE_CATEGORY_MAP:
        return _FILE_CATEGORY_MAP[basename]

    # refs/heads/* changes affect branches and status
    if rel_path.startswith("refs/heads/"):
        return {"branches", "status"}

    # refs/remotes/* changes affect branches
    if rel_path.startswith("refs/remotes/"):
        return {"branches"}

    return set()


def _resolve_git_dir(working_dir: str) -> Optional[str]:
    """Resolve the .git directory for a working directory.

    Handles both regular repos (.git/ directory) and worktrees (.git file
    containing 'gitdir: ...').
    """
    git_path = os.path.join(working_dir, ".git")

    if os.path.isdir(git_path):
        return git_path

    if os.path.isfile(git_path):
        # Worktree: .git file contains "gitdir: /path/to/main/.git/worktrees/<name>"
        try:
            with open(git_path, "r") as f:
                content = f.read().strip()
            if content.startswith("gitdir: "):
                gitdir = content[len("gitdir: "):]
                if not os.path.isabs(gitdir):
                    gitdir = os.path.join(working_dir, gitdir)
                return os.path.normpath(gitdir)
        except OSError:
            pass

    # Fallback: use git rev-parse
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            gitdir = result.stdout.strip()
            if not os.path.isabs(gitdir):
                gitdir = os.path.join(working_dir, gitdir)
            return os.path.normpath(gitdir)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


class _GitEventHandler(FileSystemEventHandler):
    """Handles filesystem events in a .git/ directory."""

    def __init__(self, watcher: "ProjectWatcher"):
        super().__init__()
        self._watcher = watcher

    def on_any_event(self, event: FileSystemEvent):
        if event.is_directory:
            return

        src_path = event.src_path
        git_dir = self._watcher.git_dir

        # Get path relative to the .git directory
        try:
            rel_path = os.path.relpath(src_path, git_dir)
        except ValueError:
            return

        categories = _classify_change(rel_path)
        if categories:
            self._watcher.schedule_update(categories)


class ProjectWatcher:
    """Manages filesystem watching for a single project's .git/ directory."""

    def __init__(
        self,
        project_id: str,
        working_dir: str,
        git_dir: str,
        service: "GitWatcherService",
        worktree_id: Optional[str] = None,
    ):
        self.project_id = project_id
        self.working_dir = working_dir
        self.git_dir = git_dir
        self.worktree_id = worktree_id
        self._service = service
        self._observer: Optional[Observer] = None
        self._pending_categories: Set[str] = set()
        self._debounce_handle: Optional[asyncio.TimerHandle] = None
        self._user_ids: Set[str] = set()
        self._ref_count = 0
        self._lock = threading.Lock()
        # Deduplication: tracks when hook events last processed each category
        self._hook_suppressed_until: Dict[str, float] = {}

    def add_ref(self, user_id: str) -> None:
        with self._lock:
            self._ref_count += 1
            self._user_ids.add(user_id)

    def remove_ref(self, user_id: str) -> bool:
        """Remove a reference. Returns True if ref count reaches zero."""
        with self._lock:
            self._ref_count = max(0, self._ref_count - 1)
            # Don't remove user_id here — other connections for same user may exist
            return self._ref_count == 0

    def start(self) -> None:
        """Start the watchdog observer."""
        if self._observer is not None:
            return

        try:
            self._observer = Observer()
            handler = _GitEventHandler(self)
            self._observer.schedule(handler, self.git_dir, recursive=True)
            self._observer.daemon = True
            self._observer.start()
            logger.debug(f"Git watcher started for project={self.project_id} dir={self.git_dir}")
        except Exception as e:
            logger.warning(f"Failed to start git watcher for {self.project_id}: {e}")
            self._observer = None

    def stop(self) -> None:
        """Stop the watchdog observer."""
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:
                pass
            self._observer = None
            logger.debug(f"Git watcher stopped for project={self.project_id}")

        # Cancel any pending debounce
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None

    def schedule_update(self, categories: Set[str]) -> None:
        """Schedule a debounced update for the given categories.

        Called from the watchdog thread, so we schedule work on the asyncio loop.
        Respects hook suppression window to prevent double-broadcast.
        """
        # Filter out categories that are currently suppressed by a recent hook event
        now = time.monotonic()
        unsuppressed = set()
        for cat in categories:
            suppressed_until = self._hook_suppressed_until.get(cat, 0)
            if now >= suppressed_until:
                unsuppressed.add(cat)

        if not unsuppressed:
            return

        with self._lock:
            self._pending_categories.update(unsuppressed)

        loop = self._service._loop
        if loop is None or loop.is_closed():
            return

        # Schedule on the event loop (thread-safe)
        loop.call_soon_threadsafe(self._reset_debounce)

    def set_hook_suppression(self, categories: Set[str]) -> None:
        """Suppress watchdog events for the given categories for a short window.

        Called after a hook event is processed to prevent double-broadcast.
        """
        until = time.monotonic() + HOOK_SUPPRESSION_SECONDS
        for cat in categories:
            self._hook_suppressed_until[cat] = until

    def _reset_debounce(self) -> None:
        """Reset the debounce timer (must be called on the event loop)."""
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()

        loop = self._service._loop
        if loop is None or loop.is_closed():
            return

        self._debounce_handle = loop.call_later(
            DEBOUNCE_SECONDS,
            lambda: asyncio.ensure_future(self._flush_update())
        )

    async def _flush_update(self) -> None:
        """Process all pending changes: invalidate cache and broadcast."""
        with self._lock:
            categories = self._pending_categories.copy()
            self._pending_categories.clear()
            user_ids = self._user_ids.copy()
            self._debounce_handle = None

        if not categories:
            return

        await self._service.process_changes(
            project_id=self.project_id,
            worktree_id=self.worktree_id,
            working_dir=self.working_dir,
            categories=categories,
            user_ids=user_ids,
        )


class GitWatcherService:
    """Manages file watchers for all active projects.

    Provides three entry points for state updates:
    1. Filesystem events (via watchdog) — debounced, for working tree changes
    2. Hook events (via process_hook_event) — immediate, for git operations
    3. API triggers (via trigger_immediate_update) — immediate, for mutations
    """

    def __init__(self):
        self._watchers: Dict[str, ProjectWatcher] = {}  # key -> ProjectWatcher
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Reverse lookup: normalized repo_path -> (project_id, working_dir, worktree_id)
        self._repo_to_project: Dict[str, Tuple[str, str, Optional[str]]] = {}

    @staticmethod
    def _make_key(project_id: str, worktree_id: Optional[str] = None) -> str:
        return f"{project_id}:{worktree_id}" if worktree_id else project_id

    async def start(self) -> None:
        """Initialize the watcher service. Call during app startup."""
        self._loop = asyncio.get_running_loop()
        logger.info("Git watcher service started")

    async def shutdown(self) -> None:
        """Stop all watchers and uninstall hooks. Call during app shutdown."""
        from app.core.git_hook_manager import git_hook_manager

        # Uninstall hooks for all watched repos
        for repo_path in list(self._repo_to_project.keys()):
            try:
                git_hook_manager.uninstall_hooks(repo_path)
            except Exception as e:
                logger.debug(f"Failed to uninstall hooks for {repo_path}: {e}")

        async with self._lock:
            for watcher in self._watchers.values():
                watcher.stop()
            self._watchers.clear()
            self._repo_to_project.clear()
        logger.info("Git watcher service stopped")

    async def watch_project(
        self,
        project_id: str,
        working_dir: str,
        user_id: str,
        worktree_id: Optional[str] = None,
    ) -> None:
        """Start watching a project's .git/ directory (or increment ref count).

        Also installs git hooks for event-driven detection.
        """
        from app.core.git_hook_manager import git_hook_manager

        key = self._make_key(project_id, worktree_id)

        async with self._lock:
            watcher = self._watchers.get(key)
            if watcher:
                watcher.add_ref(user_id)
                return

            git_dir = _resolve_git_dir(working_dir)
            if not git_dir:
                logger.debug(f"No .git dir found for project={project_id}, skipping watch")
                return

            watcher = ProjectWatcher(
                project_id=project_id,
                working_dir=working_dir,
                git_dir=git_dir,
                service=self,
                worktree_id=worktree_id,
            )
            watcher.add_ref(user_id)
            watcher.start()
            self._watchers[key] = watcher

            # Register reverse lookup for hook callback resolution
            norm_path = os.path.normpath(working_dir)
            self._repo_to_project[norm_path] = (project_id, working_dir, worktree_id)

            # Install git hooks (idempotent — skips if already installed)
            git_hook_manager.install_hooks(norm_path, git_dir)

    async def unwatch_project(
        self,
        project_id: str,
        user_id: str,
        worktree_id: Optional[str] = None,
    ) -> None:
        """Decrement ref count and stop watching if no more references.

        Uninstalls git hooks when the last reference is removed.
        """
        from app.core.git_hook_manager import git_hook_manager

        key = self._make_key(project_id, worktree_id)

        async with self._lock:
            watcher = self._watchers.get(key)
            if not watcher:
                return

            if watcher.remove_ref(user_id):
                watcher.stop()
                del self._watchers[key]

                # Uninstall hooks and remove reverse lookup
                norm_path = os.path.normpath(watcher.working_dir)
                git_hook_manager.uninstall_hooks(norm_path)
                self._repo_to_project.pop(norm_path, None)

    async def process_hook_event(
        self,
        hook_name: str,
        repo_path: str,
    ) -> None:
        """Process a git hook callback. Bypasses debounce for immediate updates.

        Called by the internal /git-hook endpoint when a hook fires.
        Maps the repo path to a project and triggers immediate state recomputation.
        """
        categories = _HOOK_CATEGORY_MAP.get(hook_name)
        if not categories:
            logger.debug(f"Unknown hook: {hook_name}")
            return

        # Resolve repo path to project info
        norm_path = os.path.normpath(repo_path)
        project_info = self._repo_to_project.get(norm_path)
        if not project_info:
            logger.debug(f"Hook callback for untracked repo: {repo_path}")
            return

        project_id, working_dir, worktree_id = project_info
        key = self._make_key(project_id, worktree_id)

        # Get user_ids from the watcher for broadcast targeting
        watcher = self._watchers.get(key)
        if not watcher:
            return

        # Set suppression window to prevent double-broadcast from watchdog
        watcher.set_hook_suppression(categories)

        # Get user_ids to broadcast to
        with watcher._lock:
            user_ids = watcher._user_ids.copy()

        if not user_ids:
            return

        logger.debug(
            f"Hook {hook_name} for project={project_id}: "
            f"processing categories={categories}"
        )

        await self.process_changes(
            project_id=project_id,
            worktree_id=worktree_id,
            working_dir=working_dir,
            categories=categories,
            user_ids=user_ids,
        )

    async def trigger_immediate_update(
        self,
        project_id: str,
        worktree_id: Optional[str],
        working_dir: str,
        categories: Set[str],
        user_id: str,
    ) -> None:
        """Trigger an immediate state update (bypasses debounce).

        Called by API mutation endpoints after git operations complete.
        Ensures the requesting user is in the broadcast target set.
        """
        key = self._make_key(project_id, worktree_id)
        watcher = self._watchers.get(key)

        # Build user_ids set: always include the requesting user,
        # plus any other users watching this project
        user_ids = {user_id}
        if watcher:
            with watcher._lock:
                user_ids.update(watcher._user_ids)
            # Suppress watchdog events to prevent double-broadcast
            watcher.set_hook_suppression(categories)

        await self.process_changes(
            project_id=project_id,
            worktree_id=worktree_id,
            working_dir=working_dir,
            categories=categories,
            user_ids=user_ids,
        )

    async def process_changes(
        self,
        project_id: str,
        worktree_id: Optional[str],
        working_dir: str,
        categories: Set[str],
        user_ids: Set[str],
    ) -> None:
        """Invalidate cache and broadcast updated state for changed categories."""
        from app.core.git_state_cache import git_state_cache
        from app.core.git_service import git_service
        from app.core.sync_engine import sync_engine

        cache_key = git_state_cache.make_key(project_id, worktree_id)

        # Invalidate affected categories in cache
        await git_state_cache.invalidate_categories(cache_key, categories)

        # Recompute changed state
        changes: Dict[str, Any] = {}

        try:
            if "status" in categories:
                status_data = git_service.get_status(working_dir)
                changes["status"] = {
                    "is_git_repo": status_data.get("is_git_repo", False),
                    "current_branch": status_data.get("current_branch"),
                    "is_detached": status_data.get("is_detached", False),
                    "head_commit": status_data.get("head_commit"),
                    "remote_url": status_data.get("remote_url"),
                    "is_clean": status_data.get("is_clean", True),
                    "staged": status_data.get("staged", []),
                    "modified": status_data.get("modified", []),
                    "untracked": status_data.get("untracked", []),
                    "ahead": status_data.get("ahead", 0),
                    "behind": status_data.get("behind", 0),
                    "conflicts": status_data.get("conflicts", []),
                }

            if "diff_stats" in categories:
                stats = git_service.get_diff_stats(working_dir, include_unstaged=True)
                changes["diff_stats"] = {
                    "additions": stats.get("additions", 0),
                    "deletions": stats.get("deletions", 0),
                    "files_changed": stats.get("files_changed", 0),
                }

            if "branches" in categories:
                branches_raw = git_service.list_branches(working_dir, include_remote=True)
                current = git_service.get_current_branch(working_dir)
                changes["branches"] = {
                    "branches": [
                        {
                            "name": b["name"],
                            "is_current": b["is_current"],
                            "is_remote": b["is_remote"],
                            "commit": b["commit"],
                            "commit_message": b["commit_message"],
                            "upstream": b["upstream"],
                            "ahead": b["ahead"],
                            "behind": b["behind"],
                        }
                        for b in branches_raw
                    ],
                    "current_branch": current,
                }
        except Exception as e:
            logger.debug(f"Error recomputing git state for {project_id}: {e}")
            # Still broadcast whatever we did compute
            if not changes:
                return

        if not changes:
            return

        # Broadcast to all connected users for this project
        for uid in user_ids:
            try:
                await sync_engine.broadcast_git_state_changed(
                    user_id=uid,
                    project_id=project_id,
                    worktree_id=worktree_id,
                    changes=changes,
                )
            except Exception as e:
                logger.debug(f"Failed to broadcast git state to user {uid[:8]}...: {e}")


# Singleton instance
git_watcher = GitWatcherService()
