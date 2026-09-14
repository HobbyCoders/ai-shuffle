"""Owner runtime state survives replication without copying executable work."""
from unittest.mock import patch

import pytest

from app.api.sessions_helpers import _annotate_cluster_fields
from app.db import database, runs, interactions, background_tasks
from app.network import engine, catalog, session_runtime
from tests.core.network.harness import join, converge
from tests.core.network.test_control import workers  # noqa: F401


def test_owner_runtime_transitions_and_project_label_on_mirror(workers):
    a, b = workers("runtime-owner"), workers("runtime-viewer")
    with a.active():
        engine.create_network("Runtime")
    join(a, b)
    with a.active():
        from app.db.projects_v2 import upsert_project
        project = upsert_project(origin="https://github.com/example/runtime.git", name="Shared project")
        database.create_session(session_id="remote-runtime", profile_id="default", project_id=None,
                                title="Remote work", project_v2_id=project["id"], is_active=True)
        engine.scan_all()
    converge([a, b])

    def assert_state(expected):
        with a.active():
            catalog.publish_owned()
            assert session_runtime.publish_owned(engine.context()) == 0  # quiet states do not churn
        converge([a, b])
        with b.active():
            rows = [dict(database.get_session("remote-runtime"))]
            with patch.object(database, "get_installs_map", return_value={
                a.device_id: {"device_name": "Owner", "online": True, "approved": True},
            }):
                _annotate_cluster_fields(rows)
            assert rows[0]["project_id"] is None
            assert rows[0]["project_name"] == "Shared project"
            assert rows[0]["remote_runtime_state"] == expected
            assert rows[0]["is_remote"]
            assert not b.sql("SELECT * FROM runs")
            assert not b.sql("SELECT * FROM turns")
            assert not b.sql("SELECT * FROM background_tasks")
            assert engine.scan_all() == 0  # a viewer cannot republish owner state

    assert_state("idle")
    with a.active():
        run = runs.claim_run("remote-runtime", "test-owner", 600)
    assert_state("working")
    with a.active():
        question = interactions.create_interaction("remote-runtime", "question", {}, None)
    assert_state("question")
    with a.active():
        permission = interactions.create_interaction("remote-runtime", "permission", {}, None)
    assert_state("permission")
    with a.active():
        interactions.answer_interaction(permission["id"], {})
        interactions.answer_interaction(question["id"], {})
        runs.release_run(run["id"], run["fence_token"])
        background_tasks.upsert_background_task({"task_id": "monitor", "session_id": "remote-runtime",
                                                 "status": "running", "kind": "shell"})
    assert_state("working")
    with a.active():
        database.set_session_parked_task_kind("remote-runtime", "shell")
    assert_state("parked")
    with a.active():
        database.set_session_parked_task_kind("remote-runtime", None)
        background_tasks.upsert_background_task({"task_id": "monitor", "session_id": "remote-runtime",
                                                 "status": "completed", "kind": "shell"})
    assert_state("idle")
    with b.active():
        rows = [dict(database.get_session("remote-runtime"))]
        with patch.object(database, "get_installs_map", return_value={}):
            _annotate_cluster_fields(rows)
        assert rows[0]["remote_runtime_state"] == "offline"
        # Online old owners and summaries from a different owner are unknown,
        # not evidence of completion.
        b.sql("UPDATE network_session_runtime SET owner_device_id = 'old-owner'")
        with patch.object(database, "get_installs_map", return_value={
            a.device_id: {"device_name": "Owner", "online": True},
        }):
            _annotate_cluster_fields(rows)
        assert rows[0]["remote_runtime_state"] == "unknown"
    with a.active():
        database.delete_session("remote-runtime")
        catalog.publish_owned()
        assert not session_runtime.for_sessions(["remote-runtime"])
    converge([a, b])
    with b.active():
        assert not session_runtime.for_sessions(["remote-runtime"])


@pytest.mark.parametrize("mode, expected", [("queued", "working"), ("blocked_setup", "working"),
                                             ("expired_run", "idle"), ("terminal_task", "idle")])
def test_runtime_uses_live_owner_work_not_terminal_history(workers, mode, expected):
    a = workers("runtime-cases")
    with a.active():
        engine.create_network("Cases")
        database.create_session(session_id="case", profile_id="default", title="Case")
        if mode in {"queued", "blocked_setup"}:
            from app.db import turns
            turn = turns.enqueue_turn("case", "user", prompt="Hello")
            if mode == "blocked_setup":
                a.sql("UPDATE turns SET state = 'blocked_setup' WHERE id = ?", (turn["id"],))
        elif mode == "expired_run":
            runs.claim_run("case", "expired-owner", -1)
        else:
            background_tasks.upsert_background_task({"task_id": "done", "session_id": "case",
                                                     "kind": "shell", "status": "completed"})
        catalog.publish_owned()
        assert session_runtime.for_sessions(["case"])["case"]["state"] == expected
