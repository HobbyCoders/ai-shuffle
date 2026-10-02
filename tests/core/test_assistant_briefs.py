"""A completed research run posts a short brief into the assistant home (#1608)."""
from datetime import timedelta

import pytest

from app.core import assistant_manager as module, assistant_service as service, turn_recorder
from app.db import assistant as store, database, turns
from app.db.base import get_db
from app.db.session_events import append_events, get_events
from tests.core.test_assistant_manager import producer  # noqa: F401 (fixture)
from tests.db.test_assistant_store import assistant_fixture, events  # noqa: F401 (fixture)


async def _finished_run(manager, reply):
    """Admit and dispatch one research run, then settle its turn with ``reply``."""
    service.register_proactive_guard(1)
    events()
    await manager.run_once()
    run = store.get_activity(service.assistant_id())[0]
    append_events(run["session_id"], [
        {"type": "message.assistant", "turn_id": run["turn_id"], "payload": {"content": reply, "kind": "text"}},
        {"type": "turn.completed", "turn_id": run["turn_id"], "payload": {"stop_reason": "end_turn", "usage": {}}},
    ])
    turns.settle_turn(run["turn_id"], "completed")
    return run


def _briefs(home_id):
    return [e for e in get_events(home_id, 0) if e["event_type"] == "message.assistant"
            and (e.get("payload") or {}).get("source") == "assistant_research_brief"]


def _brief_state(run_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT brief FROM assistant_runs WHERE id=%s", (run_id,))
        return cursor.fetchone()["brief"]


@pytest.mark.asyncio
async def test_research_that_found_something_posts_one_brief_into_the_home(producer, assistant_fixture):
    manager = module.AssistantManager()
    run = await _finished_run(manager, "An invoice from Acme is due Friday.")
    await manager.run_once()
    assert store.get_run(run["id"])["status"] == "completed"
    briefs = _briefs(assistant_fixture["id"])
    assert len(briefs) == 1
    payload = briefs[0]["payload"]
    assert payload["content"].startswith(service.BRIEF_HEADING)
    assert "An invoice from Acme is due Friday." in payload["content"]
    assert payload["research_session_id"] == run["session_id"] and payload["run_id"] == run["id"]
    assert briefs[0]["turn_id"].startswith(turn_recorder.BRIEF_TURN_PREFIX)
    # A synthetic turn: no turns row, so nothing runs in the home.
    assert turns.get_turn(briefs[0]["turn_id"]) is None
    assert _brief_state(run["id"]) == "posted"
    await manager.run_once()
    assert len(_briefs(assistant_fixture["id"])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["NOTHING_TO_REPORT", "  `nothing_to_report`.", ""])
async def test_a_run_with_nothing_to_tell_posts_nothing(producer, assistant_fixture, reply):
    manager = module.AssistantManager()
    run = await _finished_run(manager, reply)
    await manager.run_once()
    assert store.get_run(run["id"])["status"] == "completed"
    assert _briefs(assistant_fixture["id"]) == []
    assert _brief_state(run["id"]) == "none"


@pytest.mark.asyncio
async def test_the_brief_waits_while_the_home_has_a_turn_in_progress(producer, assistant_fixture):
    manager = module.AssistantManager()
    home_id = assistant_fixture["id"]
    owner_turn = turns.create_turn(home_id, "user", prompt="hello") if hasattr(turns, "create_turn") else None
    if owner_turn is None:
        from app.core import turn_manager
        owner_turn = {"id": turn_manager.submit(home_id, "user", prompt="hello")}
    run = await _finished_run(manager, "Your flight moved to 9am.")
    await manager.run_once()
    assert _briefs(home_id) == [] and _brief_state(run["id"]) is None
    turns.settle_turn(owner_turn["id"], "completed")
    await manager.run_once()
    assert len(_briefs(home_id)) == 1


@pytest.mark.asyncio
async def test_a_home_on_another_worker_never_gets_a_brief_from_here(producer, assistant_fixture):
    manager = module.AssistantManager()
    run = await _finished_run(manager, "Something worth telling.")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET instance_id='another-worker' WHERE id=%s", (assistant_fixture["id"],))
    assert service.brief_destination() == (None, "skip")
    await manager.run_once()
    assert _briefs(assistant_fixture["id"]) == []
    assert _brief_state(run["id"]) == "none"


@pytest.mark.asyncio
async def test_a_failed_post_is_retried_on_the_next_pass(producer, assistant_fixture, monkeypatch):
    manager = module.AssistantManager()
    run = await _finished_run(manager, "Worth telling.")

    async def fails(*args, **kwargs):
        return None

    with monkeypatch.context() as broken:
        broken.setattr(turn_recorder, "record_brief", fails)
        await manager.run_once()
    assert _brief_state(run["id"]) is None
    await manager.run_once()
    assert len(_briefs(assistant_fixture["id"])) == 1


def test_stale_and_pre_brief_runs_are_never_briefed(assistant_fixture):
    service.register_proactive_guard(1)
    events()
    run = store.admit_run(service.assistant_id())
    database.update_session  # noqa: B018 (keeps the import used on every path)
    with get_db() as conn:
        conn.cursor().execute("UPDATE assistant_runs SET status='completed',session_id=%s,ended_at=%s WHERE id=%s",
                              (assistant_fixture["id"], (store.now() - store.BRIEF_WINDOW - timedelta(minutes=1)).isoformat(), run["id"]))
    assert store.pending_briefs(service.assistant_id()) == []
    # Rows that existed before the column are marked decided by the migration.
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE assistant_runs SET ended_at=%s WHERE id=%s", (store.now().isoformat(), run["id"]))
    assert [r["id"] for r in store.pending_briefs(service.assistant_id())] == [run["id"]]


def test_brief_text_trims_and_bounds():
    assert service.brief_text(None) is None
    assert service.brief_text("Checked it. NOTHING_TO_REPORT") is None
    long = service.brief_text("x" * 5000)
    assert len(long) == len(service.BRIEF_HEADING) + service.BRIEF_MAX_CHARS
