"""Plan mode must not survive its own approval.

The reported failure: a session approves the plan panel and keeps behaving
read-only. Every path that could re-lock it is pinned here against one
invariant, stated three ways:

* after ANY successful approval — panel, chat, or bypass auto-approve — the
  next write tool of the SAME turn is gated by the RESTORED mode, not the plan
  one;
* the next TURN constructs the engine unlocked (``sessions.permission_mode`` no
  longer says ``plan``);
* a reconnect / resume constructs it unlocked too.

And the standing safety property behind them: a session in plan mode always has
exactly one live, answerable exit — never zero (a lock with nothing to answer)
and never two (an answer that lands on the wrong gate).
"""

from __future__ import annotations

import pytest

from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from app.core.query_engine import _sync_plan_mode_exited

from tests.test_harness_adapter import FakeEngine


class _Perms:
    def __init__(self, policy: str) -> None:
        self.approval_policy = policy


def _adapter(
    *,
    engine_plan: bool,
    options_mode: str,
    pre: str,
    policy: str = "on_write",
    app_session_id: str | None = None,
) -> tuple[HarnessAdapter, FakeEngine]:
    engine = FakeEngine([])
    engine.plan_mode = engine_plan
    engine.permissions = _Perms(policy)
    adapter = HarnessAdapter()
    adapter._engine = engine
    adapter._connected = True
    adapter._options = SessionOptions(
        model="fake-model",
        permission_mode=options_mode,
        extra={"app_session_id": app_session_id} if app_session_id else {},
    )
    adapter._pre_plan_permission_mode = pre
    return adapter, engine


def _binding_writes(monkeypatch) -> list:
    """Capture what the adapter persists onto ``sessions.permission_mode``."""
    written: list = []

    def _set(session_id, **kwargs):
        written.append((session_id, kwargs.get("permission_mode")))

    import app.db.sessions as sessions_db

    monkeypatch.setattr(sessions_db, "set_session_run_binding", _set)
    return written


# ═══ 1. The exit sync fires for every way a session gets into plan mode ══════


def test_model_initiated_plan_is_synced_even_though_options_say_default(monkeypatch):
    """The hole that left the NEXT turn locked.

    When the model enters plan mode itself, the composer chip — and therefore
    ``options.permission_mode`` — stays ``default``, while the ENTER persisted
    ``sessions.permission_mode = plan``. Gating the exit sync on the options
    alone skipped this session entirely, so the persisted binding kept saying
    ``plan`` and the next turn built the engine locked again.
    """
    written = _binding_writes(monkeypatch)
    adapter, engine = _adapter(
        engine_plan=True, options_mode="default", pre="default",
        policy="always_ask", app_session_id="sess-1",
    )

    restored = adapter.notify_plan_mode_exited()

    assert restored == "default"
    assert engine.plan_mode is False
    assert engine.permissions.approval_policy == "on_write"
    # The next turn reads this back — it must no longer say plan.
    assert written == [("sess-1", "default")]


def test_chip_initiated_plan_is_synced_and_restores_the_pre_plan_mode(monkeypatch):
    written = _binding_writes(monkeypatch)
    adapter, engine = _adapter(
        engine_plan=True, options_mode="plan", pre="acceptEdits",
        policy="always_ask", app_session_id="sess-2",
    )

    restored = adapter.notify_plan_mode_exited()

    assert restored == "acceptEdits"
    assert adapter._options.permission_mode == "acceptEdits"
    assert engine.plan_mode is False
    assert engine.permissions.approval_policy == "on_write"
    assert written == [("sess-2", "acceptEdits")]


def test_a_session_never_in_plan_mode_is_still_a_no_op(monkeypatch):
    written = _binding_writes(monkeypatch)
    adapter, engine = _adapter(
        engine_plan=False, options_mode="default", pre="default",
        app_session_id="sess-3",
    )
    assert adapter.notify_plan_mode_exited() is None
    assert written == []
    assert adapter._pending_plan_mode_events == []


def test_bypass_keeps_its_own_mode_instead_of_being_demoted_to_default(monkeypatch):
    """A bypassPermissions user whose model entered plan mode has nothing to
    restore — but the sync must not report ``default`` either. That answer is
    re-pinned onto the turn's permission refs, and it would start prompting a
    user who explicitly opted out of prompts."""
    written = _binding_writes(monkeypatch)
    adapter, engine = _adapter(
        engine_plan=True, options_mode="bypassPermissions",
        pre="bypassPermissions", policy="never_ask", app_session_id="sess-4",
    )

    restored = adapter.notify_plan_mode_exited()

    assert restored == "bypassPermissions"
    assert engine.plan_mode is False
    assert engine.permissions.approval_policy == "never_ask"
    assert written == []  # bypass never moved its chip on the way in


# ═══ 2. The invariant on the turn's own permission refs ═════════════════════


@pytest.mark.parametrize(
    "options_mode,pre,expected",
    [
        ("plan", "default", "default"),
        ("plan", "acceptEdits", "acceptEdits"),
        ("default", "default", "default"),          # model-initiated Plan
        ("bypassPermissions", "bypassPermissions", "bypassPermissions"),
    ],
)
def test_the_rest_of_the_turn_is_gated_by_the_restored_mode(
    monkeypatch, options_mode, pre, expected
):
    """``refs["permission_mode"]`` is what the can_use_tool closure reads for
    the NEXT write tool of this same turn. After an approval it must never
    still say ``plan``."""
    _binding_writes(monkeypatch)
    adapter, _ = _adapter(
        engine_plan=True, options_mode=options_mode, pre=pre,
        app_session_id="sess-5",
    )
    refs = {"adapter": adapter, "permission_mode": options_mode}

    assert _sync_plan_mode_exited(refs) == expected
    assert refs["permission_mode"] == expected
    assert refs["permission_mode"] != "plan"


# ═══ 3. Host bookkeeping failures cannot re-lock the session ════════════════


def test_a_failing_binding_write_does_not_leave_the_engine_locked(monkeypatch):
    """Persistence is best-effort; the mode itself is not."""
    import app.db.sessions as sessions_db

    def _boom(session_id, **kwargs):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(sessions_db, "set_session_run_binding", _boom)
    adapter, engine = _adapter(
        engine_plan=True, options_mode="plan", pre="default",
        policy="always_ask", app_session_id="sess-6",
    )

    assert adapter.notify_plan_mode_exited() == "default"
    assert engine.plan_mode is False
    assert engine.permissions.approval_policy == "on_write"


def test_the_plan_control_wrapper_swallows_its_own_bookkeeping_failure():
    """The wrapper runs INSIDE fluid's tool dispatch. An exception escaping it
    becomes the StopPlanning tool's result — the model reads the approval it
    was just granted as a failed tool call and goes back to planning."""
    from fluid.tools.builtins.plan import PlanModeControl
    from fluid.tools.registry import ToolCtx

    flag = {"on": True}
    engine = FakeEngine([])
    engine.ctx = ToolCtx()
    engine.ctx.extra["plan_mode_control"] = PlanModeControl(
        is_active=lambda: flag["on"],
        set_active=lambda v: flag.__setitem__("on", bool(v)),
    )
    engine.plan_mode = True
    engine.permissions = _Perms("always_ask")

    adapter = HarnessAdapter()
    adapter._engine = engine
    adapter._connected = True
    adapter._options = SessionOptions(model="fake-model", permission_mode="plan")
    adapter._wire_plan_mode_control(adapter._options)
    adapter._on_plan_mode_changed = lambda active: (_ for _ in ()).throw(
        RuntimeError("chip broadcast exploded")
    )

    engine.ctx.extra["plan_mode_control"].set_active(False)

    # The engine flag — the thing that actually decides the tool schema —
    # flipped before the bookkeeping ran, and stayed flipped.
    assert flag["on"] is False


# ═══ 4. The next turn / a reconnect construct unlocked ══════════════════════


@pytest.mark.parametrize("mode,expect_locked", [
    ("plan", True),
    ("default", False),
    ("acceptEdits", False),
    ("bypassPermissions", False),
])
def test_engine_construction_follows_the_persisted_mode(mode, expect_locked):
    """``_build_permissions`` is the single place a fresh connect (next turn
    after a respawn, a reconnect, a resume) decides whether the session starts
    read-only. Once the exit sync has written a non-plan binding, every one of
    those paths constructs unlocked."""
    adapter = HarnessAdapter()
    _, plan_mode = adapter._build_permissions(
        SessionOptions(model="fake-model", permission_mode=mode)
    )
    assert plan_mode is expect_locked


def test_approve_then_reconnect_constructs_unlocked(monkeypatch):
    """End to end across the seam: approve, read back what was persisted, and
    build the next engine from it."""
    written = _binding_writes(monkeypatch)
    adapter, engine = _adapter(
        engine_plan=True, options_mode="plan", pre="default",
        policy="always_ask", app_session_id="sess-7",
    )
    adapter.notify_plan_mode_exited()

    persisted = written[-1][1]
    assert persisted == "default"

    fresh = HarnessAdapter()
    _, plan_mode = fresh._build_permissions(
        SessionOptions(model="fake-model", permission_mode=persisted)
    )
    assert plan_mode is False
