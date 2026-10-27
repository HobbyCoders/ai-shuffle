"""Idle-expiry policy for prompt caches; never changes a provider request.

None means idle time alone is not evidence of a cold cache. Prefix changes and
measured misses remain independent signals. Unknown routes retain the old 5m
heuristic; callers without a cache may suppress it entirely.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

DEFAULT_COLD_GAP_SECONDS = 300.0


def is_real_main_call(usage: Mapping[str, Any]) -> bool:
    """Snapshots (zero prompt) and auxiliary calls cannot refresh main-cache age."""
    return not usage.get("auxiliary_source") and sum(
        value for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        if isinstance(value := usage.get(key), (int, float)) and not isinstance(value, bool)
    ) > 0


def last_real_main_usage(usages: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Newest-first usage records, scanning past zero-prompt snapshots."""
    return next((usage for usage in usages if is_real_main_call(usage)), None)


def delegated_cache_write_ttl(usage: Mapping[str, Any] | None = None) -> Literal["1h", "5m"]:
    """Only the last REAL main call's 5m-only writes override the CLI default."""
    if usage and is_real_main_call(usage):
        creation = usage.get("cache_creation")
        if isinstance(creation, Mapping):
            five = creation.get("ephemeral_5m_input_tokens", 0)
            hour = creation.get("ephemeral_1h_input_tokens", 0)
            if isinstance(five, (int, float)) and five > 0 and not hour:
                return "5m"
    return "1h"


def resolve_cache_ttl(
    leg: str | None, *, provider_type: str | None = None,
    base_url: str | None = None, model: str | None = None,
    auth_mode: str | None = None, last_usage: Mapping[str, Any] | None = None,
) -> float | None:
    """Effective cold gap in seconds for the route, not an opt-in write TTL."""
    kind = (leg or "").lower()
    ptype = (provider_type or "").lower()
    mode = (auth_mode or "").lower()
    url = urlsplit(base_url or "")
    host = (url.hostname or "").lower()
    if kind == "delegated":
        # Claude Code, "Which TTL each request gets": main conversation 1h
        # within plan usage; usage credits/API key 5m.
        # https://code.claude.com/docs/en/prompt-caching
        # Measured 2026-10-27: 115,210/115,258 delegated main calls wrote 1h;
        # hit rate 98% at 30–60 min idle, 4% at 1–2h (NOT a 2h lifetime).
        return 300.0 if delegated_cache_write_ttl(last_usage) == "5m" else 3600.0
    if kind == "anthropic":
        # https://platform.claude.com/docs/en/build-with-claude/prompt-caching
        # We send cache_control ephemeral WITHOUT ttl: default 5m, not opt-in 1h.
        return 300.0
    if kind == "openai_responses":
        if (host == "chatgpt.com" and url.path.startswith("/backend-api/codex")) or (
            ptype == "chatgpt" or mode == "chatgpt"
        ):
            # Tibo (OpenAI), 2026-10-20: logged-in Codex/Work users "keep cache
            # forever … guaranteed to hit the cache". Since Oct 20: 7/12 calls
            # after >=1h idle hit >=90%, longest 163 min; no 12h+ samples.
            # Owner policy: no idle expiry; this is NOT a promise of no misses.
            return None
        if host == "api.x.ai" or (not base_url and ptype in ("xai", "grok")) or mode == "grok":
            # https://docs.x.ai/docs/guides/prompt-caching (best practices)
            # No guaranteed lifetime; 5m is a heuristic. Our Responses requests
            # already send prompt_cache_key for routing affinity.
            return 300.0
        if host == "api.openai.com" or (not base_url and ptype == "openai"):
            # https://platform.openai.com/docs/guides/prompt-caching
            # GPT-5.6+ default: >=30 min; older default 24h retention is
            # "typically ~30 min". Use 30 min for ALL API models, no wire opt-in.
            return 1800.0
    if kind == "openrouter":
        # https://openrouter.ai/docs/guides/best-practices/prompt-caching
        # Sticky routing expires after 10 min idle, even if upstream lives longer.
        upstream = 1800.0 if (model or "").lower().startswith("openai/") else 300.0
        # anthropic/ and all other upstreams use the default 5m heuristic.
        return min(upstream, 600.0)
    return DEFAULT_COLD_GAP_SECONDS
