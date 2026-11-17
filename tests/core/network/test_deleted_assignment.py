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
