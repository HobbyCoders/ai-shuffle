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
