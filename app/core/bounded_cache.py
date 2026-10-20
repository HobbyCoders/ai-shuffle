"""Thread-safe byte/count-bounded LRU for JSON-shaped cached results."""
import logging
import sys
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def estimate_size(obj: Any, _depth: int = 0) -> int:
    """Estimated retained size of a parsed-transcript structure, in bytes.

    Walks the (acyclic, JSON-shaped) message structure summing
    ``sys.getsizeof`` for scalars and containers. Approximate on purpose: it
    only has to be proportional to the real footprint so the LRU's budget is
    meaningful, and it must be cheap enough to run once per cache insert (the
    caller has just parsed the whole transcript anyway).
    """
    if _depth > 12:  # transcripts are shallow; stop pathological nesting
        return sys.getsizeof(obj)
    if isinstance(obj, str):
        return sys.getsizeof(obj)
    if isinstance(obj, (bytes, bytearray)):
        return sys.getsizeof(obj)
    if isinstance(obj, dict):
        total = sys.getsizeof(obj)
        for k, v in obj.items():
            total += estimate_size(k, _depth + 1) + estimate_size(v, _depth + 1)
        return total
    if isinstance(obj, (list, tuple, set)):
        total = sys.getsizeof(obj)
        for item in obj:
            total += estimate_size(item, _depth + 1)
        return total
    return sys.getsizeof(obj)


class ByteBoundedLRU:
    """A small, thread-safe LRU bounded by BOTH entry count and estimated bytes.

    Deliberately not an ad-hoc dict: the eviction rule has to hold on every
    write path (there are four), and "50 entries" is not a memory bound when a
    single entry can be tens of megabytes.

    * ``__setitem__`` measures the value with :func:`estimate_size`, refuses
      anything above ``max_entry_bytes`` (evicting any previous value for that
      key, so a stale entry is never left behind), and evicts least-recently-
      used entries until both budgets are satisfied.
    * ``get`` / ``__getitem__`` / ``move_to_end`` refresh recency.
    * ``lock`` is a re-entrant lock the module also exposes, so callers can
      hold it across a read-then-write sequence without deadlocking against
      the cache's own locking.
    """

    def __init__(
        self,
        max_entries: int,
        max_bytes: int,
        max_entry_bytes: Optional[int] = None,
    ) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_entry_bytes = (
            max_entry_bytes if max_entry_bytes is not None else max_bytes
        )
        self.lock = threading.RLock()
        self._data: "OrderedDict[Any, Any]" = OrderedDict()
        self._sizes: Dict[Any, int] = {}
        self._bytes = 0

    # -- introspection ---------------------------------------------------
    @property
    def current_bytes(self) -> int:
        with self.lock:
            return self._bytes

    def __len__(self) -> int:
        with self.lock:
            return len(self._data)

    def __contains__(self, key) -> bool:
        with self.lock:
            return key in self._data

    def keys(self):
        with self.lock:
            return list(self._data.keys())

    # -- reads -----------------------------------------------------------
    def get(self, key, default=None):
        with self.lock:
            if key not in self._data:
                return default
            self._data.move_to_end(key)
            return self._data[key]

    def __getitem__(self, key):
        with self.lock:
            value = self._data[key]  # KeyError propagates, dict semantics
            self._data.move_to_end(key)
            return value

    def move_to_end(self, key) -> None:
        with self.lock:
            if key in self._data:
                self._data.move_to_end(key)

    # -- writes ----------------------------------------------------------
    def __setitem__(self, key, value) -> None:
        self.put(key, value)

    def put(self, key, value, size: Optional[int] = None) -> bool:
        """Insert ``value``; returns False when it was too big to cache."""
        if size is None:
            size = estimate_size(value)
        with self.lock:
            self._discard(key)
            if size > self.max_entry_bytes:
                logger.debug(
                    "Parse cache: entry %r (%d bytes) exceeds the per-entry "
                    "budget (%d) — not cached",
                    key, size, self.max_entry_bytes,
                )
                return False
            self._data[key] = value
            self._sizes[key] = size
            self._bytes += size
            self._evict()
            return True

    def pop(self, key, default=None):
        with self.lock:
            if key not in self._data:
                return default
            value = self._data[key]
            self._discard(key)
            return value

    def __delitem__(self, key) -> None:
        with self.lock:
            if key not in self._data:
                raise KeyError(key)
            self._discard(key)

    def clear(self) -> None:
        with self.lock:
            self._data.clear()
            self._sizes.clear()
            self._bytes = 0

    # -- internals (call with the lock held) -----------------------------
    def _discard(self, key) -> None:
        if key in self._data:
            self._bytes -= self._sizes.pop(key, 0)
            del self._data[key]

    def _evict(self) -> None:
        while self._data and (
            len(self._data) > self.max_entries or self._bytes > self.max_bytes
        ):
            oldest, _ = next(iter(self._data.items()))
            self._discard(oldest)

