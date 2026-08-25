"""Unit tests for _sync_plan_mode_exited — the approved plan-exit state sync.

Approving the plan panel (ExitPlanMode on the delegated leg, StopPlanning on
both) used to leave ``sessions.permission_mode`` bound to ``plan`` and the
turn's permission refs reading ``plan``, so every later send re-resolved plan
mode and the session looked permanently stuck. The helper delegates to
``HarnessAdapter.notify_plan_mode_exited()`` and re-pins ``refs``.
"""

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
