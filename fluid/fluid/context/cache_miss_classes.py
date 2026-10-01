"""Classify each prompt-cache miss the fluid probe reports into one of six classes.

fluid's cache probe (``fluid/context/cache_probe.py``) puts a ``cache_miss``
diagnosis on the ``Usage`` event of a call that paid for a large fresh prompt,
plus ``cache_read_report`` and ``prefix_hashes`` on every call. This module
turns that probe data into one class per miss. The engine stamps the class on
``Usage.raw["cache_miss_class"]`` and on ``CacheMissAlert.miss_class``, so the
event log carries it, and the app's adapter counts the classes in its
context-usage report, so the unexpected-miss rate can be watched as an SLO
(fluid v2 design, sections 2.4, 2.12 and 5.1). The app imports this module
through ``app.core.provider.cache_miss_classes``.

``append_growth``
    The prefix was reused and the fresh tokens are the newly appended turn.
``planned_rebuild``
    A rebuild the harness or user announced: the session's first request, a
    compaction that rewrote sent history, or a settings switch (model,
    effort, endpoint), including one that also changed the routing headers.
``harness_upgrade``
    The probe's fingerprint format changed between the two calls, which only
    a deploy does.
``provider_fallback``
    The fingerprints match through the shared prefix and the provider still
    reused less of it than it should have, within the cache lifetime. The
    request went to a replica without the cache. Also the call right after
    the engine dropped the sticky routing key (OpenRouter's ``session_id``)
    following an empty completion: the request changed only because the
    engine gave up on the replica.
``idle_past_ttl``
    The same shortfall after a gap longer than the cache lifetime.
``harness_mutation``
    Something the harness sent changed without an announcement: the system
    prompt, the tool list, already-sent history, or the routing headers
    alone. This class, together with any probe reason this module does not
    know, is the unexpected-miss count.

The functions here are pure: probe data in, class out.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

APPEND_GROWTH = "append_growth"
PLANNED_REBUILD = "planned_rebuild"
HARNESS_UPGRADE = "harness_upgrade"
PROVIDER_FALLBACK = "provider_fallback"
IDLE_PAST_TTL = "idle_past_ttl"
HARNESS_MUTATION = "harness_mutation"

MISS_CLASSES = (
    APPEND_GROWTH,
    PLANNED_REBUILD,
    HARNESS_UPGRADE,
    PROVIDER_FALLBACK,
    IDLE_PAST_TTL,
    HARNESS_MUTATION,
)

#: Classes that count against the unexpected-miss SLO.
UNEXPECTED_CLASSES = frozenset({HARNESS_MUTATION})

#: Default prompt-cache lifetime. Anthropic's default TTL is five minutes and
#: OpenAI keeps an idle prefix for five to ten, so a longer gap is idle expiry.
DEFAULT_CACHE_TTL_SECONDS = 300.0

# Verdicts of fluid's ``cache_read_report`` for a call that reused less than
# 90% of its prompt from cache.
_MISS_VERDICTS = frozenset({"partial_hit", "no_cache_read"})

# The prompt sections compaction never rewrites (invariant I2): a change there
# is a harness mutation even when a compaction ran in the same interval.
_STATIC_SEGMENTS = frozenset({"system", "tools"})


def classify_cache_miss(
    diagnosis: Mapping[str, Any],
    *,
    request_interval_seconds: Optional[float] = None,
    ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> str:
    """The class of one probe diagnosis (a ``Usage.raw["cache_miss"]`` dict).

    ``request_interval_seconds`` is the gap since the previous request
    (``Usage.raw["request_interval_seconds"]``). Without it, a shortfall on a
    matching prefix is attributed to the provider, not to expiry.
    """
    reason = diagnosis.get("reason")
    if reason == "no_previous_request":
        return PLANNED_REBUILD
    if reason == "fingerprint_version_changed":
        return HARNESS_UPGRADE
    if reason == "request_settings_changed":
        if diagnosis.get("sticky_key_dropped"):
            return PROVIDER_FALLBACK
        # Headers that moved on their own are an unannounced routing change.
        # Headers that moved with the model or endpoint (a provider switch)
        # are part of that switch. One of these on the first call after the
        # OpenAI Responses routing arm changes (arm A to B on upgrade) is
        # expected: the new arm sends new identity headers once.
        if diagnosis.get("routing_headers_changed") and not diagnosis.get(
            "other_settings_changed"
        ):
            return HARNESS_MUTATION
        return PLANNED_REBUILD
    if reason == "history_rewritten":
        return PLANNED_REBUILD
    if reason == "prefix_changed":
        if diagnosis.get("changed") in _STATIC_SEGMENTS:
            return HARNESS_MUTATION
        if diagnosis.get("history_rewritten"):
            return PLANNED_REBUILD
        return HARNESS_MUTATION
    if reason in ("prefix_grew_only", "prefix_unchanged"):
        if _past_ttl(request_interval_seconds, ttl_seconds):
            return IDLE_PAST_TTL
        return PROVIDER_FALLBACK
    # A reason this module does not know yet is counted as unexpected so that
    # it gets looked at, not silently filed as a routine miss.
    return HARNESS_MUTATION


def usage_miss_class(
    usage: Mapping[str, Any], *, ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS
) -> Optional[str]:
    """The miss class of one model call's usage record, or None for no miss.

    ``usage`` is the per-call usage dict the adapter builds from a fluid
    ``Usage`` event (the event's ``raw`` fields merged in). A call is a miss
    when the probe diagnosed one, or when the provider reused less than 90%
    of the prompt and the probe fingerprinted the call without finding lost
    reuse, which leaves the new suffix as the fresh part (``append_growth``).
    A call the probe did not fingerprint (probe off, delegated leg) has no
    class.
    """
    diagnosis = usage.get("cache_miss")
    if isinstance(diagnosis, Mapping):
        return classify_cache_miss(
            diagnosis,
            request_interval_seconds=_float_or_none(
                usage.get("request_interval_seconds")
            ),
            ttl_seconds=ttl_seconds,
        )
    report = usage.get("cache_read_report")
    if (
        isinstance(report, Mapping)
        and report.get("verdict") in _MISS_VERDICTS
        and usage.get("prefix_hashes")
    ):
        return APPEND_GROWTH
    return None


def _past_ttl(interval: Optional[float], ttl_seconds: float) -> bool:
    return interval is not None and interval > ttl_seconds


def _float_or_none(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
