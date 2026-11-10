"""A connector delete that arrives by sync purges its Telegram journal (#1399).

The delete is made on worker A with the real ``delete_connector`` and reaches
worker B through the real sync exchange and apply. B holds journal content,
replay heads, a watch and a pending transfer snapshot for that connector;
after the apply, B must hold what a local delete on B would have left.
"""
import pytest

from app.db import database, watch_journal
from app.db import watches as watch_store
from app.db.base import get_db, parse_jsonb
from app.network import engine, policy
from tests.core.network.harness import converge, exchange, join

BOT = "bot-1399"


def _journal_rows(cid):
    counts = {}
    with get_db() as conn:
        c = conn.cursor()
        for table in ("watch_connector_inbox", "watch_connector_heads"):
            c.execute("SELECT COUNT(*) AS n FROM " + table + " WHERE connector_id=%s", (cid,))
            counts[table] = c.fetchone()["n"]
        c.execute("SELECT fence FROM watch_connector_journals WHERE connector_id=%s", (cid,))
        row = c.fetchone()
        counts["fence"] = row["fence"] if row else None
    return counts


def _seed_journal(cid):
    """What a Telegram receiver and a started watch transfer leave on a worker."""
    wid = watch_store.create(
        owner="admin", creating_session_id=None, worker_id="here", agent_id="agent",
        description="Telegram updates", source={"kind": "connector", "connector_id": cid, "channel_id": "123"},
        filter={}, action="alert", permissions={"read_only": True, "tools": []},
        daily_budget_usd=1, cursor={"seq": 0})
    watch_store.record_connector_event(cid, "message_received:123:1",
                                       {"platform_chat_id": "123", "text": "private unread text"})
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO watch_transfers (watch_id,token,target,snapshot) VALUES (%s,%s,%s,%s)",
            (wid, "token", "elsewhere", '{"journal": {"entries": [{"text": "private unread text"}]}}'))
    assert _journal_rows(cid) == {"watch_connector_inbox": 1, "watch_connector_heads": 1, "fence": ""}
    return wid


@pytest.fixture
def pair(workers):
    a, b = workers("worker-a"), workers("worker-b")
    with a.active():
        engine.create_network("Home", device_name="Worker A")
    join(a, b)
    with a.active():
        database.create_connector(BOT, "telegram", "Bot", {}, [], is_active=False)
        engine.scan_all()
    converge([a, b])
    with b.active():
        assert database.get_connector(BOT) is not None
    return a, b


def test_a_synced_delete_purges_the_journal_like_a_local_delete(pair):
    a, b = pair
    with b.active():
        wid = _seed_journal(BOT)

    with a.active():
        assert database.delete_connector(BOT)
        engine.scan_all()
    exchange(a, b)

    with b.active():
        assert database.get_connector(BOT) is None
        assert _journal_rows(BOT) == {"watch_connector_inbox": 0, "watch_connector_heads": 0,
                                      "fence": "deleted"}
        watch = watch_store.get(wid)
        assert watch["status"] == "paused" and watch["cursor"]["expired"]
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT snapshot FROM watch_transfers WHERE watch_id=%s", (wid,))
            assert parse_jsonb(c.fetchone()["snapshot"]) == {}
        # A receiver callback racing the delete is refused, like after a local delete.
        with pytest.raises(ValueError, match="fenced"):
            watch_store.record_connector_event(BOT, "message_received:123:2", {"platform_chat_id": "123"})


def test_a_replayed_or_unknown_delete_changes_nothing(pair):
    a, b = pair
    coll = policy.COLLECTIONS["connectors"]
    with b.active():
        # A delete for a connector this worker never had: no journal row is made.
        with get_db() as conn:
            engine.materialize(conn.cursor(), coll, "never-here", None)
        assert _journal_rows("never-here") == {"watch_connector_inbox": 0, "watch_connector_heads": 0,
                                               "fence": None}
    with a.active():
        assert database.delete_connector(BOT)
        engine.scan_all()
    exchange(a, b)
    with b.active():
        assert _journal_rows(BOT)["fence"] == "deleted"
        with get_db() as conn:  # the same delete applied again
            engine.materialize(conn.cursor(), coll, BOT, None)
        assert _journal_rows(BOT) == {"watch_connector_inbox": 0, "watch_connector_heads": 0,
                                      "fence": "deleted"}
