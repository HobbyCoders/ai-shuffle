"""A message the owner types into a running Task worker's chat.

The worker's turn is submitted by ``provider_agent.dispatch_worker`` with the
worker's own agent and the overrides ``worker_turn_overrides`` builds (the Task
model, the caller's permission mode, the worker's checkout). The composer the
owner types into knows none of that: it sends whatever agent it last had (the
parent chat's) and the session row's bindings, and a worker's row does not carry
the permission mode its turn runs under. Stamped with those values the row
differed from the live turn on profile and permission mode, so
``SessionRuntime.steer_pending`` refused it and it waited out the whole worker
turn, shown as queued.

The turns table is real (pg_shim SQLite); the engine is stubbed.
"""

import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import provider_agent, session_runtime, turn_manager, turn_scheduler
from app.db import database
from app.db import turns as turns_db


@pytest.fixture(autouse=True)
def _clean_module_state():
    session_runtime._live_runtimes.clear()
    session_runtime.register_scheduler(None)
    turn_manager.register_kick(None)
    turn_scheduler._kick_tasks.clear()
    yield
    session_runtime._live_runtimes.clear()
    session_runtime.register_scheduler(None)
    turn_manager.register_kick(None)
    turn_scheduler._kick_tasks.clear()


class _Adapter:
    def __init__(self):
        self.calls = []

    async def steer(self, text, *, id=None, origin="user"):
        self.calls.append((text, id))


@contextmanager
def _pooled(adapter):
    pool = MagicMock(name="session_pool")
    pool.peek.return_value = MagicMock(adapter=adapter)
    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        yield


def _worker_overrides(permission_mode="bypassPermissions"):
    """What ``dispatch_worker`` submits a native-Claude worker's turn with when
    the Task call asked for ``opus`` and the caller runs in ``permission_mode``."""
    return provider_agent.worker_turn_overrides(
        provider_id=None,
        model_id="opus",
        effort="high",
        cwd="/work/task-checkout",
        permission_mode=permission_mode,
        system_prompt_append="You are a coding subagent.",
    )


#: What the web composer's ``buildOverrides`` sends from a subagent's chat: the
#: row's model and effort, an empty folder list, the fast flag, and no
#: permission mode (the worker row has none).
COMPOSER_OVERRIDES = {
    "workspace_project_ids": [], "model": "opus", "effort": "high", "fast": False,
}
#: The agent the composer still has from the parent chat it was opened from.
COMPOSER_PROFILE = "coding"


def _worker_session(session_id):
    # Same shape as ``worker_session_fields``: no permission mode on the row.
    # No profile: sessions.profile_id is an FK and these tests seed no agents.
    database.create_session(
        session_id=session_id, profile_id=None, title="Task worker",
        model="opus", effort="high", hidden=True, is_worker=True,
    )
    return session_id


def _start(session_id, source, **kwargs):
    turn = turns_db.enqueue_turn(session_id, source, prompt="the live one", **kwargs)
    turns_db.start_turn(turn["id"])
    return turns_db.get_turn(turn["id"])


def _running_worker(session_id, permission_mode="bypassPermissions"):
    _worker_session(session_id)
    live = _start(
        session_id, provider_agent.WORKER_TURN_SOURCE,
        profile_id="general-purpose", overrides=_worker_overrides(permission_mode),
        payload={"api_user_id": None},
    )
    session_runtime._live_runtimes[session_id] = session_runtime.SessionRuntime(live)
    return live


def _send(session_id, prompt="please also check the tests", *, payload=None,
          profile_id=COMPOSER_PROFILE, overrides=COMPOSER_OVERRIDES):
    """The submit ``chat_ws_handler.handle_query`` makes for a send to a busy
    session."""
    with patch.object(turn_manager, "_kick"):
        turn_id = turn_manager.submit(
            session_id, "user", prompt=prompt, profile_id=profile_id,
            overrides=dict(overrides) if overrides else None,
            payload={"deferred_user_record": True, **(payload or {})},
        )
    return turns_db.get_turn(turn_id)


async def test_a_message_typed_into_a_running_worker_chat_is_steered(mock_db):
    _running_worker("w1")
    row = _send("w1")

    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("w1") == 1

    assert adapter.calls == [("please also check the tests", row["id"])]


async def test_the_message_carries_the_workers_binding_not_the_composers(mock_db):
    live = _running_worker("w1")
    with patch("app.db.database.set_session_run_binding") as persist:
        row = _send("w1")

    assert row["profile_id"] == "general-purpose"
    assert row["overrides"] == live["overrides"]
    assert row["payload"]["worker_bound"] is True
    assert row["payload"]["deferred_user_record"] is True
    # The owner picked none of these, so the worker's row is not re-bound.
    persist.assert_not_called()


async def test_a_worker_message_never_runs_with_broader_permissions(mock_db):
    """A composer that asks for bypass cannot lift a worker that runs at the
    default level — the row takes the worker's mode, and the drained turn runs
    under it."""
    _running_worker("w1", permission_mode="default")
    row = _send("w1", overrides={**COMPOSER_OVERRIDES, "permission_mode": "bypassPermissions"})

    assert row["overrides"]["permission_mode"] == "default"


async def test_an_unsteerable_message_runs_after_the_worker_turn_as_the_worker(mock_db):
    """An attachment cannot ride a tool result, so the message waits — and when
    the worker's turn settles the drain starts it, under the worker's own agent
    and settings. It is not stranded behind a finished worker."""
    live = _running_worker("w1")
    row = _send("w1", payload={"upload_blob_ids": ["blob-1"]})

    adapter = _Adapter()
    with _pooled(adapter):
        assert await session_runtime.try_steer_pending("w1") == 0
    assert adapter.calls == []

    turns_db.settle_turn(live["id"], "completed")
    session_runtime._live_runtimes.pop("w1", None)
    execute = AsyncMock(return_value="completed")
    with patch("app.core.session_runtime.execute", new=execute):
        await turn_scheduler.on_turn_settled("w1", live["id"], "completed")
        for task in list(turn_scheduler._kick_tasks):
            await task

    execute.assert_awaited_once()
    drained = execute.await_args.args[0]
    assert drained["id"] == row["id"]
    assert drained["profile_id"] == "general-purpose"
    assert drained["overrides"] == live["overrides"]


async def test_a_parked_worker_takes_the_message_as_the_worker(mock_db):
    """A worker parked on its own background work is still mid-dispatch: the
    message runs now, but as the worker."""
    live = _running_worker("w1")
    session_runtime._live_runtimes.pop("w1", None)
    turns_db.park_turn(live["id"], ["task-1"])

    row = _send("w1")

    assert row["profile_id"] == "general-purpose"
    assert row["overrides"] == live["overrides"]


async def test_a_worker_that_parks_after_a_steered_message_is_still_the_worker(mock_db):
    """A message steered into the worker's turn settles its own queue row at
    once, so that row is newer than the turn that read it. When the worker then
    parks on its own background work, the next message must still find the
    park, not the steered row, and take the worker's binding."""
    live = _running_worker("w1")
    # Older than the steered row for certain (the shim's clock is coarse).
    mock_db.execute("UPDATE turns SET created_at='2000-01-01' WHERE id=?", (live["id"],))
    first = _send("w1", "first remark")
    assert turns_db.absorb_queued_turn(first["id"]) is True
    session_runtime._live_runtimes.pop("w1", None)
    turns_db.park_turn(live["id"], ["task-1"])

    row = _send("w1", "second remark")

    assert row["profile_id"] == "general-purpose"
    assert row["overrides"] == live["overrides"]
    assert row["payload"]["worker_bound"] is True


async def test_a_second_message_sent_before_the_first_starts_is_the_workers_too(mock_db):
    """A parked worker's chat: the first message is bound to the worker and
    queued; until the scheduler claims it, it is the session's latest turn. A
    second message sent in that window must not fall back to the composer's
    agent and settings (and must not re-bind the worker's row)."""
    live = _running_worker("w1")
    session_runtime._live_runtimes.pop("w1", None)
    turns_db.park_turn(live["id"], ["task-1"])
    # Older than the messages for certain (the shim's clock is coarse).
    mock_db.execute("UPDATE turns SET created_at='2000-01-01' WHERE id=?", (live["id"],))
    first = _send("w1", "first remark")
    assert first["state"] == "queued" and first["payload"]["worker_bound"] is True

    with patch("app.db.database.set_session_run_binding") as persist:
        second = _send("w1", "second remark")

    assert second["profile_id"] == "general-purpose"
    assert second["overrides"] == live["overrides"]
    assert second["payload"]["worker_bound"] is True
    persist.assert_not_called()


async def test_a_message_to_a_worker_not_started_yet_is_the_workers(mock_db):
    """The dispatch's turn is queued but not claimed yet: a message typed into
    the worker's chat runs after it, as the worker, not as the composer."""
    _worker_session("w1")
    queued = turns_db.enqueue_turn(
        "w1", provider_agent.WORKER_TURN_SOURCE, prompt="the task",
        profile_id="general-purpose", overrides=_worker_overrides(),
    )

    row = _send("w1")

    assert row["profile_id"] == "general-purpose"
    assert row["overrides"] == queued["overrides"]


async def test_an_ordinary_message_queued_in_an_ended_worker_chat_stays_ordinary(mock_db):
    live = _running_worker("w1")
    session_runtime._live_runtimes.pop("w1", None)
    turns_db.settle_turn(live["id"], "completed")
    first = _send("w1", "first remark")
    assert "worker_bound" not in first["payload"]

    second = _send("w1", "second remark")

    assert second["profile_id"] == COMPOSER_PROFILE
    assert second["overrides"] == COMPOSER_OVERRIDES
    assert "worker_bound" not in second["payload"]


async def test_a_worker_chat_resumed_after_the_work_ended_is_an_ordinary_chat(mock_db):
    live = _running_worker("w1")
    session_runtime._live_runtimes.pop("w1", None)
    turns_db.settle_turn(live["id"], "completed")

    row = _send("w1")

    assert row["profile_id"] == COMPOSER_PROFILE
    assert row["overrides"] == COMPOSER_OVERRIDES
    assert "worker_bound" not in row["payload"]


async def test_a_main_chat_still_holds_a_message_that_asks_for_another_model(mock_db, caplog):
    """The eligibility rule is unchanged outside worker chats: a different
    model waits for its own turn, and the refusal names the key."""
    database.create_session(session_id="main", profile_id=None, title="t")
    live = _start("main", "user", profile_id="coding",
                  overrides={"model": "sonnet", "permission_mode": "default"})
    session_runtime._live_runtimes["main"] = session_runtime.SessionRuntime(live)
    row = _send("main", "secret prompt text",
                overrides={"model": "opus", "permission_mode": "default"})

    assert row["profile_id"] == COMPOSER_PROFILE
    assert row["overrides"] == {"model": "opus", "permission_mode": "default"}

    adapter = _Adapter()
    with _pooled(adapter), caplog.at_level(logging.INFO, logger="app.core.session_runtime"):
        assert await session_runtime.try_steer_pending("main") == 0
        assert await session_runtime.try_steer_pending("main") == 0
    assert adapter.calls == []

    refusals = [r.getMessage() for r in caplog.records if "not steerable" in r.getMessage()]
    assert len(refusals) == 1, refusals
    assert "model differs" in refusals[0]
    assert row["id"] in refusals[0]
    assert "secret prompt text" not in refusals[0]


def test_a_row_naming_other_workspace_folders_is_refused_and_one_naming_none_joins(mock_db):
    """Folders still bind a turn: a send that NAMES another set waits for its
    own turn. A row naming none (a message from another device, whose route
    never carries folders) keeps the live turn's and may join it."""
    database.create_session(session_id="folders", profile_id=None, title="t")
    # A local send stores the folders it names (``turn_manager``).
    database.set_session_run_binding("folders", workspace_project_ids=["docs"])
    live = _start("folders", "user", profile_id="coding",
                  overrides={"model": "opus", "workspace_project_ids": ["docs"]})
    runtime = session_runtime.SessionRuntime(live)
    row = {"id": "turn-x", "source": "user", "prompt": "hi", "payload": {}, "profile_id": "coding",
           "overrides": {"model": "opus", "workspace_project_ids": ["docs", "api"]}}
    assert runtime._steer_refusal(row) == "workspace_project_ids differs"
    row["overrides"]["workspace_project_ids"] = []
    assert runtime._steer_refusal(row) == "workspace_project_ids differs"
    row["overrides"]["workspace_project_ids"] = ["docs"]
    assert runtime._steer_refusal(row) is None
    del row["overrides"]["workspace_project_ids"]
    assert runtime._steer_refusal(row) is None
    # Absent folders are not a wildcard for the other bindings.
    row["overrides"]["model"] = "sonnet"
    assert runtime._steer_refusal(row) == "model differs"


def test_a_row_naming_no_folders_waits_when_the_chats_folders_changed_mid_turn(mock_db):
    """A row naming no folders means the chat's STORED folders. When the owner
    changed them after the turn started (the picker saves at once), the live
    turn no longer has that set: the row waits for its own turn rather than
    keep a removed folder in use, or miss an added one."""
    database.create_session(session_id="moved", profile_id=None, title="t")
    database.set_session_run_binding("moved", workspace_project_ids=["docs", "secrets"])
    live = _start("moved", "user", profile_id="coding",
                  overrides={"model": "opus", "workspace_project_ids": ["docs", "secrets"]})
    runtime = session_runtime.SessionRuntime(live)
    row = {"id": "turn-x", "source": "user", "prompt": "hi", "payload": {}, "profile_id": "coding",
           "overrides": {"model": "opus"}}
    assert runtime._steer_refusal(row) is None
    database.set_session_run_binding("moved", workspace_project_ids=["docs"])
    assert runtime._steer_refusal(row) == "workspace_project_ids differs"
    database.set_session_run_binding("moved", workspace_project_ids=["docs", "secrets", "api"])
    assert runtime._steer_refusal(row) == "workspace_project_ids differs"
    # The same set in another order is the same folders.
    database.set_session_run_binding("moved", workspace_project_ids=["secrets", "docs"])
    assert runtime._steer_refusal(row) is None


async def test_a_worker_row_built_by_the_composer_is_refused_by_the_live_turn(mock_db):
    """The defect itself, at the eligibility seam: the row the composer used to
    produce for a worker chat differs from the worker's turn on its agent (and,
    behind that, on the permission mode the row does not carry)."""
    live = _running_worker("w1")
    runtime = session_runtime._live_runtimes["w1"]
    composer_row = {
        "id": "turn-x", "source": "user", "prompt": "hi", "payload": {},
        "profile_id": COMPOSER_PROFILE, "overrides": dict(COMPOSER_OVERRIDES),
    }
    assert runtime._steer_refusal(composer_row) == "profile_id differs"
    composer_row["profile_id"] = live["profile_id"]
    assert runtime._steer_refusal(composer_row) == "permission_mode differs"
