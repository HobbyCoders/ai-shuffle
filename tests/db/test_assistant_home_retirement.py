"""#371 step 5: the retired assistant's conversation (its "home") is deleted.

Runs ``_retire_assistant_home`` against the in-memory SQLite shim after
re-creating what an older build left behind: a home named by the runner row
and the synced account, a peer's mirrored home, nested Task workers under a
home, a chat a home started, the home's job links and the limit-notice and
brief columns. Ordinary sessions must come through untouched, and the chat a
home started must stay under the spending limits.
"""
import json

import pytest

from app.core import assistant_limits as limits, assistant_service as service, orchestration_limits
from app.db import assistant_limits as limits_db, database as db, session_events, turns
from app.db.base import column_exists, get_db, table_exists

HOME = "legacy-home"
PEER_HOME = "peer-home"
HOME_WORKER = "home-task-worker"
NESTED_WORKER = "home-task-worker-child"  # A Task worker the home's worker started.
STARTED_CHAT = "chat-the-home-started"
ORDINARY = "ordinary-chat"
ORDINARY_WORKER = "ordinary-task-worker"
AGENT = db.RETIRED_ASSISTANT_AGENT_ID


@pytest.fixture
def cursor(mock_db):
    with get_db() as conn:
        yield conn.cursor()


def _count(cursor, sql, params=()):
    cursor.execute(sql, params)
    return int(cursor.fetchone()["n"])


def _session(sid, profile="default", **fields):
    db.create_session(sid, profile, title=sid, is_active=True, **fields)
    db.update_session(sid, sdk_session_id="sdk-" + sid)
    db.add_session_message(sid, "user", "hello from " + sid)
    turn = turns.enqueue_turn(sid, "user", prompt="hi", profile_id=profile)
    session_events.append_events(sid, [{"type": "message.user", "turn_id": turn["id"],
                                        "payload": {"content": "hi"}}])
    with get_db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO usage_log (session_id, model, tokens_in, tokens_out, cost_usd, duration_ms) "
                  "VALUES (%s,'m',1,1,1.5,1)", (sid,))
        if table_exists(c, "session_store_entries"):
            c.execute("INSERT INTO session_store_entries (project_key, session_id, entry) VALUES (%s,%s,%s)",
                      ("p", "sdk-" + sid, "{}"))


def _start_marker(sid, sender):
    """The StartSession turn ``sender`` queued to begin ``sid``."""
    turns.enqueue_turn(sid, "agent_message", prompt="Start", profile_id=AGENT,
                       command_id=f"orchestration:start:{sid}", payload={"orchestration_sender": sender})


def _legacy_install(cursor):
    """What an older build left: homes, their links and the retired columns."""
    with get_db() as conn:
        service.ensure_agent(conn.cursor())
    _session(HOME, AGENT, hidden=True)
    _session(PEER_HOME, AGENT, hidden=True)  # Mirrored from a peer; no pointer here.
    _session(HOME_WORKER, AGENT, hidden=True, is_worker=True, parent_session_id=HOME)
    _session(NESTED_WORKER, AGENT, hidden=True, is_worker=True, parent_session_id=HOME_WORKER)
    # Visible: an ordinary chat, with the start marker StartSession writes.
    _session(STARTED_CHAT, AGENT, parent_session_id=HOME, mode="chat")
    _start_marker(STARTED_CHAT, HOME)
    _session(ORDINARY)
    _session(ORDINARY_WORKER, hidden=True, is_worker=True, parent_session_id=ORDINARY)
    aid = service.assistant_id()
    cursor.execute("INSERT INTO assistant_state (id, runner_id, agent_id, home_session_id) VALUES (%s,%s,%s,%s)",
                   (aid, aid, AGENT, HOME))
    cursor.execute("INSERT INTO assistant_account (id, agent_id, home_session_id, home_device_id, home_created_at) "
                   "VALUES ('account', %s, %s, 'device', '2026-01-01')", (AGENT, HOME))
    for run_id, kind, session_id in (("job-link", "job", STARTED_CHAT), ("research", "proactive", None)):
        cursor.execute("INSERT INTO assistant_runs (id, assistant_id, home_session_id, session_id, kind, status) "
                       "VALUES (%s,%s,%s,%s,%s,'completed')", (run_id, aid, HOME, session_id, kind))
    cursor.execute("ALTER TABLE assistant_runs ADD COLUMN brief TEXT")
    cursor.execute("ALTER TABLE assistant_limits ADD COLUMN warned TEXT")
    cursor.execute("ALTER TABLE assistant_limits ADD COLUMN reached TEXT")
    cursor.execute("ALTER TABLE assistant_limit_stops ADD COLUMN reported_at TIMESTAMPTZ")
    cursor.execute("CREATE INDEX idx_assistant_limit_stops_unreported ON assistant_limit_stops(assistant_id, reported_at)")


def test_the_homes_and_their_workers_are_deleted_and_everything_else_is_kept(cursor):
    _legacy_install(cursor)

    applied = db._retire_assistant_home(cursor)

    gone, kept = (HOME, PEER_HOME, HOME_WORKER, NESTED_WORKER), (STARTED_CHAT, ORDINARY, ORDINARY_WORKER)
    for sid in gone:
        assert db.get_session(sid) is None, sid
        for table in ("session_messages", "turns", "session_events"):
            assert _count(cursor, f"SELECT COUNT(*) AS n FROM {table} WHERE session_id=%s", (sid,)) == 0, (table, sid)
        if table_exists(cursor, "session_store_entries"):
            assert _count(cursor, "SELECT COUNT(*) AS n FROM session_store_entries WHERE session_id=%s",
                          ("sdk-" + sid,)) == 0
    for sid in kept:
        assert db.get_session(sid) is not None, sid
        for table in ("session_messages", "session_events", "usage_log"):
            assert _count(cursor, f"SELECT COUNT(*) AS n FROM {table} WHERE session_id=%s", (sid,)) == 1, (table, sid)
        assert _count(cursor, "SELECT COUNT(*) AS n FROM turns WHERE session_id=%s", (sid,)) >= 1, sid
    # A chat the home started survives, unlinked; an ordinary worker keeps its parent.
    assert db.get_session(STARTED_CHAT)["parent_session_id"] is None
    assert db.get_session(ORDINARY_WORKER)["parent_session_id"] == ORDINARY
    # Spend history stays, unlinked, as a normal delete leaves it.
    assert _count(cursor, "SELECT COUNT(*) AS n FROM usage_log WHERE session_id IS NULL") == len(gone)
    # Pointers are cleared; the home's job link goes and research history stays.
    cursor.execute("SELECT home_session_id FROM assistant_state")
    assert [row["home_session_id"] for row in cursor.fetchall()] == [None]
    cursor.execute("SELECT home_session_id, home_device_id, home_created_at FROM assistant_account")
    assert [(row["home_session_id"], row["home_device_id"], row["home_created_at"])
            for row in cursor.fetchall()] == [(None, None, None)]
    cursor.execute("SELECT id, home_session_id FROM assistant_runs")
    assert [(row["id"], row["home_session_id"]) for row in cursor.fetchall()] == [("research", None)]
    # The columns only the home used are dropped.
    assert not column_exists(cursor, "assistant_runs", "brief")
    assert not column_exists(cursor, "assistant_limits", "warned")
    assert not column_exists(cursor, "assistant_limits", "reached")
    assert not column_exists(cursor, "assistant_limit_stops", "reported_at")
    assert "assistant_home.sessions(4)" in applied and "assistant_runs.home_job_links(1)" in applied
    # The deleted homes are recorded so the chats they started stay capped.
    assert json.loads(db.get_system_setting(db.RETIRED_ASSISTANT_HOMES_MARKER)) == [HOME, PEER_HOME]

    # Idempotent: a second pass finds nothing and changes nothing.
    assert db._retire_assistant_home(cursor) == []
    assert all(db.get_session(sid) is not None for sid in kept)


def test_a_database_that_never_had_the_assistant_is_left_alone(cursor):
    _session(ORDINARY)
    _session(ORDINARY_WORKER, hidden=True, is_worker=True, parent_session_id=ORDINARY)
    assert db._retire_assistant_home(cursor) == []
    assert db.get_session(ORDINARY) is not None
    assert db.get_session(ORDINARY_WORKER)["parent_session_id"] == ORDINARY
    assert _count(cursor, "SELECT COUNT(*) AS n FROM usage_log WHERE session_id IS NOT NULL") == 2


def test_a_pointer_to_a_home_that_is_already_gone_is_cleared(cursor):
    cursor.execute("INSERT INTO assistant_account (id, agent_id, home_session_id) VALUES ('account', %s, %s)",
                   (AGENT, "deleted-long-ago"))
    _session(ORDINARY)
    applied = db._retire_assistant_home(cursor)
    assert applied == ["assistant_account.home_cleared(1)"]
    assert db.get_session(ORDINARY) is not None
    assert db._retire_assistant_home(cursor) == []


def test_a_chat_a_home_started_stays_under_the_limits_after_the_home_is_deleted(cursor):
    _legacy_install(cursor)
    # Control: a chat started by some other chat that was deleted keeps the ordinary rule.
    _session("started-by-a-deleted-chat", "default", mode="chat")
    _start_marker("started-by-a-deleted-chat", "a-deleted-project-chat")
    db._retire_assistant_home(cursor)

    assert orchestration_limits.role(STARTED_CHAT) == "job"
    assert orchestration_limits.role("started-by-a-deleted-chat") is None
    assert orchestration_limits.tracked_roles().get(STARTED_CHAT) == "job"  # The bounded batch reader agrees.
    limits_db.update(service.assistant_id(), {"daily_spend_limit_usd": 1})
    state = limits.status()
    # Its spend still counts (the deleted homes' own spend was unlinked with them).
    assert state["spent_today_usd"] == pytest.approx(1.5) and state["limit_reached"]
    assert limits.turn_refusal(STARTED_CHAT, "agent_message")
    assert limits.turn_refusal(STARTED_CHAT, "user") is None  # The owner's own turn still runs.
    assert limits.start_refusal(STARTED_CHAT)


def test_a_pointer_alone_still_records_the_home(cursor):
    # The synced account can name a home that never reached this worker.
    cursor.execute("INSERT INTO assistant_account (id, agent_id, home_session_id) VALUES ('account', %s, %s)",
                   (AGENT, "deleted-earlier"))
    _session(STARTED_CHAT, "default", mode="chat")
    _start_marker(STARTED_CHAT, "deleted-earlier")
    assert orchestration_limits.role(STARTED_CHAT) is None  # Before: its starter is simply gone.
    db._retire_assistant_home(cursor)
    assert orchestration_limits.role(STARTED_CHAT) == "job"
