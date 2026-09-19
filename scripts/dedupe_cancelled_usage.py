"""Identity-based duplicate cancelled-usage report (read-only).

A safe reconciliation capability for historical #1407 duplicates. Never
writes; never infers a duplicate from equal token totals. Only a shared
``settlement_key`` — the durable per-execution-attempt identity the #1407
settlement writes — is evidence that two usage_log rows book the same work.
Rows written before that column existed carry no key, so they can never be
paired by this report. They are explicitly reported as not assessable, with
row IDs for evidence collection. Do not delete or adjust them from totals alone;
reconciliation needs the original per-call engine events and a proven mapping
from each booked row to its execution attempt.

Usage (dry-run by construction — the tool has no write path)::

    python -m scripts.backfill_usage_from_events --dedupe-cancelled
    python -m scripts.backfill_usage_from_events --dedupe-cancelled --session-id ses-abc
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger("backfill_usage")


def report_duplicate_cancelled_usage(session_id=None) -> int:
    """Print usage_log rows sharing one settlement key; nothing else qualifies."""
    from app.db.base import get_db
    from app.db.compat import ph

    where = ""
    params: list = []
    if session_id:
        where = f" WHERE session_id = {ph()}"
        params = [session_id]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, session_id, settlement_key, tokens_in, tokens_out, "
            "cache_read_tokens, cache_creation_tokens, cost_usd, created_at "
            f"FROM usage_log{where} ORDER BY session_id, created_at", params)
        rows = [dict(r) for r in cursor.fetchall()]

    groups: Dict[Any, list] = {}
    for row in rows:
        groups.setdefault(row.get("session_id"), []).append(row)

    flagged = 0
    for sid, srows in sorted(groups.items(), key=lambda kv: str(kv[0])):
        seen: Dict[Any, list] = {}
        unkeyed = [r for r in srows if not r.get("settlement_key")]
        if unkeyed:
            ids = ", ".join(str(r["id"]) for r in unkeyed)
            print(f"  {sid}: NOT ASSESSABLE — {len(unkeyed)} historical row(s) "
                  f"without execution identity; ids={ids}. No deletion or "
                  "counter correction proposed. Obtain original engine Usage "
                  "events and attempt-to-booking evidence; equal totals do not "
                  "prove duplicate work.")
        for r in srows:
            key = r.get("settlement_key")
            if not key:
                # No identity, no report: equal tokens are not evidence.
                logger.debug(
                    "session %s usage_log id=%s carries no settlement key; "
                    "no identity evidence — not reported", sid, r["id"])
                continue
            seen.setdefault(key, []).append(r)
        for key, dupes in seen.items():
            if len(dupes) > 1:
                flagged += 1
                print(f"  {sid} settlement_key={key}: {len(dupes)} rows")
                for r in dupes:
                    print(
                        f"    id={r['id']} created={r['created_at']} "
                        f"in={r['tokens_in']} out={r['tokens_out']} "
                        f"cache_r={r['cache_read_tokens']} "
                        f"cache_c={r['cache_creation_tokens']} "
                        f"cost={r['cost_usd']}"
                    )
    logger.info("%d duplicate settlement group(s) found", flagged)
    return 0
