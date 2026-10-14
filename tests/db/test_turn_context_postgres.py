"""Context inheritance must use PostgreSQL's actual NULL ordering."""
from uuid import uuid4

import pytest

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401

pytestmark = requires_real_pg


def test_terminal_context_prefers_ended_turn_over_legacy_null():
    from app.core import turn_scheduler
    from app.db import base, turns

    sid = "pg-context-" + uuid4().hex
    with base.get_db() as conn:
        conn.cursor().execute("INSERT INTO sessions (id) VALUES (%s)", (sid,))
    recent = turns.enqueue_turn(sid, "user", profile_id="recent-profile")
    turns.start_turn(recent["id"], None, "runner")
    turns.settle_turn(recent["id"], "completed")
    legacy = turns.enqueue_turn(sid, "agent_message")
    with base.get_db() as conn:
        conn.cursor().execute(
            "UPDATE turns SET state='completed', ended_at=NULL WHERE id=%s", (legacy["id"],),
        )
    context = turn_scheduler._latest_context_turn(sid)
    assert context["id"] == recent["id"]
    assert context["profile_id"] == "recent-profile"
