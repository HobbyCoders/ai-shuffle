"""Reclaim glibc's free arenas after a large, cold transcript hydration.

This is not garbage collection or a cache eviction: only already-free pages
are returned. Never run on ordinary turns, and cap attempts process-wide.
"""
from __future__ import annotations

import asyncio
import ctypes
import sys
import threading
import time

_TRIM_INTERVAL_SECONDS = 60.0
_last_trim: float | None = None
_trim_lock = threading.Lock()


def _trim_glibc() -> bool:
    try:
        libc = ctypes.CDLL(None)
        # Linux alone is not enough: musl and other libcs are a no-op.
        getattr(libc, "gnu_get_libc_version")
        trim = libc.malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return bool(trim(0))
    except (AttributeError, OSError):
        return False


async def release_cold_resume_memory() -> bool:
    """Best-effort, off-loop trim, at most once a minute on Linux/glibc."""
    global _last_trim
    if sys.platform != "linux":
        return False
    now = time.monotonic()
    with _trim_lock:
        if _last_trim is not None and now - _last_trim < _TRIM_INTERVAL_SECONDS:
            return False
        _last_trim = now
    return await asyncio.to_thread(_trim_glibc)
