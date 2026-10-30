"""Serialize account operations for a provider without coupling them to billing."""

import asyncio
from functools import lru_cache


@lru_cache(maxsize=None)
def operation_lock(provider_id: str) -> asyncio.Lock:
    return asyncio.Lock()
