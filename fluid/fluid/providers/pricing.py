"""Cross-provider model pricing — the last fallback under every cost estimate.

Each provider client may expose its own ``estimate_cost`` (see
``fluid/providers/base.py``); Anthropic and the OpenAI Responses dialect ship
sourced tables, and OpenRouter prices from rates the embedder injects. That
left every other lane at ``cost_usd = None``: a Grok id on the
``openai_responses`` dialect matches no ``gpt-``/``o``-prefix row, a GLM id on
OpenRouter has no injected rate, and the host stores 0.0 for a missing cost —
so a whole session's spend reads as free.

This module is the seam under all of them:

* **A table you can edit.** ``PRICES`` maps a lowercased model-id prefix to a
  :class:`ModelPrice` of per-MTok USD rates. Rows whose price is not sourced
  carry ``None`` rates and a ``source`` note saying so — a known model with an
  unknown price yields no cost rather than a fabricated one. **Never guess a
  rate.** A wrong number is worse than no number: no number is visibly missing,
  a wrong one is silently believed.
* **Provider-prefixed ids resolve.** ``z-ai/glm-5.3-flash``,
  ``anthropic/claude-opus-5`` and ``openai/gpt-5.6-sol`` normalize to their
  bare ids before lookup, so an OpenRouter slug prices like the direct one.
* **The host wins.** An app with its own model catalog passes a
  :data:`PricingResolver` (``NativeLoop(pricing=...)``); it is consulted first
  and its rates are used verbatim.
* **Nothing here raises.** Every entry point swallows its own errors and
  returns ``None``. A missing price is an accounting gap, never a failed turn.

Rates are USD per 1,000,000 tokens.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "ModelPrice",
    "PricingResolver",
    "PRICES",
    "normalize_model_id",
    "resolve_price",
    "estimate_cost",
]


@dataclass(frozen=True, kw_only=True)
class ModelPrice:
    """Per-MTok USD rates for one model. ``None`` means "not sourced".

    ``cached_input`` falls back to ``input`` when unset (the conservative
    reading: an unpriced cache read bills at the full input rate, because
    unpriced does not mean free). ``cache_write`` does the same.

    ``reasoning`` is for providers that bill reasoning tokens on their own
    line. It is left ``None`` on every row here on purpose: fluid's normalized
    usage folds ``reasoning_tokens`` into ``output_tokens`` (see
    ``openai_responses/serialize.py``), so pricing them again would
    double-count. Set it only alongside a usage shape that reports reasoning
    separately.
    """

    input: float | None = None
    cached_input: float | None = None
    cache_write: float | None = None
    output: float | None = None
    reasoning: float | None = None
    # Where the numbers came from, or why there are none. Kept on the row so
    # the table stays auditable when someone refreshes it.
    source: str = ""

    @property
    def priced(self) -> bool:
        """True when this row can produce a cost at all."""
        return self.input is not None or self.output is not None

    @classmethod
    def coerce(cls, value: Any) -> ModelPrice | None:
        """Accept a ``ModelPrice`` or a plain mapping of the same field names.

        Hosts keep prices in their own catalog shape; taking a mapping means
        they need not import this dataclass to answer.
        """
        if isinstance(value, ModelPrice):
            return value
        if not isinstance(value, Mapping):
            return None
        fields = {
            "input": None, "cached_input": None, "cache_write": None,
            "output": None, "reasoning": None,
        }
        for key in fields:
            raw = value.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                fields[key] = float(raw)
        source = value.get("source")
        return cls(**fields, source=source if isinstance(source, str) else "host")


# A host hook: given a model id (as the engine knows it), return rates for it
# or None to defer to fluid's own table.
PricingResolver = Callable[[str], "ModelPrice | Mapping[str, Any] | None"]


# Vendor prefixes an aggregator prepends to an otherwise-standard model id.
# Stripped before lookup so one row prices both the direct and the routed id.
_VENDOR_PREFIXES = (
    "anthropic/",
    "openai/",
    "x-ai/",
    "xai/",
    "z-ai/",
    "zai/",
    "google/",
    "meta-llama/",
    "mistralai/",
    "deepseek/",
    "qwen/",
    "moonshotai/",
    "groq/",
    "openrouter/",
)


def normalize_model_id(model: str | None) -> str:
    """Lowercase the id and drop an aggregator's vendor prefix and variant tag.

    ``"Z-AI/glm-5.3-flash:nitro"`` → ``"glm-5.3-flash"``. The rest of the id is
    left alone: Anthropic's ``[1m]`` long-context suffix and OpenAI's dated
    ids are load-bearing in their own tables.
    """
    lowered = (model or "").strip().lower()
    for prefix in _VENDOR_PREFIXES:
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :]
            break
    # OpenRouter routing variants (":free", ":nitro", ":floor") price the same
    # model; the tag is a routing preference, not a different model.
    head, _, _ = lowered.partition(":")
    return head


# Longest-prefix-first, matched against the normalized id with ``startswith``.
# Order matters: specific families must precede family-wide fallbacks.
#
# Rows exist here only for models NO provider table already covers — Claude ids
# resolve through ``fluid.providers.anthropic.pricing`` and GPT/o-series ids
# through ``fluid.providers.openai_responses.pricing``, both of which carry
# dated, sourced numbers. Adding a duplicate row here would fork them.
# Sourced 2026-09-04 from the vendors' own pricing pages. Every rate is the
# SHORT-CONTEXT tier: xAI and OpenAI bill the whole request at roughly double
# once the prompt reaches their long-context threshold (200k tokens at xAI,
# the "long context" band at OpenAI), and ``ModelPrice`` carries one tier —
# so a very long prompt is under-estimated here, never over-estimated. The
# ``source`` note says so on each row.
_XAI = "docs.x.ai/docs/models, 2026-09-04; short-context tier (<200k prompt)"
_ZAI = "docs.z.ai/guides/overview/pricing, 2026-09-04"
# The gpt-6 rows (astra, sol, luna) as the pricing page read on this date.
_OPENAI_6_LUNA = "developers.openai.com/api/docs/pricing, 2026-09-23; short-context tier"

PRICES: list[tuple[str, ModelPrice]] = [
    # ── xAI, reached through the openai_responses dialect ────────────────
    # Grok ids match no row in the OpenAI table, so they are priced here.
    # Families the page does not list (grok-4, grok-3) stay
    # recognized-but-unpriced.
    ("grok-4.6", ModelPrice(input=2.00, cached_input=0.50, output=6.00, source=_XAI)),
    ("grok-4.5", ModelPrice(input=2.00, cached_input=0.30, output=6.00, source=_XAI)),
    ("grok-4.3", ModelPrice(input=1.25, cached_input=0.20, output=2.50, source=_XAI)),
    ("grok-4.20", ModelPrice(input=1.25, cached_input=0.20, output=2.50, source=_XAI)),
    ("grok-build", ModelPrice(input=1.00, cached_input=0.20, output=2.00, source=_XAI)),
    ("grok-4", ModelPrice(source="xAI — rate not sourced; host must supply")),
    ("grok-3", ModelPrice(source="xAI — rate not sourced; host must supply")),
    ("grok", ModelPrice(source="xAI — rate not sourced; host must supply")),
    # ── Z.AI / GLM (direct or through OpenRouter) ─────────────────────────
    # Longest prefix first: "glm-5.3-flash" must precede "glm-5.3", and the
    # "-flashx"/"-airx" variants their plain siblings. The 5.3-flash rate is
    # the page's promotional one (ends 2026-09-09).
    ("glm-5.3-flash", ModelPrice(input=0.075, cached_input=0.015, output=0.25, source=_ZAI)),
    ("glm-5.3", ModelPrice(input=1.40, cached_input=0.26, output=4.40, source=_ZAI)),
    ("glm-5.2", ModelPrice(input=1.40, cached_input=0.26, output=4.40, source=_ZAI)),
    ("glm-5.1", ModelPrice(input=1.40, cached_input=0.26, output=4.40, source=_ZAI)),
    ("glm-5", ModelPrice(input=1.00, cached_input=0.20, output=3.20, source=_ZAI)),
    ("glm-4.7-flashx", ModelPrice(input=0.07, cached_input=0.01, output=0.40, source=_ZAI)),
    ("glm-4.7-flash", ModelPrice(input=0.0, cached_input=0.0, output=0.0, source=_ZAI + "; free")),
    ("glm-4.7", ModelPrice(input=0.60, cached_input=0.11, output=2.20, source=_ZAI)),
    ("glm-4.6", ModelPrice(input=0.60, cached_input=0.11, output=2.20, source=_ZAI)),
    ("glm-4.5-airx", ModelPrice(input=1.10, cached_input=0.22, output=4.50, source=_ZAI)),
    ("glm-4.5-air", ModelPrice(input=0.20, cached_input=0.03, output=1.10, source=_ZAI)),
    ("glm-4.5-flash", ModelPrice(input=0.0, cached_input=0.0, output=0.0, source=_ZAI + "; free")),
    ("glm-4.5-x", ModelPrice(input=2.20, cached_input=0.45, output=8.90, source=_ZAI)),
    ("glm-4.5", ModelPrice(input=0.60, cached_input=0.11, output=2.20, source=_ZAI)),
    ("glm", ModelPrice(source="Z.AI — rate not sourced; host must supply")),
    # ── OpenAI ids newer than the openai_responses table ─────────────────
    # cache_write is listed explicitly: without it, writes would bill at the
    # input rate instead of the listed cache-write rate.
    (
        "gpt-6-astra",
        ModelPrice(
            input=10.00, cached_input=1.00, cache_write=12.50, output=50.00,
            source=_OPENAI_6_LUNA,
        ),
    ),
    (
        "gpt-6-sol",
        ModelPrice(
            input=2.00, cached_input=0.20, cache_write=2.50, output=10.00,
            source=_OPENAI_6_LUNA,
        ),
    ),
    # Without this row luna would fall through to the unpriced "gpt-6" row
    # below and record no cost.
    (
        "gpt-6-luna",
        ModelPrice(
            input=0.10, cached_input=0.01, cache_write=0.125, output=0.50,
            source=_OPENAI_6_LUNA,
        ),
    ),
    (
        "gpt-6",
        ModelPrice(source="OpenAI — rate not sourced; host must supply"),
    ),
]


def _table_price(normalized: str) -> ModelPrice | None:
    for prefix, price in PRICES:
        if normalized.startswith(prefix):
            return price
    return None


def resolve_price(
    model: str, *, resolver: PricingResolver | None = None
) -> ModelPrice | None:
    """Rates for ``model``: the host resolver first, then fluid's own table.

    Returns ``None`` when neither knows the id, and an *unpriced* row (all
    rates ``None``) when the model is recognized but its price is not sourced
    — the caller can tell "unknown model" from "known, unpriced".
    """
    if resolver is not None:
        try:
            price = ModelPrice.coerce(resolver(model))
        except Exception:  # noqa: BLE001 — a host hook never fails a turn
            log.debug("host pricing resolver failed for %s", model, exc_info=True)
            price = None
        if price is not None and price.priced:
            return price
    return _table_price(normalize_model_id(model))


def _cost_from_price(price: ModelPrice, usage: Mapping[str, Any]) -> float | None:
    if not price.priced:
        return None

    def count(key: str) -> float:
        value = usage.get(key, 0) or 0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value)

    input_rate = price.input or 0.0
    # Unpriced cache tiers bill at the plain input rate, not free.
    cached_rate = price.cached_input if price.cached_input is not None else input_rate
    write_rate = price.cache_write if price.cache_write is not None else input_rate
    output_rate = price.output or 0.0
    total = (
        count("input_tokens") * input_rate
        + count("cache_read_input_tokens") * cached_rate
        + count("cache_creation_input_tokens") * write_rate
        + count("output_tokens") * output_rate
    )
    if price.reasoning is not None:
        # Opt-in only — see ModelPrice.reasoning. Normalized usage folds
        # reasoning into output, so a row that sets this must know better.
        total += count("reasoning_tokens") * price.reasoning
    return total / 1_000_000


def _delegated_cost(normalized: str, usage: Mapping[str, Any]) -> float | None:
    """Reuse the sourced per-provider tables for ids they already cover."""
    if normalized.startswith("claude"):
        from fluid.providers.anthropic.pricing import estimate_cost as anthropic_cost

        return anthropic_cost(normalized, dict(usage))
    if normalized.startswith(("gpt-", "o1", "o3", "o4", "codex-")):
        from fluid.providers.openai_responses.pricing import (
            estimate_cost as openai_cost,
        )

        return openai_cost(normalized, dict(usage))
    return None


def estimate_cost(
    model: str,
    usage: Mapping[str, Any],
    *,
    resolver: PricingResolver | None = None,
) -> float | None:
    """Estimated USD cost of one model call, or ``None`` when unpriceable.

    Order: the host resolver, then this module's table, then the sourced
    per-provider tables (Claude and GPT/o-series ids price there even when
    they arrive vendor-prefixed from an aggregator). Never raises.
    """
    try:
        price = resolve_price(model, resolver=resolver)
        if price is not None:
            cost = _cost_from_price(price, usage)
            if cost is not None:
                return cost
        return _delegated_cost(normalize_model_id(model), usage)
    except Exception:  # noqa: BLE001 — accounting never fails a turn
        log.debug("cost estimate failed for %s", model, exc_info=True)
        return None
