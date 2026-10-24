"""#553: actual old-peer deltas and snapshots preserve the attachable chat surface."""
from collections.abc import Callable

import pytest

from app.db import database
from app.db.base import get_db
from app.network import engine
from tests.core.network.harness import Worker, converge, join


@pytest.mark.parametrize("mode", [None, "code"])
@pytest.mark.parametrize("snapshot", [False, True], ids=["signed-delta", "bootstrap"])
def test_old_peer_session_stays_attachable_after_startup_and_later_edits(
    workers: Callable[[str], Worker], mode: str | None, snapshot: bool,
) -> None:
    source, target = workers("ServerWorker"), workers("DesktopWorker")
    with source.active():
        engine.create_network("Legacy sessions")
    with target.active():
        database.init_database()
        assert database.get_system_setting(database.MIGRATIONS_FINGERPRINT_KEY) == (
            database.migrations_fingerprint()
        )
    if not snapshot:
        join(source, target)
    with source.active():
        database.create_session("legacy-peer", "default", permission_mode="bypassPermissions")
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE sessions SET mode=%s WHERE id='legacy-peer'", (mode,),
            )
        engine.scan_all()
    if snapshot:
        join(source, target)
    converge([source, target])
    with target.active():
        assert database.get_session("legacy-peer")["mode"] == "chat"
    with source.active():
        assert database.get_session("legacy-peer")["mode"] == mode
        database.update_session("legacy-peer", title="Later old-peer edit")
        engine.scan_all()
    converge([source, target])
    with target.active():
        row = database.get_session("legacy-peer")
        assert row["title"] == "Later old-peer edit"
        assert row["mode"] == "chat"
        database.create_project("local-project", "Project", None, "/tmp/project")
        attached = database.attach_session_project("legacy-peer", "local-project", None)
        assert attached["project_id"] == "local-project"
        assert attached["mode"] is None
        assert attached["permission_mode"] == "default"
