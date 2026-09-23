"""Durable, credential-scoped budgets for account usage requests."""

import asyncio
import hashlib
import math
import random
import secrets
import time
from email.utils import parsedate_to_datetime

import httpx

from app.db.base import get_db


class UsageDeferred(Exception):
    """A cached read must not bypass an upstream cooldown."""


# Authentication refusals and permanently unsupported capabilities should not
# be retried on the normal exponential schedule.  The state row is durable,
# so this negative cache also survives a worker restart.  A changed
# credential produces a different identity; callers that repair/reconfigure
# the same credential use ``resume_authentication`` to clear the old state.
_PAUSE_STATUSES = frozenset((401, 403, 405, 410, 501))


def identity(url, headers):
    # Include scope but never retain a credential in the state table or logs.
    credential = headers.get("Authorization", "")
    org = headers.get("OpenAI-Organization", "")
    return hashlib.sha256(f"{url}\0{org}\0{credential}".encode()).hexdigest()


def capability_fingerprint(source_id, headers, revision=""):
    """Digest naming one credential revision's access to a usage capability.

    Stored with a refusal so a known-denied capability is not re-probed. A
    changed credential, organization or stored revision yields a new digest,
    which invalidates the negative cache without any explicit cleanup.
    """
    credential = headers.get("Authorization", "")
    org = headers.get("OpenAI-Organization", "")
    return hashlib.sha256(f"{source_id}\0{org}\0{credential}\0{revision or ''}".encode()).hexdigest()


def claim(key, now, interval=300):
    lease = secrets.token_hex(16)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO usage_poll_state (identity, next_attempt, failures, paused, lease_token)
            VALUES (%s, %s, 0, FALSE, %s)
            ON CONFLICT(identity) DO UPDATE SET next_attempt = EXCLUDED.next_attempt,
                lease_token = EXCLUDED.lease_token
            WHERE usage_poll_state.paused = FALSE AND usage_poll_state.next_attempt <= %s
            RETURNING identity
        """, (key, now + max(600, interval + 30), lease, now))
        return lease if cur.fetchone() is not None else None


def finish(key, now, status, retry_after=0, interval=300, *, lease=None):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT failures, lease_token FROM usage_poll_state WHERE identity = %s FOR UPDATE", (key,))
        row = cur.fetchone()
        if row is None or (lease is not None and row["lease_token"] != lease):
            return
        success = status is not None and 200 <= status < 300
        failures = 0 if success else min(int(row["failures"]) + 1, 10)
        delay = interval if success else min(3600, interval * 2 ** (failures - 1))
        delay = max(delay + random.uniform(0, 30), retry_after)
        cur.execute("""
            UPDATE usage_poll_state SET next_attempt = %s, failures = %s, paused = %s, lease_token = NULL
            WHERE identity = %s
        """, (now + delay, failures, status in _PAUSE_STATUSES, key))


def resume_authentication(url, headers):
    """Only explicit credential configuration resumes an authentication refusal."""
    resume_identity(identity(url, headers))


def resume_identity(key):
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE usage_poll_state SET paused = FALSE, next_attempt = 0, failures = 0, lease_token = NULL WHERE identity = %s AND paused = TRUE",
            (key,),
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
    from app.network.usage_budget import acquire
    budget = await acquire(key, interval)
    status, retry = None, 0
    try:
        # A socket read timeout alone doesn't bound a trickling response. Finish
        # before the coordinator's ten-minute lease can expire and be reclaimed.
        async with asyncio.timeout(240), httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
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
        await budget.finish(status, retry, interval)
