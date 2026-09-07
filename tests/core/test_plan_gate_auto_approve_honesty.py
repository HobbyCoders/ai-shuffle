"""An auto-approved plan gate must not look like one a person answered.

In bypassPermissions the ``StopPlanning`` gate closes on policy in about seven
milliseconds. Two audited sessions then had the model tell the user "the plan
is approved" while a Plan Approval panel — Approve / Keep planning — sat on the
screen with nobody coming to answer it, because nothing retired it.

What the host can honestly contribute is pinned here: the stale gate is
retired, the approval carries an explicit "no user approval was collected"
marker beside the string the engine reads, and the moment is logged at INFO
rather than debug. (Wording the tool result itself is fluid's half.)
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import app.core.query_engine as qe
from app.core.user_question_handler import FLUID_ANSWER_KEY


_PLAN_INPUT = {
    "question": "…the plan…\n\nApprove this plan and leave plan mode?",
    "schema": {
        "question": "Approve this plan and leave plan mode?",
        "header": "Plan",
        "options": [{"label": "Approve"}, {"label": "Keep planning"}],
    },
}


async def test_the_stale_answerable_gate_is_retired(caplog):
    supersede = AsyncMock(return_value=1)
    refs = {"permission_mode": "bypassPermissions"}

    with patch.object(qe.permission_handler, "supersede_plan_approvals", supersede), \
            patch.object(qe, "_sync_plan_mode_exited", return_value="bypassPermissions"):
        await qe._auto_approve_plan_exit("sess-1", dict(_PLAN_INPUT), refs)

    supersede.assert_awaited_once_with("sess-1")


async def test_the_answer_is_still_an_approval_the_engine_reads():
    with patch.object(qe.permission_handler, "supersede_plan_approvals", AsyncMock()), \
            patch.object(qe, "_sync_plan_mode_exited", return_value=None):
        updated = await qe._auto_approve_plan_exit("sess-1", dict(_PLAN_INPUT), {})

    # fluid matches the answer string against its approval set; anything else
    # would read as plan FEEDBACK and leave the session read-only.
    assert updated[FLUID_ANSWER_KEY] == "approve"


async def test_the_provenance_rides_its_own_key():
    with patch.object(qe.permission_handler, "supersede_plan_approvals", AsyncMock()), \
            patch.object(qe, "_sync_plan_mode_exited", return_value=None):
        updated = await qe._auto_approve_plan_exit("sess-1", dict(_PLAN_INPUT), {})

    assert updated[qe.HOST_AUTO_APPROVED_KEY] == qe.HOST_AUTO_APPROVED_NOTE
    assert "no user approval was collected" in qe.HOST_AUTO_APPROVED_NOTE
    assert "host policy" in qe.HOST_AUTO_APPROVED_NOTE
    # And it is a SEPARATE key — putting it in the answer would turn the
    # approval into feedback.
    assert qe.HOST_AUTO_APPROVED_KEY != FLUID_ANSWER_KEY


async def test_the_marker_is_additive():
    """The engine still receives the tool input it sent."""
    with patch.object(qe.permission_handler, "supersede_plan_approvals", AsyncMock()), \
            patch.object(qe, "_sync_plan_mode_exited", return_value=None):
        updated = await qe._auto_approve_plan_exit("sess-1", dict(_PLAN_INPUT), {})

    for key, value in _PLAN_INPUT.items():
        assert updated[key] == value


async def test_it_is_logged_at_info_not_debug(caplog):
    with patch.object(qe.permission_handler, "supersede_plan_approvals", AsyncMock()), \
            patch.object(qe, "_sync_plan_mode_exited", return_value=None), \
            caplog.at_level(logging.DEBUG, logger=qe.__name__):
        await qe._auto_approve_plan_exit("sess-42", dict(_PLAN_INPUT), {})

    records = [
        r for r in caplog.records if "StopPlanning auto-approved" in r.getMessage()
    ]
    assert records, caplog.text
    assert records[0].levelno == logging.INFO
    assert "sess-42" in records[0].getMessage()
    assert "no user approval was collected" in records[0].getMessage()


async def test_the_plan_mode_exit_sync_still_runs():
    """The whole reason bypass returns early: the session must not stay locked
    in plan mode after the exit is approved."""
    sync = patch.object(qe, "_sync_plan_mode_exited", return_value="bypassPermissions")
    refs = {"permission_mode": "plan"}

    with patch.object(qe.permission_handler, "supersede_plan_approvals", AsyncMock()), \
            sync as spy:
        await qe._auto_approve_plan_exit("sess-1", dict(_PLAN_INPUT), refs)

    spy.assert_called_once_with(refs)


# ---------------------------------------------------------------------------
# The plan itself (2026-09-05 audit F5 / F11 / F13)
# ---------------------------------------------------------------------------
#
# The Plan Approval panel is what would have rendered ``question_input["plan"]``
# — and on this path it never opens, so the plan markdown lived only inside
# fluid's own QuestionRequest event and the user never saw it. Two audited
# sessions bet a whole round on a decision nobody outside the model read.

_PLAN_MARKDOWN = (
    "## Round 1\n\n1. Rewrite `parked_task_kind`\n2. Ship it behind a flag"
)
_ISOLATED_PLAN_INPUT = {
    **_PLAN_INPUT,
    "plan": _PLAN_MARKDOWN,
}


class _FakeAdapter:
    """Only the surface the disclosure touches: the plan-mode note queue that
    ``receive_events`` drains into the stream on the next engine event."""

    def __init__(self):
        self._pending_plan_mode_events = []


async def test_the_plan_reaches_the_transcript():
    from app.core.plan_disclosure import PLAN_DISCLOSURE_SUBTYPE

    adapter = _FakeAdapter()

    with patch.object(qe.permission_handler, "supersede_plan_approvals", AsyncMock()), \
            patch.object(qe, "_sync_plan_mode_exited", return_value=None):
        await qe._auto_approve_plan_exit(
            "sess-plan", dict(_ISOLATED_PLAN_INPUT), {"adapter": adapter}
        )

    queued = adapter._pending_plan_mode_events
    assert len(queued) == 1
    assert queued[0].subtype == PLAN_DISCLOSURE_SUBTYPE
    assert queued[0].data["plan"] == _PLAN_MARKDOWN
    # The generic system card renders ``data["content"]``, so the plan has to
    # be in there too, not only in a field nothing reads.
    assert _PLAN_MARKDOWN in queued[0].data["content"]


async def test_the_note_says_who_approved_it():
    """The same entry is the app's durable answer to a gate nobody was offered,
    so it has to carry the provenance rather than read as an approval."""
    adapter = _FakeAdapter()

    with patch.object(qe.permission_handler, "supersede_plan_approvals", AsyncMock()), \
            patch.object(qe, "_sync_plan_mode_exited", return_value=None):
        await qe._auto_approve_plan_exit(
            "sess-plan", dict(_ISOLATED_PLAN_INPUT), {"adapter": adapter}
        )

    data = adapter._pending_plan_mode_events[0].data
    assert data["answeredBy"] == "host_policy"
    assert data["approved"] is True
    assert data["userReviewed"] is False
    assert "No approval was collected from you" in data["content"]


async def test_the_note_is_not_a_modal():
    """It is an ordinary ``system`` entry. The app's question and permission
    modals are driven by ``pending_interactions`` rows, and this path
    deliberately creates none — so nothing here can render as one."""
    from app.core.provider.events import SystemMessage
    from app.core.system_message_filter import is_hidden_system_subtype
    from app.core.plan_disclosure import PLAN_DISCLOSURE_SUBTYPE

    adapter = _FakeAdapter()

    with patch.object(qe.permission_handler, "supersede_plan_approvals", AsyncMock()), \
            patch.object(qe, "_sync_plan_mode_exited", return_value=None):
        await qe._auto_approve_plan_exit(
            "sess-plan", dict(_ISOLATED_PLAN_INPUT), {"adapter": adapter}
        )

    note = adapter._pending_plan_mode_events[0]
    assert isinstance(note, SystemMessage)
    # Not filtered out of history either — a hidden subtype would put us back
    # where we started.
    assert not is_hidden_system_subtype(PLAN_DISCLOSURE_SUBTYPE)


async def test_the_approval_question_is_stripped_from_the_fallback():
    """Older inputs carry no isolated ``plan``; the raw QuestionRequest message
    ends with fluid's synthetic approval prompt, which must not be replayed to
    a user who was never asked it."""
    from app.core.plan_disclosure import plan_markdown

    raw = (
        _PLAN_MARKDOWN
        + "\n\nApprove this plan and leave plan mode? "
        + "(1 = Approve, anything else = feedback)"
    )

    assert plan_markdown({"question": raw}) == _PLAN_MARKDOWN
    assert plan_markdown({"plan": _PLAN_MARKDOWN, "question": raw}) == _PLAN_MARKDOWN
    assert plan_markdown({}) == ""
    assert plan_markdown(None) == ""


async def test_a_missing_queue_never_fails_the_plan_exit():
    """The SDK leg reaches plan approval through ExitPlanMode and its own
    panel; it has no note queue, and the exit must still succeed."""
    from app.core.user_question_handler import FLUID_ANSWER_KEY

    with patch.object(qe.permission_handler, "supersede_plan_approvals", AsyncMock()), \
            patch.object(qe, "_sync_plan_mode_exited", return_value=None):
        updated = await qe._auto_approve_plan_exit(
            "sess-plan", dict(_ISOLATED_PLAN_INPUT), {"adapter": object()}
        )

    assert updated[FLUID_ANSWER_KEY] == "approve"


async def test_the_plan_note_precedes_the_mode_switch_note():
    """Transcript order: the plan, then the decision. ``_sync_plan_mode_exited``
    is what queues 'Switched to … mode.' onto the same list."""
    adapter = _FakeAdapter()

    def _mode_note(refs):
        adapter._pending_plan_mode_events.append("mode-switch")
        return None

    with patch.object(qe.permission_handler, "supersede_plan_approvals", AsyncMock()), \
            patch.object(qe, "_sync_plan_mode_exited", side_effect=_mode_note):
        await qe._auto_approve_plan_exit(
            "sess-plan", dict(_ISOLATED_PLAN_INPUT), {"adapter": adapter}
        )

    assert adapter._pending_plan_mode_events[-1] == "mode-switch"
    assert len(adapter._pending_plan_mode_events) == 2
