"""
Git State Cache

In-memory cache for git state (status, diff_stats, branches) keyed by
project_id or project_id:worktree_id. Prevents redundant git subprocess
calls when the same state is requested multiple times (e.g., rapid tab switching).

Cache entries are invalidated explicitly by the git file watcher or mutating
API endpoints, and auto-expire after TTL as a safety net.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Cache entries expire after 60 seconds even without explicit invalidation
CACHE_TTL_SECONDS = 60


@dataclass
class CachedGitState:
    """Cached git state for a single project/worktree."""
    status: Optional[Dict[str, Any]] = None
    diff_stats: Optional[Dict[str, Any]] = None
    branches: Optional[list] = None
    timestamp: float = field(default_factory=time.monotonic)

    def is_expired(self) -> bool:
        return (time.monotonic() - self.timestamp) > CACHE_TTL_SECONDS

    def has(self, category: str) -> bool:
        """Check if a specific category is cached and not expired."""
        if self.is_expired():
            return False
        return getattr(self, category, None) is not None


class GitStateCache:
    """Thread-safe in-memory cache for git state."""

    def __init__(self):
        self._cache: Dict[str, CachedGitState] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def make_key(project_id: str, worktree_id: Optional[str] = None) -> str:
        return f"{project_id}:{worktree_id}" if worktree_id else project_id

    async def get(self, key: str) -> Optional[CachedGitState]:
        """Get cached state, returning None if expired or missing."""
        async with self._lock:
            entry = self._cache.get(key)
            if entry and not entry.is_expired():
                return entry
            # Clean up expired entry
            if entry:
                del self._cache[key]
            return None

    async def get_category(self, key: str, category: str) -> Optional[Any]:
        """Get a specific cached category (status, diff_stats, branches)."""
        entry = await self.get(key)
        if entry:
            return getattr(entry, category, None)
        return None

    async def set_category(self, key: str, category: str, data: Any) -> None:
        """Set a specific category in the cache, creating the entry if needed."""
        async with self._lock:
            entry = self._cache.get(key)
            if not entry or entry.is_expired():
                entry = CachedGitState()
                self._cache[key] = entry
            setattr(entry, category, data)
            entry.timestamp = time.monotonic()

    async def set_full(self, key: str, state: CachedGitState) -> None:
        """Set the full cached state for a key."""
        async with self._lock:
            self._cache[key] = state

    async def invalidate(self, key: str) -> None:
        """Invalidate a specific cache entry."""
        async with self._lock:
            self._cache.pop(key, None)

    async def invalidate_project(self, project_id: str) -> None:
        """Invalidate all cache entries for a project (including worktrees)."""
        async with self._lock:
            keys_to_remove = [
                k for k in self._cache
                if k == project_id or k.startswith(f"{project_id}:")
            ]
            for k in keys_to_remove:
                del self._cache[k]

    async def invalidate_categories(self, key: str, categories: set) -> None:
        """Invalidate specific categories within a cache entry."""
        async with self._lock:
            entry = self._cache.get(key)
            if entry:
                for cat in categories:
                    if hasattr(entry, cat):
                        setattr(entry, cat, None)

    async def clear(self) -> None:
        """Clear all cache entries."""
        async with self._lock:
            self._cache.clear()


# Singleton instance
git_state_cache = GitStateCache()
