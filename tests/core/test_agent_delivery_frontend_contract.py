"""Peer-message prompts the chat renders, pinned to a shared fixture (#1618).

A turn that delivers peer messages records its prompt as the turn's
``message.user``. The real scheduler builds four shapes: one idle message (a
single envelope), and three that carry messages held while the parent was
parked, inside a ``<system-reminder>`` (a promotion wake, an unpark wake after
its task notification, and an urgent message after the held ones).
``frontend/src/lib/utils/__fixtures__/agent-delivery-producer.json`` holds
those prompts; ``agent-delivery.test.ts`` feeds them to the chat reducer and
requires one attributed agent message per peer and no visible reminder text.
This test rebuilds the prompts from the real producers and requires they
match the fixture, so a producer change fails here until the frontend parser
is checked against it.

Regenerate after an intentional change with:
    UPDATE_AGENT_DELIVERY_FIXTURE=1 python -m pytest tests/core/test_agent_delivery_frontend_contract.py
"""

import json
import os
from pathlib import Path

import pytest_asyncio

from app.core import turn_scheduler, wakeup_service
from app.db import turns as turns_db

FIXTURE = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "utils"
           / "__fixtures__" / "agent-delivery-producer.json")

#: (sender, text) held while parked, oldest first. The second spans lines.
HELD = [("worker-a", "Found the failing path."), ("worker-b", "Tests are red:\n\n- one\n- two")]
SENDERS = {"worker-a", "worker-b", "worker-c"}


@pytest_asyncio.fixture(autouse=True)
async def _quiet(monkeypatch):
    monkeypatch.setattr(turn_scheduler, "kick", lambda _: None)
    turn_scheduler._delivered_held.clear()
    yield
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()
    turn_scheduler._delivered_held.clear()


def _park(session_id):
    turn = turns_db.enqueue_turn(session_id, "user", prompt="Delegate", profile_id="profile")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.park_turn(turn["id"], ["worker-a", "worker-b"])


async def _hold(session_id):
    _park(session_id)
    for sender, text in HELD:
        assert await turn_scheduler.on_agent_message(session_id, sender, text) == turn_scheduler.HELD


def _only_queued(session_id):
    (row,) = turns_db.get_queued_turns(session_id)
    return row


async def _prompts():
    out = {}

    await turn_scheduler.on_agent_message("idle", "worker-c", "Done; report attached.")
    out["idle_single"] = _only_queued("idle")["prompt"]

    await _hold("promo")
    (row,) = _held_rows("promo")
    await turn_scheduler.promote_held_peer_messages("promo", row["prompt"])
    out["promotion"] = _only_queued("promo")["prompt"]

    await _hold("wake")
    frame = {"task_id": "worker-a", "status": "completed", "summary": "worker-a finished"}
    async with turn_scheduler._terminal_delivery_lock("wake"):
        await turn_scheduler._enqueue_context_wake(
            "wake", "unpark", [frame], ["worker-a"],
            turn_scheduler._build_task_notification_prompt([frame]),
        )
    out["unpark_with_held"] = _only_queued("wake")["prompt"]

    await _hold("urgent")
    await turn_scheduler.on_agent_message("urgent", "worker-c", "Blocked: may I edit control.py?", urgent=True)
    out["urgent_with_held"] = _only_queued("urgent")["prompt"]
    return out


def _held_rows(session_id):
    from app.db import wakeups as wakeups_db

    return wakeups_db.get_session_wakeups(session_id, wakeup_service.HELD_PEER_SOURCE)


async def test_shared_frontend_fixture_matches_the_real_producers(mock_db):
    prompts = await _prompts()
    # The shapes the frontend has to split: every held message, in order, in
    # one reminder; the fresh urgent message after it.
    for key in ("promotion", "unpark_with_held", "urgent_with_held"):
        text = prompts[key]
        assert text.count("<system-reminder>") == 1, key
        assert text.index("Found the failing path.") < text.index("Tests are red:"), key
    assert prompts["unpark_with_held"].index("<task-notification>") < prompts["unpark_with_held"].index("<system-reminder>")
    assert prompts["urgent_with_held"].index("</system-reminder>") < prompts["urgent_with_held"].index("Blocked:")
    assert "<system-reminder>" not in prompts["idle_single"]

    payload = {"held": [{"sender": s, "content": t} for s, t in HELD], "prompts": prompts}
    if os.environ.get("UPDATE_AGENT_DELIVERY_FIXTURE") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(payload, indent="\t", sort_keys=True) + "\n",
                           encoding="utf-8", newline="\n")
    assert FIXTURE.exists(), f"missing {FIXTURE}; regenerate with UPDATE_AGENT_DELIVERY_FIXTURE=1"
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == payload, (
        "Peer-delivery prompts changed; check frontend/src/lib/utils/agent-delivery.ts still "
        "splits them, then regenerate the fixture with UPDATE_AGENT_DELIVERY_FIXTURE=1")
