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
    db.create_model_provider(row_id, name, gemini_provider.PROVIDER, gemini_provider.API_ROOT)


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
    assert provider_capability.needs_update_message(["GamingPC"]) == "GamingPC needs an update to use this."
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
        monkeypatch.setattr(db, "get_session", lambda session_id: {"id": session_id, "provider_id": "gem"})
        with pytest.raises(ValueError, match="Old worker needs an update to use Gemini key"):
            session_handoff._refuse_incapable("s1", b.device_id)
        session_handoff._refuse_incapable("s1", a.device_id)  # this worker can

        db.create_automation(automation_id="auto1", name="Nightly", trigger_type="schedule",
                             trigger_config={"cron": "0 0 * * *"}, prompt="do", profile_id="default",
                             provider_id="gem")
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
