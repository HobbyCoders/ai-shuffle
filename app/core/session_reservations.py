"""History does not reserve resources; only unfinished work does.

Shared by checkout mutation, branch rebinding and primary-PR adoption. Active
working-set chats reserve their checkout; settled chats reserve only unfinished work.
"""
def holds_resources(session_id: str) -> bool:
    from app.db.base import get_db
    from app.core.task_registry import task_registry
    if (task_registry.get_parking_background_work(session_id)
            or task_registry.has_unresolved_background_launches(session_id)):
        return True
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_active FROM sessions WHERE id=%s", (session_id,))
        row = cursor.fetchone()
        if row and row["is_active"]:
            return True
        cursor.execute("""WITH RECURSIVE family AS (
            SELECT id FROM sessions WHERE id=%s
            UNION ALL SELECT s.id FROM sessions s JOIN family f ON s.parent_session_id=f.id
        ) SELECT 1 FROM turns t JOIN family f ON t.session_id=f.id
          WHERE t.state IN ('queued','running','interrupting','draining','parked')
          UNION ALL SELECT 1 FROM background_tasks b JOIN family f ON b.session_id=f.id
          WHERE b.status NOT IN ('completed','error','killed') LIMIT 1""", (session_id,))
        return cursor.fetchone() is not None
