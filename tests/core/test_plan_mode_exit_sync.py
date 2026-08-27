"""Unit tests for _sync_plan_mode_exited — the approved plan-exit state sync.

Approving the plan panel (ExitPlanMode on the delegated leg, StopPlanning on
both) used to leave ``sessions.permission_mode`` bound to ``plan`` and the
turn's permission refs reading ``plan``, so every later send re-resolved plan
mode and the session looked permanently stuck. The helper delegates to
``HarnessAdapter.notify_plan_mode_exited()`` and re-pins ``refs``.
"""

import pytest

from app.core.query_engine import _sync_plan_mode_exited


class _FakeAdapter:
    def __init__(self, restored):
        self.restored = restored
        self.called = False

    def notify_plan_mode_exited(self):
        self.called = True
        return self.restored


def test_sync_updates_refs_to_restored_mode():
    adapter = _FakeAdapter("default")
    refs = {"adapter": adapter, "permission_mode": "plan"}
    assert _sync_plan_mode_exited(refs) == "default"
    assert adapter.called is True
    assert refs["permission_mode"] == "default"


def test_sync_noop_when_adapter_not_in_plan():
    adapter = _FakeAdapter(None)
    refs = {"adapter": adapter, "permission_mode": "default"}
    assert _sync_plan_mode_exited(refs) is None
    assert adapter.called is True
    # Refs untouched when there was nothing to restore.
    assert refs["permission_mode"] == "default"


def test_sync_tolerates_missing_or_broken_adapter():
    refs = {"permission_mode": "plan"}
    assert _sync_plan_mode_exited(refs) is None

    class _Boom:
        def notify_plan_mode_exited(self):
            raise RuntimeError("no engine")

    refs2 = {"adapter": _Boom(), "permission_mode": "plan"}
    assert _sync_plan_mode_exited(refs2) is None


# ═══ The app↔fluid ordering the whole sync depends on ════════════════════════
#
# The app answers a parked StopPlanning in TWO steps and in THIS order: sync app
# state first (which flips the engine's plan flag), submit the answer second.
# fluid's second MRTR leg therefore runs with plan mode ALREADY off. It used to
# re-check that flag as a precondition and reject the retry with "Not in plan
# mode — nothing to stop. Enter it with Plan." — an approved exit delivered to
# the model as an error whose stated remedy re-locks the session. These pin the
# vendored behavior the ordering above relies on.


async def test_vendored_stop_planning_confirms_after_the_host_flipped_the_flag():
    from fluid.tools.builtins.plan import PlanModeControl, stop_handler
    from fluid.tools.registry import ToolCtx

    state = {"active": False}  # the app already synced
    ctx = ToolCtx()
    ctx.extra["plan_mode_control"] = PlanModeControl(
        is_active=lambda: state["active"],
        set_active=lambda v: state.__setitem__("active", bool(v)),
    )

    outcome = await stop_handler(
        {"plan": "the plan", "__input_responses__": {"approve": "approve"}}, ctx
    )

    assert not outcome.is_error, outcome.text
    assert "approved" in outcome.text.lower() and "OFF" in outcome.text
    assert "Enter it with Plan" not in outcome.text
    assert state["active"] is False


@pytest.mark.parametrize("answer", ["approve", "1", "yes"])
async def test_every_answer_the_app_sends_reads_as_approval(answer):
    """``query_engine`` translates an allowed panel/chat verdict into exactly
    these strings. Each one must end plan mode on fluid's side."""
    from fluid.tools.builtins.plan import is_approval

    assert is_approval(answer)


async def test_the_apps_rejection_token_is_not_an_approval():
    from fluid.tools.builtins.plan import is_approval

    assert not is_approval("keep planning")
