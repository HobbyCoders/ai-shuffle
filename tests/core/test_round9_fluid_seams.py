"""App-side halves of the round-9 fluid seams.

* A ``SubagentEvent(kind="progress")`` whose task id starts with ``peer:`` is a
  peer message fluid queued while the session was parked — engine-internal,
  never a task. No consumer may turn it into a card or a pending id.
* ``ToolRegistry(external_call_timeout=...)`` and
  ``NativeLoop(turn_watchdog_seconds=...)`` are passed when the vendored fluid
  accepts them, from app settings.
* ``_memory_snapshot`` passes fluid's non-empty placeholder "# Memory" section
  through instead of filtering it out.
"""
from __future__ import annotations


from app.core.config import settings


# ─── peer progress events ────────────────────────────────────────────────────


def test_translator_drops_queued_peer_messages():
    from fluid.events import model as lm

    import app.core.provider.harness_adapter as ha

    adapter = ha.HarnessAdapter()
    queued = lm.SubagentEvent(
        session_id="ses_x", task_id="peer:review-diff", kind="progress",
        description="review-diff", summary="I found the bug", wake=False,
    )
    assert adapter._translate(queued) == []
    # An ordinary progress event still translates.
    real = lm.SubagentEvent(session_id="ses_x", task_id="t1", kind="progress", description="p")
    assert adapter._translate(real)


def test_orphan_pending_ignores_peer_progress_rows(monkeypatch):
    from app.core import pending_reconcile

    rows = [
        {"event": {"_cls": "SubagentEvent", "task_id": "t1", "kind": "started"}},
        {"event": {"_cls": "SubagentEvent", "task_id": "peer:review-diff", "kind": "progress",
                   "summary": "hello"}},
        {"event": {"_cls": "SubagentEvent", "task_id": "t2", "kind": "started"}},
        {"event": {"_cls": "SubagentEvent", "task_id": "t2", "kind": "finished"}},
    ]
    monkeypatch.setattr(pending_reconcile, "_subagent_event_rows", lambda sid: rows)
    assert pending_reconcile.orphan_pending_ids("ses_x") == ["t1"]


# ─── registry / loop bounds ──────────────────────────────────────────────────


def test_bound_kwargs_are_passed_only_when_the_seam_exists(monkeypatch):
    import app.core.provider.harness_adapter as ha

    class _OldRegistry:
        def __init__(self, *, manager=None):
            pass

    class _NewRegistry:
        def __init__(self, *, manager=None, external_call_timeout=75.0):
            pass

    class _OldLoop:
        def __init__(self, provider, store, model):
            pass

    class _NewLoop:
        def __init__(self, provider, store, model, turn_watchdog_seconds=900.0):
            pass

    monkeypatch.setattr(settings, "fluid_external_call_timeout_seconds", 330)
    monkeypatch.setattr(settings, "fluid_turn_watchdog_seconds", 900)
    assert ha._registry_bound_kwargs(_OldRegistry) == {}
    assert ha._registry_bound_kwargs(_NewRegistry) == {"external_call_timeout": 330.0}
    assert ha._loop_bound_kwargs(_OldLoop) == {}
    assert ha._loop_bound_kwargs(_NewLoop) == {"turn_watchdog_seconds": 900.0}
    # The real vendored classes: whichever build is vendored, the call is valid.
    ha.ToolRegistry(**ha._registry_bound_kwargs())
    assert set(ha._loop_bound_kwargs()) <= {"turn_watchdog_seconds"}


def test_external_call_timeout_clears_the_longest_legitimate_block():
    """browser_batch can chain twenty 15s browser_waits; Monitor's inline wait
    runs to 120s. The default must sit above both."""
    from app.core.monitor_spawn import MAX_WAIT_SECONDS

    default = settings.model_fields["fluid_external_call_timeout_seconds"].default
    assert default > 20 * 15 and default > MAX_WAIT_SECONDS


# ─── the memory placeholder rides through ────────────────────────────────────


def test_memory_snapshot_passes_a_non_empty_placeholder_through(monkeypatch):
    import app.core.provider.harness_adapter as ha
    from app.core.provider.types import SessionOptions

    class _Store:
        def render_memory_section(self, project):
            return "# Memory\n(no saved facts yet — Remember saves one, Recall searches)"

    monkeypatch.setattr("app.core.fluid_memory.session_memory", lambda *a: (_Store(), "project:x"))
    adapter = ha.HarnessAdapter()
    # Stamped with an app session id: an unstamped adapter fails closed and
    # renders neither facts nor the placeholder.
    adapter._options = SessionOptions(model="m", cwd=".", extra={"app_session_id": "app-sess-1"})
    note = adapter._memory_snapshot()
    assert note is not None and "# Memory" in note and "no saved facts yet" in note
    assert note.startswith("<system-reminder>")

    class _Empty(_Store):
        def render_memory_section(self, project):
            return None

    monkeypatch.setattr("app.core.fluid_memory.session_memory", lambda *a: (_Empty(), "project:x"))
    assert adapter._memory_snapshot() is None
