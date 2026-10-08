"""StartSession on another worker (#1682): caller receipt -> signed control -> owner DB."""
import json
from unittest.mock import AsyncMock

import pytest
from fluid.tools.builtins.session_orchestration import SPECS
from fluid.tools.registry import ToolCtx

from app.core import session_orchestration as orch
from app.core import turn_scheduler
from app.db import database, turns
from app.db import network as store
from app.db.base import get_db
from app.network import control, engine, transport
from tests.core.network.harness import converge, join
from tests.core.network.test_session_active_control import connect_owner


@pytest.fixture(autouse=True)
def install_local_principal(monkeypatch):
    # The network harness has no mobile identity tables; these are owner chats.
    monkeypatch.setattr(orch, "_principal", lambda sid: None)


@pytest.fixture
def kicks(monkeypatch):
    seen = []
    monkeypatch.setattr(turn_scheduler, "kick", seen.append)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda sid: None)
    return seen


def _caller(worker, session_id="caller", **fields):
    with worker.active():
        database.create_session(session_id=session_id, profile_id="default", project_id=None, title="Windows chat",
                                is_active=True, model="sonnet", permission_mode="acceptEdits", **fields)
        turn = turns.enqueue_turn(session_id, "user", prompt="work", profile_id="default")
        with get_db() as conn:
            conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))
        engine.scan_all()


def network(workers, monkeypatch, *, online=True):
    """A ("Unraid") owns the new chat; B ("Windows") runs the caller."""
    a, b = workers("unraid"), workers("windows")
    served = connect_owner(monkeypatch, a)
    with a.active():
        engine.create_network("Home", device_name="Unraid")
    join(a, b, name="Windows")
    for worker in (a, b):
        with worker.active():
            from app.db import mobile_tool_identity
            mobile_tool_identity.init_schema()
    _caller(b)
    converge([a, b])
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
    transport._last_contact.pop(a.device_id, None)
    if online:
        transport.note_contact(a.device_id)
    return a, b, served


async def start(args, sender="caller"):
    _, handler = next((s, h) for s, h in SPECS if s.name == "StartSession")
    return await handler(args, ToolCtx(extra={"session_orchestration": orch.make_handler(lambda: sender)}))


def receipts():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id LIKE %s", (orch.REMOTE_RECEIPT_PREFIX + "%",))
        return [turns._row(r) for r in cursor.fetchall()]


def agent_turns(session_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s AND source='agent_message'", (session_id,))
        return [turns._row(r) for r in cursor.fetchall()]


ARGS = {"title": "Folder sandbox test", "prompt": "Run the private-folder test here.",
        "request_id": "unraid-test", "worker": "Unraid"}


@pytest.mark.parametrize("effort,expected", [(None, "max"), ("low", "low")])
async def test_remote_start_receiver_preference_beats_sender_fallback_not_explicit(workers, monkeypatch, kicks, effort, expected):
    from app.core import preference_identity
    from app.core.model_effort import effort_preference_key
    a, b, served = network(workers, monkeypatch)
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    with a.active():
        database.set_user_preference("account", "owner", effort_preference_key("opus"), "max")
    with b.active():
        database.set_session_run_binding("caller", model="opus", effort="low")
        result = await start({**ARGS, **({"effort": effort} if effort is not None else {})})
        assert not result.is_error, result.text
        child = json.loads(result.text)["session_id"]
    with a.active():
        row = database.get_session(child)
        [turn] = agent_turns(child)
        assert row["effort"] == turn["overrides"]["effort"] == expected


async def test_starts_on_the_chosen_worker_once(workers, monkeypatch, kicks):
    a, b, served = network(workers, monkeypatch)
    with b.active():
        first = await start(ARGS)
        assert not first.is_error, first.text
        result = json.loads(first.text)
        assert result["created"] is True and result["delivery_status"] == "queued"
        assert result["worker"] == {"device_id": a.device_id, "name": "Unraid"}
        # The receipt is caller-side accounting only, settled at birth.
        [receipt] = receipts()
        assert receipt["state"] == "completed" and receipt["command_id"].startswith("orchestration:start:")
        assert database.get_session(result["session_id"]) is None or \
            database.get_session(result["session_id"])["owner_device_id"] == a.device_id
    with a.active():
        row = database.get_session(result["session_id"])
        assert row["owner_device_id"] == a.device_id and row["is_active"]
        assert row["title"] == "Folder sandbox test" and row["profile_id"] == "default"
        assert row["model"] == "sonnet" and row["permission_mode"] == "acceptEdits"
        # No checkout path, worktree or branch travels from the caller.
        assert not row.get("worktree_path") and not row.get("worktree_id") and not row.get("branch")
        [turn] = agent_turns(result["session_id"])
        assert turn["state"] == "queued" and turn["id"] == result["turn_id"]
        payload = turn["payload"]
        assert payload["orchestration_remote_origin"] == b.device_id
        assert payload["peer_message_content"] == ARGS["prompt"]
        assert payload["peer_sender_label"] == "Windows chat (on Windows)"
        assert "orchestration_root" not in payload
        assert "another top-level session" in turn["prompt"]
    assert kicks == [result["session_id"]]
    assert ("POST", "/api/v1/network/peer/control/orchestration-start") in served.requests

    # The same request_id reads the same chat back and never makes another.
    with b.active():
        again = json.loads((await start(ARGS)).text)
    assert again["session_id"] == result["session_id"] and again["created"] is False
    assert again["status"] == "already_queued"
    with a.active():
        assert len(agent_turns(result["session_id"])) == 1
    with b.active():
        assert len(receipts()) == 1
    assert kicks == [result["session_id"]]

    # Same request_id, different arguments: refused before any call.
    with b.active():
        changed = await start({**ARGS, "prompt": "Something else"})
    assert changed.is_error and "different arguments" in changed.text


@pytest.mark.parametrize("worker", [None, "Windows"])
async def test_omitted_or_own_worker_starts_here(workers, monkeypatch, kicks, worker):
    _a, b, _ = network(workers, monkeypatch)
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=AssertionError("no remote call")))
    args = {k: v for k, v in ARGS.items() if k != "worker"}
    if worker:
        args["worker"] = worker
    with b.active():
        outcome = await start(args)
        assert not outcome.is_error, outcome.text
        result = json.loads(outcome.text)
        assert "worker" not in result and result["created"] is True
        row = database.get_session(result["session_id"])
        assert row["owner_device_id"] in (None, b.device_id)
        assert receipts() == []


async def test_offline_worker_is_refused_not_started_here(workers, monkeypatch, kicks):
    _a, b, _ = network(workers, monkeypatch, online=False)
    call = AsyncMock(side_effect=AssertionError("no remote call"))
    monkeypatch.setattr(control, "call", call)
    with b.active():
        before = len(database.get_sessions(limit=100))
        outcome = await start(ARGS)
        assert outcome.is_error and "Unraid is offline" in outcome.text
        assert len(database.get_sessions(limit=100)) == before and receipts() == []
    call.assert_not_called()


async def test_unknown_worker_names_the_choices(workers, monkeypatch, kicks):
    _a, b, _ = network(workers, monkeypatch)
    with b.active():
        outcome = await start({**ARGS, "worker": "Laptop"})
    assert outcome.is_error and "Unknown worker 'Laptop'" in outcome.text
    assert "Unraid (online)" in outcome.text and "Windows (this worker)" in outcome.text


async def test_removed_worker_is_refused(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    with b.active():
        from app.network import membership
        real_member = engine.context().member

        def member(device_id):
            found = real_member(device_id)
            if found and device_id == a.device_id:
                return membership.Member(**{**found.__dict__, "status": "revoked"})
            return found
        monkeypatch.setattr(engine.context(), "member", member, raising=False)
        outcome = await start(ARGS)
    assert outcome.is_error and "not an active worker" in outcome.text


async def test_destination_refusal_releases_the_reservation(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    with b.active():
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=%s WHERE id=%s", ("pv2-not-on-unraid", "caller"))
        engine.scan_all()
    converge([a, b])
    with b.active():
        outcome = await start(ARGS)
        assert outcome.is_error and "Unraid refused the new chat" in outcome.text
        assert "not checked out on this worker" in outcome.text
        # Nothing was made, so the budget and request_id are given back.
        assert receipts() == []
    assert kicks == []


async def test_unreachable_keeps_the_receipt_and_retry_is_idempotent(workers, monkeypatch, kicks):
    _a, b, _served = network(workers, monkeypatch)
    real_call = control.call
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise control.RunnerUnreachable("Unraid did not answer in time.")
        return await real_call(*args, **kwargs)
    monkeypatch.setattr(control, "call", flaky)
    with b.active():
        first = await start(ARGS)
        assert first.is_error and "same request_id" in first.text
        assert len(receipts()) == 1
        second = json.loads((await start(ARGS)).text)
        assert second["created"] is True
        third = json.loads((await start(ARGS)).text)
        assert third["created"] is False and third["session_id"] == second["session_id"]
        assert len(receipts()) == 1


async def test_remote_started_chat_cannot_fan_out(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    with b.active():
        result = json.loads((await start(ARGS)).text)
    with a.active():
        new_id = result["session_id"]
        [turn] = agent_turns(new_id)
        assert turns.start_turn(turn["id"]) and turns.settle_turn(turn["id"], "completed")
        refused = await start({"title": "More", "prompt": "Spawn", "request_id": "fan"}, sender=new_id)
        assert refused.is_error and "started from another worker" in refused.text


async def test_only_owner_chats_may_start_remotely(workers, monkeypatch, kicks):
    _a, b, _ = network(workers, monkeypatch)
    monkeypatch.setattr(orch, "_principal", lambda sid: {"kind": "paired", "owner": "account:x"})
    monkeypatch.setattr("app.core.mobile_tool_identity.validate", lambda principal: None)
    with b.active():
        outcome = await start(ARGS)
        assert outcome.is_error and "Only the owner's chats" in outcome.text
        assert receipts() == []


async def test_destination_refuses_non_worker_devices(workers):
    a, b = workers("unraid"), workers("windows")
    with a.active():
        engine.create_network("Home", device_name="Unraid")
    join(a, b, name="Windows")
    with a.active():
        with pytest.raises(PermissionError):
            orch.accept_remote_start({}, "not-a-member")
        with pytest.raises(PermissionError):
            orch.accept_remote_start({}, a.device_id)
        with pytest.raises(ValueError, match="identity"):
            orch.accept_remote_start({"session_id": "00000000-0000-0000-0000-000000000000",
                                      "command_id": "orchestration:start:" + "0" * 64}, b.device_id)


def test_start_session_schema_offers_worker():
    spec = next(s for s, _ in SPECS if s.name == "StartSession")
    assert spec.input_schema["properties"]["worker"]["type"] == "string"
    assert spec.input_schema["required"] == ["title", "prompt", "request_id"]


# ---------------------------------------------------------------------------
# The owner's assistant names a project; the chat reports back across workers
# ---------------------------------------------------------------------------

def _as_assistant_home(monkeypatch, home="caller"):
    from app.core import assistant_service
    monkeypatch.setattr(assistant_service, "is_primary_session", lambda sid: sid == home)
    monkeypatch.setattr(assistant_service, "get_primary_session_id", lambda: home)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)


def _project_on(a, b, tmp_path, *, checked_out_on_a=True):
    from app.core.config import settings
    from app.db import checkouts, projects_v2
    for worker in (a, b):
        with worker.active():
            project = projects_v2.upsert_project("git:shuffle", name="ai-shuffle")
    if checked_out_on_a:
        with a.active():
            checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(tmp_path))
    return project


async def test_assistant_project_runs_on_the_worker_with_the_checkout_and_reports_back(
        workers, monkeypatch, kicks, tmp_path):
    from app.core import away_mode
    from app.db import session_events
    a, b, _ = network(workers, monkeypatch)
    _as_assistant_home(monkeypatch)
    project = _project_on(a, b, tmp_path)
    with b.active():
        outcome = await start({"title": "Fix the flaky test", "prompt": "Fix it.", "request_id": "p1",
                               "project": "ai-shuffle"})
        assert not outcome.is_error, outcome.text
        result = json.loads(outcome.text)
        assert result["worker"]["name"] == "Unraid"
        assert "reports back to you automatically" in result["note"] and "Away mode" in result["note"]
        [receipt] = receipts()
        assert receipt["payload"]["report_to"] == "caller"
    new_id = result["session_id"]
    with a.active():
        row = database.get_session(new_id)
        assert row["project_v2_id"] == project["id"] and row["away_mode"]
        [turn] = agent_turns(new_id)
        assert turn["payload"]["orchestration_report_device"] == b.device_id
        assert away_mode.is_away(new_id)
        # Away mode there defers approvals (never grants them).
        assert turns.start_turn(turn["id"])
        session_events.append_events(new_id, [{"type": "message.user", "turn_id": turn["id"],
                                               "payload": {"content": "Fix it."}}])
        assert await away_mode.defer_if_away(new_id, "permission for Bash") == away_mode.DEFERRED_MESSAGE
        session_events.append_events(new_id, [{"type": "assistant.text_delta", "turn_id": turn["id"],
                                               "payload": {"text": "Fixed; push deferred."}}])
        assert turns.settle_turn(turn["id"], "completed")

    real_call = control.call

    async def deliver(device_id, action, body, **kwargs):
        if action != orch.REPORT_ACTION:
            return await real_call(device_id, action, body, **kwargs)
        assert device_id == b.device_id
        with b.active():
            return await control.handle(action, body, from_device=a.device_id)
    monkeypatch.setattr(control, "call", deliver)
    with a.active():
        await orch.report_settled_start(new_id, turns.get_turn(turn["id"]), "completed")
    with b.active():
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM turns WHERE session_id='caller' AND source='unpark'")
            [wake] = [turns._row(r) for r in cursor.fetchall()]
    assert "<kind>session</kind>" in wake["prompt"] and "Fixed; push deferred." in wake["prompt"]
    assert "on Unraid" in wake["prompt"] and "permission for Bash" in wake["prompt"]
    assert wake["payload"]["orchestration_root"] == receipt["parent_turn_id"]


async def test_reports_are_only_taken_from_the_worker_that_holds_the_chat(workers, monkeypatch, kicks, tmp_path):
    a, b, _ = network(workers, monkeypatch)
    _as_assistant_home(monkeypatch)
    _project_on(a, b, tmp_path)
    with b.active():
        result = json.loads((await start({"title": "T", "prompt": "P", "request_id": "p2",
                                          "project": "ai-shuffle"})).text)
        report = {"status": "completed", "summary": "done", "agent_message_turn_id": "t1"}
        with pytest.raises(PermissionError):
            orch.accept_remote_report({"session_id": result["session_id"], "report": report}, b.device_id)
        with pytest.raises(PermissionError):
            orch.accept_remote_report({"session_id": "some-other-chat", "report": report}, a.device_id)
        with pytest.raises(ValueError, match="status"):
            orch.accept_remote_report({"session_id": result["session_id"],
                                       "report": {**report, "status": "approved"}}, a.device_id)


async def test_assistant_project_with_no_online_checkout_is_refused(workers, monkeypatch, kicks, tmp_path):
    _a, b, _ = network(workers, monkeypatch, online=False)
    _as_assistant_home(monkeypatch)
    _project_on(_a, b, tmp_path)
    with b.active():
        before = len(database.get_sessions(limit=100))
        outcome = await start({"title": "T", "prompt": "P", "request_id": "p3", "project": "ai-shuffle"})
        assert outcome.is_error and "No online worker has ai-shuffle checked out" in outcome.text
        assert "Unraid (offline)" in outcome.text
        assert len(database.get_sessions(limit=100)) == before and receipts() == []


async def test_ordinary_remote_start_keeps_its_old_body(workers, monkeypatch, kicks):
    a, b, _ = network(workers, monkeypatch)
    with b.active():
        result = json.loads((await start(ARGS)).text)
    assert "report back" not in result["note"]
    with a.active():
        row = database.get_session(result["session_id"])
        assert not row.get("away_mode")
        [turn] = agent_turns(result["session_id"])
        assert "orchestration_report_device" not in turn["payload"]


# ---------------------------------------------------------------------------
# A model the user asked for, on another worker (#1830)
# ---------------------------------------------------------------------------

def _user_says(worker, text, session_id="caller"):
    with worker.active():
        turn = turns.enqueue_turn(session_id, "user", prompt=text, profile_id="default")
        with get_db() as conn:
            conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))


def _grok(worker):
    with worker.active():
        database.create_model_provider("grok", "Grok", "xai", "https://example.invalid/v1",
                                       models=[{"id": "grok-4.5", "label": "Grok 4.5"}])


@pytest.mark.parametrize("wording", ["prov:grok:grok-4.5", "GROK 4.5", "prov:grok:GROK 4.5"])
async def test_a_named_model_starts_on_the_chosen_worker_with_its_provider(workers, monkeypatch, kicks, wording):
    from app.core import task_dispatch
    monkeypatch.setattr(task_dispatch, "_credential_gap", lambda provider: None)
    a, b, _ = network(workers, monkeypatch)
    _grok(a)
    # A remote-only exact composite remains supported. Spoken forms need a
    # registry to identify the provider, then the receiver validates its own.
    if wording != "prov:grok:grok-4.5":
        _grok(b)
    _user_says(b, f"Run the test on Unraid with {wording}")
    with b.active():
        outcome = await start({**ARGS, "model": wording})
    assert not outcome.is_error, outcome.text
    result = json.loads(outcome.text)
    assert result["model"] == "prov:grok:grok-4.5"
    assert result["provider"]["id"] == "grok"
    assert result["model_override"] == {"source": "named_by_user", "instead_of": "sonnet"}
    with a.active():
        row = database.get_session(result["session_id"])
        assert (row["provider_id"], row["model"]) == ("grok", "prov:grok:grok-4.5")
        [turn] = agent_turns(result["session_id"])
        assert turn["overrides"]["model"] == "prov:grok:grok-4.5"
        assert turn["payload"]["model_override"]["source"] == "named_by_user"


@pytest.mark.parametrize("wording", ["prov:grok:grok-4.5", "GROK 4.5"])
async def test_the_destination_never_reinterprets_a_resolved_model_id(workers, monkeypatch, kicks, wording):
    from app.core import task_dispatch
    monkeypatch.setattr(task_dispatch, "_credential_gap", lambda provider: None)
    a, b, _ = network(workers, monkeypatch)
    _grok(b)
    with a.active():
        database.create_model_provider("grok", "Grok", "xai", "https://example.invalid/v1",
                                       models=[{"id": "different-id", "label": "Grok 4.5"}])
        before = len(database.get_sessions(limit=100))
    _user_says(b, f"Run the test on Unraid with {wording}")
    with b.active():
        outcome = await start({**ARGS, "model": wording})
        assert outcome.is_error, outcome.text
        assert "Unraid refused the new chat" in outcome.text and "exact model id" in outcome.text
        assert "prov:grok:different-id" in outcome.text
        assert receipts() == []
    with a.active():
        assert len(database.get_sessions(limit=100)) == before
    assert kicks == []


@pytest.mark.parametrize("wording", ["prov:grok:grok-4.5", "GROK 4.5"])
async def test_the_destination_refuses_a_model_it_cannot_run(workers, monkeypatch, kicks, wording):
    from app.core import task_dispatch
    monkeypatch.setattr(task_dispatch, "_credential_gap", lambda provider: None)
    a, b, _ = network(workers, monkeypatch)
    _grok(b)  # A local match is NOT evidence that the receiver can run it.
    with a.active():
        before = len(database.get_sessions(limit=100))
    _user_says(b, f"Run the test on Unraid with {wording}")
    with b.active():
        outcome = await start({**ARGS, "model": wording})
        assert outcome.is_error
        assert "Unraid refused the new chat" in outcome.text and "cannot run on this worker" in outcome.text
        # Nothing was made there, so the receipt is given back.
        assert receipts() == []
    with a.active():
        assert len(database.get_sessions(limit=100)) == before
    assert kicks == []
