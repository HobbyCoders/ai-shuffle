"""When a compaction rewrite is worth paying for: the cache-priced schedule.

A batch rewrites messages the provider already cached, so the next request
re-bills everything after the first changed message at the full input price.
Whether that is worth it depends on the cache. When the cache is cold anyway
(a long pause, a changed system prompt or tool list, a resumed engine), the
rewrite costs nothing extra and should happen. When the cache is warm, the
rewrite is a purchase: it costs one re-billed tail now and saves the cached
price of the reclaimed tokens on every later call. Deferring is rent: each
warm call that carries the unreclaimed tokens pays their cached price.
Committing once the accumulated rent reaches the buy price is deterministic
ski rental, which never pays more than twice the best schedule in hindsight.

Everything here is pure: ``CacheSignal`` is built by the engine before each
call, and ``decide_schedule`` turns a signal, the zones of the window and the
prompt size into a decision. ``Compactor.apply`` owns the state (the rent
paid so far) and the candidate it prices. All sizes are measured tokens.

Zones on a known window W (see the compaction review, plan item 3):

* below F = ``opportunistic_floor_window_ratio * W``: nothing is built.
* F to S (the trigger): a candidate is built only when the cache is cold, and
  it commits only if it gets near the deep target or reclaims a lot.
* S to H (the hard ceiling): priced. Cold commits at once; warm commits at
  break-even and defers until then.
* H and above: the legacy hard zone and window protection, which commit
  regardless of the cache.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# A gap longer than this between two requests means the provider has most
# likely dropped the prefix (OpenAI's in-memory retention and Anthropic's
# default TTL are both about five minutes).
COLD_GAP_SECONDS = 300.0
# Cached input price as a fraction of the full input price when pricing does
# not say (0.1 for the gpt rows and for Anthropic cache reads).
DEFAULT_CACHE_PRICE_RATIO = 0.1
# The miss-rate EWMA: weight of the newest call, how many persisted calls
# seed it on resume, and the hit rate under which one call counts as a miss.
P_COLD_ALPHA = 0.5
P_COLD_SEED_CALLS = 8
MISS_HIT_RATE = 0.5
# The opportunistic band commits a cold candidate only when it lands within
# this fraction above the deep target, or reclaims at least the larger of
# these two amounts. Small cold rewrites would still move the prefix for
# little gain.
OPPORTUNISTIC_TARGET_SLACK = 0.1
OPPORTUNISTIC_MIN_RECLAIM_TOKENS = 15_000
OPPORTUNISTIC_MIN_RECLAIM_RATIO = 0.1

# Zone labels, persisted as ``schedule`` on decisions and events.
ZONE_BELOW_FLOOR = "below_floor"
ZONE_OPPORTUNISTIC = "opportunistic"
ZONE_PRICED = "priced"
ZONE_HARD = "hard"
ZONE_WINDOW = "window"


@dataclass(frozen=True, kw_only=True)
class CacheSignal:
    """What the engine knows about the provider cache before one call.

    ``gap_s`` is the time since the previous request of this session on this
    engine, or None when there was none. ``static_changed`` says the system
    prompt or tool list about to be sent differs from the last one sent.
    ``resumed`` marks the first call after an unpark or on a new engine.
    ``p_cold`` is an EWMA of per-call misses (hit rate under 0.5), seeded
    from the last persisted calls on resume. ``reuse_floor`` is the size of
    the prefix a rewrite never touches (system prompt and tool schemas) in
    the same chars/4 estimate units as ``overhead_tokens``; the schedule
    scales it to measured tokens. ``cache_price_ratio`` is the cached input
    price over the full input price for the model.
    """

    gap_s: float | None = None
    static_changed: bool = False
    resumed: bool = False
    p_cold: float = 0.0
    reuse_floor: int = 0
    cache_price_ratio: float = DEFAULT_CACHE_PRICE_RATIO

    @property
    def cold_reason(self) -> str:
        """Why the cache is cold now, or "" when it is presumed warm. A single
        earlier miss does not count: a miss writes the prefix it missed."""
        if self.resumed:
            return "resumed"
        if self.static_changed:
            return "static_changed"
        if self.gap_s is not None and self.gap_s > COLD_GAP_SECONDS:
            return "gap"
        return ""

    @property
    def cold_now(self) -> bool:
        return bool(self.cold_reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_s": None if self.gap_s is None else round(self.gap_s, 3),
            "static_changed": self.static_changed,
            "resumed": self.resumed,
            "p_cold": round(self.p_cold, 4),
            "reuse_floor": self.reuse_floor,
            "cache_price_ratio": self.cache_price_ratio,
            "cold_now": self.cold_now,
            "cold_reason": self.cold_reason,
        }


def update_p_cold(p_cold: float | None, hit_rate: float) -> float:
    """Fold one call's hit rate into the miss EWMA. The first call sets it."""
    miss = 1.0 if hit_rate < MISS_HIT_RATE else 0.0
    if p_cold is None:
        return miss
    return P_COLD_ALPHA * miss + (1.0 - P_COLD_ALPHA) * p_cold


def p_cold_from_hit_rates(hit_rates: Iterable[float]) -> float | None:
    """The EWMA over hit rates given oldest first, or None for none."""
    p_cold: float | None = None
    for rate in hit_rates:
        p_cold = update_p_cold(p_cold, rate)
    return p_cold


@dataclass(frozen=True, kw_only=True)
class Zones:
    """The schedule's zones for one decision, in measured tokens.

    ``floor`` is F, ``trigger`` is S, ``hard`` is H (None when there is no
    hard zone), ``target`` is the deep batch target T and ``limit`` is the
    window-protection limit L.
    """

    floor: int
    trigger: int
    hard: int | None
    target: int
    limit: int

    def zone_of(self, prompt: float) -> str:
        if prompt >= self.limit:
            return ZONE_WINDOW
        if self.hard is not None and prompt >= self.hard:
            return ZONE_HARD
        if prompt >= self.trigger:
            return ZONE_PRICED
        if prompt >= self.floor:
            return ZONE_OPPORTUNISTIC
        return ZONE_BELOW_FLOOR


@dataclass(frozen=True, kw_only=True)
class Schedule:
    """One schedule decision.

    ``action`` is "skip" (build no candidate), "build" (build one and ask
    again with its size), "legacy" (the unpriced ladder decides: the hard
    zone and window protection), or, for a built candidate, "commit",
    "defer" or "reject". ``buy_price`` (R) and ``rent_per_call`` (s) are in
    measured tokens and are set only when a priced-band candidate was
    weighed.
    """

    zone: str
    action: str
    reason: str = ""
    buy_price: int = 0
    rent_per_call: int = 0


def decide_schedule(
    signal: CacheSignal,
    zones: Zones,
    *,
    prompt: float,
    scale: float = 1.0,
    candidate_after: float | None = None,
    rent_paid: float = 0.0,
) -> Schedule:
    """Decide one call. Pure: the same inputs give the same answer.

    Called once with ``candidate_after=None`` to learn whether to build a
    candidate, then again with the candidate's measured size to learn
    whether to commit it. ``rent_paid`` is the rent the warm deferrals since
    the last commit have accumulated, and ``scale`` (measured over estimated,
    at most 1) converts the signal's ``reuse_floor`` to measured tokens.

    In the priced band the buy price is
    ``R = max(0, after - reuse_floor) * (1 - c) * (1 - p_cold)``: the tail a
    rewrite re-bills at full price instead of the cached price c, discounted
    by the chance the next call would have missed anyway. The rent per warm
    call is ``s = c * (before - after)``: the cached price of the tokens the
    candidate would reclaim. A warm call commits once the rent paid so far
    plus this call's rent reaches R, so a flaky cache (p_cold near 1) commits
    at the trigger the way the unpriced ladder does.
    """
    zone = zones.zone_of(prompt)
    if zone in (ZONE_WINDOW, ZONE_HARD):
        return Schedule(zone=zone, action="legacy")
    if zone == ZONE_BELOW_FLOOR or (
        zone == ZONE_OPPORTUNISTIC and not signal.cold_now
    ):
        return Schedule(zone=zone, action="skip", reason="below_threshold")
    if candidate_after is None:
        return Schedule(zone=zone, action="build")
    reclaim = max(0.0, prompt - candidate_after)
    if zone == ZONE_OPPORTUNISTIC:
        near_target = candidate_after <= zones.target * (1 + OPPORTUNISTIC_TARGET_SLACK)
        large = reclaim >= max(
            OPPORTUNISTIC_MIN_RECLAIM_TOKENS, OPPORTUNISTIC_MIN_RECLAIM_RATIO * prompt
        )
        if near_target or large:
            return Schedule(zone=zone, action="commit", reason="cold_opportunistic")
        return Schedule(zone=zone, action="reject", reason="insufficient_reclaim")
    c = min(1.0, max(0.0, signal.cache_price_ratio))
    p_cold = min(1.0, max(0.0, signal.p_cold))
    floor = signal.reuse_floor * (min(1.0, scale) if scale > 0 else 1.0)
    buy = max(0.0, candidate_after - floor) * (1.0 - c) * (1.0 - p_cold)
    rent = c * reclaim
    priced = {"buy_price": round(buy), "rent_per_call": round(rent)}
    if signal.cold_now:
        return Schedule(zone=zone, action="commit", reason="cold_commit", **priced)
    if rent_paid + rent >= buy:
        return Schedule(zone=zone, action="commit", reason="break_even", **priced)
    return Schedule(zone=zone, action="defer", reason="warm_defer", **priced)
