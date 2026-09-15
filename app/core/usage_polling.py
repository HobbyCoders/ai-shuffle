"""Durable, credential-scoped budgets for account usage requests."""

import hashlib
import math
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.db.async_database import run_sync
from app.db.base import get_db


class UsageDeferred(Exception):
    """A cached read must not bypass an upstream cooldown."""


def identity(url, headers):
    # Include scope but never retain a credential in the state table or logs.
    credential = headers.get("Authorization", "")
    org = headers.get("OpenAI-Organization", "")
    return hashlib.sha256(f"{url}\0{org}\0{credential}".encode()).hexdigest()


def claim(key, now):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO usage_poll_state (identity, next_attempt, failures, paused)
            VALUES (%s, %s, 0, FALSE)
            ON CONFLICT(identity) DO UPDATE SET next_attempt = EXCLUDED.next_attempt
            WHERE usage_poll_state.paused = FALSE AND usage_poll_state.next_attempt <= %s
            RETURNING identity
        """, (key, now + 600, now))
        return cur.fetchone() is not None


def finish(key, now, status, retry_after=0, interval=300):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT failures FROM usage_poll_state WHERE identity = %s FOR UPDATE", (key,))
        row = cur.fetchone()
        if row is None:
            return
        success = status is not None and 200 <= status < 300
        failures = 0 if success else min(int(row["failures"]) + 1, 10)
        delay = interval if success else min(3600, interval * 2 ** (failures - 1))
        delay = max(delay + random.uniform(0, 30), retry_after)
        cur.execute("""
            UPDATE usage_poll_state SET next_attempt = %s, failures = %s, paused = %s
            WHERE identity = %s
        """, (now + delay, failures, status in (401, 403), key))


def resume_authentication(url, headers):
    """Only explicit credential configuration resumes an authentication refusal."""
    from app.db.base import get_db
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE usage_poll_state SET paused = FALSE, next_attempt = 0, failures = 0 WHERE identity = %s AND paused = TRUE",
            (identity(url, headers),),
        )


def retry_seconds(value, now):
    if not isinstance(value, (str, int, float)):
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0


async def usage_get(url, *, headers, params=None, timeout=20, paginate=False, interval=300):
    """GET a supported usage endpoint, at most once per cooldown.

    A paginated observation owns one lease. Partial observations never escape.
    Authentication refusals stay paused across restart, until the key changes.
    """
    key = identity(url, headers)
    if not await run_sync(claim, key, time.time()):
        raise UsageDeferred()
    status, retry = None, 0
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            query = dict(params or {})
            buckets, cursors = [], set()
            for _ in range(8 if paginate else 1):
                response = await client.get(url, headers=headers, **({"params": dict(query)} if query else {}))
                status = response.status_code
                retry = retry_seconds(response.headers.get("Retry-After"), time.time())
                if not paginate or status != 200:
                    return response
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                    raise ValueError("Invalid usage response")
                buckets.extend(body["data"])
                if not body.get("has_more"):
                    return httpx.Response(200, json={"data": buckets}, request=response.request)
                cursor = body.get("next_page")
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise ValueError("Invalid usage pagination")
                cursors.add(cursor)
                query["page"] = cursor
            raise ValueError("Usage pagination limit reached")
    except BaseException:
        # Includes cancellation: no immediate retry after an interrupted batch.
        status = None
        raise
    finally:
        await run_sync(finish, key, time.time(), status, retry, interval)
