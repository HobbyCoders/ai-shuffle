"""Gemini API (AI Studio key) list prices, Standard tier, USD per 1M tokens.

Source: https://aistudio.google.com/docs/pricing, read 2026-11-11. Output
prices include thinking tokens, and the normalized usage folds thinking into
``output_tokens`` (see serialize.normalize_usage), so ``reasoning`` stays
unset. ``cached_input`` is the "Context caching price" a cache hit bills at;
these legs only use implicit caching, so no storage price applies.

Several rows are scheduled: Google lists a price "through" one date and
another "starting" the next. A call is priced by the UTC date it ran.
A model without a row returns None: never guess a price.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from typing import Any

from fluid.providers.pricing import ModelPrice, _cost_from_price

_SOURCE = "https://aistudio.google.com/docs/pricing; read 2026-11-11; Standard tier"

# model → [(first UTC day the price applies, or None for "always", price)],
# oldest first.
_Schedule = list[tuple[_dt.date | None, ModelPrice]]


def _p(input_: float, cached: float, output: float) -> ModelPrice:
    return ModelPrice(input=input_, cached_input=cached, output=output, source=_SOURCE)


_GEMINI_4 = [
    (None, _p(2.00, 0.10, 10.00)),
    (_dt.date(2026, 12, 1), _p(4.00, 0.20, 20.00)),
]
_FLASH_3_6_PLUS = [
    (None, _p(0.75, 0.075, 3.75)),
    (_dt.date(2027, 1, 1), _p(1.50, 0.15, 7.50)),
]

PRICES: dict[str, _Schedule] = {
    "gemini-4-carbon": _GEMINI_4,
    "gemini-4-argon": _GEMINI_4,
    "gemini-3.8-flash": _FLASH_3_6_PLUS,
    "gemini-3.6-flash": _FLASH_3_6_PLUS,
    "gemini-3.5-flash-lite": [(None, _p(0.30, 0.03, 2.50))],
    # Text / image / video rates; audio input is priced higher and is not
    # something this leg sends.
    "gemini-3.1-flash-lite": [(None, _p(0.25, 0.025, 1.50))],
}


def price_for(model: str, on: _dt.date | None = None) -> ModelPrice | None:
    schedule = PRICES.get((model or "").strip().lower().removeprefix("models/"))
    if not schedule:
        return None
    day = on or _dt.datetime.now(_dt.timezone.utc).date()
    chosen: ModelPrice | None = None
    for start, price in schedule:
        if start is None or day >= start:
            chosen = price
    return chosen


def estimate_cost(model: str, usage: Mapping[str, Any], on: _dt.date | None = None) -> float | None:
    price = price_for(model, on)
    return _cost_from_price(price, usage) if price is not None else None
