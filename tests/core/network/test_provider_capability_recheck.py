"""Capability-gated provider rows are judged again when a worker joins later (#1519).

A ``model_providers`` row syncs to every member. A row type an older build
cannot run (Kimi/MiniMax/OpenCode, Google Gemini, Antigravity) is gated by a
capability in the member's signed presence. The creation-time refusal only
covered members present at creation; these tests cover a member admitted
afterwards, its presence arriving or changing, an older presence with no
capabilities field, and revocation.
"""
import asyncio
import json
import logging

import pytest

from app.core import gemini_provider, provider_capability
from app.db import database as db
from app.network import engine
from tests.core.network.harness import exchange, join


@pytest.fixture(autouse=True)
def _fresh_recheck_state():
    provider_capability._reset_for_tests()
    yield
    provider_capability._reset_for_tests()


def _admit(founder, joiner, name="Old worker"):
    """Admit ``joiner`` without it publishing any presence (an older build, or not seen yet)."""
    with founder.active():
        engine.admit_device(device_id=joiner.device_id, signing_public=joiner.identity.signing_public,
                            encryption_public=joiner.identity.encryption_public, name=name)


def _gemini_row(row_id="gem", name="Gemini key"):
    db.create_model_provider(row_id, name, gemini_provider.PROVIDER, gemini_provider.API_ROOT,
                             models=[{"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"}])


def _set_presence(device_id, capabilities):
    """Write a peer's presence row as sync would have stored it."""
    with db.get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO network_presence (device_id, endpoints, capabilities, version, updated_at) "
            "VALUES (%s, '[]', %s, 'old', NOW()) ON CONFLICT (device_id) DO UPDATE SET "
            "capabilities = EXCLUDED.capabilities",
            (device_id, json.dumps(capabilities)))


def test_member_admitted_after_a_gated_row_is_reported_and_refused(workers, caplog):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="Current")
        engine.publish_presence()
        _gemini_row()
    caplog.set_level(logging.WARNING, logger="app.core.provider_capability")
    _admit(a, b)
    with a.active():
        # Admission judged the gated row for the new member at once.
        assert "Old worker needs an update to use these providers: Gemini key" in caplog.text
        rows = db.get_all_model_providers()
        assert provider_capability.workers_requiring_update_for_rows(rows) == {"gem": ["Old worker"]}
        assert gemini_provider.workers_requiring_update() == ["Old worker"]
        reason = provider_capability.refusal(b.device_id, "gem")
        assert reason == "Old worker needs an update to use Gemini key."
        # This worker runs this build; an ungated row is never refused.
        assert provider_capability.refusal(a.device_id, "gem") is None
        db.create_model_provider("or", "OpenRouter", "openrouter", "https://openrouter.ai/api")
        assert provider_capability.refusal(b.device_id, "or") is None


# "missing": no presence row at all (never seen); {}: an older build's presence
# with no capabilities reported; the others report something else.
@pytest.mark.parametrize("capabilities", ["missing", {}, {"some_other_v1": 1}, {gemini_provider.CAPABILITY: 0}])
def test_presence_without_the_capability_counts_as_not_capable(workers, capabilities):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        _gemini_row()
    _admit(a, b)
    with a.active():
        if capabilities != "missing":
            _set_presence(b.device_id, capabilities)
        assert provider_capability.refusal(b.device_id, "gem") is not None
        assert provider_capability.recheck([b.device_id]) == {b.device_id: ["gem"]}


def test_presence_reporting_the_capability_clears_the_gap(workers, caplog):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        engine.publish_presence()
        _gemini_row()
    join(a, b, name="Old worker")
    with a.active():
        # What a has stored for b is an older build's presence: nothing reported.
        _set_presence(b.device_id, {})
        assert provider_capability.recheck() == {b.device_id: ["gem"]}
    # b updates: its signed presence reports the capability and reaches a
    # through the normal sync path, which re-checks on arrival.
    with b.active():
        engine.publish_presence(version="updated")
    caplog.set_level(logging.INFO, logger="app.core.provider_capability")
    exchange(b, a)
    with a.active():
        assert "Old worker can now use every provider on this network" in caplog.text
        assert provider_capability.refusal(b.device_id, "gem") is None
        assert provider_capability.workers_requiring_update_for_rows(db.get_all_model_providers()) == {}
        assert provider_capability.recheck() == {}


def test_revoked_members_are_ignored(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        _gemini_row()
    _admit(a, b)
    with a.active():
        assert gemini_provider.workers_requiring_update() == ["Old worker"]
        engine.revoke_device(b.device_id)
        assert gemini_provider.workers_requiring_update() == []
        assert provider_capability.workers_requiring_update_for_rows(db.get_all_model_providers()) == {}
        assert provider_capability.recheck() == {}


def test_every_gated_type_uses_one_rule():
    from app.core import antigravity, native_providers

    for ptype in native_providers.PROVIDERS:
        assert provider_capability.capability_for(ptype) == native_providers.CAPABILITY
    assert provider_capability.capability_for("google") == gemini_provider.CAPABILITY
    assert provider_capability.capability_for(antigravity.PROVIDER_TYPE) == antigravity.CAPABILITY
    for ptype in ("anthropic", "openai", "chatgpt", "xai", "grok", "openrouter", "custom", None):
        assert provider_capability.capability_for(ptype) is None


def test_needs_update_message_wording():
    assert provider_capability.needs_update_message(["Office PC"]) == "Office PC needs an update to use this."
    assert (provider_capability.needs_update_message(["A", "B", "C"], "Gemini key")
            == "A, B and C need an update to use Gemini key.")


def test_forwarded_turn_on_a_gated_row_is_refused_before_it_is_sent(workers, monkeypatch):
    from app.network import control

    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        _gemini_row()
    _admit(a, b)

    async def never(*args, **kwargs):
        raise AssertionError("the turn must not be sent to a worker that cannot run it")

    monkeypatch.setattr(control, "call", never)
    with a.active():
        with pytest.raises(control.ControlRefused, match="Old worker needs an update to use Gemini key"):
            asyncio.run(control.submit_turn(b.device_id, "s1", prompt="hi",
                                            overrides={"model": "prov:gem:gemini-2.5-pro"}))
        with pytest.raises(control.ControlRefused, match="needs an update"):
            asyncio.run(control.submit_turn(b.device_id, "s1", prompt="hi", overrides={"provider": "gem"}))


def test_hand_off_and_automation_owner_refuse_an_incapable_worker(workers, monkeypatch):
    from app.core import session_handoff
    from app.network import ownership

    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        _gemini_row()
    _admit(a, b)
    with a.active():
        _gemini_session("s1")
        with pytest.raises(ValueError, match="Old worker needs an update to use Gemini key"):
            session_handoff._refuse_incapable("s1", b.device_id)
        session_handoff._refuse_incapable("s1", a.device_id)  # this worker can
        # A chat that names no gated row moves freely.
        db.create_session("s2", "default", model="sonnet", derive_provider_from_profile=False)
        session_handoff._refuse_incapable("s2", b.device_id)

        db.create_automation(automation_id="auto1", name="Nightly", trigger_type="schedule",
                             trigger_config={"cron": "0 0 * * *"}, prompt="do", profile_id="default",
                             model="prov:gem:gemini-2.5-pro", provider_id="gem")
        with pytest.raises(ValueError, match="needs an update"):
            ownership.assign("automations", "auto1", b.device_id)
        _set_presence(b.device_id, {gemini_provider.CAPABILITY: 1})
        assert ownership.assign("automations", "auto1", b.device_id)["owner_device_id"] == b.device_id


def test_remote_task_on_a_gated_row_is_not_dispatched(workers, monkeypatch):
    from app.core import remote_task

    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        _gemini_row()
    _admit(a, b)
    with a.active():
        result = asyncio.run(remote_task._dispatch_remote(
            worker={"device_id": b.device_id, "device_name": "Old worker"}, caller_row={},
            caller_session_id="s1", root_session_id="s1", depth=1, profile_id="default",
            agent_name="Task", result_agent="Task", provider_id="gem", model_id="gemini-2.5-pro",
            effort=None, effort_explicit=False, prompt="do it", label="Task", roster_name=None,
            mode=None, launch_tool_use_id=None, tool_allowlist=None, stop_generation=None))
    assert result["ok"] is False
    assert "Old worker needs an update to use Gemini key" in result["error"]
    assert remote_task.NOT_DISPATCHED in result["error"]
    # A bare model id the gated row lists, with no provider, is refused too.
    with a.active():
        result = asyncio.run(remote_task._dispatch_remote(
            worker={"device_id": b.device_id, "device_name": "Old worker"}, caller_row={},
            caller_session_id="s1", root_session_id="s1", depth=1, profile_id="default",
            agent_name="Task", result_agent="Task", provider_id=None, model_id="gemini-2.5-pro",
            effort=None, effort_explicit=False, prompt="do it", label="Task", roster_name=None,
            mode=None, launch_tool_use_id=None, tool_allowlist=None, stop_generation=None))
    assert result["ok"] is False and "needs an update" in result["error"]


def test_provider_list_reports_workers_needing_an_update(workers):
    from app.api import model_providers as api

    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        _gemini_row()
        db.create_model_provider("or", "OpenRouter", "openrouter", "https://openrouter.ai/api")
    _admit(a, b)
    with a.active():
        listed = {p.id: p.workers_requiring_update for p in asyncio.run(api.list_providers(token=None)).providers}
        assert listed == {"gem": ["Old worker"], "or": []}
        _set_presence(b.device_id, {gemini_provider.CAPABILITY: 1})
        listed = {p.id: p.workers_requiring_update for p in asyncio.run(api.list_providers(token=None)).providers}
        assert listed == {"gem": [], "or": []}


def test_open_provider_pages_refresh_only_when_a_gap_changes(workers, monkeypatch):
    from app.core.global_events import global_events

    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        _gemini_row()
    sent = []
    monkeypatch.setattr(global_events, "resources_changed",
                        lambda *resources, **kw: sent.append((resources, kw.get("relay", True))))
    _admit(a, b)
    with a.active():
        assert sent == [(("settings",), False)]  # the admission opened a gap
        provider_capability.recheck([b.device_id])
        assert len(sent) == 1  # unchanged gap: no refetch
        _set_presence(b.device_id, {gemini_provider.CAPABILITY: 1})
        provider_capability.recheck([b.device_id])
        assert sent[-1] == (("settings",), False) and len(sent) == 2


# ---------------------------------------------------------------------------
# The provider a turn runs on is the session's binding unless the turn picks
# another; every control action that wakes a session there is judged (#1519).
# ---------------------------------------------------------------------------

def _gemini_session(session_id="s-gem", owner=None):
    db.create_session(session_id, "default", provider_id="gem", model="gemini-2.5-pro",
                      derive_provider_from_profile=False)
    if owner:
        with db.get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET owner_device_id=%s WHERE id=%s", (owner, session_id))


def _network_with_old_worker(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        _gemini_row()
    _admit(a, b)
    return a, b


def _no_send(monkeypatch):
    from app.network import control

    sent = []

    async def call(device_id, action, body=None, **kwargs):
        sent.append((device_id, action, body))
        return {"turn_id": "t1", "session_id": (body or {}).get("session_id")}

    monkeypatch.setattr(control, "call", call)
    return sent


def test_turns_without_overrides_follow_the_session_binding(workers, monkeypatch):
    """A composer queue, a voice request and a held-message replay carry no
    overrides: the session's own Gemini binding decides."""
    from app.core import voice_remote
    from app.network import control

    a, b = _network_with_old_worker(workers)
    sent = _no_send(monkeypatch)
    with a.active():
        _gemini_session(owner=b.device_id)
        with pytest.raises(control.ControlRefused, match="Old worker needs an update to use Gemini key"):
            asyncio.run(control.submit_turn(b.device_id, "s-gem", source="queue", prompt="hi", owner_send=True))
        with pytest.raises(control.ControlRefused, match="needs an update"):
            asyncio.run(voice_remote.submit(b.device_id, {"id": "s-gem", "profile_id": "default"},
                                            "hello", instructions="be brief"))
        assert sent == []
        # This worker, and a session on a native binding, are never judged.
        db.create_session("s-native", "default", model="sonnet", derive_provider_from_profile=False)
        asyncio.run(control.submit_turn(b.device_id, "s-native", prompt="hi"))
        asyncio.run(control.submit_turn(a.device_id, "s-gem", prompt="hi"))
        assert len(sent) == 2


def test_any_gated_pick_refuses_without_predicting_the_binding(workers, monkeypatch):
    """Nothing predicts which row the older worker would bind: any session or
    turn value naming a gated row refuses, and only those do."""
    from app.core import credential_gate
    from app.network import control

    a, b = _network_with_old_worker(workers)
    _no_send(monkeypatch)
    refused = provider_capability.session_refusal
    with a.active():
        db.create_model_provider("or", "OpenRouter", "openrouter", "https://openrouter.ai/api")
        _gemini_session(owner=b.device_id)
        # The session's own row is gated: a turn picking another row or native
        # Claude is refused too (the older worker may still bind Gemini).
        for overrides in ({"provider": "or"}, {"model": "sonnet"}, {"model": "prov:or:openai/gpt-5"}):
            assert "needs an update" in refused(b.device_id, "s-gem", overrides)
        # A bare model id the gated row lists, with no provider.
        db.create_session("s-bare", "default", model="gemini-2.5-pro", derive_provider_from_profile=False)
        assert "needs an update" in refused(b.device_id, "s-bare")
        # Only the launched model names it.
        db.create_session("s-launch", "default", derive_provider_from_profile=False)
        with db.get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET launched_model='prov:gem:gemini-2.5-pro' WHERE id='s-launch'")
        assert "needs an update" in refused(b.device_id, "s-launch")
        # A native or ungated session, and a turn picking the gated row on it.
        db.create_session("s-or", "default", provider_id="or", model="openai/gpt-5",
                          derive_provider_from_profile=False)
        assert refused(b.device_id, "s-or") is None
        assert refused(b.device_id, "s-or", {"model": "sonnet"}) is None
        assert "needs an update" in refused(b.device_id, "s-or", {"model": "prov:gem:gemini-2.5-pro"})
        assert "needs an update" in refused(b.device_id, "s-or", {"provider": "gem"})
        with pytest.raises(control.ControlRefused, match="needs an update"):
            asyncio.run(control.submit_turn(b.device_id, "s-or", prompt="hi", overrides={"provider": "gem"}))
        # Bound to nothing: the default run model decides.
        db.create_session("s-none", "default", derive_provider_from_profile=False)
        monkeypatch.setattr(credential_gate, "default_run_model", lambda **kw: (None, "sonnet"))
        assert refused(b.device_id, "s-none") is None
        monkeypatch.setattr(credential_gate, "default_run_model", lambda **kw: ("gem", "gemini-2.5-pro"))
        assert "needs an update" in refused(b.device_id, "s-none")
        # This worker, and a worker that reports the capability, never.
        assert refused(a.device_id, "s-gem") is None
        _set_presence(b.device_id, {gemini_provider.CAPABILITY: 1})
        assert refused(b.device_id, "s-gem") is None
        assert refused(b.device_id, "s-none") is None


def test_a_refused_held_message_is_kept_and_reported(workers, monkeypatch):
    from app.core import session_handoff

    a, b = _network_with_old_worker(workers)
    sent = _no_send(monkeypatch)
    entry = {"prompt": "carry on", "source": "user", "overrides": None}
    told = []

    async def tell(session_id, target_id, content, meta):
        told.append((content, meta))
        return True

    held = [entry]
    monkeypatch.setattr(session_handoff, "_first_held", lambda session_id: held[0] if held else None)
    monkeypatch.setattr(session_handoff, "_delivered", lambda session_id, done: held.clear())
    monkeypatch.setattr(session_handoff, "_take_rest", lambda session_id: list(held))
    monkeypatch.setattr(session_handoff, "_resend_safe", lambda target_id: True)
    monkeypatch.setattr(session_handoff, "_tell", tell)
    with a.active():
        _gemini_session(owner=b.device_id)
        asyncio.run(session_handoff._deliver_active("s-gem", b.device_id, "Old worker"))
    assert sent == []
    content, meta = told[0]
    assert "Old worker needs an update to use Gemini key" in content and "carry on" in content
    assert meta["status"] == "held_failed" and meta["messages"] == ["carry on"]


def test_session_messages_to_an_incapable_owner_are_refused_with_the_reason(workers, monkeypatch):
    from app.core import session_orchestration as orch
    from app.core.task_registry import task_registry

    a, b = _network_with_old_worker(workers)
    sent = _no_send(monkeypatch)
    with a.active():
        _gemini_session(owner=b.device_id)
        plan = {"remote_message": {"device_id": b.device_id, "device_name": "Old worker"},
                "reserved": False, "receipt_id": None, "body": {"session_id": "s-gem"}}
        with pytest.raises(ValueError, match="Old worker refused the message: Old worker needs an update"):
            asyncio.run(orch._message_remote(plan, "sender", task_registry.stop_generation("sender")))
    assert sent == []


def _outbox(stop_reason):
    from app.core import session_orchestration as orch

    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT payload FROM turns WHERE session_id=%s AND stop_reason=%s",
                       (orch.REPORT_OUTBOX, stop_reason))
        return [row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
                for row in cursor.fetchall()]


def test_started_chat_reports_wait_for_the_worker_update_then_arrive(workers, monkeypatch, caplog):
    from app.core import session_orchestration as orch
    from app.network import control

    a, b = _network_with_old_worker(workers)
    sent = []

    async def call(device_id, action, body=None, **kwargs):
        sent.append((device_id, action, (body or {}).get("session_id") or (body or {}).get("home")))
        return {"delivered": True}

    monkeypatch.setattr(control, "call", call)
    frame = {"task_id": "child", "agent_message_turn_id": "t9", "status": "completed", "summary": "done"}
    caplog.set_level(logging.WARNING, logger=orch.log.name)
    with a.active():
        _gemini_session(owner=b.device_id)
        db.create_model_provider("or", "OpenRouter", "openrouter", "https://openrouter.ai/api")
        db.create_session("s-or", "default", provider_id="or", model="openai/gpt-5",
                          derive_provider_from_profile=False)
        with db.get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET owner_device_id=%s WHERE id='s-or'", (b.device_id,))
        # A report forwarded from the outbox, and the first report of a chat
        # the assistant started from that worker.
        orch._owe_forward("s-gem", b.device_id, frame, 0)
        orch._owe_forward("s-or", b.device_id, dict(frame, task_id="other"), 0)
        monkeypatch.setattr(orch, "build_started_report", lambda *args: (None, b.device_id, dict(frame, task_id="c2")))
        monkeypatch.setattr(orch, "_report_sender", lambda session_id: "s-gem")
        asyncio.run(orch.report_settled_start("c2", {"id": "t9"}, "completed"))
        for _ in range(3):
            asyncio.run(orch.deliver_forwarded_reports(force=True))
        # Kept pending with the reason; the report for a chat it can run went.
        kept = _outbox(orch._FORWARD_PENDING)
        assert sorted(r["report"]["task_id"] for r in kept) == ["c2", "child"]
        assert all("Old worker needs an update to use Gemini key" in r["deferred"] for r in kept)
        assert _outbox(orch._FORWARD_REFUSED) == []
        assert sent == [(b.device_id, orch.FORWARD_REPORT_ACTION, "s-or")]
        # One warning per report, not one per pass.
        assert caplog.text.count("kept until that worker is updated") == 2
        # The worker updates: the next pass sends both, each its own way.
        _set_presence(b.device_id, {gemini_provider.CAPABILITY: 1})
        provider_capability.recheck([b.device_id])
        assert asyncio.run(orch.deliver_forwarded_reports()) == 2
        assert _outbox(orch._FORWARD_PENDING) == []
    assert sorted(sent[1:]) == sorted([(b.device_id, orch.FORWARD_REPORT_ACTION, "s-gem"),
                                       (b.device_id, orch.REPORT_ACTION, "c2")])


def test_task_reports_wait_for_the_worker_update_then_arrive(workers, monkeypatch, caplog):
    from app.core import remote_task
    from app.db import remote_tasks
    from app.network import control

    a, b = _network_with_old_worker(workers)
    sent = []

    async def call(device_id, action, body=None, **kwargs):
        sent.append((device_id, action))
        return {"delivered": True}

    monkeypatch.setattr(control, "call", call)
    monkeypatch.setattr(remote_task, "remote_parent", lambda session_id: {
        "device_id": b.device_id, "owner_session_id": "main", "parent_session_id": "main"})
    caplog.set_level(logging.WARNING, logger=remote_task.logger.name)
    with a.active():
        _gemini_session("main", owner=b.device_id)
        with db.get_db() as conn:
            remote_tasks.record_child("child", b.device_id, conn=conn)
        assert remote_tasks.owe_report("child", "c1", {"worker_session_id": "child"})
        for _ in range(3):
            assert asyncio.run(remote_task.deliver_due()) == 0
        owed = remote_tasks.due_reports()
        assert [r["worker_session_id"] for r in owed] == ["child"]
        assert "Old worker needs an update to use Gemini key" in owed[0]["last_error"]
        assert int(owed[0]["attempts"] or 0) == 0
        assert caplog.text.count("kept until that worker is updated") == 1
        assert sent == []
        _set_presence(b.device_id, {gemini_provider.CAPABILITY: 1})
        assert asyncio.run(remote_task.deliver_due()) == 1
        assert remote_tasks.due_reports() == []
    assert sent == [(b.device_id, remote_task.TASK_REPORT_ACTION)]


def test_task_messages_to_an_incapable_worker_are_refused_with_the_reason(workers, monkeypatch):
    from app.core import remote_task

    a, b = _network_with_old_worker(workers)
    sent = _no_send(monkeypatch)
    with a.active():
        _gemini_session("main", owner=b.device_id)
        monkeypatch.setattr(remote_task, "remote_parent", lambda session_id: {
            "device_id": b.device_id, "owner_session_id": "main", "parent_session_id": "main",
            "worker_name": "Old worker"})
        answer = asyncio.run(remote_task.send_to_parent("child", {}, "status?"))
    assert sent == []
    assert answer["is_error"] and "Old worker needs an update to use Gemini key" in answer["text"]
    assert "not delivered" in answer["text"]


def test_automation_steps_decide_the_owner_check(workers):
    from app.core import automation_service
    from app.network import ownership

    a, b = _network_with_old_worker(workers)
    step = {"type": "send_prompt", "prompt": "summarise", "model": "prov:gem:gemini-2.5-pro"}
    with a.active():
        db.create_automation(automation_id="auto2", name="Steps", trigger_type="schedule",
                             trigger_config={"cron": "0 0 * * *"}, prompt="", profile_id="default",
                             action_steps=[step])
        with pytest.raises(ValueError, match="Old worker needs an update to use Gemini key"):
            ownership.assign("automations", "auto2", b.device_id)
        # Owned by the old worker on native Claude, then edited to pick Gemini.
        db.create_automation(automation_id="auto3", name="Plain", trigger_type="schedule",
                             trigger_config={"cron": "0 0 * * *"}, prompt="do", profile_id="default")
        assert ownership.assign("automations", "auto3", b.device_id)["owner_device_id"] == b.device_id
        existing = db.get_automation("auto3")
        assert "needs an update" in automation_service.owner_refusal(existing, {"action_steps": [step]})
        assert "needs an update" in automation_service.owner_refusal(existing, {"model": "prov:gem:gemini-2.5-pro"})
        assert automation_service.owner_refusal(existing, {"name": "Renamed"}) is None
        assert automation_service.owner_refusal(existing, {"action_steps": [dict(step, model="sonnet")]}) is None
        with pytest.raises(ValueError, match="needs an update"):
            asyncio.run(automation_service.update_automation("auto3", action_steps=[step]))


def _full_form(automation, **changes):
    """What the edit form (AutomationsCard) sends on every save: every field."""
    form = {"name": automation["name"], "description": None,
            "triggers": [{"type": "cron", "config": {"cron": "0 0 * * *", "timezone": "UTC"}}],
            "trigger_type": "cron", "trigger_config": {"cron": "0 0 * * *", "timezone": "UTC"},
            "prompt": automation.get("prompt") or None, "profile_id": automation["profile_id"],
            "project_id": None, "output_config": None, "action_steps": automation.get("action_steps") or None,
            "max_turns": None, "timeout_seconds": None, "max_cost_per_run": None, "max_cost_per_day": None,
            "max_cost_total": None, "max_runs_per_day": None, "requires_approval": False,
            "model": automation.get("model") or None}
    form.update(changes)
    return form


def test_a_full_form_edit_rechecks_only_what_it_adds(workers):
    """The edit form resends every field: renaming a Gemini automation whose
    owner lost the capability is not refused; adding Gemini to one it owns is."""
    from fastapi import HTTPException

    from app.api import automations as api
    from app.core.models import AutomationUpdate

    a, b = _network_with_old_worker(workers)
    with a.active():
        db.create_automation(automation_id="auto4", name="Nightly", trigger_type="schedule",
                             trigger_config={"cron": "0 0 * * *"}, prompt="do", profile_id="default",
                             model="prov:gem:gemini-2.5-pro")
        db.create_automation(automation_id="auto5", name="Plain", trigger_type="schedule",
                             trigger_config={"cron": "0 0 * * *"}, prompt="do", profile_id="default")
        with db.get_db() as conn:
            conn.cursor().execute("UPDATE automations SET owner_device_id=%s WHERE id IN ('auto4','auto5')",
                                  (b.device_id,))

        def patch(automation_id, **changes):
            form = _full_form(db.get_automation(automation_id), **changes)
            return asyncio.run(api.update_automation_endpoint(automation_id, AutomationUpdate(**form), token=None))

        assert patch("auto4", name="Nightly (renamed)")["name"] == "Nightly (renamed)"
        assert patch("auto4", prompt="do it better")["prompt"] == "do it better"
        assert patch("auto5", name="Plain (renamed)")["name"] == "Plain (renamed)"
        with pytest.raises(HTTPException) as refused:
            patch("auto5", model="prov:gem:gemini-2.5-pro")
        assert refused.value.status_code == 400
        assert "Old worker needs an update to use Gemini key" in refused.value.detail
        assert db.get_automation("auto5")["model"] in (None, "")


def test_a_legacy_instance_id_names_the_same_worker(workers, monkeypatch):
    from app.core.config import settings

    a, b = _network_with_old_worker(workers)
    with a.active():
        monkeypatch.setattr(engine, "instance_aliases", lambda: {"inst-b": b.device_id})
        assert provider_capability.refusal("inst-b", "gem") == "Old worker needs an update to use Gemini key."
        _set_presence(b.device_id, {gemini_provider.CAPABILITY: 1})
        assert provider_capability.refusal("inst-b", "gem") is None
        # This worker's own legacy id is this worker.
        assert provider_capability.refusal(settings.effective_instance_id, "gem") is None


def test_a_revoked_member_loses_its_recorded_gap(workers, monkeypatch):
    from app.core.global_events import global_events

    a, b = _network_with_old_worker(workers)
    sent = []
    monkeypatch.setattr(global_events, "resources_changed",
                        lambda *resources, **kw: sent.append(resources))
    with a.active():
        assert provider_capability.recheck() == {b.device_id: ["gem"]}
        sent.clear()
        # The revocation's own full re-check drops the gap and refreshes pages.
        engine.revoke_device(b.device_id)
        assert provider_capability._last_gaps == {}
        assert sent == [("settings",)]
        assert provider_capability.recheck() == {}


# ---------------------------------------------------------------------------
# Review round 3: a bound row outranks a shared bare model id; deferred
# reports leave the hot retry set on deletion or revocation; a deferred first
# report is never sent twice
# ---------------------------------------------------------------------------

def test_a_model_id_a_gated_row_also_lists_does_not_move_bound_work(workers):
    """``gemini-2.5-pro`` is listed by the gated Gemini row and is also a model
    of a bound OpenRouter row: every build keeps the bound row."""
    a, b = _network_with_old_worker(workers)
    refused = provider_capability.session_refusal
    with a.active():
        db.create_model_provider("or", "OpenRouter", "openrouter", "https://openrouter.ai/api")
        db.create_session("s-or", "default", provider_id="or", model="gemini-2.5-pro",
                          derive_provider_from_profile=False)
        with db.get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET launched_model='gemini-2.5-pro' WHERE id='s-or'")
        assert refused(b.device_id, "s-or") is None
        assert refused(b.device_id, "s-or", {"model": "gemini-2.5-pro"}) is None
        # The turn's provider binds it too.
        db.create_session("s-none", "default", derive_provider_from_profile=False)
        assert refused(b.device_id, "s-none", {"provider": "or", "model": "gemini-2.5-pro"}) is None
        # Still refused: the bare id with nothing bound (or a value that names
        # no row), and a gated row named outright next to a bound one.
        assert "needs an update" in refused(b.device_id, "s-none", {"model": "gemini-2.5-pro"})
        assert "needs an update" in refused(b.device_id, "s-none",
                                            {"provider": "nope", "model": "gemini-2.5-pro"})
        assert "needs an update" in refused(b.device_id, "s-or", {"model": "prov:gem:gemini-2.5-pro"})
        with db.get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET launched_model='gemini-2.5-pro' WHERE id='s-none'")
        assert "needs an update" in refused(b.device_id, "s-none")
        # The start and dispatch picks follow the same rule.
        assert provider_capability.picks_refusal(b.device_id, ["or"], ["gemini-2.5-pro"]) is None
        assert "needs an update" in provider_capability.picks_refusal(b.device_id, [], ["gemini-2.5-pro"])


def _deferred_first_report(orch, monkeypatch, b, frame):
    monkeypatch.setattr(orch, "build_started_report", lambda *args: (None, b.device_id, frame))
    monkeypatch.setattr(orch, "_report_sender", lambda session_id: "s-gem")
    asyncio.run(orch.report_settled_start(frame["task_id"], {"id": "t9"}, "completed"))
    assert [r["report_of"] for r in _outbox(orch._FORWARD_PENDING)] == [frame["task_id"]]


def test_a_deferred_report_for_a_deleted_assistant_chat_stays_on_the_started_chat(workers, monkeypatch):
    from app.core import session_orchestration as orch

    from app.core import credential_gate

    a, b = _network_with_old_worker(workers)
    sent = _no_send(monkeypatch)
    monkeypatch.setattr(credential_gate, "default_run_model", lambda **kw: ("gem", "gemini-2.5-pro"))
    frame = {"task_id": "c2", "agent_message_turn_id": "t9", "status": "completed", "summary": "done"}
    with a.active():
        _gemini_session(owner=b.device_id)
        db.create_session("c2", "default", derive_provider_from_profile=False)
        _deferred_first_report(orch, monkeypatch, b, frame)
        assert db.delete_session("s-gem")
        with db.get_db() as conn:
            from app.db import sessions
            sessions.record_deleted_chat(conn.cursor(), "s-gem")
        asyncio.run(orch.deliver_forwarded_reports(force=True))
        assert _outbox(orch._FORWARD_PENDING) == [] and _outbox(orch._FORWARD_REFUSED) == []
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT session_id FROM session_events WHERE event_type='orchestration.report_deferred'")
            assert [row["session_id"] for row in cursor.fetchall()] == ["c2"]
    assert sent == []


def test_reports_for_a_removed_worker_leave_the_capability_wait(workers, monkeypatch):
    """A removed worker is not judged on capabilities: its reports take the
    path any report to it takes (unreachable, retried with backoff), and are
    delivered if it is admitted again."""
    from app.core import remote_task
    from app.core import session_orchestration as orch
    from app.db import remote_tasks
    from app.network import control

    a, b = _network_with_old_worker(workers)
    frame = {"task_id": "c2", "agent_message_turn_id": "t9", "status": "completed", "summary": "done"}
    tried = []

    async def call(device_id, action, body=None, **kwargs):
        tried.append(action)
        raise control.RunnerUnreachable("The owning worker was removed from this network")

    with a.active():
        _gemini_session(owner=b.device_id)
        _gemini_session("main", owner=b.device_id)
        _deferred_first_report(orch, monkeypatch, b, frame)
        with db.get_db() as conn:
            remote_tasks.record_child("child", b.device_id, conn=conn)
        assert remote_tasks.owe_report("child", "c1", {"worker_session_id": "child"})
        monkeypatch.setattr(remote_task, "remote_parent", lambda session_id: {
            "device_id": b.device_id, "owner_session_id": "main", "parent_session_id": "main"})
        engine.revoke_device(b.device_id)
        assert provider_capability.session_refusal(b.device_id, "s-gem") is None
        monkeypatch.setattr(control, "call", call)
        asyncio.run(orch.deliver_forwarded_reports(force=True))
        asyncio.run(remote_task.deliver_due())
        assert tried == [orch.REPORT_ACTION, remote_task.TASK_REPORT_ACTION]
        kept = _outbox(orch._FORWARD_PENDING)
        assert kept[0]["attempts"] == 1 and "deferred" not in kept[0]
        assert kept[0]["next_attempt_at"] > 0
        with db.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT attempts, state FROM remote_task_children WHERE worker_session_id='child'")
            owed = cursor.fetchone()
        assert owed["state"] == remote_tasks.OWED and int(owed["attempts"] or 0) == 1


def test_a_task_report_for_a_deleted_caller_goes_to_its_worker(workers, monkeypatch):
    """The parent stores a report for a deleted caller without waking anyone
    (``handle_task_report``): nothing waits for a capability, even when the
    default run model (what a missing chat would fall back to) is gated."""
    from app.core import credential_gate, remote_task
    from app.db import remote_tasks
    from app.network import control

    a, b = _network_with_old_worker(workers)
    sent = []

    async def call(device_id, action, body=None, **kwargs):
        sent.append(action)
        return {"delivered": True}

    monkeypatch.setattr(control, "call", call)
    monkeypatch.setattr(credential_gate, "default_run_model", lambda **kw: ("gem", "gemini-2.5-pro"))
    monkeypatch.setattr(remote_task, "remote_parent", lambda session_id: {
        "device_id": b.device_id, "owner_session_id": "main", "parent_session_id": "main"})
    with a.active():
        _gemini_session("main", owner=b.device_id)
        with db.get_db() as conn:
            remote_tasks.record_child("child", b.device_id, conn=conn)
        assert remote_tasks.owe_report("child", "c1", {"worker_session_id": "child"})
        assert asyncio.run(remote_task.deliver_due()) == 0
        assert db.delete_session("main")
        with db.get_db() as conn:
            from app.db import sessions
            sessions.record_deleted_chat(conn.cursor(), "main")
        assert asyncio.run(remote_task.deliver_due()) == 1
        assert remote_tasks.due_reports() == []
    assert sent == [remote_task.TASK_REPORT_ACTION]


def test_a_deferred_first_report_that_may_have_arrived_is_not_sent_again(workers, monkeypatch):
    """A first report may steer into a live turn there, which keeps no
    receipt: after a lost reply it is not resent. One that never left (no
    route) is, and so is a forwarded report (its receiver keeps a receipt)."""
    from app.core import session_orchestration as orch
    from app.network import control

    a, b = _network_with_old_worker(workers)
    frame = {"task_id": "c2", "agent_message_turn_id": "t9", "status": "completed", "summary": "done"}
    sent = []
    failure = [control.RunnerUnreachable("no route")]

    async def call(device_id, action, body=None, **kwargs):
        sent.append(action)
        raise failure[0]

    with a.active():
        _gemini_session(owner=b.device_id)
        _deferred_first_report(orch, monkeypatch, b, frame)
        orch._owe_forward("s-gem", b.device_id, dict(frame, task_id="child"), 0)
        _set_presence(b.device_id, {gemini_provider.CAPABILITY: 1})
        monkeypatch.setattr(control, "call", call)
        asyncio.run(orch.deliver_forwarded_reports(force=True))
        assert len(_outbox(orch._FORWARD_PENDING)) == 2
        failure[0] = control.MaybeApplied("did not answer in time")
        asyncio.run(orch.deliver_forwarded_reports(force=True))
        kept = _outbox(orch._FORWARD_PENDING)
        assert [r["report"]["task_id"] for r in kept] == ["child"]
        assert kept[0]["attempts"] == 2
        asyncio.run(orch.deliver_forwarded_reports(force=True))
    assert sent.count(orch.REPORT_ACTION) == 2
    assert sent.count(orch.FORWARD_REPORT_ACTION) == 3


def test_a_deferred_first_report_whose_connection_hangs_stays_owed(workers, monkeypatch):
    """Connecting to a blackholed address outlasts the report's own timeout
    (two connect attempts and the pause between them) before anything is
    sent: the first report stays owed and goes once the address answers."""
    import httpx

    from app.core import session_orchestration as orch
    from app.network import control, transport

    a, b = _network_with_old_worker(workers)
    frame = {"task_id": "c2", "agent_message_turn_id": "t9", "status": "completed", "summary": "done"}
    monkeypatch.setattr(orch, "REMOTE_TIMEOUT", 0.2)
    monkeypatch.setattr(control, "CONNECT_TIMEOUT", 0.15)
    monkeypatch.setattr(control, "CONNECT_RETRY_DELAY", 0.05)
    monkeypatch.setitem(transport._links, b.device_id, transport.PeerLink(b.device_id, ["http://192.0.2.1:9"]))
    blackholed, sent = [True], []

    async def post(link, client, ctx, route, action, payload, timeout):
        if blackholed[0]:
            await asyncio.sleep(control.CONNECT_TIMEOUT)
            raise control._RouteFailed(httpx.ConnectTimeout("timed out"))
        sent.append(action)
        return {"delivered": True}

    with a.active():
        _gemini_session(owner=b.device_id)
        _deferred_first_report(orch, monkeypatch, b, frame)
        _set_presence(b.device_id, {gemini_provider.CAPABILITY: 1})
        monkeypatch.setattr(control, "_post", post)
        asyncio.run(orch.deliver_forwarded_reports(force=True))
        kept = _outbox(orch._FORWARD_PENDING)
        assert [r["report_of"] for r in kept] == ["c2"] and kept[0]["attempts"] == 1
        blackholed[0] = False
        asyncio.run(orch.deliver_forwarded_reports(force=True))
        assert _outbox(orch._FORWARD_PENDING) == []
    assert sent == [orch.REPORT_ACTION]
