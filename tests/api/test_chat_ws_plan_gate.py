"""Plan approvals typed into the chat box reach the StopPlanning gate (P10).

fluid's ``StopPlanning`` parks the turn on a durable ``pending_interactions``
row (kind ``permission``, tool ``StopPlanning``, title "Plan Approval"). Before
this, ONLY the panel's ``permission_response`` frame resolved it: a user who
answered the way users actually answer — "Proceed." in the composer — got their
message queued as a fresh turn while the gate sat parked until its 300s TTL
denied it, the parked turn burning nodes the whole time.

``handle_query`` now routes such a message into the gate:

* an approval answers it ``allow`` and runs NO turn of its own (the parked turn
  resumes and implements the plan) — the message is still recorded;
* anything else answers it "keep planning" and then takes the ordinary
  queue/steer path, so the user's actual words still reach the model;
* an ORDINARY permission prompt is never touched — "yes" typed under a
  dangerous-command prompt still needs its explicit click.

Runs against the real turns + pending_interactions tables (``mock_db`` pg-shim);
only the WS collaborators are patched (shared with test_chat_ws_v2_turns).
"""

from app.core.permission_handler import (
    is_plan_approval_text,
    normalize_plan_answer,
    permission_handler,
)
from app.db import interactions
from app.db import turns as turns_db

from tests.api.test_chat_ws_v2_turns import (
    _Patches,
    _make_handler,
    _make_session,
    _sent,
)


def _park_plan_gate(session_id, request_id="perm-plan-1"):
    """The row query_engine's StopPlanning branch persists, shape for shape."""
    return interactions.create_interaction(
        session_id,
        "permission",
        {
            "request_id": request_id,
            "profile_id": "default",
            "tool_name": "StopPlanning",
            "tool_input": {"plan": "1. Add the thing\n2. Test the thing"},
            "title": "Plan Approval",
            "display_name": "Approve Plan",
        },
        300,
    )


def _park_bash_prompt(session_id, request_id="perm-bash-1"):
    return interactions.create_interaction(
        session_id,
        "permission",
        {
            "request_id": request_id,
            "profile_id": "default",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /srv/data"},
        },
        300,
    )


def _start_live_turn(session_id="sess-1"):
    live = turns_db.enqueue_turn(session_id, "user", prompt="plan the work")["id"]
    turns_db.start_turn(live, run_id="run-1")
    return live


def _messages(mock_db, session_id="sess-1"):
    rows = mock_db.execute(
        f"SELECT role, content FROM session_messages WHERE session_id='{session_id}'"
    ).fetchall()
    return [(r["role"], r["content"]) for r in rows]


# ═══ the message classifier ══════════════════════════════════════════════════


def test_affirmatives_are_recognized_through_punctuation_and_case():
    for text in (
        "Proceed.",
        "proceed",
        "Yes, go ahead!",
        "PLAN APPROVED",
        "  lgtm  ",
        "Looks good to me.",
        "ship it!",
        "do it",
        "let's do it",
        "1",
        "Approve the plan",
    ):
        assert is_plan_approval_text(text), text


def test_anything_else_is_feedback():
    for text in (
        "",
        "   ",
        "use Postgres instead of SQLite",
        "no",
        "not yet",
        "yes but split step 2 first",
        "why did you pick that library?",
    ):
        assert not is_plan_approval_text(text), text


def test_normalize_keeps_the_apostrophe_that_carries_the_word():
    assert normalize_plan_answer("Let's do it!") == "let's do it"


# ═══ finding the open gate ═══════════════════════════════════════════════════


def test_find_pending_plan_approval_ignores_ordinary_prompts(mock_db):
    _make_session("sess-1")
    _park_bash_prompt("sess-1")
    assert permission_handler.find_pending_plan_approval("sess-1") is None

    plan_row = _park_plan_gate("sess-1")
    found = permission_handler.find_pending_plan_approval("sess-1")
    assert found is not None and found["id"] == plan_row["id"]


def test_the_live_gate_wins_over_a_stale_one(mock_db):
    """The zombie-gate lock.

    A gate parked by a turn that died stays ``pending`` for the rest of its
    300s TTL. The session resumes still in plan mode and the model presents its
    plan again — so for those minutes the session has TWO answerable plan
    gates. Answering oldest-first sent the user's approval to a request_id
    nobody was waiting on, while the live gate sat there until its own TTL
    denied it: an approval that visibly did nothing, and a session that stayed
    read-only.
    """
    _make_session("sess-1")
    _park_plan_gate("sess-1", request_id="perm-plan-dead")
    live = _park_plan_gate("sess-1", request_id="perm-plan-live")

    found = permission_handler.find_pending_plan_approval("sess-1")
    assert found is not None and found["id"] == live["id"]
    assert found["payload"]["request_id"] == "perm-plan-live"


async def test_a_new_gate_supersedes_the_dead_ones_before_it_parks(mock_db):
    """query_engine's StopPlanning branch retires the corpses first, so the
    two-gate window never opens in the first place — and the stale purple
    panel clears off every device instead of looking answerable for minutes."""
    _make_session("sess-1")
    dead = _park_plan_gate("sess-1", request_id="perm-plan-dead")
    bash = _park_bash_prompt("sess-1")

    superseded = await permission_handler.supersede_plan_approvals("sess-1")

    assert superseded == 1
    assert interactions.get_interaction(dead["id"])["state"] == "cancelled"
    # An ordinary permission prompt is none of its business.
    assert interactions.get_interaction(bash["id"])["state"] == "pending"


async def test_supersede_is_a_no_op_with_nothing_to_retire(mock_db):
    _make_session("sess-1")
    assert await permission_handler.supersede_plan_approvals("sess-1") == 0


async def test_chat_approval_answers_the_live_gate_not_the_dead_one(mock_db):
    """The invariant, through the real WS routing: a session in plan mode has
    exactly one answerable exit, and "Proceed." finds it."""
    _make_session("sess-1")
    _start_live_turn()
    dead = _park_plan_gate("sess-1", request_id="perm-plan-dead")
    live = _park_plan_gate("sess-1", request_id="perm-plan-live")

    with _Patches():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "Proceed.",
            "session_id": "sess-1",
            "profile": "claude-code",
        })

    answered = interactions.get_interaction(live["id"])
    assert answered["state"] == "answered"
    assert answered["answer"]["decision"] == "allow"
    # The dead gate is left exactly as it was — nothing was answered on its
    # behalf, and the user's approval was not spent on it.
    assert interactions.get_interaction(dead["id"])["state"] == "pending"
    acks = _sent(handler, "permission_response_ack")
    assert [a["request_id"] for a in acks] == ["perm-plan-live"]


# ═══ handle_query routing ════════════════════════════════════════════════════


async def test_chat_approval_answers_the_gate_and_runs_no_turn(mock_db):
    """"Proceed." resolves the parked gate as an approval, records the user's
    message, and submits NOTHING — the parked turn resumes on its own."""
    _make_session("sess-1")
    live = _start_live_turn()
    row = _park_plan_gate("sess-1")

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "Proceed.",
            "session_id": "sess-1",
            "profile": "claude-code",
        })

    answered = interactions.get_interaction(row["id"])
    assert answered["state"] == "answered"
    assert answered["answer"]["decision"] == "allow"

    # No duplicate turn: the live turn is untouched and nothing queued behind it.
    assert turns_db.get_live_turn("sess-1")["id"] == live
    assert turns_db.get_queued_turns("sess-1") == []
    assert _sent(handler, "query_queued") == []

    # ...but the message is still the user's message.
    assert _messages(mock_db) == [("user", "Proceed.")]
    p.sync.broadcast_message_added.assert_awaited_once()

    # The panel card clears on the answering device.
    acks = _sent(handler, "permission_response_ack")
    assert [a["request_id"] for a in acks] == ["perm-plan-1"]


async def test_chat_feedback_keeps_planning_and_still_reaches_the_model(mock_db):
    """Non-affirmative text answers the gate as "keep planning" (fluid revises
    instead of exiting plan mode) and then takes the ordinary queue/steer path,
    so the words themselves land in front of the model."""
    _make_session("sess-1")
    _start_live_turn()
    row = _park_plan_gate("sess-1")

    with _Patches():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "Use Postgres instead of SQLite",
            "session_id": "sess-1",
            "profile": "claude-code",
        })

    answered = interactions.get_interaction(row["id"])
    assert answered["state"] == "answered"
    assert answered["answer"]["decision"] == "deny"

    queued = turns_db.get_queued_turns("sess-1")
    assert [t["prompt"] for t in queued] == ["Use Postgres instead of SQLite"]
    assert _sent(handler, "query_queued") == [
        {"type": "query_queued", "session_id": "sess-1"}
    ]


async def test_chat_text_never_answers_an_ordinary_permission_prompt(mock_db):
    """A bare "yes" while a dangerous-command prompt is up keeps needing the
    explicit click — and stays an ordinary queued message."""
    _make_session("sess-1")
    _start_live_turn()
    row = _park_bash_prompt("sess-1")

    with _Patches():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "yes",
            "session_id": "sess-1",
            "profile": "claude-code",
        })

    assert interactions.get_interaction(row["id"])["state"] == "pending"
    assert [t["prompt"] for t in turns_db.get_queued_turns("sess-1")] == ["yes"]
    assert _sent(handler, "permission_response_ack") == []


async def test_no_gate_leaves_the_ordinary_query_path_alone(mock_db):
    """No plan gate open: an affirmative-looking message is just a message."""
    _make_session("sess-1")

    with _Patches():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "proceed",
            "session_id": "sess-1",
            "profile": "claude-code",
        })

    assert [
        (t["source"], t["prompt"]) for t in turns_db.get_queued_turns("sess-1")
    ] == [("user", "proceed")]
    assert _messages(mock_db) == [("user", "proceed")]
