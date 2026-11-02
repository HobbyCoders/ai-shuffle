"""SuggestTask from a subagent on another worker, over the real signed control endpoint."""
import asyncio

import pytest

from app.core import credential_gate, task_chips
from app.core import session_orchestration as orch
from app.db import database, remote_tasks
from app.db.base import get_db
from app.db import task_chips as store
from app.network import control, engine
from tests.core.network.harness import converge
from tests.core.network.test_remote_task_control import WINDOWS_URL, dispatch, kicks, network  # noqa: F401

BRIEF = {"title": "Fix the flaky test", "summary": "It hides real failures.", "prompt": "Fix tests/x.py.",
         "model": "sonnet", "request_id": "r1"}


@pytest.fixture
def linked(workers, monkeypatch, kicks):  # noqa: F811
    """``a`` runs a Task child of ``b``'s chat, ``caller``; the chat lives only on ``b``."""
    monkeypatch.setattr(credential_gate, "turn_refusal", lambda leg: None)
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    a, b, router = network(workers, monkeypatch)
    with b.active():
        result = dispatch(a, b)
    assert result["ok"], result
    return a, b, router, result["worker_session_id"]


def relay_from(worker, sender, **changes):
    with worker.active():
        return asyncio.run(task_chips.suggest(sender, {**BRIEF, **changes}))


def chips_on(worker, session_id="caller"):
    with worker.active():
        return store.list_for_session(session_id)


def test_a_remote_subagents_chip_lands_on_the_root_chat_on_its_worker(linked):
    a, b, router, child = linked
    with a.active():
        assert task_chips.offered_to(child) is True
    answer = relay_from(a, child)
    assert answer["created"] is True and answer["state"] == "pending" and "separate chat" in answer["note"]
    assert ("POST", "/api/v1/network/peer/control/task-chip-suggest") in router.requests(WINDOWS_URL)
    [chip] = chips_on(b)
    assert chip["id"] == answer["chip_id"] and chip["origin_session_id"] == child
    assert chip["session_id"] == "caller" and chip["title"] == BRIEF["title"] and chip["model"] == "sonnet"
    assert chip["base"] == "default" and chip["origin_label"]
    with b.active():
        assert task_chips.list_chips("caller")["chips"][0]["origin_label"] == chip["origin_label"]


def test_the_subagents_worker_writes_no_chip_of_its_own(linked):
    a, b, _, child = linked
    relay_from(a, child)
    assert chips_on(a) == []
    with a.active():
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM session_task_chips")
            assert cursor.fetchone()["n"] == 0


def test_a_subagent_under_the_remote_child_reaches_the_root_chat_too(linked):
    a, b, _, child = linked
    with a.active():
        database.create_session(session_id="nested", profile_id="default", project_id=None, title="Nested helper",
                                is_active=True, parent_session_id=child, is_worker=True)
        assert task_chips.offered_to("nested") is True
    answer = relay_from(a, "nested")
    [chip] = chips_on(b)
    assert chip["id"] == answer["chip_id"] and chip["origin_session_id"] == "nested"
    assert chip["origin_label"] == "Nested helper"


def test_a_resent_suggestion_makes_one_chip(linked):
    a, b, _, child = linked
    first = relay_from(a, child)
    again = relay_from(a, child)
    assert again["chip_id"] == first["chip_id"] and again["created"] is False
    assert len(chips_on(b)) == 1


def test_the_chip_says_the_subagent_had_less_access_than_the_chat(linked):
    a, b, _, child = linked
    with a.active():
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET permission_ceiling='default', permission_mode='default' "
                                  "WHERE id=%s", (child,))
    relay_from(a, child)
    with b.active():
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET permission_mode='bypassPermissions' WHERE id='caller'")
        access = task_chips.list_chips("caller")["chips"][0]["access"]
    assert access["mode"] == "bypassPermissions" and access["wider_than_source"] is True
    assert "asks before using tools" in access["source_note"]


def test_a_subagent_this_worker_did_not_send_there_is_refused(linked):
    """Being a signed-in worker is not enough: the dispatch names the one worker it went to."""
    a, b, _, child = linked
    body = {"version": 1, "worker_session_id": "not-a-dispatch", "sender_session_id": child, **BRIEF}
    with a.active():
        with pytest.raises(control.ControlRefused, match="No subagent with this id"):
            asyncio.run(control.call(b.device_id, "task-chip-suggest", body))
    assert chips_on(b) == []
    with b.active():
        with get_db() as conn:
            conn.cursor().execute("UPDATE remote_tasks SET target_device_id='someone-else' WHERE worker_session_id=%s",
                                  (child,))
    with a.active():
        with pytest.raises(control.ControlRefused, match="No subagent with this id"):
            asyncio.run(control.call(b.device_id, "task-chip-suggest", {**body, "worker_session_id": child}))
    assert chips_on(b) == []


def test_a_stopped_subagent_cannot_suggest(linked):
    a, b, _, child = linked
    with b.active():
        remote_tasks.request_stop(child, reason="stopped")
    with pytest.raises(ValueError, match="stopped this subagent"):
        relay_from(a, child)
    assert chips_on(b) == []


def test_only_a_fresh_copy_of_the_default_branch_can_cross_workers(linked):
    a, b, _, child = linked
    with pytest.raises(ValueError, match='Use base="default"'):
        relay_from(a, child, base="current")
    assert chips_on(b) == []


def test_the_chats_worker_decides_the_model_and_says_what_it_can_run(linked):
    a, b, _, child = linked
    with pytest.raises(ValueError, match="Nothing was recorded"):
        relay_from(a, child, model="not-a-model")
    assert chips_on(b) == []


def test_claude_off_on_the_chats_worker_refuses_the_relay(linked, monkeypatch):
    a, b, _, child = linked
    monkeypatch.setattr(credential_gate, "turn_refusal", lambda leg: "Claude is turned off.")
    with pytest.raises(ValueError, match="Claude is turned off"):
        relay_from(a, child)
    assert chips_on(b) == []


def test_a_project_the_chats_worker_does_not_have_is_refused(linked):
    a, b, _, child = linked
    body = {"version": 1, "worker_session_id": child, "sender_session_id": child, **BRIEF, "project_v2_id": "nowhere"}
    with a.active():
        with pytest.raises(control.ControlRefused, match="not on the worker running its chat"):
            asyncio.run(control.call(b.device_id, "task-chip-suggest", body))
    assert chips_on(b) == []


def test_an_unreachable_chat_worker_says_nothing_was_recorded(linked):
    a, b, router, child = linked
    router.down.add(WINDOWS_URL)
    with pytest.raises(ValueError, match="Could not reach .*Nothing was recorded"):
        relay_from(a, child)


def test_an_older_chat_worker_is_named_as_needing_an_update(linked, monkeypatch):
    a, b, _, child = linked
    monkeypatch.delitem(control._LAZY_HANDLERS, "task-chip-suggest")
    with pytest.raises(ValueError, match="needs an update"):
        relay_from(a, child)


def test_unknown_fields_are_refused(linked):
    a, b, _, child = linked
    body = {"version": 1, "worker_session_id": child, "sender_session_id": child, **BRIEF, "session_id": "other"}
    with a.active():
        with pytest.raises(control.ControlRefused, match="Unknown suggestion fields: session_id"):
            asyncio.run(control.call(b.device_id, "task-chip-suggest", body))


def test_the_chip_reaches_the_subagents_worker_as_a_mirror_row(linked):
    a, b, _, child = linked
    answer = relay_from(a, child)
    with b.active():
        engine.scan_all()
    converge([a, b])
    assert [chip["id"] for chip in chips_on(a)] == [answer["chip_id"]]
