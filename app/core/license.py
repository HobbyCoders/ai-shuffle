"""Settings-only Cloud plan information from the signed-in account.

The hub enforces remote capacity. This cache is never an authorization verdict
and local use never depends on a plan, a hub response, or old saved state.
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

from app.core import account_hub
from app.db import mobile_hub

PLAN_TTL = 600.0
RETRY_INTERVAL = 30.0
PLAN_TIMEOUT = 2.0
_LOCK = threading.Lock()
_entry = None
_generation = 0


def _identity(binding):
    if not binding:
        return None
    return tuple(binding.get(k) for k in (
        "hub_url", "server_id", "linked_by", "encrypted_secret", "instance_id"))


def invalidate_plan_cache():
    """Refresh on the next status read, including after a tunnel-limit refusal."""
    global _generation
    with _LOCK:
        _generation += 1
        if _entry is not None:
            _entry["expires"] = 0


def _free_plan():
    return {"plan": "free", "status": "none", "remote_worker_limit": 2,
            "seats": 2, "used": None, "renews_at": None}


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100


def _normalize(value):
    if value.get("plan") not in ("free", "paid") or value.get("status") not in (
        "active", "trialing", "grace", "none"
    ):
        raise ValueError("Invalid account plan")
    paid = value["plan"] == "paid" and value["status"] in ("active", "trialing", "grace")
    limit = value.get("remote_worker_limit")
    if not _count(limit) or limit < 1:
        limit = 4 if paid else 2
    if not paid and value["plan"] == "paid":
        limit = 2
    seats = value.get("seats")
    if seats is not None and not _count(seats):
        raise ValueError("Invalid account seats")
    used = value.get("used")
    if used is not None and not _count(used):
        raise ValueError("Invalid account usage")
    renewal = value.get("renews_at")
    if renewal is not None:
        if not isinstance(renewal, (int, float)) or isinstance(renewal, bool):
            raise ValueError("Invalid account renewal")
        # Validate the epoch before caching so malformed metadata falls back to
        # the last good response rather than breaking Settings.
        datetime.fromtimestamp(renewal / 1000, timezone.utc)
    return {"plan": "paid" if paid else "free", "status": value["status"],
            "remote_worker_limit": limit, "seats": seats if paid else limit,
            "used": used, "renews_at": renewal}


def _account_plan(binding):
    global _entry
    key = _identity(binding)
    with _LOCK:
        entry = _entry
        if entry is None or entry["key"] != key:
            entry = {"key": key, "value": _free_plan(), "expires": 0, "fetching": False}
            _entry = entry
        if entry["fetching"] or time.monotonic() < entry["expires"]:
            return dict(entry["value"])
        entry["fetching"] = True
        generation = _generation
    value = None
    ttl = PLAN_TTL
    try:
        value = _normalize(account_hub.call(binding, "GET", "plan", timeout=PLAN_TIMEOUT))
    except Exception as exc:
        if account_hub.hub_status(exc) == 404:
            value = _free_plan()  # Hub rollout compatibility, not a lease fallback.
        else:
            ttl = RETRY_INTERVAL
    finally:
        with _LOCK:
            if value is not None:
                entry["value"] = value
            entry["expires"] = time.monotonic() + ttl if generation == _generation else 0
            entry["fetching"] = False
    return dict(entry["value"])


def status() -> dict:
    """Retain the legacy response shape, with no key or lease functionality."""
    binding = mobile_hub.get_binding()
    key = _identity(binding)
    linked = key is not None
    plan = _account_plan(binding) if linked else None
    # An in-flight response must never reveal the previous account after relink.
    current_key = _identity(mobile_hub.get_binding())
    if current_key != key:
        linked = current_key is not None
        plan = _free_plan() if linked else None
    result = {
        "source": "account", "mode": "cloud", "state": "not_linked",
        "linked": linked, "allowed": True, "reason": None, "banner": None,
        "message": "Not linked to AI Shuffle Cloud. Local use is free.",
        "remote_worker_limit": 0, "lease_state": "none", "plan": None,
        "seats": 0, "seats_used": None, "account_id": None, "license_id": None,
        "key_hint": None, "install_id": None, "issued_at": None, "expires_at": None,
        "renews_at": None, "grace": False, "grace_until": None,
        "last_refresh_at": None, "next_refresh_at": None, "last_error": None,
        "server_status": None, "server_configured": False,
    }
    if plan is not None:
        paid = plan["plan"] == "paid"
        limit = plan["remote_worker_limit"]
        renewal = plan["renews_at"]
        result.update(
            state=plan["plan"], plan=plan["plan"], seats=plan["seats"],
            seats_used=plan["used"], remote_worker_limit=limit,
            message=f"{'Paid plan' if paid else 'Free account'}: {limit} remote workers.",
            renews_at=renewal,
            expires_at=datetime.fromtimestamp(renewal / 1000, timezone.utc).isoformat() if renewal is not None else None,
            grace=paid and plan["status"] == "grace",
        )
    return result
