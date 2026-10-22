"""Durable, conservative generation reservations against the existing daily cap.

Reservations survive process crashes and unknown provider outcomes. They remain
charged until the day rolls over; generation errors must not authorize retries
that could spend the same budget twice. Networked callers reserve on the founder.
"""
from decimal import Decimal
import json

from app.db.base import get_db

PREFIX = "network_generation_spend:"


def reserved(cursor, day):
    cursor.execute("SELECT value FROM system_settings WHERE key=%s", (PREFIX + day,))
    row = cursor.fetchone()
    return Decimal(json.loads(row["value"])["reserved"]) if row else Decimal(0)


def reserve(amount):
    return _record(amount, enforce_limit=True)


def record_overrun(amount):
    """Record already-incurred cost above a reservation, even beyond the cap.

    Admission must still use reserve(); this never authorizes a submission.
    """
    return _record(amount, enforce_limit=False)


def _record(amount, *, enforce_limit):
    from app.core import assistant_limits

    from decimal import InvalidOperation
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid generation reservation.") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("Invalid generation reservation.")
    day = assistant_limits.day_window()[0]
    key = PREFIX + day
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO system_settings (key,value) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                       (key, '{"reserved":"0"}'))
        cursor.execute("SELECT value FROM system_settings WHERE key=%s FOR UPDATE", (key,))
        current = Decimal(json.loads(cursor.fetchone()["value"])["reserved"])
        # The locked row serializes generation admission; status sees committed
        # reservations through its own connection and includes ordinary AI usage.
        state = assistant_limits.status()
        spent = Decimal(str(assistant_limits._measure()["spent"]))
        if enforce_limit and state["spend_limit_enabled"] and spent + amount > Decimal(str(state["daily_spend_limit_usd"])):
            return False
        cursor.execute("UPDATE system_settings SET value=%s WHERE key=%s",
                       (json.dumps({"reserved": str(current + amount)}), key))
    return True
