"""Versioned request-prefix fingerprints and estimated cache-reuse shortfall.

Hashes contain no request payloads. Normalized checkpoints cover every content
field; a provider serializer may replace them with serialized checkpoints.
Neither matching normalized hashes nor a shortfall proves provider fault:
cache lifetime, routing and provider tokenization remain outside this probe.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from fluid.context.trim import estimate_tokens_of_messages
from fluid.messages import Message

__all__ = [
    "CacheProbeConfig",
    "fingerprint_prefix",
    "fingerprint_responses_request",
    "normalize_routing_headers",
    "diagnose_cache_miss",
    "is_unexplained_miss",
    "checkpoint_positions",
    "message_count",
    "system_text_of",
    "system_paragraph_hashes",
    "system_change_detail",
    "cache_read_report",
]

_DIGEST_BYTES = 8  # 16 hex chars — collision-irrelevant for a diff signal

SYSTEM_SEGMENT = "system"
TOOLS_SEGMENT = "tools"
# Hash of the routing headers alone, next to the ``request`` identity that also
# covers them, so a diagnosis can say whether the headers were what changed.
ROUTING_HEADERS_KEY = "_routing_headers"
# Hash of the request identity without the routing headers, written whenever
# headers are hashed, so a diagnosis can tell a header-only change from a
# settings switch (model, endpoint) that also moved the headers.
REQUEST_SETTINGS_KEY = "_request_settings"


@dataclass(frozen=True, kw_only=True)
class CacheProbeConfig:
    """Thresholds for fresh input and estimated reusable-prefix shortfall."""

    enabled: bool = True
    # Minimum fresh (uncached) input tokens before a miss is worth reporting.
    min_input_tokens: int = 10_000
    # Legacy fingerprints lack token estimates and retain the near-zero gate.
    # Versioned fingerprints instead measure the shared-prefix shortfall.
    max_cache_read_tokens: int = 1_024
    # Consecutive shortfalls with a matching shared fingerprint and no local
    # rewrite before a CacheMissAlert. At most one alert per turn; 0 disables it.
    # Cache lifetime and routing remain possible causes, not proven diagnoses.
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


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ─── system-section diff support ─────────────────────────────────────────────
#
# A system-section hash alone cannot say WHAT changed in a large system prompt.
# The fingerprint also carries a per-paragraph hash list (4 hex chars each,
# ~750 bytes for a 20k-token prompt), so the first differing paragraph can be
# located even across a process boundary, and the engine records that
# paragraph's text (up to SYSTEM_DIFF_HEAD_CHARS) with the section it belongs
# to.

SYSTEM_PARAGRAPHS_KEY = "_system_paragraphs"
SYSTEM_DIFF_HEAD_CHARS = 300
_PARAGRAPH_DIGEST_HEX = 4


def system_text_of(system: Any) -> str:
    """The system prompt as one text: a string as is, Anthropic-style blocks
    joined on their ``text`` fields, anything else canonicalized."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, (list, tuple)):
        parts = []
        for block in system:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            else:
                parts.append(_canonical(block))
        return "\n\n".join(parts)
    return _canonical(system)


def _paragraphs(text: str) -> list[str]:
    return text.split("\n\n") if text else []


def system_paragraph_hashes(text: str) -> str:
    """Space-separated short hashes, one per ``\\n\\n``-separated paragraph."""
    out = []
    for paragraph in _paragraphs(text):
        h = hashlib.blake2b(digest_size=2)
        h.update(paragraph.encode("utf-8", "replace"))
        out.append(h.hexdigest()[:_PARAGRAPH_DIGEST_HEX])
    return " ".join(out)


def _section_of(index: int, sections: Sequence[tuple[str, str]] | None, text: str) -> str | None:
    """Which compiled section paragraph ``index`` falls in, when the joined
    section texts are exactly the prompt (otherwise ``None``)."""
    if not sections:
        return None
    if "\n\n".join(s_text for _kind, s_text in sections) != text:
        return None
    cursor = 0
    for kind, s_text in sections:
        count = len(_paragraphs(s_text))
        if index < cursor + count:
            return kind
        cursor += count
    return None


def system_change_detail(
    previous: Mapping[str, str] | None,
    current: Mapping[str, str],
    *,
    current_text: str,
    previous_text: str | None = None,
    sections: Sequence[tuple[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Where the system section changed between two fingerprints.

    Returns ``None`` when the system hashes match or neither fingerprint
    carries paragraph hashes. Otherwise the first differing paragraph's
    index, its section name (when the prompt was compiled from sections),
    the head of the current paragraph, and the head of the previous one when
    that text is still in this process. Across a park/wake only the previous
    hashes survive (they ride the persisted Usage event), which still names
    the paragraph position and the current text at it.
    """
    if previous is None or previous.get(SYSTEM_SEGMENT) == current.get(SYSTEM_SEGMENT):
        return None
    before = (previous.get(SYSTEM_PARAGRAPHS_KEY) or "").split()
    after = (current.get(SYSTEM_PARAGRAPHS_KEY) or "").split()
    if not before or not after:
        return None
    index = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), None)
    if index is None:
        index = min(len(before), len(after))
    paragraphs = _paragraphs(current_text)
    current_head = paragraphs[index][:SYSTEM_DIFF_HEAD_CHARS] if index < len(paragraphs) else ""
    out: dict[str, Any] = {
        "paragraph_index": index,
        "paragraphs_before": len(before),
        "paragraphs_after": len(after),
        "current_head": current_head,
    }
    section = _section_of(index, sections, current_text)
    if section is not None:
        out["section"] = section
    if previous_text is not None:
        prev_paragraphs = _paragraphs(previous_text)
        if index < len(prev_paragraphs):
            out["previous_head"] = prev_paragraphs[index][:SYSTEM_DIFF_HEAD_CHARS]
        elif len(before) != len(after):
            out["previous_head"] = ""
    return out


def cache_read_report(
    usage: Mapping[str, Any], *, input_includes_cached: bool
) -> dict[str, Any]:
    """The provider's own cached-token count next to the probe's verdict.

    Recorded on EVERY call, hit or miss, so a partial hit (some prefix reused)
    and a full miss (zero reused) are distinguishable in the event log without
    re-deriving them from four usage counters. ``input_includes_cached`` says
    which accounting the caller supplies. Engine usage is always normalized
    to exclusive input, regardless of the provider's wire format.
    """
    def _int(key: str) -> int:
        try:
            return max(0, int(usage.get(key, 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    input_tokens = _int("input_tokens")
    cached = _int("cache_read_input_tokens") or _int("cache_read_tokens")
    created = _int("cache_creation_input_tokens") or _int("cache_creation_tokens")
    total = input_tokens if input_includes_cached else input_tokens + cached + created
    fraction = round(min(cached, total) / total, 3) if total > 0 else 0.0
    if total < 1_000:
        verdict = "small_prompt"
    elif fraction >= 0.9:
        verdict = "full_hit"
    elif cached > 0:
        verdict = "partial_hit"
    else:
        verdict = "no_cache_read"
    return {
        "provider_cached_tokens": cached,
        "provider_cache_creation_tokens": created,
        "prompt_tokens_total": total,
        "cached_fraction": fraction,
        "verdict": verdict,
        "input_includes_cached": input_includes_cached,
    }


def normalize_routing_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Header names lower-cased (HTTP names are case-insensitive), values as text."""
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def _feed_block(h: Any, block: Any) -> None:
    # Field names and delimiters avoid ambiguous concatenations. Include full
    # image data, tool names and opaque reasoning, not length/head heuristics.
    data = asdict(block)
    data.pop("delivered_steers", None)  # persistence metadata, not provider content
    data.pop("delivery_metadata_version", None)
    h.update(_canonical({"kind": type(block).__name__, "value": data}).encode())


def fingerprint_prefix(
    *,
    system: Any,
    tools: Sequence[dict[str, Any]] | None,
    messages: Sequence[Message],
    extra_checkpoints: Sequence[int] = (),
    request_identity: Mapping[str, Any] | None = None,
    routing_headers: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Short stable hashes of the request prefix, in prefix order.

    ``routing_headers`` are the non-credential HTTP headers the provider uses
    to route the request (see ``routing_headers()`` on the Responses client).
    They join the ``request`` identity, so a call that changes only a header
    is diagnosed as ``request_settings_changed`` instead of an unchanged
    prefix. None or empty leaves the identity as it was before headers were
    hashed, so providers without routing headers keep their fingerprints.

    Keys: ``system``, ``tools``, then ``m1``/``m2``/``m4``… ``m<len>`` — each
    ``mK`` covering the first K messages cumulatively. Dict order IS prefix
    order, which is what :func:`diagnose_cache_miss` walks, so the message
    keys are always emitted in ascending order however ``extra_checkpoints``
    interleaves with the powers of two.
    """
    out: dict[str, str] = {
        SYSTEM_SEGMENT: _hash_text(_canonical(system or "")),
    }
    try:
        tools_text = json.dumps(list(tools or ()), default=str, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover — defensive
        tools_text = "<unserializable>"
    out[TOOLS_SEGMENT] = _hash_text(tools_text)

    identity = dict(request_identity or {})
    headers = normalize_routing_headers(routing_headers)
    settings_hash = _hash_text(_canonical(identity))
    if headers:
        out["request"] = _hash_text(_canonical({**identity, "headers": headers}))
        out[REQUEST_SETTINGS_KEY] = settings_hash
        out[ROUTING_HEADERS_KEY] = _hash_text(_canonical(headers))
    else:
        out["request"] = settings_hash
    out[SYSTEM_PARAGRAPHS_KEY] = system_paragraph_hashes(system_text_of(system))
    out["_version"] = "normalized-v2"
    static_tokens = (len(_canonical(system or "")) + len(tools_text)) // 4
    out["_tokens_system"] = str(len(_canonical(system or "")) // 4)
    out["_tokens_tools"] = str(static_tokens)
    wanted = set(checkpoint_positions(len(messages), extra_checkpoints))
    running = _hasher()
    tokens = static_tokens
    for index, message in enumerate(messages, start=1):
        running.update(_canonical({"role": message.role, "meta": message.provider_meta}).encode())
        tokens += estimate_tokens_of_messages([message])
        for block in message.content:
            _feed_block(running, block)
        if index in wanted:
            out[f"m{index}"] = running.hexdigest()
            out[f"_tokens_m{index}"] = str(tokens)
    return out


def fingerprint_responses_request(
    request: Any,
    fingerprint: dict[str, str],
    *,
    previous_count: int,
    endpoint: Any = None,
    routing_headers: Mapping[str, Any] | None = None,
) -> None:
    """Replace normalized checkpoints with the Responses serializer's items.

    Serialization is O(n): absolute power-of-two checkpoints plus at most one
    prior-length checkpoint visit fewer than four full histories in total.

    The ``request`` identity covers the body settings and ``routing_headers``.
    The ChatGPT backend routes on headers as well as the body, so a header
    that changes between two calls is an identity change even when every body
    byte matched. v3 added the headers; a v2 fingerprint from before the
    upgrade therefore reads as ``fingerprint_version_changed``, not as a
    settings change.
    """
    from dataclasses import replace
    from fluid.providers.openai_responses.serialize import build_request_body

    def digest(value: Any) -> str:
        return _hash_text(_canonical(value))

    body = build_request_body(request)
    headers = normalize_routing_headers(routing_headers)
    fingerprint["_version"] = "responses-wire-v3"
    fingerprint["_wire_hash"] = digest(body)
    fingerprint["system"] = digest(body.get("instructions"))
    fingerprint["tools"] = digest(body.get("tools"))
    settings = {
        "provider": "openai_responses", "endpoint": endpoint,
        **{k: v for k, v in body.items() if k not in {"input", "instructions", "tools"}},
    }
    fingerprint["request"] = digest({**settings, "headers": headers})
    fingerprint[REQUEST_SETTINGS_KEY] = digest(settings)
    fingerprint[ROUTING_HEADERS_KEY] = digest(headers)
    for count in checkpoint_positions(len(request.messages), (previous_count,)):
        prefix = build_request_body(replace(request, messages=request.messages[:count]))
        fingerprint[f"m{count}"] = digest(prefix.get("input"))
    if "input" in request.extra:
        # An explicit input override severs the normalized-history denominator.
        for key in fingerprint:
            if key.startswith("_tokens_"):
                fingerprint[key] = "0"


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


def _settings_hash(fingerprint: Mapping[str, str]) -> str | None:
    """The request identity without routing headers.

    A fingerprint without headers has no separate key: its ``request`` hash
    already leaves them out, and hashes the same identity the same way.
    """
    return fingerprint.get(REQUEST_SETTINGS_KEY, fingerprint.get("request"))


def diagnose_cache_miss(
    *,
    previous: dict[str, str] | None,
    current: dict[str, str],
    input_tokens: int,
    cache_read_tokens: int,
    config: CacheProbeConfig | None = None,
    history_rewrites: Sequence[Mapping[str, Any]] | None = None,
    explicit_breakpoints: bool = True,
    system_detail: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Name the first prefix segment that changed, when a call missed cache.

    ``system_detail`` (see :func:`system_change_detail`) is attached as
    ``system_diff`` when the changed segment is the system section, so the
    audit can read the varying fragment instead of a hash.

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
    legacy = not current.get("_version")
    if legacy and cache_read_tokens > config.max_cache_read_tokens:
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

    if previous.get("_version") != current.get("_version"):
        return {"reason": "fingerprint_version_changed", "input_tokens": input_tokens,
                "cache_read_tokens": cache_read_tokens}
    shared = [k for k in current if k in previous and not k.startswith("_")]
    # Request identity is an eligibility boundary, even if text stayed equal.
    if previous.get("request") != current.get("request"):
        # Routing headers are part of the identity; say when they moved, and
        # whether anything else did. Headers that moved on their own are a
        # routing change the host did not announce; headers that moved with
        # the model or endpoint are part of that switch.
        headers_changed = (
            previous.get(ROUTING_HEADERS_KEY) != current.get(ROUTING_HEADERS_KEY)
        )
        settings_changed = _settings_hash(previous) != _settings_hash(current)
        if headers_changed and settings_changed:
            note = ("a request setting (model, effort, extra, endpoint) and the "
                    "routing headers changed")
        elif headers_changed:
            note = "the routing headers changed"
        else:
            note = "a request setting (model, effort, extra, endpoint) changed"
        return {"reason": "request_settings_changed", "input_tokens": input_tokens,
                "cache_read_tokens": cache_read_tokens,
                "routing_headers_changed": headers_changed,
                "other_settings_changed": settings_changed,
                "note": note}
    shared = [k for k in shared if k != "request"]
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
    eligible = int(current.get(f"_tokens_{stable_through}", "0"))
    # Conservative allowance for tokenization, cache block rounding and images.
    uncertainty = max(1_024, int(eligible * 0.2))
    shortfall = max(0, eligible - cache_read_tokens - uncertainty)
    telemetry = {"shared_prefix_tokens_estimated": eligible,
                 "estimate_uncertainty_tokens": uncertainty,
                 "reuse_shortfall_tokens_estimated": shortfall,
                 "fingerprint_version": current.get("_version", "legacy")}
    if not legacy and changed is None and not rewrites and shortfall < config.min_input_tokens:
        return None  # A large new suffix is not lost reuse.

    if changed is None:
        # The fingerprinted representation matches through the common prefix.
        # Normalized checkpoints are not a claim about exact serialized bytes;
        # even wire checkpoints cannot establish provider cache eligibility.
        grew = message_count(current) > shared_messages
        out = {
            "reason": "prefix_grew_only" if grew else "prefix_unchanged",
            "stable_through": stable_through,
            "shared_messages": shared_messages,
            "verified_messages": verified_messages,
            "full_coverage": full_coverage,
            **telemetry,
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
        if not legacy:
            note += (
                f"; scope={current.get('_version')}, reuse and token counts are "
                "estimated, not proof of provider fault"
            )
        out["note"] = note
        return out

    out = {
        "reason": "prefix_changed",
        **telemetry,
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
        if changed == SYSTEM_SEGMENT and system_detail:
            out["system_diff"] = dict(system_detail)
            where = system_detail.get("section")
            head = str(system_detail.get("current_head") or "")[:80]
            out["note"] += (
                f" — first differing paragraph #{system_detail.get('paragraph_index')}"
                + (f" in the {where} section" if where else "")
                + (f': "{head}…"' if head else "")
            )
    if rewrites:
        out["history_rewritten"] = rewrites
        out["note"] += f"; {_rewrite_phrase(rewrites)} since the last request"
    return out


# Matching shared fingerprints with an estimated reuse shortfall. These are
# alert candidates, not evidence that the provider violated a cache guarantee.
_UNEXPLAINED_REASONS = frozenset({"prefix_grew_only", "prefix_unchanged"})


def is_unexplained_miss(diagnosis: Mapping[str, Any] | None) -> bool:
    """True for a reuse shortfall without a detected local prefix rewrite.

    Checkpoints must cover the whole shared prefix. Partial cache reads can
    qualify; a large appended suffix alone cannot. Versionless historical
    diagnostics retain their original near-zero-read interpretation.
    """
    if not diagnosis:
        return False
    return (
        diagnosis.get("reason") in _UNEXPLAINED_REASONS
        and bool(diagnosis.get("full_coverage"))
        and not diagnosis.get("history_rewritten")
        and (int(diagnosis.get("reuse_shortfall_tokens_estimated") or 0) > 0
             or (diagnosis.get("fingerprint_version", "legacy") == "legacy"
                 and int(diagnosis.get("cache_read_tokens") or 0) == 0))
    )
