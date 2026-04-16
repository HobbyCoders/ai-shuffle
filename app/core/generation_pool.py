"""
Generation client pool.

Keeps a pre-warmed `ClaudeSDKClient` subprocess alive to avoid the 8-15s
`claude.exe` cold-start cost paid by `generate_with_claude()` for
commit messages, PR descriptions, profile generation, and similar one-shot
AI calls.

Design constraints (per user):
- No long-held locks that block chat/other ops. The pool only serializes its
  own acquire/release; queries run outside the lock.
- No cross-call context bleed. Each pooled client is destroyed after a small
  number of uses (`MAX_USES`), not reused indefinitely.
- No ship-it pipeline held hostage by another user. If the client is busy or
  not yet warm, callers fall back to a fresh one-shot `query()`.

Lifecycle:
- `start()` spawns a warm client in the background at server boot.
- `acquire(model)` returns a client if one is ready and matches `model`; else
  returns `None` and the caller falls back.
- `release(client, destroy=False)` marks the client not-busy, increments its
  use counter, and triggers a respawn when the counter hits `MAX_USES` or
  `destroy=True` is passed (e.g., the client crashed mid-call).
- `stop()` disconnects the current client at shutdown.

This is a size-1 pool. For self-hosted single-user AI Shuffle deployments the
cost of a second idle subprocess (~150MB) is not worth it; concurrent ship-its
degrade gracefully via the one-shot fallback path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

logger = logging.getLogger(__name__)


# Tune these based on expected usage:
# - MAX_USES: ship-it fires 2 AI calls; allow 2 to cover one full pipeline
#   on a warm client, then recycle to avoid context accumulation.
# - IDLE_RECYCLE_SECONDS: if no one uses the pool for this long, recycle the
#   client anyway — stale subprocesses can accumulate issues over hours.
# - ACQUIRE_WAIT_SECONDS: how long to wait for an in-flight warmup to finish
#   before giving up and returning None (caller then falls back to one-shot).
MAX_USES = 2
IDLE_RECYCLE_SECONDS = 300.0
ACQUIRE_WAIT_SECONDS = 2.0


@dataclass
class _PooledClient:
    """Handle returned by `acquire()`. Opaque to callers."""
    client: ClaudeSDKClient
    model: str
    generation: int  # incremented on respawn so release knows if client is stale


def _build_options(model: str) -> ClaudeAgentOptions:
    """
    Options used for all pool clients.

    Note: we deliberately do NOT set `max_turns=1` the way the one-shot
    `generate_with_claude()` path does. `max_turns` may be applied at session
    scope in `ClaudeSDKClient`, which would block the second query on a
    reused client. With no tools exposed, each query naturally terminates
    after one assistant turn anyway.
    """
    return ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        # Prevent "nested session" detection when the server is started from
        # within a Claude Code session.
        env={"CLAUDECODE": ""},
    )


class GenerationPool:
    """Size-1 pool of pre-warmed `ClaudeSDKClient` instances."""

    def __init__(self) -> None:
        self._client: Optional[ClaudeSDKClient] = None
        self._client_model: Optional[str] = None
        self._uses: int = 0
        self._busy: bool = False
        self._last_used: float = 0.0
        self._generation: int = 0
        self._lock = asyncio.Lock()
        self._warmup_task: Optional[asyncio.Task[None]] = None
        self._warmup_event = asyncio.Event()
        self._started = False
        self._stopped = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin pre-warming the first client. Returns immediately."""
        if self._started:
            return
        self._started = True
        self._stopped = False
        self._kick_warmup()
        logger.info("[GenerationPool] Started; warmup kicked off in background")

    async def stop(self) -> None:
        """Disconnect the current client and cancel any in-flight warmup."""
        self._stopped = True
        if self._warmup_task and not self._warmup_task.done():
            self._warmup_task.cancel()
            try:
                await self._warmup_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._disconnect_current()
        logger.info("[GenerationPool] Stopped")

    # ------------------------------------------------------------------
    # Acquire / Release
    # ------------------------------------------------------------------

    async def acquire(self, model: str) -> Optional[_PooledClient]:
        """
        Acquire the warm client if it matches `model` and is idle.

        Returns `None` if the client is unavailable (busy, model mismatch,
        still warming, or pool stopped). Callers must fall back to a one-shot
        `query()` in that case.
        """
        if self._stopped:
            return None

        # First attempt without waiting.
        pooled = await self._try_acquire_locked(model)
        if pooled is not None:
            return pooled

        # If a warmup is in flight, wait briefly for it, then try once more.
        if self._warmup_task and not self._warmup_task.done():
            try:
                await asyncio.wait_for(
                    self._warmup_event.wait(), timeout=ACQUIRE_WAIT_SECONDS
                )
            except asyncio.TimeoutError:
                return None
            return await self._try_acquire_locked(model)

        return None

    async def _try_acquire_locked(self, model: str) -> Optional[_PooledClient]:
        async with self._lock:
            if self._stopped or self._client is None or self._busy:
                return None

            # Model mismatch → recycle with new model rather than serving
            # with wrong model. Trigger respawn, return None (fall back).
            if self._client_model != model:
                logger.info(
                    f"[GenerationPool] Model changed {self._client_model} -> {model}, "
                    f"recycling client"
                )
                await self._disconnect_current_locked()
                self._kick_warmup(model=model)
                return None

            # Idle-recycle: client sat too long, recycle.
            if self._last_used and (time.time() - self._last_used) > IDLE_RECYCLE_SECONDS:
                logger.info("[GenerationPool] Idle recycle triggered")
                await self._disconnect_current_locked()
                self._kick_warmup(model=model)
                return None

            self._busy = True
            return _PooledClient(
                client=self._client,
                model=self._client_model or model,
                generation=self._generation,
            )

    async def release(self, pooled: _PooledClient, destroy: bool = False) -> None:
        """
        Release a previously acquired client.

        If `destroy=True` (or the client has hit MAX_USES), the subprocess is
        disconnected and a respawn is kicked off in the background.
        """
        async with self._lock:
            # Stale release (client was already recycled underneath us).
            if pooled.generation != self._generation or self._client is None:
                self._busy = False
                return

            self._busy = False
            self._uses += 1
            self._last_used = time.time()

            should_recycle = destroy or self._uses >= MAX_USES
            if should_recycle:
                logger.debug(
                    f"[GenerationPool] Recycling client after {self._uses} use(s), "
                    f"destroy={destroy}"
                )
                await self._disconnect_current_locked()
                self._kick_warmup()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _kick_warmup(self, model: Optional[str] = None) -> None:
        """Schedule a background warmup task if one isn't already running."""
        if self._stopped:
            return
        if self._warmup_task and not self._warmup_task.done():
            return
        self._warmup_event.clear()
        self._warmup_task = asyncio.create_task(self._warmup(model))

    async def _warmup(self, model: Optional[str] = None) -> None:
        """Spawn and connect a fresh client. Runs in the background."""
        from app.core.profiles import get_default_ai_model

        effective_model = model or get_default_ai_model()
        logger.info(f"[GenerationPool] Warming up client (model={effective_model})")
        t0 = time.perf_counter()

        try:
            client = ClaudeSDKClient(options=_build_options(effective_model))
            await client.connect()
        except Exception as e:
            logger.warning(f"[GenerationPool] Warmup failed: {e}")
            self._warmup_event.set()  # unblock any waiters; they'll see no client
            return

        async with self._lock:
            if self._stopped:
                # Shutdown raced with warmup — tear down the brand-new client.
                try:
                    await client.disconnect()
                except Exception:
                    pass
                self._warmup_event.set()
                return
            self._client = client
            self._client_model = effective_model
            self._uses = 0
            self._last_used = time.time()
            self._generation += 1

        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            f"[GenerationPool] Client ready in {elapsed_ms:.0f}ms "
            f"(generation={self._generation})"
        )
        self._warmup_event.set()

    async def _disconnect_current(self) -> None:
        async with self._lock:
            await self._disconnect_current_locked()

    async def _disconnect_current_locked(self) -> None:
        """Caller must hold `self._lock`."""
        if self._client is None:
            return
        client = self._client
        self._client = None
        self._client_model = None
        self._uses = 0
        self._last_used = 0.0
        try:
            await client.disconnect()
        except Exception as e:
            logger.warning(f"[GenerationPool] Disconnect raised: {e}")


# Module-level singleton.
generation_pool = GenerationPool()
