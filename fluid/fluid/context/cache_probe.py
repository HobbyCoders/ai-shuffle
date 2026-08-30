"""Prompt-cache miss instrumentation — naming the block that broke the prefix.

A provider prompt cache hits only while the request prefix is byte-identical to
the previous one. When it isn't, the bill jumps (fresh input instead of cached
read) and the transcript gives no clue *which* part of the prompt moved: a
mutated system section, a tool schema that appeared or vanished, or some
rewritten message deep in history all look the same from the outside.

This module fingerprints the prefix of every request with a handful of short
hashes and, when a call reports a large uncached prompt, bisects the previous
call's fingerprint against this one to name the segment that changed.

Two properties make the fingerprints comparable across calls:

* **Absolute checkpoints, not fractions.** ``m8`` always means "the first 8
  messages", so it stays byte-stable as history grows. Checkpoints at powers of
  two (plus the full prefix) give O(log n) entries and localize a mutation to
  the half-open message range ``(prev_checkpoint, checkpoint]``.
* **Cumulative.** Each ``mK`` folds in everything before it, so the FIRST
  differing checkpoint is the mutation, and every later difference is its echo.

Cost: one pass over the text already in memory, hashed with blake2b at 8 bytes
per digest (~16 hex chars). Nothing is re-serialized that the provider
serializer would not serialize anyway, and the whole payload attached to a
``Usage`` event is a dozen short strings.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fluid.messages import Message, ToolResultBlock

__all__ = [
    "CacheProbeConfig",
    "fingerprint_prefix",
    "diagnose_cache_miss",
    "checkpoint_positions",
]

_DIGEST_BYTES = 8  # 16 hex chars — collision-irrelevant for a diff signal

SYSTEM_SEGMENT = "system"
TOOLS_SEGMENT = "tools"


@dataclass(frozen=True, kw_only=True)
class CacheProbeConfig:
    """Knobs for the miss diagnostic. Defaults match the audit heuristic:
    a prompt over 10k fresh input tokens that read (almost) nothing from cache
    is a real miss worth naming; anything smaller is noise."""

    enabled: bool = True
    # Minimum fresh (uncached) input tokens before a miss is worth reporting.
    min_input_tokens: int = 10_000
    # Treat a small structural cache read as no effective cache reuse; providers can return such a read even on
    # a full resend.
    max_cache_read_tokens: int = 1_024


def _hasher() -> Any:
    return hashlib.blake2b(digest_size=_DIGEST_BYTES)


def _hash_text(text: str) -> str:
    h = _hasher()
    h.update(text.encode("utf-8", "replace"))
    return h.hexdigest()


def checkpoint_positions(count: int) -> list[int]:
    """Message counts to snapshot: 1, 2, 4, 8 … plus ``count`` itself.

    Absolute (not relative to ``count``) so the same key means the same prefix
    on every call, which is the whole point — a fraction-based checkpoint would
    cover different messages each turn and always look "changed"."""
    if count <= 0:
        return []
    positions: list[int] = []
    k = 1
    while k < count:
        positions.append(k)
        k *= 2
    positions.append(count)
    return positions


def _feed_block(h: Any, block: Any) -> None:
    """Fold one content block's cache-relevant bytes into ``h``.

    Streaming rather than concatenating: the text is already in memory and
    blake2b is far cheaper than building a joined string per call.
    """
    h.update(type(block).__name__.encode())
    text = getattr(block, "text", None)
    if isinstance(text, str):
        h.update(text.encode("utf-8", "replace"))
    input_ = getattr(block, "input", None)
    if isinstance(input_, dict) and input_:
        try:
            h.update(json.dumps(input_, default=str, sort_keys=True).encode())
        except (TypeError, ValueError):  # pragma: no cover — defensive
            h.update(b"<unserializable>")
    for attr in ("id", "tool_use_id", "media_type", "signature",
                 "encrypted_content", "origin"):
        value = getattr(block, attr, None)
        if isinstance(value, str):
            h.update(value.encode())
    data = getattr(block, "data", None)
    if isinstance(data, str):
        # Image payloads are large and immutable; their length plus a short
        # digest of the head is enough to notice a swap without hashing MBs.
        h.update(f"img:{len(data)}:{data[:64]}".encode())
    if isinstance(block, ToolResultBlock):
        h.update(b"err" if block.is_error else b"ok")
        for inner in block.content or ():
            _feed_block(h, inner)


def fingerprint_prefix(
    *,
    system: str | None,
    tools: Sequence[dict[str, Any]] | None,
    messages: Sequence[Message],
) -> dict[str, str]:
    """Short stable hashes of the request prefix, in prefix order.

    Keys: ``system``, ``tools``, then ``m1``/``m2``/``m4``… ``m<len>`` — each
    ``mK`` covering the first K messages cumulatively. Dict order IS prefix
    order, which is what :func:`diagnose_cache_miss` walks.
    """
    out: dict[str, str] = {
        SYSTEM_SEGMENT: _hash_text(system or ""),
    }
    try:
        tools_text = json.dumps(list(tools or ()), default=str, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover — defensive
        tools_text = "<unserializable>"
    out[TOOLS_SEGMENT] = _hash_text(tools_text)

    wanted = set(checkpoint_positions(len(messages)))
    if not wanted:
        return out
    running = _hasher()
    for index, message in enumerate(messages, start=1):
        running.update(message.role.encode())
        for block in message.content:
            _feed_block(running, block)
        if index in wanted:
            out[f"m{index}"] = running.hexdigest()
    return out


def _segment_span(
    key: str, keys: Sequence[str]
) -> tuple[int, int] | None:
    """The half-open message range ``(lo, hi]`` a checkpoint key covers."""
    if not key.startswith("m"):
        return None
    try:
        hi = int(key[1:])
    except ValueError:  # pragma: no cover — defensive
        return None
    lo = 0
    for other in keys:
        if other == key or not other.startswith("m"):
            continue
        try:
            value = int(other[1:])
        except ValueError:  # pragma: no cover — defensive
            continue
        if value < hi:
            lo = max(lo, value)
    return lo, hi


def diagnose_cache_miss(
    *,
    previous: dict[str, str] | None,
    current: dict[str, str],
    input_tokens: int,
    cache_read_tokens: int,
    config: CacheProbeConfig | None = None,
) -> dict[str, Any] | None:
    """Name the first prefix segment that changed, when a call missed cache.

    Returns ``None`` unless the call actually looks like a miss worth naming
    (big fresh prompt, ~no cache read) — accounting noise stays out of the
    event log. ``changed`` is the earliest differing segment; everything after
    it is a consequence, not evidence.
    """
    config = config or CacheProbeConfig()
    if not config.enabled:
        return None
    if input_tokens < config.min_input_tokens:
        return None
    if cache_read_tokens > config.max_cache_read_tokens:
        return None
    if previous is None:
        return {
            "reason": "no_previous_request",
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read_tokens,
        }

    shared = [k for k in current if k in previous]
    changed: str | None = None
    stable_through: str | None = None
    for key in shared:
        if previous[key] != current[key]:
            changed = key
            break
        stable_through = key

    if changed is None:
        # Prefix is byte-identical as far as both fingerprints reach. Either
        # history only grew (append-only, cache SHOULD have hit) or the miss is
        # provider-side: expiry, a different upstream replica, no breakpoint.
        grew = len(current) > len(shared)
        return {
            "reason": "prefix_grew_only" if grew else "prefix_unchanged",
            "stable_through": stable_through,
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read_tokens,
            "note": (
                "no prefix segment changed — suspect cache expiry, provider "
                "routing, or a missing cache breakpoint"
            ),
        }

    out: dict[str, Any] = {
        "reason": "prefix_changed",
        "changed": changed,
        "stable_through": stable_through,
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read_tokens,
    }
    span = _segment_span(changed, shared)
    if span is not None:
        lo, hi = span
        out["message_range"] = [lo, hi]
        out["note"] = (
            f"mutation is in message {lo + 1}..{hi} of the rendered history"
        )
    else:
        out["note"] = f"the {changed} section of the prompt changed"
    return out
