"""Small TTL cache primitive shared across the app.

Extracted from ``app.core.github_service`` so that caching behaviour is not
owned by one service module. Used by the GitHub auth probe, the PR-state
cache and the working-directory resolver.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, List, Optional


# Public "no fresh entry" sentinel: cached values may legitimately be None or
# False, so a distinct object is the only safe miss signal.
@dataclass
class _CacheEntry:
    """One cached value plus the wall-clock bookkeeping for expiry/staleness."""
    value: Any
    stored_at: float
    expires_at: float

    def is_fresh(self, now: Optional[float] = None) -> bool:
        return (time.monotonic() if now is None else now) < self.expires_at

    def age(self, now: Optional[float] = None) -> float:
        return (time.monotonic() if now is None else now) - self.stored_at


CACHE_MISS = object()


class TTLCache:
    """Bounded, thread-safe TTL cache with explicit invalidation.

    Deliberately small, but with the three properties the naive
    ``dict``-with-a-timestamp pattern lacks:

    * **Bounded** — ``max_entries`` with expired-first, then oldest-first
      eviction, so a per-path/per-branch key space cannot grow without limit.
    * **Thread-safe** — every read and write is under a lock, because these
      caches are populated from ``asyncio.to_thread`` workers as well as from
      the event loop.
    * **Stale-readable** — :meth:`peek` returns an *expired* entry so callers
      can implement stale-while-revalidate (serve the last known-good value,
      flagged, instead of inventing a negative answer) when a refresh fails.

    Values are stored as-is; store immutable values (or copies) if the caller
    might mutate them.
    """

    __slots__ = ("_ttl", "_max_entries", "_name", "_lock", "_entries")

    def __init__(self, ttl: float, max_entries: int = 256, name: str = "") -> None:
        if ttl <= 0:
            raise ValueError("ttl must be > 0")
        if max_entries <= 0:
            raise ValueError("max_entries must be > 0")
        self._ttl = float(ttl)
        self._max_entries = int(max_entries)
        self._name = name
        self._lock = threading.RLock()
        self._entries: "OrderedDict[Any, _CacheEntry]" = OrderedDict()

    # -- reads ---------------------------------------------------------------

    def get(self, key: Any, default: Any = None) -> Any:
        """Return the fresh value for *key*, or *default* on miss/expiry."""
        entry = self._get_fresh_entry(key)
        return default if entry is None else entry.value

    def get_or_miss(self, key: Any) -> Any:
        """Return the fresh value, or the ``CACHE_MISS`` sentinel.

        Needed when ``None``/``False`` are legitimate cached values.
        """
        entry = self._get_fresh_entry(key)
        return CACHE_MISS if entry is None else entry.value

    def peek(self, key: Any) -> Optional[_CacheEntry]:
        """Return the entry for *key* even if expired (None if absent)."""
        with self._lock:
            return self._entries.get(key)

    def __contains__(self, key: Any) -> bool:
        return self._get_fresh_entry(key) is not None

    def __len__(self) -> int:
        """Number of *fresh* entries.

        Deliberately non-destructive: expired entries are retained (they are
        what :meth:`peek` serves during stale-while-revalidate) and are dropped
        only by eviction or explicit invalidation.
        """
        now = time.monotonic()
        with self._lock:
            return sum(1 for e in self._entries.values() if e.is_fresh(now))

    def keys(self) -> List[Any]:
        """Snapshot of all stored keys, including expired ones."""
        with self._lock:
            return list(self._entries.keys())

    def _get_fresh_entry(self, key: Any) -> Optional[_CacheEntry]:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if not entry.is_fresh(now):
                # Keep the value around for stale-while-revalidate peeks, but
                # move it to the front of the eviction queue.
                self._entries.move_to_end(key, last=False)
                return None
            return entry

    # -- writes --------------------------------------------------------------

    def set(self, key: Any, value: Any, ttl: Optional[float] = None) -> None:
        """Store *value* under *key* for *ttl* seconds (default: the cache TTL)."""
        now = time.monotonic()
        span = self._ttl if ttl is None else float(ttl)
        with self._lock:
            self._entries[key] = _CacheEntry(
                value=value, stored_at=now, expires_at=now + span
            )
            self._entries.move_to_end(key, last=True)
            self._evict_if_needed()

    def invalidate(self, key: Any = None) -> None:
        """Drop *key*, or everything when *key* is None."""
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)

    def invalidate_where(self, predicate: Callable[[Any], bool]) -> int:
        """Drop every key for which *predicate* is true. Returns the count."""
        with self._lock:
            doomed = [k for k in self._entries if predicate(k)]
            for k in doomed:
                self._entries.pop(k, None)
            return len(doomed)

    clear = invalidate

    # -- internals -----------------------------------------------------------

    def _purge_expired(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        for key in [k for k, e in self._entries.items() if not e.is_fresh(now)]:
            self._entries.pop(key, None)

    def _evict_if_needed(self) -> None:
        if len(self._entries) <= self._max_entries:
            return
        self._purge_expired()
        while len(self._entries) > self._max_entries:
            # OrderedDict is kept in insertion/refresh order: oldest first.
            self._entries.popitem(last=False)
