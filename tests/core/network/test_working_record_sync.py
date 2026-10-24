"""Working items converge through the real signed owner network transport."""
from app.db import database
from app.db import working_record as store
from app.network import engine
from tests.core.network.harness import converge, join


def test_independent_items_and_acknowledgements_follow_owner(workers):
    first, second = workers("first"), workers("second")
    with first.active():
        engine.create_network("Working record")
        decision = store.add("decision", "Ship after review", "old-chat")
    join(first, second)
    converge([first, second])
    with second.active():
        assert any(row["id"] == decision for row in store.items())
        pending = store.add("pending", "Read the benchmark", "fresh-chat")
        assert store.resolve(decision)
    with first.active():
        goal = store.add("goal", "Finish the release", "other-chat")
    converge([first, second])
    for worker in (first, second):
        with worker.active():
            assert {row["id"] for row in store.items()} == {pending, goal}
            assert next(row for row in store.items(include_resolved=True)
                        if row["id"] == decision)["status"] == "resolved"


def test_remote_session_state_is_used_without_mirroring_execution_tables(workers, monkeypatch):
    from app.core import session_orchestration as orch
    from app.core import working_record
    first, second = workers("first"), workers("second")
    with first.active():
        engine.create_network("Working record")
        database.create_session("peer-work", "default", title="Build the release", is_active=True)
        # The runtime publisher samples the real producing turn on its owner.
        from app.db import turns
        turns.enqueue_turn("peer-work", "user", prompt="Build", profile_id="default")
        engine.scan_all()
    join(first, second)
    converge([first, second])
    with second.active():
        monkeypatch.setattr(orch, "_principal", lambda sid: None)
        database.create_session("fresh", "default", title="Fresh", is_active=True, mode="chat")
        text = working_record.summary("fresh")
        assert "Build the release" in text and '"state": "running"' in text
        from app.db.base import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM turns WHERE session_id='peer-work'")
            assert cur.fetchone() is None
