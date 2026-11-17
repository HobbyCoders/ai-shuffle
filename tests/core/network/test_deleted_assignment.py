"""A tombstoned optional parent clears just the assignment, not the child's other edits."""
import pytest

from app.db import api_users, database, network as store, profiles, templates
from app.network import engine
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_pending_apply_budget import pg_workers, private_cluster  # noqa: F401


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize("child,field", [("templates", "profile_id"), ("api_users", "profile_id"),
                                        ("connectors", "api_user_id")])
def test_accept_edit_with_deleted_assignment(pg_workers, snapshot, child, field):
    a, b = pg_workers("a"), pg_workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        if field == "profile_id":
            database.create_profile("parent", "Original agent", None, {})
        else:
            api_users.create_api_user("parent", "Original key", "test-hash")
        if child == "templates":
            templates.create_template("child", "before", "prompt")
        elif child == "api_users":
            api_users.create_api_user("child", "before", "test-hash")
        else:
            database.create_connector("child", "telegram", "before", {}, [], is_active=False)
        engine.scan_all()
    converge([a, b])
    with a.active():
        if field == "profile_id":
            profiles.delete_profile("parent")
        else:
            api_users.delete_api_user("parent")
        engine.scan_all()
    with b.active():
        with database.get_db() as conn:
            conn.cursor().execute(f"UPDATE {child} SET name=%s, {field}=%s WHERE id=%s", ("edited", "parent", "child"))
        engine.scan_collection(child)
        chunk = engine.snapshot_chunk(child, 0)
        database.create_tag("later", "later")
        engine.scan_all()
    with a.active():
        if snapshot:
            engine.apply_snapshot_chunk(chunk)
            assert a.sql(f"SELECT name, {field} FROM {child} WHERE id='child'")[0] == {"name": "edited", field: None}
        delivered = exchange(b, a)
        assert not delivered.retry and not delivered.rejected, delivered.reasons
        assert database.get_tag("later")
        row = a.sql(f"SELECT name, {field} FROM {child} WHERE id='child'")[0]
        assert row == {"name": "edited", field: None}
        assert any(c["losing_record"] == {field: "parent"} and c["record_id"] == "child"
                   for c in store.list_conflicts())
        if field == "profile_id":
            database.create_profile("parent", "Replacement", None, {})
            assert a.sql(f"SELECT {field} FROM {child} WHERE id='child'")[0][field] is None


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize("case", ["same", "replacement", "legacy"])
@pytest.mark.parametrize("child", ["templates", "api_users"])
def test_delayed_assignment_after_join(pg_workers, monkeypatch, snapshot, case, child):
    a, b, c = (pg_workers(n) for n in "abc")
    with a.active():
        engine.create_network("Home")
        database.create_profile("agent", "Original", None, {})
        engine.scan_all()
    join(a, b)
    if case == "replacement":
        with a.active():
            profiles.delete_profile("agent")
            engine.scan_collection("agents")
            database.create_profile("agent", "Different", None, {})
            engine.scan_collection("agents")
    # C joined after any replacement. Its state copy has no history of
    # the old agent's deletion. B still refers to its original.
    join(a, c)
    with c.active():
        assert not c.sql("SELECT 1 FROM sync_ops WHERE collection='agents' AND record_id='agent'")
    with b.active(), monkeypatch.context() as m:
        if case == "legacy":
            serialize = engine.serialize_row
            def legacy(*args, **kwargs):
                record = serialize(*args, **kwargs)
                if record:
                    record.pop("_profile_incarnation", None)
                return record
            m.setattr(engine, "serialize_row", legacy)
        if child == "templates":
            templates.create_template("delayed", "peer edit", "prompt")
        else:
            api_users.create_api_user("delayed", "peer edit", "test-hash")
        with database.get_db() as conn:
            conn.cursor().execute(f"UPDATE {child} SET profile_id='agent' WHERE id='delayed'")
        engine.scan_collection(child)
        chunk = engine.snapshot_chunk(child, 0)
        database.create_tag("tail", "tail")
        engine.scan_all()
    with c.active():
        def verify():
            expected = "agent" if case == "same" else None
            assert c.sql(f"SELECT name, profile_id FROM {child} WHERE id='delayed'")[0] == {
                "name": "peer edit", "profile_id": expected}
            assert any(r["record_id"] == "delayed" and r["losing_record"] == {"profile_id": "agent"}
                       for r in store.list_conflicts()) == (case != "same")
        if snapshot:
            engine.apply_snapshot_chunk(chunk)
            verify()
        result = exchange(b, c)
        assert not result.retry and not result.rejected, result.reasons
        verify()
        assert database.get_tag("tail")
