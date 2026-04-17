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

Profile honoring:
- All generation calls use the Default AI profile's configuration: model,
  permission_mode, setting_sources, thinking_mode, effort, system_prompt, env,
  and extra_args. Tools/agents/MCP/hooks are always force-disabled for
  one-shot generation since they'd add cold-start overhead without benefit.
- The pool matches its cached client against a *fingerprint* of the profile
  config. Any change (model, thinking, setting_sources, etc.) forces a
  recycle + respawn so callers never get stale behavior.

Lifecycle:
- `start()` spawns a warm client in the background at server boot.
- `acquire(profile_config, model_override)` returns a client if one is ready
  and matches the request's fingerprint; else returns `None` and the caller
  falls back to a one-shot `query()`.
- `release(client, destroy=False)` marks the client not-busy, increments its
  use counter, and triggers a respawn when the counter hits `MAX_USES` or
  `destroy=True` is passed (e.g., the client crashed mid-call).
- `stop()` disconnects the current client at shutdown.

Size-1 pool. For self-hosted single-user AI Shuffle deployments the cost of a
second idle subprocess (~150MB) is not worth it; concurrent ship-its degrade
gracefully via the one-shot fallback path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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


# Thinking budget mapping — mirrors `app.core.query_options.THINKING_TOKEN_BUDGETS`.
# Replicated here to avoid importing the heavy `query_options` module (which
# pulls in MCP, agents, hooks, and provider wiring irrelevant to generation).
_THINKING_TOKEN_BUDGETS: Dict[str, int] = {
    "minimal": 4000,
    "basic": 10000,
    "medium": 20000,
    "thorough": 32000,
    "extensive": 60000,
}


# ---------------------------------------------------------------------------
# Profile -> ClaudeAgentOptions
# ---------------------------------------------------------------------------


def _resolve_setting_sources(
    profile_config: Optional[Dict[str, Any]],
) -> Optional[List[str]]:
    """
    Resolve `setting_sources` for generation calls from the Default AI profile.

    Works around an SDK quirk: `subprocess_cli.py` uses
    `if self._options.setting_sources:` to decide whether to emit
    `--setting-sources`. An empty list is falsy, so the flag is omitted and
    `claude.exe` falls back to loading ALL user/project/local settings —
    including `~/.claude/settings.json` which may carry `effortLevel: medium`.
    Applied to haiku (which doesn't support effort), the CLI takes a slow
    fallback path (12-17s vs 3-6s sonnet).

    Behavior:
    - `None`: not configured — return `None` (SDK default, loads all scopes).
    - `[]`: user explicitly disabled all sources — return `["local"]` as a
      workaround. Forces SDK to emit the flag, and `local` only loads
      `.claude/settings.local.json` in the working directory (rare), so the
      leak path (user-global `effortLevel`) is cut off.
    - non-empty list: pass through unchanged.
    """
    if not profile_config:
        return None
    sources = profile_config.get("setting_sources")
    if sources is None or not isinstance(sources, list):
        return None
    if len(sources) == 0:
        return ["local"]
    return sources


def _resolve_thinking_and_effort(
    profile_config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Map profile's `thinking_mode` + `effort` to ClaudeAgentOptions kwargs.

    Mirrors `app.core.query_options._build_thinking_config` so generation calls
    behave the same as chat sessions for extended-thinking / effort config.
    """
    result: Dict[str, Any] = {}
    if not profile_config:
        return result

    thinking_mode = profile_config.get("thinking_mode", "default")
    if thinking_mode == "disabled":
        result["thinking"] = {"type": "disabled"}
    elif thinking_mode == "adaptive":
        result["thinking"] = {"type": "adaptive"}
    elif thinking_mode in _THINKING_TOKEN_BUDGETS:
        result["thinking"] = {
            "type": "enabled",
            "budget_tokens": _THINKING_TOKEN_BUDGETS[thinking_mode],
        }
    # "default" or unrecognized: omit, let SDK use its default.

    effort = profile_config.get("effort")
    if effort in ("low", "medium", "high", "max"):
        result["effort"] = effort

    max_thinking_tokens = profile_config.get("max_thinking_tokens")
    if max_thinking_tokens:
        result["max_thinking_tokens"] = max_thinking_tokens

    return result


def _resolve_env(profile_config: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """
    Merge profile's env with the fixed generation overrides.

    Always forces `CLAUDECODE=""` to prevent "nested session" detection when
    AI Shuffle is started from within a Claude Code session. Other env vars
    from the profile pass through.
    """
    env: Dict[str, str] = {}
    if profile_config:
        profile_env = profile_config.get("env")
        if isinstance(profile_env, dict):
            env.update({str(k): str(v) for k, v in profile_env.items()})
    env["CLAUDECODE"] = ""
    return env


def build_generation_options(
    profile_config: Optional[Dict[str, Any]],
    model_override: Optional[str] = None,
    max_turns: Optional[int] = None,
) -> ClaudeAgentOptions:
    """
    Build a `ClaudeAgentOptions` for a one-shot or pooled generation call.

    Honored from the Default AI profile config:
    - `model` (unless `model_override` is supplied)
    - `permission_mode` (default `bypassPermissions`)
    - `setting_sources` (via `_resolve_setting_sources` workaround)
    - `thinking_mode` / `effort` / `max_thinking_tokens`
    - `system_prompt` (when present and non-empty)
    - `env` (merged with `CLAUDECODE=""`)
    - `extra_args`

    Always forced off for generation calls:
    - tools, agents, MCP servers, hooks, file checkpointing, partial
      messages, fork_session, continue_conversation.

    `max_turns` is passed through verbatim. The one-shot path sets 1; the pool
    intentionally leaves it unset so a reused client can serve multiple
    independent queries.
    """
    config: Dict[str, Any] = profile_config or {}

    # Model: explicit override > profile > hard fallback
    model = model_override or config.get("model") or "sonnet"

    # Permission mode: profile value or bypassPermissions
    permission_mode = config.get("permission_mode") or "bypassPermissions"

    kwargs: Dict[str, Any] = {
        "model": model,
        "permission_mode": permission_mode,
        "env": _resolve_env(config),
        "tools": [],  # Generation never uses tools.
        "include_partial_messages": False,
        "continue_conversation": False,
        "fork_session": False,
    }

    if max_turns is not None:
        kwargs["max_turns"] = max_turns

    sources = _resolve_setting_sources(config)
    if sources is not None:
        kwargs["setting_sources"] = sources

    # Thinking / effort / max_thinking_tokens — spread into options.
    kwargs.update(_resolve_thinking_and_effort(config))

    # System prompt — only forward when the user actually set one. An empty
    # string or missing field means "use SDK default" so we don't accidentally
    # pass a blank custom prompt that overrides Claude's built-in behavior.
    system_prompt = config.get("system_prompt")
    if isinstance(system_prompt, dict):
        content = system_prompt.get("content")
        if content:
            kwargs["system_prompt"] = system_prompt
    elif isinstance(system_prompt, str) and system_prompt.strip():
        kwargs["system_prompt"] = system_prompt

    # Extra args pass-through for future CLI flags the user may configure.
    extra_args = config.get("extra_args")
    if isinstance(extra_args, dict) and extra_args:
        kwargs["extra_args"] = extra_args

    return ClaudeAgentOptions(**kwargs)


def generation_fingerprint(
    profile_config: Optional[Dict[str, Any]],
    model_override: Optional[str] = None,
) -> Tuple[Any, ...]:
    """
    Stable hashable fingerprint of the config a generation call will use.

    The pool uses this to decide whether its cached client can serve a new
    request or must be recycled. Must capture every profile field that
    `build_generation_options` reads.
    """
    config: Dict[str, Any] = profile_config or {}

    model = model_override or config.get("model") or "sonnet"
    permission_mode = config.get("permission_mode") or "bypassPermissions"
    sources = _resolve_setting_sources(config)
    sources_tuple: Tuple[str, ...] = tuple(sources) if sources else ()

    thinking_cfg = _resolve_thinking_and_effort(config)
    # Serialize dicts for hashability.
    thinking_repr = json.dumps(thinking_cfg.get("thinking"), sort_keys=True) \
        if thinking_cfg.get("thinking") is not None else ""
    effort = thinking_cfg.get("effort") or ""
    max_thinking = thinking_cfg.get("max_thinking_tokens") or 0

    env = _resolve_env(config)
    env_tuple: Tuple[Tuple[str, str], ...] = tuple(sorted(env.items()))

    system_prompt = config.get("system_prompt")
    system_prompt_repr = json.dumps(system_prompt, sort_keys=True) \
        if system_prompt else ""

    extra_args = config.get("extra_args") or {}
    extra_args_tuple: Tuple[Tuple[str, Any], ...] = tuple(sorted(extra_args.items())) \
        if isinstance(extra_args, dict) else ()

    return (
        model,
        permission_mode,
        sources_tuple,
        thinking_repr,
        effort,
        max_thinking,
        env_tuple,
        system_prompt_repr,
        extra_args_tuple,
    )


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


@dataclass
class _PooledClient:
    """Handle returned by `acquire()`. Opaque to callers."""
    client: ClaudeSDKClient
    fingerprint: Tuple[Any, ...]
    generation: int  # incremented on respawn so release knows if client is stale


class GenerationPool:
    """Size-1 pool of pre-warmed `ClaudeSDKClient` instances."""

    def __init__(self) -> None:
        self._client: Optional[ClaudeSDKClient] = None
        self._client_fingerprint: Optional[Tuple[Any, ...]] = None
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

    async def acquire(
        self,
        profile_config: Optional[Dict[str, Any]],
        model_override: Optional[str] = None,
    ) -> Optional[_PooledClient]:
        """
        Acquire the warm client if its fingerprint matches the request's.

        Returns `None` if the client is unavailable (busy, fingerprint mismatch,
        still warming, idle-recycled, or pool stopped). Callers must fall back
        to a one-shot `query()` in that case.
        """
        if self._stopped:
            return None

        requested_fp = generation_fingerprint(profile_config, model_override)

        # First attempt without waiting.
        pooled = await self._try_acquire_locked(requested_fp, profile_config, model_override)
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
            return await self._try_acquire_locked(requested_fp, profile_config, model_override)

        return None

    async def _try_acquire_locked(
        self,
        requested_fp: Tuple[Any, ...],
        profile_config: Optional[Dict[str, Any]],
        model_override: Optional[str],
    ) -> Optional[_PooledClient]:
        async with self._lock:
            if self._stopped or self._client is None or self._busy:
                return None

            # Fingerprint mismatch — profile config changed since warmup.
            # Recycle with the new config and fall back to one-shot for this call.
            if self._client_fingerprint != requested_fp:
                logger.info(
                    f"[GenerationPool] Fingerprint changed, recycling client. "
                    f"old={self._client_fingerprint} new={requested_fp}"
                )
                await self._disconnect_current_locked()
                self._kick_warmup(
                    profile_config=profile_config, model_override=model_override
                )
                return None

            # Idle-recycle: client sat too long, recycle.
            if self._last_used and (time.time() - self._last_used) > IDLE_RECYCLE_SECONDS:
                logger.info("[GenerationPool] Idle recycle triggered")
                await self._disconnect_current_locked()
                self._kick_warmup(
                    profile_config=profile_config, model_override=model_override
                )
                return None

            self._busy = True
            return _PooledClient(
                client=self._client,
                fingerprint=self._client_fingerprint or requested_fp,
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

    def _kick_warmup(
        self,
        profile_config: Optional[Dict[str, Any]] = None,
        model_override: Optional[str] = None,
    ) -> None:
        """Schedule a background warmup task if one isn't already running."""
        if self._stopped:
            return
        if self._warmup_task and not self._warmup_task.done():
            return
        self._warmup_event.clear()
        self._warmup_task = asyncio.create_task(
            self._warmup(profile_config=profile_config, model_override=model_override)
        )

    async def _warmup(
        self,
        profile_config: Optional[Dict[str, Any]] = None,
        model_override: Optional[str] = None,
    ) -> None:
        """Spawn and connect a fresh client. Runs in the background."""
        from app.core.profiles import get_default_ai_profile

        # If caller didn't pin a profile_config, fetch the live Default AI.
        # Keeps pool in sync with profile edits between ship-its.
        if profile_config is None:
            profile = get_default_ai_profile()
            profile_config = (profile or {}).get("config") if profile else None

        fingerprint = generation_fingerprint(profile_config, model_override)

        logger.info(
            f"[GenerationPool] Warming up client (fingerprint={fingerprint})"
        )
        t0 = time.perf_counter()

        try:
            client = ClaudeSDKClient(
                options=build_generation_options(
                    profile_config,
                    model_override=model_override,
                    # Pool clients are long-lived; don't cap turns or reused
                    # queries can get blocked by a session-scoped turn limit.
                    max_turns=None,
                )
            )
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
            self._client_fingerprint = fingerprint
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
        self._client_fingerprint = None
        self._uses = 0
        self._last_used = 0.0
        try:
            await client.disconnect()
        except Exception as e:
            logger.warning(f"[GenerationPool] Disconnect raised: {e}")


# Module-level singleton.
generation_pool = GenerationPool()
