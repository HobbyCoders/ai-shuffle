"""Upgrade a real pre-flag resume snapshot, then overwrite and hydrate it."""
import pytest

from app.core import agent_messaging, task_persistence, task_registry as registries
from app.core.config import settings
from app.db import background_tasks, database
from app.db.base import get_db, column_exists
from tests.core.test_session_orchestration import peers  # noqa: F401
from tests.core.test_agent_messaging import runtime, worker  # noqa: F401


async def test_legacy_resume_is_not_parent_after_migration_or_later_upsert(peers, runtime, monkeypatch):
    worker(runtime, "root", "caller")
    worker(runtime, "root", "peer")
    await task_persistence.flush()
    with get_db() as conn:
        cur = conn.cursor()
        # Only this disposable fixture database is reverted to the old schema.
        cur.execute("ALTER TABLE background_tasks DROP COLUMN assignment_owner")
        cur.execute("INSERT INTO background_tasks "
                    "(task_id, session_id, instance_id, kind, tool_use_id, status, terminal_notified) "
                    "VALUES (%s, %s, %s, 'agent', %s, 'completed', TRUE)",
                    ("peer", "caller", settings.effective_instance_id, "peer"))
        database._run_column_migrations(cur)
        applied = []
        database._migrate_task_assignment_owner(cur, applied)
        assert applied == []  # second boot is a no-op

    # Lifecycle upserts overwrite tool_use_id; the durable classification must
    # survive even an older/missing-flag snapshot defaulting to owner.
    background_tasks.upsert_background_task({
        "task_id": "peer", "session_id": "caller", "kind": "agent",
        "tool_use_id": "later-real-tool-id", "status": "completed",
        "terminal_notified": True, "summary": "retained continuation",
    })
    row = background_tasks.get_background_task("peer", "caller")
    assert not row["assignment_owner"]
    assert row["tool_use_id"] == "later-real-tool-id"
    restored = registries.TaskRegistry()
    monkeypatch.setattr(registries, "task_registry", restored)
    assert restored.get_task("caller", "peer", durable=True)["assignmentOwner"] is False  # cold fallback
    assert await restored.ensure_hydrated("caller") == 1
    assert restored.get_task("caller", "peer")["assignmentOwner"] is False
    assert not restored.mark_terminal_notified("caller", "peer")
    assert await agent_messaging.message_provenance_async("peer", "caller") == "peer"
    assert await agent_messaging.message_provenance_async("peer", "root") == "parent"
    # The legacy heuristic runs only when adding the column. A new, explicitly
    # owned resume must not lose authority on the next boot merely due to its id.
    background_tasks.upsert_background_task({
        "task_id": "fresh", "session_id": "caller", "kind": "agent",
        "tool_use_id": "fresh", "status": "completed", "assignment_owner": True,
    })
    with get_db() as conn:
        database._migrate_task_assignment_owner(conn.cursor(), [])
    assert background_tasks.get_background_task("fresh", "caller")["assignment_owner"]


@pytest.mark.parametrize("nested", [False, True], ids=["direct-parent", "legacy-nested-fails-safe"])
async def test_production_nested_caller_resume(peers, runtime, monkeypatch, nested):
    database.create_session("root", "agent")
    database.create_session("sibling", "agent", parent_session_id="root")
    direct = "middle" if nested else "root"
    if nested:
        database.create_session("middle", "agent", parent_session_id="root")
        worker(runtime, "root", "middle")
        worker(runtime, "middle", "child")
    from app.core.provider_agent import worker_session_fields
    fields = worker_session_fields(
        worker_session_id="child", profile_id="agent", title="Child",
        parent_session_id="root", provider_id=None, model_id="sonnet", effort=None,
    )
    # Use the actual durable producer's fields: nested context_session_id is
    # only a function argument, while parent_session_id routes to the root.
    assert "context_session_id" not in fields
    database.create_session(**fields)
    worker(runtime, "root", "child")
    assert await agent_messaging.message_provenance_async("child", direct) == "parent"
    await task_persistence.flush()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("ALTER TABLE background_tasks DROP COLUMN assignment_owner")
        assert not column_exists(cur, "sessions", "context_session_id")
        # Old root and direct-caller resumes both overwrote dispatch tool ids.
        cur.execute("UPDATE background_tasks SET tool_use_id=task_id, status='completed' "
                    "WHERE task_id='child'")
        cur.execute("INSERT INTO background_tasks "
                    "(task_id, session_id, instance_id, kind, tool_use_id, status) "
                    "VALUES ('child', 'sibling', %s, 'agent', 'child', 'completed')",
                    (settings.effective_instance_id,))
        database._run_column_migrations(cur)
    assert background_tasks.get_background_task("child", "root")["assignment_owner"]
    assert not background_tasks.get_background_task("child", "sibling")["assignment_owner"]
    restored = registries.TaskRegistry()
    monkeypatch.setattr(registries, "task_registry", restored)
    await restored.ensure_hydrated("root")
    await restored.ensure_hydrated("sibling")
    assert "assignmentOwner" not in restored.get_task("root", "child", durable=True)
    assert restored.get_task("sibling", "child")["assignmentOwner"] is False
    assert await agent_messaging.message_provenance_async("child", "root") == "parent"
    assert await agent_messaging.message_provenance_async("child", "sibling") == "peer"
    if nested:
        # No durable direct-caller identity survived. Explicitly accept the
        # requested safe fallback: middle loses legacy authority, not siblings
        # gaining it. New post-flag dispatch records are not reclassified.
        assert not background_tasks.get_background_task("child", "middle")["assignment_owner"]
        await restored.ensure_hydrated("middle")
        assert restored.get_task("middle", "child")["assignmentOwner"] is False
        assert await agent_messaging.message_provenance_async("child", "middle") == "peer"
    [main] = await agent_messaging.peer_rows_async("child")
    assert main["name"] == "main" and main["worker_session_id"] == "root"


async def test_legacy_remote_dispatch_without_local_session_is_not_demoted(peers, runtime):
    background_tasks.upsert_background_task({
        "task_id": "remote-child", "session_id": "middle", "kind": "agent",
        "tool_use_id": "remote-child", "status": "completed", "remote_device_id": "other-worker",
    })
    assert database.get_session("remote-child") is None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("ALTER TABLE background_tasks DROP COLUMN assignment_owner")
        database._run_column_migrations(cur)
    row = background_tasks.get_background_task("remote-child", "middle")
    assert row["assignment_owner"]
    assert row["remote_device_id"] == "other-worker"
