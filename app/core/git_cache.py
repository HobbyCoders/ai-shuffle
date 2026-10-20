"""
Git Result Cache

Thread-safe per-key TTL cache for expensive git operations.

Also hosts the module-level ``_native_reader`` (pygit2) singleton so that
both ``git_service.py`` and the mixin modules can import it without
circular-import issues.
"""

import logging
import time
from typing import Any, Optional
from collections import OrderedDict

from app.core.bounded_cache import ByteBoundedLRU, estimate_size

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
    """Separate bulk results, long-lived markers and short-lived large results."""

    def __init__(self) -> None:
        self._data = ByteBoundedLRU(256, 8 * 1024 * 1024, 2 * 1024 * 1024)
        self._markers = ByteBoundedLRU(256, 1024 * 1024)
        # Large repos must not rerun their most expensive walk on every caller.
        # Four per-key slots are an explicit count-bounded exception to the byte
        # budget; each expires after two seconds, swept on every cache access.
        self._oversized = OrderedDict()
        self._oversized_sizes = {}
        self._lock = self._data.lock

    def _expire_oversized(self, now):
        for key, (_, created) in list(self._oversized.items()):
            if now - created >= STATUS_CACHE_TTL:
                self._oversized.pop(key)
                self._oversized_sizes.pop(key)

    def get(self, key: str, ttl: float) -> Optional[Any]:
        with self._lock:
            now = time.monotonic()
            self._expire_oversized(now)
            for bucket in (self._oversized, self._data, self._markers):
                entry = bucket.get(key)
                if entry is not None:
                    if now - entry[1] < ttl:
                        return entry[0]
                    self.invalidate(key)
            return None

    def put(self, key: str, value: Any) -> None:
        entry = (value, time.monotonic())
        size = estimate_size(entry)
        with self._lock:
            self._expire_oversized(entry[1])
            self.invalidate(key)
            if not key.startswith(("status:", "diff_stats:", "per_file_diff:")):
                self._markers.put(key, entry, size=size)
            elif size > self._data.max_entry_bytes:
                self._oversized[key] = entry
                self._oversized_sizes[key] = size
                while len(self._oversized) > 4:
                    oldest, _ = self._oversized.popitem(last=False)
                    self._oversized_sizes.pop(oldest)
            else:
                self._data.put(key, entry, size=size)

    def invalidate(self, key: str) -> None:
        with self._lock:
            for bucket in (self._data, self._markers, self._oversized):
                bucket.pop(key, None)
            self._oversized_sizes.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        with self._lock:
            for bucket in (self._data, self._markers, self._oversized):
                for key in list(bucket.keys()):
                    if key.startswith(prefix):
                        self.invalidate(key)

    def purge_expired(self, max_ttl: float = 300.0) -> int:
        now = time.monotonic()
        removed = 0
        with self._lock:
            for bucket in (self._data, self._markers, self._oversized):
                ttl = min(max_ttl, STATUS_CACHE_TTL) if bucket is self._oversized else max_ttl
                for key in list(bucket.keys()):
                    if now - bucket[key][1] >= ttl:
                        self.invalidate(key)
                        removed += 1
        return removed

    def memory_stats(self) -> dict:
        with self._lock:
            return {
                "git_cache_entries": len(self._data) + len(self._markers) + len(self._oversized),
                "git_cache_estimated_bytes": (self._data.current_bytes + self._markers.current_bytes
                                              + sum(self._oversized_sizes.values())),
                "git_cache_oversized_entries": len(self._oversized),
                "git_cache_oversized_estimated_bytes": sum(self._oversized_sizes.values()),
            }


# Module-level singleton used by all git modules
_git_cache = _GitResultCache()
