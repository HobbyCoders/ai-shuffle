"""Assistant research identity, provenance and source helpers."""
import pytest

from app.core import assistant_service as service, task_registry
from app.db import assistant as store, database, turns
from app.db.base import get_db
from tests.db.test_assistant_store import assistant_fixture, events, lose_agent


def test_a_changed_builtin_agent_row_is_restored(assistant_fixture):
    with get_db() as conn:
        conn.cursor().execute("UPDATE agents SET name='Edited', config=%s WHERE id=%s",
                              ('{"model": "pinned", "system_prompt": "x"}', service.AGENT_ID))
    service.reconcile()
    agent = database.get_profile(service.AGENT_ID)
    assert agent["name"] == service.AGENT_NAME and agent["config"] == service.AGENT_CONFIG and agent["is_builtin"]


# ---------------------------------------------------------------------------
# Prompt assembly: every chat gets the same delegation text
# ---------------------------------------------------------------------------

async def _assemble(session_id, profile_id):
    from unittest.mock import AsyncMock
    from app.core.provider import harness_adapter as ha
    from app.core.query_options import build_options_from_profile
    options, _ = build_options_from_profile(database.get_profile(profile_id), session_id=session_id)
    adapter = ha.HarnessAdapter()
    adapter.connect = AsyncMock()
    await adapter.connect_with_sdk_options(options)
    projected = adapter.connect.await_args.args[0]
    return options, projected, "\n\n".join(ha._workflow_prompt_blocks(projected, None))


@pytest.mark.asyncio
async def test_no_chat_gets_a_personal_assistant_prompt(assistant_fixture):
    from app.core.agent_provider_models import DELEGATE_WHEN_AUTHORIZED
    for session_id, profile_id in ((assistant_fixture["id"], "assistant-agent"),
                                   (database.create_session("on-builtin", service.AGENT_ID, is_active=True)["id"],
                                    service.AGENT_ID)):
        options, projected, workflow = await _assemble(session_id, profile_id)
        assert "# Personal assistant" not in str(options.system_prompt)
        assert "assistant_home" not in projected.extra
        assert DELEGATE_WHEN_AUTHORIZED in workflow


def test_unconfigured_safe_empty():
    assert service.source_catalog() == []
    assert not store.research_allowed(store.get_state(service.assistant_id()))


def test_any_session_can_settle(assistant_fixture):
    assert not database.set_session_active(assistant_fixture["id"], False)["is_active"]
    on_builtin = database.create_session("on-builtin", service.AGENT_ID, is_active=True, hidden=True)
    assert not database.set_session_active(on_builtin["id"], False)["is_active"]


def test_proactive_provenance_revoke_and_forgery(assistant_fixture):
    events()
    run = store.admit_run(service.assistant_id())
    marker = service.proactive_metadata(run)
    assert service.assert_proactive_allowed(marker, run["session_id"])["id"] == run["id"]
    with pytest.raises(ValueError, match="match"):
        service.assert_proactive_allowed(dict(marker, source_ids=["forged"]))
    with pytest.raises(ValueError, match="Invalid"):
        service.assert_proactive_allowed(dict(marker, read_only=False))
    # A legacy pause no longer revokes; removing the agent does.
    store.configure(service.assistant_id(), {"paused": True, "enabled": False})
    service.reconcile()  # Sets enabled again and ignores paused.
    assert service.assert_proactive_allowed(marker)["id"] == run["id"]
    lose_agent()
    with pytest.raises(ValueError, match="missing its agent"):
        service.assert_proactive_allowed(marker)
    service.reconcile()  # The built-in agent is still there: projection restores it.
    assert service.assert_proactive_allowed(marker)["id"] == run["id"]
    database.update_connector("conn-mail", is_active=False)
    with pytest.raises(ValueError, match="grant"):
        service.assert_proactive_allowed(marker)


def test_owner_runner_and_explicit_source_optin(assistant_fixture):
    database.create_connector("peer", "gmail", "Another worker", {}, [], is_active=True, runner_id="other-worker")
    database.update_connector_status("peer", "connected")
    database.create_api_user("another-user", "Other", "unique-user-key")
    database.create_connector("user-mail", "gmail", "API user's mail", {}, [], api_user_id="another-user", is_active=True, runner_id=service.assistant_id())
    database.update_connector_status("user-mail", "connected")
    assert events(connector_id="peer") == []
    assert events(connector_id="user-mail") == []
    catalog = service.source_catalog()
    assert {s["id"] for s in catalog} == {"conn-mail", "peer"}
    assert not next(s for s in catalog if s["id"] == "peer")["available"]
    with pytest.raises(ValueError, match="unavailable"):
        service.set_source("peer", True)
    service.set_source("conn-mail", False)
    assert events() == []


def test_stop_latch_and_durable_stop_are_honored(assistant_fixture):
    sid = assistant_fixture["id"]
    task_registry.task_registry.mark_user_stopped(sid)
    assert service.session_stopped(sid)
    task_registry.task_registry.clear_user_stop(sid)
    turn = turns.enqueue_turn(sid, "user", prompt="run")
    turns.settle_turn(turn["id"], "aborted", stop_reason="user_stopped")
    assert service.session_stopped(sid)
    turns.enqueue_turn(sid, "user", prompt="user continued")
    assert not service.session_stopped(sid)


def test_dedicated_connector_credential_revocation_and_agent_scope(assistant_fixture):
    events()
    run = store.admit_run(service.assistant_id())
    marker = service.proactive_metadata(run)
    database.update_api_user("mail-user", is_active=False)
    assert not next(s for s in service.source_catalog() if s["id"] == "conn-mail")["available"]
    assert events(message_id="revoked-credential") == []
    with pytest.raises(ValueError, match="grant"):
        service.assert_proactive_allowed(marker)
    database.update_api_user("mail-user", is_active=True)
    database.create_profile("other-allowed-agent", "Scoped specialist", None, {})
    database.update_api_user("mail-user", profile_ids=["other-allowed-agent"])
    assert events(message_id="wrong-agent") == []
    with pytest.raises(ValueError, match="grant"):
        service.assert_proactive_allowed(marker)
    database.update_api_user("mail-user", profile_ids=[service.AGENT_ID])
    assert service.assert_proactive_allowed(marker)["id"] == run["id"]
    assert events(message_id="allowed-agent")


@pytest.mark.asyncio
async def test_slow_assistant_retention_does_not_stall_reconciliation_and_stops(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app.core import assistant_manager as module, assistant_limits

    manager = module.AssistantManager()
    monkeypatch.setattr(module, "RETENTION_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(manager, "run_once", AsyncMock())
    monkeypatch.setattr(manager, "detect_owner", lambda *a: None)
    monkeypatch.setattr(module.service, "reconcile", lambda: None)
    monkeypatch.setattr(assistant_limits, "enforce", AsyncMock())
    entered, reconciled, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()

    def sweep(assistant_id, *, stop):
        loop.call_soon_threadsafe(entered.set)
        assert stop.wait(3)
        loop.call_soon_threadsafe(finished.set)
        return {}

    def activity(*a):
        if entered.is_set():
            loop.call_soon_threadsafe(reconciled.set)
        loop.call_soon_threadsafe(manager._wake.set)
        return []

    monkeypatch.setattr(store, "retention_sweep", sweep)
    monkeypatch.setattr(store, "get_activity", activity)
    manager._task = asyncio.create_task(manager._loop())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(reconciled.wait(), 2)
        assert not manager._retention_task.done()
    finally:
        await manager.shutdown()
    await asyncio.wait_for(finished.wait(), 2)


@pytest.mark.asyncio
async def test_manager_loop_sweeps_retention_at_most_once_per_interval(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.core import assistant_manager as module

    manager = module.AssistantManager()
    monkeypatch.setattr(module, "RETENTION_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(manager, "run_once", AsyncMock())
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    sweeps = []

    def sweep(assistant_id, *, stop):
        assert not stop.is_set()
        sweeps.append(assistant_id)
        raise RuntimeError("database unavailable")  # Logged; never stops the loop.
    monkeypatch.setattr(store, "retention_sweep", sweep)
    loop = asyncio.get_running_loop()
    passes = []
    done = asyncio.Event()

    def activity(*args):
        passes.append(True)
        loop.call_soon_threadsafe(done.set if len(passes) >= 4 else manager._wake.set)
        return []
    monkeypatch.setattr(store, "get_activity", activity)
    task = asyncio.create_task(manager._loop())
    try:
        await asyncio.wait_for(done.wait(), timeout=3)
        await asyncio.wait_for(manager._retention_task, 2)
        assert len(sweeps) == 1 and not task.done()
        clock.now += module.RETENTION_INTERVAL_SECONDS
        await manager._maybe_retention_sweep()
        await manager._maybe_retention_sweep()
        await asyncio.wait_for(manager._retention_task, 2)
        assert len(sweeps) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
