"""Approval for a generation above the price threshold.

In the app the owner decides on the regular permission prompt (:func:`ask_on_prompt`).
Elsewhere the tool hands the agent a one-use receipt, which survives the next
turn's tool-server rebuild.
"""
import json
import re
import secrets
import time
import uuid

from app.db.async_database import run_sync
from app.db.base import get_db

PREFIX = "network_generation_confirmation:"
SPEND_TOOL = "ApproveGenerationSpend"
TTL = 15 * 60


def _expired(value, now):
    try:
        return not float(json.loads(value)["expires"]) > now
    except (ValueError, TypeError, KeyError):
        return True


def spend_request(kind, candidate, prompt) -> dict:
    """The request the owner approves, which also identifies its retry (#523)."""
    from app.core.generation_resolver import canonical_price

    price = canonical_price(candidate.price or {})
    request = {"kind": kind, "prompt": prompt, "model": candidate.model, "account": candidate.account,
               "worker": candidate.worker, "options": candidate.options,
               "price": str(price.get("total") or price.get("amount") or "?")}
    rate = price.get("rate") if price.get("total") is None else None
    if isinstance(rate, dict):
        # An unpriced run (generation_resolver.INTERACTIVE_UNPRICED_APPROVAL).
        request["price"] = "not bounded"
        request["rate"] = rate_text(rate)
    return request


def rate_text(rate) -> str:
    """fal's raw rate, e.g. "$0.30 per second" or "$0.0214 per 1000 tokens"."""
    from decimal import Decimal, InvalidOperation

    unit = str(rate.get("unit") or "").strip() or "unit"
    if re.fullmatch(r"[a-z]+s", unit):
        unit = unit[:-1]
    amount = rate.get("amount")
    try:
        # One text per amount ("0.30" and "0.300" match an approval), cents kept.
        value = Decimal(str(amount)).normalize()
        amount = f"{value.quantize(Decimal('0.01')) if value.as_tuple().exponent > -2 else value:f}"
    except (InvalidOperation, ValueError, TypeError):
        pass
    return f"${amount} per {unit}"


def unpriced_question(kind, candidate) -> str:
    price = candidate.price or {}
    why = price.get("detail") or "The total is not known in advance."
    return (f"Run {candidate.model} for this {kind} with no price limit? fal charges {rate_text(price.get('rate') or {})}. "
            f"{why} The total is not bounded.")


async def ask_on_prompt(session_id, profile_id, kind, candidate, prompt):
    """Ask on the regular permission prompt. None when approved, else why not.

    Worded and shaped so a retry of the same request matches this one: an owner
    who approves after sending another message is not asked twice (#523).
    """
    from app.core import permission_handler as handler
    from app.core import provider_agent_approvals

    async def no_origin_socket(_event):
        pass  # permission_handler also broadcasts to the authenticated owner.

    price = candidate.price or {}
    tool_input = spend_request(kind, candidate, prompt)
    amount = tool_input["price"]
    cost = f"${amount}" if price.get("currency") == "USD" else f"{amount} {price.get('currency') or ''}".strip()
    question = (unpriced_question(kind, candidate) if tool_input["price"] == "not bounded"
                else f"Spend about {cost} to generate this {kind} with {candidate.model}?")
    decision = await provider_agent_approvals.route_permission({
        "request_id": f"generation-{uuid.uuid4()}",
        "session_id": session_id,
        "profile_id": profile_id or "",
        "tool_name": SPEND_TOOL,
        "tool_input": tool_input,
        "broadcast_func": no_origin_socket,
        "title": question,
        "display_name": "Approve spend",
        "description": question,
        "skip_saved_rules": True,
        "safety_check": True,
    }, handler.permission_handler.request_permission)
    if isinstance(decision, handler.PermissionResultAllow):
        return None
    return getattr(decision, "message", "") or "The owner did not approve this spend. Nothing was generated or charged."


class Confirmations:
    async def issue(self, fingerprint):
        token = secrets.token_urlsafe(24)
        await run_sync(self._issue, token, fingerprint)
        return token

    @staticmethod
    def _issue(token, fingerprint):
        with get_db() as conn:
            cursor = conn.cursor()
            # Receipts that expired unused are removed here, so declined
            # confirmations do not accumulate in system_settings.
            cursor.execute("SELECT key, value FROM system_settings WHERE key LIKE %s", (PREFIX + "%",))
            now = time.time()
            for row in cursor.fetchall():
                if row["key"].startswith(PREFIX) and _expired(row["value"], now):
                    cursor.execute("DELETE FROM system_settings WHERE key=%s", (row["key"],))
            cursor.execute("INSERT INTO system_settings (key,value) VALUES (%s,%s)",
                           (PREFIX + token, json.dumps({"fingerprint": fingerprint, "expires": now + TTL})))

    async def consume(self, token, fingerprint):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
            return False
        return await run_sync(self._consume, token, fingerprint)

    @staticmethod
    def _consume(token, fingerprint):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM system_settings WHERE key=%s RETURNING value", (PREFIX + token,))
            row = cursor.fetchone()
        if not row:
            return False
        data = json.loads(row["value"])
        return data["expires"] > time.time() and secrets.compare_digest(data["fingerprint"], fingerprint)
