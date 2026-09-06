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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fluid.messages import Message, ToolResultBlock

__all__ = [
    "CacheProbeConfig",
    "fingerprint_prefix",
    "diagnose_cache_miss",
    "is_unexplained_miss",
    "checkpoint_positions",
    "message_count",
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
    # Consecutive UNEXPLAINED misses (verified-identical prefix, zero cache
    # read, no compaction in between) inside one turn before the engine raises
    # a CacheMissAlert. One is indistinguishable from a TTL expiry; two in a
    # row is a pattern. 0 disables the alert.
    alert_after_misses: int = 2


def _hasher() -> Any:
    return hashlib.blake2b(digest_size=_DIGEST_BYTES)


def _hash_text(text: str) -> str:
    h = _hasher()
    h.update(text.encode("utf-8", "replace"))
    return h.hexdigest()


def checkpoint_positions(
    count: int, extra: Sequence[int] = ()
) -> list[int]:
    """Message counts to snapshot: 1, 2, 4, 8 … plus ``count`` itself.

    Absolute (not relative to ``count``) so the same key means the same prefix
    on every call, which is the whole point — a fraction-based checkpoint would
    cover different messages each turn and always look "changed".

    ``extra`` adds further absolute positions — the caller passes the PREVIOUS
    request's message count, so the two fingerprints share a checkpoint at the
    end of their common prefix. Without it, 63 messages snapshot at m1…m32 and
    m63, the next call at m1…m64 and m70, and messages 33-63 are never
    compared: the diagnosis reports "stable through m32" and calls a mutation
    at message 40 an unchanged prefix.
    """
    if count <= 0:
        return []
    positions: set[int] = set()
    k = 1
    while k < count:
        positions.add(k)
        k *= 2
    positions.add(count)
    for value in extra:
        if 0 < value <= count:
            positions.add(int(value))
    return sorted(positions)


def message_count(fingerprint: Mapping[str, str] | None) -> int:
    """The number of messages a fingerprint covers — its largest ``mK``.

    Lets the engine ask the previous fingerprint how long the previous
    request was without keeping a parallel counter (and so it survives being
    rehydrated from a persisted ``Usage`` event).
    """
    best = 0
    for key in fingerprint or ():
        if not key.startswith("m"):
            continue
        try:
            best = max(best, int(key[1:]))
        except ValueError:  # pragma: no cover — defensive
            continue
    return best


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
    extra_checkpoints: Sequence[int] = (),
) -> dict[str, str]:
    """Short stable hashes of the request prefix, in prefix order.

    Keys: ``system``, ``tools``, then ``m1``/``m2``/``m4``… ``m<len>`` — each
    ``mK`` covering the first K messages cumulatively. Dict order IS prefix
    order, which is what :func:`diagnose_cache_miss` walks, so the message
    keys are always emitted in ascending order however ``extra_checkpoints``
    interleaves with the powers of two.
    """
    out: dict[str, str] = {
        SYSTEM_SEGMENT: _hash_text(system or ""),
    }
    try:
        tools_text = json.dumps(list(tools or ()), default=str, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover — defensive
        tools_text = "<unserializable>"
    out[TOOLS_SEGMENT] = _hash_text(tools_text)

    wanted = set(checkpoint_positions(len(messages), extra_checkpoints))
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


def _rewrite_phrase(rewrites: Sequence[Mapping[str, Any]]) -> str:
    """Human phrase naming the rungs applied, for the diagnosis note."""
    parts = []
    for rewrite in rewrites:
        tier = rewrite.get("tier")
        reason = rewrite.get("reason") or "trigger"
        parts.append(f"tier-{tier} {reason}" if tier else str(reason))
    joined = ", ".join(parts)
    subject = "compactions were" if len(parts) > 1 else "a compaction was"
    return f"{subject} applied ({joined})"


def diagnose_cache_miss(
    *,
    previous: dict[str, str] | None,
    current: dict[str, str],
    input_tokens: int,
    cache_read_tokens: int,
    config: CacheProbeConfig | None = None,
    history_rewrites: Sequence[Mapping[str, Any]] | None = None,
    explicit_breakpoints: bool = True,
) -> dict[str, Any] | None:
    """Name the first prefix segment that changed, when a call missed cache.

    Returns ``None`` unless the call actually looks like a miss worth naming
    (big fresh prompt, ~no cache read) — accounting noise stays out of the
    event log. ``changed`` is the earliest differing segment; everything after
    it is a consequence, not evidence.

    ``history_rewrites`` are the compaction/trim rungs applied between the
    previous request and this one (see
    :func:`fluid.context.compaction.describe_promotions`). A rung rewrites a
    message the provider already cached, so a miss right after one is fluid's
    own doing — naming it beats sending the reader after provider routing.

    ``explicit_breakpoints`` is the provider's
    :attr:`~fluid.providers.base.ProviderCaps.explicit_cache_breakpoints`.
    On a provider that has no breakpoint API at all (OpenAI Responses caches
    automatically), "a missing cache breakpoint" is not one of the suspects,
    and every audit that chased it was chasing a request field that does not
    exist.
    """
    config = config or CacheProbeConfig()
    if not config.enabled:
        return None
    if input_tokens < config.min_input_tokens:
        return None
    if cache_read_tokens > config.max_cache_read_tokens:
        return None
    rewrites = [dict(r) for r in history_rewrites or ()]
    if previous is None:
        first: dict[str, Any] = {
            "reason": "no_previous_request",
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read_tokens,
        }
        if rewrites:
            first["history_rewritten"] = rewrites
        return first

    shared = [k for k in current if k in previous]
    changed: str | None = None
    stable_through: str | None = None
    for key in shared:
        if previous[key] != current[key]:
            changed = key
            break
        stable_through = key

    # How much of the common message prefix the checkpoints actually compared.
    # ``shared_messages`` is min(len_prev, len_cur); ``verified_messages`` is
    # the largest checkpoint proven identical. They match whenever the two
    # fingerprints share a checkpoint at the end of the common prefix — which
    # is what passing the previous request's message count arranges. When they
    # do not, the gap is stated rather than papered over.
    shared_messages = min(message_count(previous), message_count(current))
    verified_messages = 0
    if stable_through and stable_through.startswith("m"):
        try:
            verified_messages = int(stable_through[1:])
        except ValueError:  # pragma: no cover — defensive
            verified_messages = 0
    full_coverage = shared_messages > 0 and verified_messages >= shared_messages

    if changed is None:
        # Prefix is byte-identical as far as both fingerprints reach. Either
        # history only grew (append-only, cache SHOULD have hit) or the miss is
        # provider-side: expiry, a different upstream replica, no breakpoint.
        grew = message_count(current) > shared_messages
        out = {
            "reason": "prefix_grew_only" if grew else "prefix_unchanged",
            "stable_through": stable_through,
            "shared_messages": shared_messages,
            "verified_messages": verified_messages,
            "full_coverage": full_coverage,
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read_tokens,
        }
        suspects = (
            "cache expiry, provider routing, or a missing cache breakpoint"
            if explicit_breakpoints
            else (
                "cache expiry or provider routing (this provider caches "
                "automatically — there is no breakpoint to place)"
            )
        )
        if full_coverage:
            note = (
                f"every shared checkpoint matched and they cover the whole "
                f"shared prefix (all {shared_messages} messages) — suspect "
                f"{suspects}"
            )
        else:
            note = (
                f"every shared checkpoint matched, but they only reach "
                f"message {verified_messages} of {shared_messages}: messages "
                f"{verified_messages + 1}..{shared_messages} were never "
                f"compared, so a rewrite in there looks exactly like this. "
                f"Failing that, suspect {suspects}"
            )
        if rewrites:
            # A trim/digest rewrote already-sent history between the two
            # requests. That is a local cause with a name; do not hand the
            # reader "suspect the provider" for something fluid did.
            out["reason"] = "history_rewritten"
            note = (
                f"{_rewrite_phrase(rewrites)} between the two requests, so "
                f"already-sent history was rewritten"
            )
            if not full_coverage:
                note += (
                    f" (the checkpoints verified only message "
                    f"{verified_messages} of {shared_messages}, so the "
                    f"rewrite may sit past the last shared checkpoint)"
                )
            out["history_rewritten"] = rewrites
        out["note"] = note
        return out

    out = {
        "reason": "prefix_changed",
        "changed": changed,
        "stable_through": stable_through,
        "shared_messages": shared_messages,
        "verified_messages": verified_messages,
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
    if rewrites:
        out["history_rewritten"] = rewrites
        out["note"] += f"; {_rewrite_phrase(rewrites)} since the last request"
    return out


# Verdicts where fluid has verified it did NOT move the prompt: the whole
# shared prefix hashed identical, no compaction ran between the two requests,
# and the provider still billed everything fresh. The other verdicts name a
# local cause (``prefix_changed``, ``history_rewritten``) or an unavoidable
# one (``no_previous_request``) and are not worth alerting on.
_UNEXPLAINED_REASONS = frozenset({"prefix_grew_only", "prefix_unchanged"})


def is_unexplained_miss(diagnosis: Mapping[str, Any] | None) -> bool:
    """True when a miss survives every explanation fluid can offer itself.

    The bar is deliberately high: the checkpoints must cover the WHOLE shared
    prefix (a partial verification cannot rule out a rewrite past the last
    shared checkpoint), the provider must have read literally nothing, and no
    compaction rung may have run since the previous request.
    """
    if not diagnosis:
        return False
    return (
        diagnosis.get("reason") in _UNEXPLAINED_REASONS
        and bool(diagnosis.get("full_coverage"))
        and not diagnosis.get("history_rewritten")
        and int(diagnosis.get("cache_read_tokens") or 0) == 0
    )
