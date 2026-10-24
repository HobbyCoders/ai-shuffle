"""Pure watch value helpers; safe on the event loop (no database imports)."""
import json
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc)


def at(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
