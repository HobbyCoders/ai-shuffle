"""Durable pull-request watches (app/core/pr_watch.py).

One row per watched pull request. ``state`` holds what the last poll saw and
what is waiting to be delivered; ``wakes`` lists the agent turns the watch
started (for the per-day caps and the cost tally). At most one active watch
exists per repository and number.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.db.base import get_db, row_to_dict

STATUSES = ("active", "stopped", "ended")
_JSON = ("state", "wakes")


def create_schema(cursor: Any) -> None:
    cursor.execute("""CREATE TABLE IF NOT EXISTS pr_watches (
        id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        worker_id TEXT NOT NULL,
        repo TEXT NOT NULL,
        pr_number INTEGER NOT NULL,
        pr_url TEXT,
        pr_title TEXT,
        session_id TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        reason TEXT,
        state JSONB NOT NULL DEFAULT '{}',
        wakes JSONB NOT NULL DEFAULT '[]',
        next_poll_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_error TEXT,
        failures INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pr_watches_active "
        "ON pr_watches(repo, pr_number) WHERE status = 'active'"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pr_watches_due ON pr_watches(worker_id, status, next_poll_at)"
    )


def parse(row: Any) -> Optional[dict[str, Any]]:
    row = row_to_dict(row)
    if row:
        for key in _JSON:
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
        row.setdefault("state", {})
        row.setdefault("wakes", [])
        if row["state"] is None:
            row["state"] = {}
        if row["wakes"] is None:
            row["wakes"] = []
    return row


def get(wid: str) -> Optional[dict[str, Any]]:
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM pr_watches WHERE id=%s", (wid,))
        return parse(c.fetchone())


def get_active(repo: str, pr_number: int) -> Optional[dict[str, Any]]:
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM pr_watches WHERE LOWER(repo)=LOWER(%s) AND pr_number=%s AND status='active'",
            (repo, int(pr_number)),
        )
        return parse(c.fetchone())


def create(
    *,
    owner: str,
    worker_id: str,
    repo: str,
    pr_number: int,
    pr_url: Optional[str],
    pr_title: Optional[str],
    session_id: Optional[str],
) -> dict[str, Any]:
    """Insert an active watch, or return the one already watching this PR."""
    wid = str(uuid.uuid4())
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO pr_watches (id, owner, worker_id, repo, pr_number, pr_url, pr_title, session_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (repo, pr_number) WHERE status = 'active' DO NOTHING""",
            (wid, owner, worker_id, repo, int(pr_number), pr_url, pr_title, session_id),
        )
    row = get(wid) or get_active(repo, pr_number)
    if row is None:
        raise ValueError("Could not create the pull request watch")
    return row


def list_watches(owner: Optional[str] = None, *, recent_hours: int = 24) -> list[dict[str, Any]]:
    """Active watches plus those that stopped or ended in the last ``recent_hours``."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=recent_hours)
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM pr_watches WHERE (status='active' OR updated_at > %s)"
            + (" AND owner=%s" if owner else "")
            + " ORDER BY created_at DESC, id",
            (cutoff, owner) if owner else (cutoff,),
        )
        return [parse(r) for r in c.fetchall()]


def recent_wakes(repo: str, pr_number: int, *, since: datetime, exclude: str) -> list[dict[str, Any]]:
    """Wakes since ``since`` recorded by this PR's other watches, oldest first.

    Each keeps the chat it ran in, so its cost is still read from that chat.
    """
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM pr_watches WHERE LOWER(repo)=LOWER(%s) AND pr_number=%s AND id<>%s "
            "AND updated_at > %s ORDER BY created_at, id",
            (repo, int(pr_number), exclude, since),
        )
        rows = [parse(r) for r in c.fetchall()]
    out: list[dict[str, Any]] = []
    for row in rows:
        for wake in row["wakes"]:
            if not isinstance(wake, dict):
                continue
            try:
                at = datetime.fromisoformat(str(wake.get("at")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) > since:
                out.append({**wake, "session_id": wake.get("session_id") or row.get("session_id")})
    return sorted(out, key=lambda w: str(w.get("at")))


def list_due(worker_id: str, now: datetime, limit: int = 20) -> list[dict[str, Any]]:
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM pr_watches WHERE worker_id=%s AND status='active' AND next_poll_at <= %s "
            "ORDER BY next_poll_at, id LIMIT %s",
            (worker_id, now, int(limit)),
        )
        return [parse(r) for r in c.fetchall()]


def save(wid: str, **fields: Any) -> None:
    """Write the given columns of an active watch (a stopped watch is left alone)."""
    allowed = {
        "state", "wakes", "next_poll_at", "last_activity_at", "last_error", "failures",
        "pr_url", "pr_title", "session_id",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown pr_watches columns: {sorted(unknown)}")
    if not fields:
        return
    names = sorted(fields)
    values = [json.dumps(fields[n]) if n in _JSON else fields[n] for n in names]
    sets = ", ".join(f"{n}=%s" for n in names)
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            f"UPDATE pr_watches SET {sets}, updated_at=NOW() WHERE id=%s AND status='active'",
            (*values, wid),
        )


def set_status(wid: str, status: str, reason: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Stop or end an active watch. Stopped and ended are final."""
    if status not in STATUSES or status == "active":
        raise ValueError("A watch can only be stopped or ended")
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE pr_watches SET status=%s, reason=%s, updated_at=NOW() WHERE id=%s AND status='active'",
            (status, reason, wid),
        )
    return get(wid)
