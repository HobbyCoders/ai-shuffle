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
        # A native Claude pick that the row does not curate runs natively:
        # the same binding rule as a turn, so no false refusal.
        db.create_session("s2", "default", provider_id="gem", model="sonnet",
                          derive_provider_from_profile=False)
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
        # A turn that rebinds to native Claude, or to an ungated row, may go.
        db.create_model_provider("or", "OpenRouter", "openrouter", "https://openrouter.ai/api")
        asyncio.run(control.submit_turn(b.device_id, "s-gem", prompt="hi", overrides={"model": "sonnet"}))
        asyncio.run(control.submit_turn(b.device_id, "s-gem", prompt="hi",
                                        overrides={"model": "prov:or:openai/gpt-5"}))
        # This worker, and a session on a native binding, are never judged.
        db.create_session("s-native", "default", derive_provider_from_profile=False)
        asyncio.run(control.submit_turn(b.device_id, "s-native", prompt="hi"))
        assert len(sent) == 2 + 1


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


def test_started_chat_reports_to_an_incapable_worker_are_kept_refused(workers, monkeypatch):
    from app.core import session_orchestration as orch

    a, b = _network_with_old_worker(workers)
    sent = _no_send(monkeypatch)
    frame = {"task_id": "child", "agent_message_turn_id": "t9", "status": "completed", "summary": "done"}
    with a.active():
        _gemini_session(owner=b.device_id)
        # Forwarded from the outbox: refused for good, with the reason.
        orch._owe_forward("s-gem", b.device_id, frame, 0)
        asyncio.run(orch.deliver_forwarded_reports(force=True))
        # The first report of a chat the assistant started from that worker.
        monkeypatch.setattr(orch, "build_started_report", lambda *args: (None, b.device_id, dict(frame, task_id="c2")))
        monkeypatch.setattr(orch, "_report_sender", lambda session_id: "s-gem")
        asyncio.run(orch.report_settled_start("c2", {"id": "t9"}, "completed"))
        refused = _outbox(orch._FORWARD_REFUSED)
    assert sent == []
    assert sorted(r["report"]["task_id"] for r in refused) == ["c2", "child"]
    assert all("Old worker needs an update to use Gemini key" in r["error"] for r in refused)


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
