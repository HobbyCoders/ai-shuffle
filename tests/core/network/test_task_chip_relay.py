"""SuggestTask from a subagent on another worker, over the real signed control endpoint."""
import asyncio
import json

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
    # The dispatch is the one id this worker can check; the name is the sender's.
    assert chip["id"] == answer["chip_id"] and chip["origin_session_id"] == child
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


def body_from(child, **changes):
    return {"version": 1, "worker_session_id": child, "sender_session_id": child, **BRIEF, **changes}


def signed(worker, target, action, body):
    with worker.active():
        return asyncio.run(control.call(target.device_id, action, body))


def test_two_subagents_on_a_worker_keep_their_own_request_ids(linked):
    a, b, _, child = linked
    with a.active():
        database.create_session(session_id="nested", profile_id="default", project_id=None, title="Nested helper",
                                is_active=True, parent_session_id=child, is_worker=True)
    first = relay_from(a, child)
    second = relay_from(a, "nested", title="Another idea")
    assert second["chip_id"] != first["chip_id"]
    assert relay_from(a, "nested", title="Another idea")["chip_id"] == second["chip_id"]


def test_a_made_up_sender_is_not_recorded_as_the_origin(linked):
    a, b, _, child = linked
    signed(a, b, "task-chip-suggest", body_from(child, sender_session_id="forged-origin"))
    [chip] = chips_on(b)
    assert chip["origin_session_id"] == child


def test_a_finished_task_cannot_add_a_suggestion(linked):
    a, b, _, child = linked
    with b.active():
        assert remote_tasks.mark_delivered(child, completion_id="done", status="completed", report_path=None)
    with pytest.raises(control.ControlRefused, match="already ended.*Mention the issue in your report"):
        signed(a, b, "task-chip-suggest", body_from(child))
    assert chips_on(b) == []


def test_an_agent_on_its_last_turn_is_told_to_put_it_in_its_report(linked):
    """A late suggestion is not an attack: the agent reads why, and what to do instead."""
    a, b, _, child = linked
    with b.active():
        assert remote_tasks.mark_delivered(child, completion_id="done", status="completed", report_path=None)
    with pytest.raises(ValueError, match="already ended.*Mention the issue in your report"):
        relay_from(a, child)
    assert chips_on(b) == []


def test_a_finished_task_may_still_read_back_what_it_recorded(linked):
    """A lost answer: the resend must not read as a refusal."""
    a, b, _, child = linked
    first = relay_from(a, child)
    with b.active():
        assert remote_tasks.mark_delivered(child, completion_id="done", status="completed", report_path=None)
    again = relay_from(a, child)
    assert again["chip_id"] == first["chip_id"] and again["created"] is False
    with pytest.raises(ValueError, match="already ended"):
        relay_from(a, child, prompt="Do something else instead.")
    with pytest.raises(ValueError, match="already ended"):
        relay_from(a, child, model="opus")
    assert len(chips_on(b)) == 1


def test_a_stopped_task_may_still_read_back_what_it_recorded(linked):
    a, b, _, child = linked
    first = relay_from(a, child)
    with b.active():
        remote_tasks.request_stop(child, reason="stopped")
    assert relay_from(a, child)["chip_id"] == first["chip_id"]
    with pytest.raises(ValueError, match="stopped this subagent"):
        relay_from(a, child, request_id="r2", title="New")
    assert len(chips_on(b)) == 1


def test_a_resumed_task_can_suggest_again(linked):
    a, b, _, child = linked
    with b.active():
        assert remote_tasks.mark_delivered(child, completion_id="done", status="completed", report_path=None)
        assert remote_tasks.reopen_dispatch(child, "done")
    assert relay_from(a, child)["created"] is True


def test_a_report_that_lands_while_the_chip_is_being_checked_wins(linked, monkeypatch):
    """The dispatch is read again under its lock, in the transaction that writes the chip."""
    a, b, _, child = linked
    real = task_chips._chosen_model

    def report_lands(*args):
        result = real(*args)
        assert remote_tasks.mark_delivered(child, completion_id="done", status="completed", report_path=None)
        return result

    monkeypatch.setattr(task_chips, "_chosen_model", report_lands)
    with pytest.raises(ValueError, match="already ended"):
        relay_from(a, child)
    assert chips_on(b) == []


def test_the_worker_running_the_subagent_says_what_it_could_do(linked):
    """Worked out over there, where its agent's lists live, deny lists included."""
    a, b, _, child = linked
    with a.active():
        database.create_profile("restricted", "Restricted", None, {
            "model": "sonnet", "tool_selection_mode": "disallow", "disallowed_tools": ["Bash", "Write", "Edit"]})
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET profile_id='restricted' WHERE id=%s", (child,))
    relay_from(a, child)
    [chip] = chips_on(b)
    assert json.loads(chip["origin_tools"])["disallowed"] == ["Bash", "Write", "Edit"]
    with b.active():
        access = task_chips.list_chips("caller")["chips"][0]["access"]
    assert access["wider_than_source"] is True and "only some tools" in access["source_note"]


def test_unreadable_tools_from_a_worker_read_as_a_subagent_with_none(linked):
    a, b, _, child = linked
    signed(a, b, "task-chip-suggest", body_from(child, origin_tools="not json"))
    with b.active():
        access = task_chips.list_chips("caller")["chips"][0]["access"]
    assert access["wider_than_source"] is True and "only some tools" in access["source_note"]


def test_a_signed_worker_cannot_start_a_chip_on_a_forged_access(linked):
    a, b, _, child = linked
    chip_id = relay_from(a, child)["chip_id"]
    with b.active():
        shown = task_chips.list_chips("caller")["chips"][0]["access"]
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET permission_mode='bypassPermissions' WHERE id='caller'")
    for body in ({"shown_access": "bypassPermissions"}, {"access_fingerprint": "bypassPermissions"},
                 {"access_fingerprint": shown["fingerprint"]}, {}):
        started = signed(a, b, "task-chip", {"session_id": "caller", "chip_id": chip_id, "action": "start", **body})
        assert started["state"] == "failed" and "access changed" in started["error"]
        assert not started["started_session_id"]


@pytest.fixture
def two_hops(linked):
    """The chat on ``b``, its Task child on ``a``, and that child's own Task child back on ``b``.

    Reached through ``dispatch_remote`` itself: the Task tool keeps a remote
    child's subagents on its own worker today.
    """
    a, b, router, child = linked
    with a.active():
        first = database.get_session(child)
        result = dispatch(b, a, caller_row=first, caller_session_id=child, root_session_id=child, depth=2)
    assert result["ok"], result
    return a, b, router, child, result["worker_session_id"]


def test_a_subagent_two_hops_away_reaches_the_top_level_chat(two_hops):
    a, b, _, child, nested = two_hops
    answer = relay_from(b, nested)
    assert answer["created"] is True and answer["state"] == "pending"
    [chip] = chips_on(b)
    assert chip["id"] == answer["chip_id"] and chip["session_id"] == "caller"
    assert chip["origin_session_id"] == child and chip["title"] == BRIEF["title"]
    with a.active():
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM session_task_chips")
            assert cursor.fetchone()["n"] == 0
    assert relay_from(b, nested)["chip_id"] == answer["chip_id"]
    assert len(chips_on(b)) == 1


def test_each_hop_checks_the_worker_that_asked(two_hops):
    a, b, _, child, nested = two_hops
    # ``b`` holds this dispatch's child but never sent it to ``a``'s records as a sender of its own.
    with a.active():
        with get_db() as conn:
            conn.cursor().execute("UPDATE remote_tasks SET target_device_id='someone-else' WHERE worker_session_id=%s",
                                  (nested,))
    with pytest.raises(ValueError, match="No subagent with this id"):
        relay_from(b, nested)
    with a.active():
        with get_db() as conn:
            conn.cursor().execute("UPDATE remote_tasks SET target_device_id=%s WHERE worker_session_id=%s",
                                  (b.device_id, nested))
    with b.active():
        with get_db() as conn:
            conn.cursor().execute("UPDATE remote_tasks SET target_device_id='someone-else' WHERE worker_session_id=%s",
                                  (child,))
    with pytest.raises(ValueError, match="No subagent with this id"):
        relay_from(b, nested)
    assert chips_on(b) == []


def test_a_finished_hop_in_the_middle_stops_new_suggestions(two_hops):
    a, b, _, child, nested = two_hops
    first = relay_from(b, nested)
    with a.active():
        assert remote_tasks.mark_delivered(nested, completion_id="done", status="completed", report_path=None)
    assert relay_from(b, nested)["chip_id"] == first["chip_id"]
    with pytest.raises(ValueError, match="already ended"):
        relay_from(b, nested, request_id="r2", title="New")
    assert len(chips_on(b)) == 1


def test_a_finished_task_at_the_end_of_the_chain_stops_new_suggestions(two_hops):
    a, b, _, child, nested = two_hops
    with b.active():
        assert remote_tasks.mark_delivered(child, completion_id="done", status="completed", report_path=None)
    with pytest.raises(ValueError, match="already ended"):
        relay_from(b, nested)
    assert chips_on(b) == []


def test_a_suggestion_cannot_pass_through_too_many_workers(two_hops):
    a, b, _, child, nested = two_hops
    body = body_from(nested, hops=task_chips._MAX_RELAY_HOPS)
    with pytest.raises(control.ControlRefused, match="too many workers.*Nothing was recorded"):
        signed(b, a, "task-chip-suggest", body)
    for hops in (task_chips._MAX_RELAY_HOPS + 1, -1, "1", True):
        with pytest.raises(control.ControlRefused, match="too many workers"):
            signed(b, a, "task-chip-suggest", body_from(nested, hops=hops))
    assert chips_on(b) == []
    # The last permitted hop still records: only forwarding past the limit is refused.
    answer = signed(a, b, "task-chip-suggest", body_from(child, hops=task_chips._MAX_RELAY_HOPS))
    assert answer["created"] is True


def test_the_worker_in_the_middle_unreachable_says_nothing_was_recorded(two_hops):
    a, b, router, child, nested = two_hops
    router.down.add(WINDOWS_URL)
    with pytest.raises(ValueError, match="Could not reach .*Nothing was recorded"):
        relay_from(a, child)
    assert chips_on(b) == []


def test_the_chip_reaches_the_subagents_worker_as_a_mirror_row(linked):
    a, b, _, child = linked
    answer = relay_from(a, child)
    with b.active():
        engine.scan_all()
    converge([a, b])
    assert [chip["id"] for chip in chips_on(a)] == [answer["chip_id"]]
