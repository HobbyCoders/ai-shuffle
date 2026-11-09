"""Pure, host-independent wall-clock context; no locale, settings or I/O."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

RELATIVE_TIME_RULE = (
    "Resolve relative dates in older messages against their send time before repeating them; "
    "use absolute dates in anything that will be read later."
)


def time_zone(name: str | None):
    """An unusable/local/unavailable IANA zone is explicitly labelled UTC."""
    if name and name != "local":
        try:
            return ZoneInfo(name), name
        except (ValueError, TypeError, KeyError, OSError):
            pass
    return UTC, "UTC; fallback — owner timezone unavailable"


def as_utc(value) -> datetime | None:
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, (float, int)):
            dt = datetime.fromtimestamp(value, UTC)
        elif isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return None
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        return None


def absolute_time(value, zone: str | None = None) -> str:
    """Stable absolute send-time rendering, including offset across DST folds."""
    dt = as_utc(value)
    if dt is None:
        return "unknown time"
    tz, label = time_zone(zone)
    return f"{dt.astimezone(tz):%Y-%m-%d %H:%M:%S %Z %z} ({label})"


def turn_time_lines(*, zone: str | None = None, last_owner_at=None,
                    now: datetime | None = None) -> list[str]:
    now = as_utc(now) or datetime.now(UTC)
    tz, label = time_zone(zone)
    local = now.astimezone(tz)
    # Explicit English names and portable hour formatting (no %-I on Windows).
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    def wall(dt):
        return (f"{days[dt.weekday()]} {months[dt.month - 1]} {dt.day}, {dt.year}, "
                f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'} {dt:%Z}")
    lines = [f"Current time: {wall(local)} ({label})"]
    last = as_utc(last_owner_at)
    if last is not None:
        seconds = (now - last).total_seconds()
        previous = last.astimezone(tz)
        if seconds >= 0 and (seconds >= 3600 or previous.date() != local.date()):
            n = int(seconds // 3600) if seconds >= 3600 else int(seconds // 60)
            unit = "hour" if seconds >= 3600 else "minute"
            lines.append(f"Owner's previous message: {wall(previous)} ({n} {unit}{'' if n == 1 else 's'} ago)")
    return lines
