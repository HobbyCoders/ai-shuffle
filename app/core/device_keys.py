"""Bounded mobile credentials with retry-safe, device-authenticated rotation."""
from datetime import datetime, timedelta, timezone

KEY_LIFETIME_DAYS = 90
RENEW_BEFORE_DAYS = 30
LEGACY_KEY_LIFETIME_DAYS = 365


def key_expiry(device):
    value = device.get('api_key_expires_at')
    legacy = value is None
    value = value or device.get('created_at')
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value + timedelta(days=LEGACY_KEY_LIFETIME_DAYS) if legacy else value
