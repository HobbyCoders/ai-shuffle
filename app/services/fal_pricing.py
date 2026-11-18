"""fal.ai rates and the prices built from them.

fal's pricing API returns one rate per endpoint (``unit_price`` per ``unit``)
and rate-limits callers hard, so rates are fetched in batches and cached; a
catalog page costs one request, not one per model. A price is either an
estimate with a conservative total or ``status: "unknown"`` with a
machine-readable ``reason`` (``REASONS``), a short ``detail`` and, when fal
listed one, the raw ``rate``. Unknown is never zero.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from decimal import Decimal, InvalidOperation

import httpx

from app.core.generation_resolver import UNKNOWN_PRICE

PRICING_URL = "https://api.fal.ai/v1/models/pricing"
BATCH = 50
FOUND_TTL = 6 * 3600
MISSING_TTL = 3600
# A 429 is retried after each of these pauses (4 attempts in all).
RETRY_DELAYS = (0.5, 1.0, 2.0)
# The most one fresh (admission or save-time) read may take: every retry
# pause plus four replies. Past it the read fails; it never uses the cache.
FRESH_READ_SECONDS = 20

REASONS = {
    "no_pricing_row": "fal lists no price for this model.",
    "pricing_request_failed": "fal's price list could not be read right now.",
    "schema_unavailable": "The model's declared options are unavailable.",
    "unsupported_unit": "fal prices this model in a unit that is not supported yet.",
    "billed_by_usage": "fal bills this model by what each run uses, which is not known in advance.",
    "depends_on_input": "The cost depends on the input media.",
    "needs_option": "An option the price depends on is not declared.",
    "length_not_declared": "The model sets its own length without a declared limit.",
    "needs_prompt": "Priced by text length; the prompt is needed for a price.",
    "rate_not_listed": "fal lists no rate for the chosen options.",
    "invalid_options": "The options do not match the model.",
}


class PricingRequestFailed(Exception):
    pass


def unknown(reason: str, detail: str | None = None, row: dict | None = None) -> dict:
    price = {**UNKNOWN_PRICE, "reason": reason, "detail": detail or REASONS.get(reason, "")}
    rate = rate_of(row)
    if rate:
        price["rate"] = rate
    return price


def rate_of(row: dict | None) -> dict | None:
    if not isinstance(row, dict) or row.get("currency") != "USD" or not isinstance(row.get("unit"), str):
        return None
    try:
        amount = Decimal(str(row["unit_price"]))
    except (KeyError, InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return {"amount": str(amount), "unit": row["unit"].strip()}


def price_from_row(row: dict | None, schema, options: dict | None, prompt: str | None) -> dict:
    """The price for ``options`` (merged over the schema's declared defaults)."""
    from app.services.fal_schema import PricingUnknown

    if row is None:
        return unknown("no_pricing_row")
    rate = rate_of(row)
    if rate is None:
        return unknown("no_pricing_row", "fal lists no usable USD rate for this model.")
    if schema is None:
        return unknown("schema_unavailable", row=row)
    defaults = {f.name: f.default for f in schema.fields if not f.is_primary_text and f.default is not None}
    try:
        # Discovery and execution resolve exactly the same defaults. Partial
        # validation permits models with required per-call media.
        values = schema.validate_options({**defaults, **(options or {})}, "", partial=True)
    except ValueError:
        return unknown("invalid_options", row=row)
    notes: list[str] = []
    try:
        quantity, count = schema.pricing_quantity(row["unit"], values, prompt, notes)
    except PricingUnknown as exc:
        return unknown(exc.reason, str(exc), row)
    except (ValueError, ArithmeticError, TypeError):
        return unknown("invalid_options", row=row)
    amount = Decimal(rate["amount"]) * quantity
    price = {"currency": "USD", "unit": "item", "amount": str(amount), "total": str(amount * count), "status": "estimated"}
    if notes:
        price["basis"] = "; ".join(dict.fromkeys(notes))
    return price


class FalRates:
    """Batched, cached fal rate rows keyed by the calling key (rates can be
    account-specific). A failed or malformed reply is never cached. The cache
    serves discovery only: admitting a run reads ``fresh`` so a raised rate
    is never reserved or confirmed at its old figure."""

    def __init__(self):
        self._cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _scope(api_key: str | None) -> str:
        return hashlib.sha256((api_key or "").encode()).hexdigest()[:16]

    def clear(self):
        self._cache.clear()

    async def rows(self, endpoint_ids, api_key: str | None, *, fresh: bool = False) -> dict[str, dict | None]:
        """``{endpoint: row or None}``; None means fal lists no price.
        Raises :class:`PricingRequestFailed` when fal could not be read;
        with ``fresh`` that is never answered from the cache."""
        scope = self._scope(api_key)
        now = time.time()
        found: dict[str, dict | None] = {}
        wanted = []
        for endpoint in dict.fromkeys(e for e in endpoint_ids if isinstance(e, str) and e):
            cached = None if fresh else self._cache.get((scope, endpoint))
            if cached and cached[0] > now:
                found[endpoint] = cached[1]
            else:
                wanted.append(endpoint)
        if not wanted:
            return found
        if fresh:
            # One endpoint, bounded, and not queued behind a catalog page's
            # batches on the lock.
            try:
                async with asyncio.timeout(FRESH_READ_SECONDS):
                    return await self._fetch(wanted, api_key, scope)
            except TimeoutError:
                raise PricingRequestFailed(f"fal pricing did not answer within {FRESH_READ_SECONDS} s.") from None
        async with self._lock:
            return {**found, **await self._fetch(wanted, api_key, scope)}

    async def _fetch(self, wanted, api_key, scope) -> dict[str, dict | None]:
        found: dict[str, dict | None] = {}
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            queue = [wanted[i:i + BATCH] for i in range(0, len(wanted), BATCH)]
            while queue:
                chunk = queue.pop(0)
                response = await self._get(client, chunk, api_key)
                if response.status_code == 404 and len(chunk) > 1:
                    # fal answers 404 for a whole batch naming one retired
                    # endpoint; split it so the rest still price.
                    half = len(chunk) // 2
                    queue[:0] = [chunk[:half], chunk[half:]]
                    continue
                if response.status_code == 404:
                    rows = {}
                elif response.status_code != 200:
                    raise PricingRequestFailed(f"fal pricing returned HTTP {response.status_code}.")
                else:
                    rows = self._parse(response)
                stamp = time.time()
                for endpoint in chunk:
                    row = rows.get(endpoint)
                    found[endpoint] = row
                    if row is None or rate_of(row) is not None:
                        # A listed row fal answered with no usable rate is
                        # read again next time, not remembered.
                        self._cache[(scope, endpoint)] = (stamp + (FOUND_TTL if row else MISSING_TTL), row)
        return found

    @staticmethod
    def _parse(response) -> dict[str, dict]:
        """A 200 reply must be ``{"prices": [row, ...]}``; anything else is a
        failed read (uncached), never "fal lists no price"."""
        try:
            prices = response.json().get("prices")
        except (ValueError, AttributeError):
            prices = None
        if not isinstance(prices, list) or not all(
                isinstance(r, dict) and isinstance(r.get("endpoint_id"), str) for r in prices):
            raise PricingRequestFailed("fal pricing returned an unreadable reply.")
        return {r["endpoint_id"]: r for r in prices}

    @staticmethod
    async def _get(client, chunk, api_key):
        for delay in (*RETRY_DELAYS, None):
            try:
                response = await client.get(PRICING_URL, params=[("endpoint_id", e) for e in chunk],
                                            headers={"Authorization": "Key " + (api_key or "")})
            except httpx.HTTPError:
                raise PricingRequestFailed("fal pricing could not be reached.") from None
            if response.status_code != 429 or delay is None:
                return response
            await asyncio.sleep(delay)
        return response


rates = FalRates()
