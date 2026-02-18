"""
Git Tracker — Consolidated Git State Tracking Service

Single service that owns all git state monitoring for active projects:
- Watchdog filesystem observer (debounced) for working tree changes
- Git hook management (immediate) for commit/checkout/merge/rewrite
- In-memory cache (60s TTL safety net) for avoiding redundant subprocess calls
- WebSocket broadcasting of state changes to connected clients

All change detection paths converge on _recompute_and_broadcast(), which
computes fresh git state and pushes it to clients via sync_engine.

Usage:
    git_tracker.start(port=8000)                    # App startup
    await git_tracker.track(project_id, dir, uid)   # Lazy-start per project
    await git_tracker.notify(project_id, cats)       # After API mutations
    await git_tracker.on_hook("post-commit", repo)   # Git hook callbacks
    stats = await git_tracker.get_diff_stats(...)    # Cached reads
    await git_tracker.shutdown()                     # App shutdown
"""

import asyncio
import hmac
import logging
import os
import platform
import secrets
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Watchdog debounce: git operations cause many rapid .git/ file changes
DEBOUNCE_SECONDS = 0.5

# After a hook or API event fires, suppress watchdog events for the same
# categories for this duration to prevent double-broadcast.
HOOK_SUPPRESSION_SECONDS = 1.0

# Cache entries auto-expire as a safety net (primary invalidation is explicit)
CACHE_TTL_SECONDS = 60

# Hooks we install for event-driven git state detection
HOOK_MARKER = "# AI-SHUFFLE-MANAGED-HOOK"
HOOK_VERSION = "v1"
MANAGED_HOOKS = ["post-commit", "post-checkout", "post-merge", "post-rewrite"]

# .git file → cache categories that need invalidation
_FILE_CATEGORY_MAP = {
    "index": {"status", "diff_stats"},
    "HEAD": {"status", "branches"},
    "MERGE_HEAD": {"status"},
    "REBASE_HEAD": {"status"},
    "COMMIT_EDITMSG": {"status", "diff_stats"},
    "packed-refs": {"branches", "status"},
}

# Hook name → cache categories (higher precision than filesystem events)
_HOOK_CATEGORY_MAP = {
    "post-commit": {"status", "diff_stats", "branches"},
    "post-checkout": {"status", "branches", "diff_stats"},
    "post-merge": {"status", "diff_stats", "branches"},
    "post-rewrite": {"status", "diff_stats", "branches"},
}


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _classify_change(rel_path: str) -> Set[str]:
    """Map a changed file path (relative to .git/) to cache categories."""
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
    """Resolve the .git directory, handling regular repos and worktrees."""
    git_path = os.path.join(working_dir, ".git")

    if os.path.isdir(git_path):
        return git_path

    if os.path.isfile(git_path):
        # Worktree: .git file contains "gitdir: /path/to/.git/worktrees/<name>"
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

    # Fallback: ask git directly
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            gitdir = result.stdout.strip()
            if not os.path.isabs(gitdir):
                gitdir = os.path.join(working_dir, gitdir)
            return os.path.normpath(gitdir)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


def _get_hooks_dir(working_dir: str, git_dir: str) -> str:
    """Get the hooks directory, respecting core.hooksPath config."""
    try:
        result = subprocess.run(
            ["git", "config", "core.hooksPath"],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            hooks_path = result.stdout.strip()
            if not os.path.isabs(hooks_path):
                hooks_path = os.path.join(working_dir, hooks_path)
            return os.path.normpath(hooks_path)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return os.path.join(git_dir, "hooks")


def _generate_hook_script(hook_name: str, port: int, token: str, hooks_dir: str) -> str:
    """Generate a POSIX shell hook script with HTTP callback and chaining."""
    hook_path = os.path.join(hooks_dir, hook_name).replace("\\", "/")

    return f"""#!/bin/sh
{HOOK_MARKER} {HOOK_VERSION}
# Managed by AI Shuffle — do not edit manually.
# Sends a callback to the local backend and chains to any existing user hook.

curl -s -m 2 -X POST "http://127.0.0.1:{port}/api/v1/internal/git-hook" \\
  -H "Content-Type: application/json" \\
  -H "X-Hook-Token: {token}" \\
  -d '{{"hook":"{hook_name}","repo":"'"$(git rev-parse --show-toplevel 2>/dev/null)"'"}}' \\
  >/dev/null 2>&1 || true

# Chain to user's original hook if present
if [ -x "{hook_path}.user" ]; then
  exec "{hook_path}.user" "$@"
fi
"""


def _is_managed_hook(hook_path: str) -> bool:
    """Check if a hook file is one we installed (by marker comment)."""
    try:
        with open(hook_path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(5):
                line = f.readline()
                if HOOK_MARKER in line:
                    return True
        return False
    except (OSError, IOError):
        return False


# ---------------------------------------------------------------------------
# Per-project state (replaces ProjectWatcher + CachedGitState + HookInstallation)
# ---------------------------------------------------------------------------

@dataclass
class _TrackedProject:
    """All state for a single tracked project/worktree."""

    # Identity
    project_id: str
    working_dir: str
    git_dir: str
    worktree_id: Optional[str] = None

    # Ref counting (multi-connection lifecycle)
    ref_count: int = 0
    user_ids: Set[str] = field(default_factory=set)

    # Watchdog observer
    observer: Optional[Observer] = None

    # Debounce state
    pending_categories: Set[str] = field(default_factory=set)
    debounce_handle: Optional[asyncio.TimerHandle] = None

    # Hook suppression (prevents double-broadcast when hook + watchdog both fire)
    hook_suppressed_until: Dict[str, float] = field(default_factory=dict)

    # Hook installation tracking
    hooks_dir: Optional[str] = None
    installed_hooks: Set[str] = field(default_factory=set)
    chained_hooks: Set[str] = field(default_factory=set)

    # Inline cache (60s TTL safety net; primary invalidation is explicit)
    cached_status: Optional[Dict[str, Any]] = None
    cached_diff_stats: Optional[Dict[str, Any]] = None
    cached_branches: Optional[Dict[str, Any]] = None
    cache_ts: float = 0.0

    # Thread safety for fields mutated from watchdog thread
    lock: threading.Lock = field(default_factory=threading.Lock)


# ---------------------------------------------------------------------------
# Filesystem event handler (watchdog)
# ---------------------------------------------------------------------------

class _GitEventHandler(FileSystemEventHandler):
    """Handles filesystem events in a .git/ directory."""

    def __init__(self, tracker: "GitTracker", proj: _TrackedProject):
        super().__init__()
        self._tracker = tracker
        self._proj = proj

    def on_any_event(self, event: FileSystemEvent):
        if event.is_directory:
            return

        try:
            rel_path = os.path.relpath(event.src_path, self._proj.git_dir)
        except ValueError:
            return

        categories = _classify_change(rel_path)
        if categories:
            self._tracker._schedule_debounced_update(self._proj, categories)


# ---------------------------------------------------------------------------
# GitTracker — the consolidated service
# ---------------------------------------------------------------------------

class GitTracker:
    """Consolidated git state tracking: watch, cache, hooks, and broadcast.

    Replaces the previous three-component system (git_watcher + git_hook_manager
    + git_state_cache) with a single service. All change detection paths converge
    on _recompute_and_broadcast().
    """

    def __init__(self):
        self._projects: Dict[str, _TrackedProject] = {}  # key -> _TrackedProject
        self._repo_to_key: Dict[str, str] = {}  # normalized repo_path -> key
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._hook_token: str = ""
        self._server_port: int = 8000

    # -------------------------------------------------------------------
    # Key helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _make_key(project_id: str, worktree_id: Optional[str] = None) -> str:
        return f"{project_id}:{worktree_id}" if worktree_id else project_id

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    def start(self, port: int) -> None:
        """Initialize the tracker. Call during app startup (from lifespan)."""
        self._loop = asyncio.get_running_loop()
        self._server_port = port
        self._hook_token = secrets.token_hex(16)
        logger.info("GitTracker started")

    async def shutdown(self) -> None:
        """Stop all watchers, uninstall all hooks, clear state."""
        async with self._lock:
            for proj in self._projects.values():
                self._stop_observer(proj)
                self._uninstall_hooks(proj)
            self._projects.clear()
            self._repo_to_key.clear()
        logger.info("GitTracker stopped — all watchers and hooks removed")

    # -------------------------------------------------------------------
    # Project tracking (ref-counted)
    # -------------------------------------------------------------------

    async def track(
        self,
        project_id: str,
        working_dir: str,
        user_id: str,
        worktree_id: Optional[str] = None,
    ) -> None:
        """Start tracking a project (or increment ref count).

        Starts filesystem watcher and installs git hooks on first call.
        Subsequent calls for the same project add the user and bump ref_count.
        """
        key = self._make_key(project_id, worktree_id)

        async with self._lock:
            proj = self._projects.get(key)
            if proj:
                with proj.lock:
                    proj.ref_count += 1
                    proj.user_ids.add(user_id)
                return

            git_dir = _resolve_git_dir(working_dir)
            if not git_dir:
                logger.debug(f"No .git dir found for project={project_id}, skipping track")
                return

            proj = _TrackedProject(
                project_id=project_id,
                working_dir=working_dir,
                git_dir=git_dir,
                worktree_id=worktree_id,
                ref_count=1,
                user_ids={user_id},
            )

            self._start_observer(proj)
            self._install_hooks(proj)

            self._projects[key] = proj
            norm_path = os.path.normpath(working_dir)
            self._repo_to_key[norm_path] = key

    async def untrack(
        self,
        project_id: str,
        user_id: str,
        worktree_id: Optional[str] = None,
    ) -> None:
        """Decrement ref count. Stop watching and uninstall hooks when it reaches zero."""
        key = self._make_key(project_id, worktree_id)

        async with self._lock:
            proj = self._projects.get(key)
            if not proj:
                return

            with proj.lock:
                proj.ref_count = max(0, proj.ref_count - 1)
                should_remove = proj.ref_count == 0

            if should_remove:
                self._stop_observer(proj)
                self._uninstall_hooks(proj)
                del self._projects[key]
                norm_path = os.path.normpath(proj.working_dir)
                self._repo_to_key.pop(norm_path, None)

    # -------------------------------------------------------------------
    # Unified notification (replaces _broadcast_git_update + trigger_immediate_update)
    # -------------------------------------------------------------------

    async def notify(
        self,
        project_id: str,
        categories: Set[str],
        worktree_id: Optional[str] = None,
        user_id: Optional[str] = None,
        working_dir: Optional[str] = None,
    ) -> None:
        """Notify that git state has changed. Invalidates cache and broadcasts.

        Called by API mutation endpoints after git operations complete.
        Does NOT debounce — changes are broadcast immediately.

        Args:
            project_id: The project whose state changed.
            categories: Which categories changed: {"status", "diff_stats", "branches"}
            worktree_id: Optional worktree ID.
            user_id: The user who triggered the change (always included in broadcast).
            working_dir: Working directory (used if project is not yet tracked).
        """
        key = self._make_key(project_id, worktree_id)
        proj = self._projects.get(key)

        # Build broadcast target set
        user_ids = {user_id} if user_id else set()
        resolved_dir = working_dir

        if proj:
            with proj.lock:
                user_ids.update(proj.user_ids)
            resolved_dir = proj.working_dir
            # Suppress watchdog events to prevent double-broadcast
            self._set_hook_suppression(proj, categories)
            # Invalidate cache
            self._invalidate_cache(proj, categories)

        if not user_ids or not resolved_dir:
            return

        await self._recompute_and_broadcast(
            project_id=project_id,
            worktree_id=worktree_id,
            working_dir=resolved_dir,
            categories=categories,
            user_ids=user_ids,
        )

    # -------------------------------------------------------------------
    # Hook callback handler
    # -------------------------------------------------------------------

    async def on_hook(self, hook_name: str, repo_path: str) -> None:
        """Handle a git hook callback. Bypasses debounce for immediate updates.

        Called by the internal /git-hook endpoint via asyncio.ensure_future,
        so we catch all exceptions to avoid unhandled-task-exception noise.
        """
        try:
            categories = _HOOK_CATEGORY_MAP.get(hook_name)
            if not categories:
                logger.debug(f"Unknown hook: {hook_name}")
                return

            norm_path = os.path.normpath(repo_path)
            key = self._repo_to_key.get(norm_path)
            if not key:
                logger.debug(f"Hook callback for untracked repo: {repo_path}")
                return

            proj = self._projects.get(key)
            if not proj:
                return

            # Set suppression window and invalidate cache
            self._set_hook_suppression(proj, categories)
            self._invalidate_cache(proj, categories)

            with proj.lock:
                user_ids = proj.user_ids.copy()

            if not user_ids:
                return

            logger.debug(
                f"Hook {hook_name} for project={proj.project_id}: "
                f"processing categories={categories}"
            )

            await self._recompute_and_broadcast(
                project_id=proj.project_id,
                worktree_id=proj.worktree_id,
                working_dir=proj.working_dir,
                categories=categories,
                user_ids=user_ids,
            )
        except Exception as e:
            logger.debug(f"Error processing git hook {hook_name} for {repo_path}: {e}")

    def verify_hook_token(self, token: str) -> bool:
        """Verify a token from a hook callback (timing-safe)."""
        if not self._hook_token or not token:
            return False
        return hmac.compare_digest(token, self._hook_token)

    # -------------------------------------------------------------------
    # Cached getters (replaces manual cache read/write in API endpoints)
    # -------------------------------------------------------------------

    async def get_status(
        self,
        project_id: str,
        working_dir: str,
        worktree_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get git status, returning cached data if available."""
        key = self._make_key(project_id, worktree_id)
        proj = self._projects.get(key)

        if proj and proj.cached_status is not None and not self._is_cache_expired(proj):
            return proj.cached_status

        from app.core.git_service import git_service
        status_data = await asyncio.to_thread(git_service.get_status, working_dir)

        result = {
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

        if proj:
            proj.cached_status = result
            proj.cache_ts = time.monotonic()

        return result

    async def get_diff_stats(
        self,
        project_id: str,
        working_dir: str,
        worktree_id: Optional[str] = None,
        include_unstaged: bool = True,
    ) -> Dict[str, Any]:
        """Get diff stats, returning cached data if available.

        Cache only stores the include_unstaged=True variant (the common case
        and what broadcasts produce). Requests with include_unstaged=False
        always recompute fresh.
        """
        key = self._make_key(project_id, worktree_id)
        proj = self._projects.get(key)

        # Only serve cache for the default include_unstaged=True case
        if include_unstaged and proj and proj.cached_diff_stats is not None and not self._is_cache_expired(proj):
            return proj.cached_diff_stats

        from app.core.git_service import git_service
        stats = await asyncio.to_thread(git_service.get_diff_stats, working_dir, include_unstaged=include_unstaged)

        result = {
            "additions": stats.get("additions", 0),
            "deletions": stats.get("deletions", 0),
            "files_changed": stats.get("files_changed", 0),
        }

        # Only cache the include_unstaged=True variant
        if proj and include_unstaged:
            proj.cached_diff_stats = result
            proj.cache_ts = time.monotonic()

        return result

    async def get_branches(
        self,
        project_id: str,
        working_dir: str,
        worktree_id: Optional[str] = None,
        include_remote: bool = True,
    ) -> Dict[str, Any]:
        """Get branch list, returning cached data if available."""
        key = self._make_key(project_id, worktree_id)
        proj = self._projects.get(key)

        if proj and proj.cached_branches is not None and not self._is_cache_expired(proj):
            return proj.cached_branches

        from app.core.git_service import git_service
        branches_raw, current = await asyncio.gather(
            asyncio.to_thread(git_service.list_branches, working_dir, include_remote=include_remote),
            asyncio.to_thread(git_service.get_current_branch, working_dir),
        )

        result = {
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

        if proj:
            proj.cached_branches = result
            proj.cache_ts = time.monotonic()

        return result

    # -------------------------------------------------------------------
    # Bulk cache invalidation (for init/pull that affect all worktrees)
    # -------------------------------------------------------------------

    def invalidate_project(self, project_id: str) -> None:
        """Invalidate all cache entries for a project (including all worktrees)."""
        for key, proj in self._projects.items():
            if proj.project_id == project_id:
                proj.cached_status = None
                proj.cached_diff_stats = None
                proj.cached_branches = None

    # -------------------------------------------------------------------
    # Cache helpers (private)
    # -------------------------------------------------------------------

    def _is_cache_expired(self, proj: _TrackedProject) -> bool:
        return (time.monotonic() - proj.cache_ts) > CACHE_TTL_SECONDS

    def _invalidate_cache(self, proj: _TrackedProject, categories: Set[str]) -> None:
        """Invalidate specific categories in a project's cache."""
        if "status" in categories:
            proj.cached_status = None
        if "diff_stats" in categories:
            proj.cached_diff_stats = None
        if "branches" in categories:
            proj.cached_branches = None

    # -------------------------------------------------------------------
    # Observer (watchdog) helpers (private)
    # -------------------------------------------------------------------

    def _start_observer(self, proj: _TrackedProject) -> None:
        """Start a watchdog Observer for the project's .git directory."""
        if proj.observer is not None:
            return
        try:
            observer = Observer()
            handler = _GitEventHandler(self, proj)
            observer.schedule(handler, proj.git_dir, recursive=True)
            observer.daemon = True
            observer.start()
            proj.observer = observer
            logger.debug(f"Git watcher started for project={proj.project_id} dir={proj.git_dir}")
        except Exception as e:
            logger.warning(f"Failed to start observer for {proj.project_id}: {e}")

    def _stop_observer(self, proj: _TrackedProject) -> None:
        """Stop and clean up a watchdog Observer."""
        if proj.observer is not None:
            try:
                proj.observer.stop()
                proj.observer.join(timeout=2)
            except Exception:
                pass
            proj.observer = None
            logger.debug(f"Git watcher stopped for project={proj.project_id}")

        if proj.debounce_handle is not None:
            proj.debounce_handle.cancel()
            proj.debounce_handle = None

    # -------------------------------------------------------------------
    # Hook suppression (private)
    # -------------------------------------------------------------------

    def _set_hook_suppression(self, proj: _TrackedProject, categories: Set[str]) -> None:
        """Suppress watchdog events for these categories to prevent double-broadcast."""
        until = time.monotonic() + HOOK_SUPPRESSION_SECONDS
        for cat in categories:
            proj.hook_suppressed_until[cat] = until

    # -------------------------------------------------------------------
    # Debounce (called from watchdog thread)
    # -------------------------------------------------------------------

    def _schedule_debounced_update(self, proj: _TrackedProject, categories: Set[str]) -> None:
        """Schedule a debounced update from a watchdog filesystem event.

        Called from the watchdog thread — uses call_soon_threadsafe to bridge
        into the asyncio event loop.
        """
        # Filter out categories suppressed by a recent hook/API event
        now = time.monotonic()
        unsuppressed = {
            cat for cat in categories
            if now >= proj.hook_suppressed_until.get(cat, 0)
        }
        if not unsuppressed:
            return

        with proj.lock:
            proj.pending_categories.update(unsuppressed)

        if self._loop is None or self._loop.is_closed():
            return

        self._loop.call_soon_threadsafe(self._reset_debounce, proj)

    def _reset_debounce(self, proj: _TrackedProject) -> None:
        """Reset the debounce timer (runs on event loop)."""
        if proj.debounce_handle is not None:
            proj.debounce_handle.cancel()

        if self._loop is None or self._loop.is_closed():
            return

        proj.debounce_handle = self._loop.call_later(
            DEBOUNCE_SECONDS,
            lambda: asyncio.ensure_future(self._flush_debounced(proj))
        )

    async def _flush_debounced(self, proj: _TrackedProject) -> None:
        """Flush pending debounced categories after the debounce window."""
        with proj.lock:
            categories = proj.pending_categories.copy()
            proj.pending_categories.clear()
            user_ids = proj.user_ids.copy()
            proj.debounce_handle = None

        if not categories:
            return

        self._invalidate_cache(proj, categories)

        await self._recompute_and_broadcast(
            project_id=proj.project_id,
            worktree_id=proj.worktree_id,
            working_dir=proj.working_dir,
            categories=categories,
            user_ids=user_ids,
        )

    # -------------------------------------------------------------------
    # Core convergence point
    # -------------------------------------------------------------------

    async def _recompute_and_broadcast(
        self,
        project_id: str,
        worktree_id: Optional[str],
        working_dir: str,
        categories: Set[str],
        user_ids: Set[str],
    ) -> None:
        """Recompute changed git state and broadcast via WebSocket.

        All change paths (watchdog, hooks, API mutations) converge here.
        The WebSocket message format is identical to what the frontend expects.
        """
        from app.core.git_service import git_service
        from app.core.sync_engine import sync_engine

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

    # -------------------------------------------------------------------
    # Hook installation (private, absorbed from GitHookManager)
    # -------------------------------------------------------------------

    def _install_hooks(self, proj: _TrackedProject) -> None:
        """Install managed git hooks for a tracked project.

        Chains with existing user hooks by renaming them to .user suffix.
        """
        hooks_dir = _get_hooks_dir(proj.working_dir, proj.git_dir)

        try:
            os.makedirs(hooks_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"Cannot create hooks dir {hooks_dir}: {e}")
            return

        proj.hooks_dir = hooks_dir

        for hook_name in MANAGED_HOOKS:
            hook_path = os.path.join(hooks_dir, hook_name)
            user_hook_path = hook_path + ".user"

            try:
                # Check if there's an existing hook
                if os.path.exists(hook_path):
                    if _is_managed_hook(hook_path):
                        # Already our hook — overwrite with fresh script
                        # (port/token may have changed after restart)
                        pass
                    else:
                        # User's hook — chain it
                        if not os.path.exists(user_hook_path):
                            os.rename(hook_path, user_hook_path)
                        proj.chained_hooks.add(hook_name)

                # Write our hook script
                script = _generate_hook_script(
                    hook_name=hook_name,
                    port=self._server_port,
                    token=self._hook_token,
                    hooks_dir=hooks_dir,
                )
                with open(hook_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(script)

                # Make executable on Unix
                if platform.system() != "Windows":
                    os.chmod(
                        hook_path,
                        os.stat(hook_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
                    )

                proj.installed_hooks.add(hook_name)

            except OSError as e:
                logger.warning(f"Failed to install {hook_name} hook in {hooks_dir}: {e}")

        if proj.installed_hooks:
            chained = f" (chained: {', '.join(proj.chained_hooks)})" if proj.chained_hooks else ""
            logger.debug(
                f"Installed git hooks in {hooks_dir}: "
                f"{', '.join(proj.installed_hooks)}{chained}"
            )

    def _uninstall_hooks(self, proj: _TrackedProject) -> None:
        """Uninstall managed git hooks, restoring user hooks from .user suffix."""
        if not proj.hooks_dir:
            return

        for hook_name in proj.installed_hooks.copy():
            hook_path = os.path.join(proj.hooks_dir, hook_name)
            user_hook_path = hook_path + ".user"

            try:
                if os.path.exists(hook_path) and _is_managed_hook(hook_path):
                    os.remove(hook_path)
                    if os.path.exists(user_hook_path):
                        os.rename(user_hook_path, hook_path)
            except OSError as e:
                logger.warning(f"Failed to uninstall {hook_name} hook from {proj.hooks_dir}: {e}")

        proj.installed_hooks.clear()
        logger.debug(f"Uninstalled git hooks from {proj.hooks_dir}")


# Singleton instance
git_tracker = GitTracker()
