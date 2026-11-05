"""Return already-free glibc heap pages after bursts, without evicting live data.

The cold-resume hint and the background sweep share one process-wide budget.
All allocator inspection/trim work runs off-loop; other platforms are no-ops.
"""
from __future__ import annotations

import asyncio
import ctypes
from functools import lru_cache
import logging
import sys
import threading
import time

import psutil

logger = logging.getLogger(__name__)
# A controlled 128 MiB hydration took ~42 ms to trim. At most one attempt per
# minute bounds allocator contention; an unchanged/mostly occupied heap skips it.
_TRIM_INTERVAL_SECONDS = 60.0
_MIN_FREE_BYTES = 32 * 1024 * 1024
_last_check: float | None = None
_last_trim_state: tuple[int, int] | None = None
_trim_lock = threading.Lock()


class _Mallinfo2(ctypes.Structure):
    _fields_ = [(name, ctypes.c_size_t) for name in (
        "arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
        "fsmblks", "uordblks", "fordblks", "keepcost",
    )]


@lru_cache(maxsize=1)
def _glibc_functions():
    try:
        libc = ctypes.CDLL(None)
        # Linux alone is not enough: musl and older glibc are safe no-ops.
        getattr(libc, "gnu_get_libc_version")
        info = libc.mallinfo2
        info.argtypes = []
        info.restype = _Mallinfo2
        trim = libc.malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return info, trim
    except (AttributeError, OSError):
        return None


def _trim_glibc() -> bool:
    global _last_check, _last_trim_state
    # Keep the lock IN the worker: cancellation cannot release it while libc
    # still runs. Another caller never queues behind an in-progress trim.
    if not _trim_lock.acquire(blocking=False):
        return False
    try:
        now = time.monotonic()
        if _last_check is not None and now - _last_check < _TRIM_INTERVAL_SECONDS:
            return False
        _last_check = now
        functions = _glibc_functions()
        if functions is None:
            return False
        info, trim = functions
        free = info().fordblks
        if free < _MIN_FREE_BYTES:
            return False
        process = psutil.Process()
        before = process.memory_info().rss
        if _last_trim_state is not None:
            rss, previous_free = _last_trim_state
            # madvise returns pages, not arena address space: fordblks can
            # stay high after a successful trim. Retry only after meaningful
            # RSS/heap change (in either direction, including a smaller live set).
            if max(abs(before - rss), abs(free - previous_free)) < _MIN_FREE_BYTES:
                return False
        started = time.monotonic()
        result = bool(trim(0))
        elapsed_ms = (time.monotonic() - started) * 1000
        after = process.memory_info().rss
        _last_trim_state = (after, info().fordblks)
        # Observed RSS decrease, not a count of live Python objects. Concurrent
        # allocations may offset it; never log transcript or session content.
        logger.info(
            "[MemoryReclaim] reclaimed_bytes=%d rss_before_bytes=%d "
            "rss_after_bytes=%d free_heap_bytes=%d trim_ms=%.1f",
            max(0, before - after), before, after, free, elapsed_ms,
        )
        return result
    finally:
        _trim_lock.release()


async def release_unused_memory() -> bool:
    """Best-effort off-loop reclaim, bounded process-wide, with no cache eviction."""
    if sys.platform != "linux":
        return False
    try:
        return await asyncio.to_thread(_trim_glibc)
    except (OSError, psutil.Error):
        logger.debug("Heap reclamation unavailable", exc_info=True)
        return False


async def release_cold_resume_memory() -> bool:
    """Retain the cold-resume hint; it shares the general sweep's budget."""
    return await release_unused_memory()


async def maintain_heap_memory() -> None:
    """Reclaim after ANY burst, including reads/exports that never resume a chat."""
    if sys.platform != "linux":
        return
    while True:
        await asyncio.sleep(_TRIM_INTERVAL_SECONDS)
        await release_unused_memory()
