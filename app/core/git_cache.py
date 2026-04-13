"""
Git Result Cache

Thread-safe per-key TTL cache for expensive git operations.

Also hosts the module-level ``_native_reader`` (pygit2) singleton so that
both ``git_service.py`` and the mixin modules can import it without
circular-import issues.
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# TTL constants for various cache categories
STATUS_CACHE_TTL = 2.0            # seconds — match active poll cadence for fresh UI
DIFF_STATS_CACHE_TTL = 2.0        # seconds
DEFAULT_BRANCH_CACHE_TTL = 300.0   # 5 minutes — rarely changes
AHEAD_BEHIND_CACHE_TTL = 30.0     # seconds — only changes on commit/push/fetch

# ---------------------------------------------------------------------------
# Native git reader (pygit2) — falls back to CLI if unavailable
# ---------------------------------------------------------------------------
try:
    from app.core.git_native import GitNativeReader
    _native_reader: "GitNativeReader | None" = GitNativeReader()
    logger.info("Git read operations: using pygit2 (native)")
except ImportError:
    _native_reader = None
    logger.info("Git read operations: using CLI subprocess (pygit2 not available)")


class _GitResultCache:
    """Thread-safe per-key TTL cache for git results."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> (result, monotonic_timestamp)
        self._data: Dict[str, tuple] = {}

    def get(self, key: str, ttl: float) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry and (time.monotonic() - entry[1]) < ttl:
                return entry[0]
            return None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (value, time.monotonic())

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        """Invalidate all keys starting with *prefix*."""
        with self._lock:
            to_remove = [k for k in self._data if k.startswith(prefix)]
            for k in to_remove:
                del self._data[k]

    def purge_expired(self, max_ttl: float = 300.0) -> int:
        """Remove all entries older than max_ttl seconds. Returns count removed."""
        now = time.monotonic()
        with self._lock:
            expired = [k for k, (_, ts) in self._data.items() if (now - ts) > max_ttl]
            for k in expired:
                del self._data[k]
            return len(expired)


# Module-level singleton used by all git modules
_git_cache = _GitResultCache()
