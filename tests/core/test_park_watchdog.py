"""Park watchdog — a park that never wakes is bounded, then broken.

A session parked on background work wakes when that work settles. When the
settle never arrives the park is silent AND unbounded, and from outside it
looks exactly like a healthy long wait: the audited session sat parked 9m37s
past a green CI run and only moved because an unrelated force-cleanup killed
the shell. ``sweep_stalled_parks`` runs on the resource monitor's 5-minute
tick beside the stale-row sweep and gives the park a time bound in two stages
— report it, then force the terminal frames that wake it.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.core import startup_reconcile as sr
from app.core.task_registry import task_registry
from app.db.background_tasks import get_background_task, upsert_background_task
from app.db.base import get_db
from app.db.turns import list_stalled_parked_turns

_OLD_TS = "2000-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _clean():
    task_registry.reset_all()
    sr._park_warned.clear()
    yield
    task_registry.reset_all()
    sr._park_warned.clear()


def _seed_turn(session_id, turn_id, state, *, ended_at=_OLD_TS, created_at=_OLD_TS,
               instance_id=None):
    from app.core.config import settings

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO turns (id, session_id, state, source, instance_id, "
            "created_at, ended_at) VALUES (%s, %s, %s, 'user', %s, %s, %s)",
            (
                turn_id, session_id, state,
                settings.effective_instance_id if instance_id is None else instance_id,
                created_at, ended_at,
            ),
        )


def _seed_task(session_id, task_id, status="running", tool_use_id=None):
    upsert_background_task({
        "task_id": task_id,
        "session_id": session_id,
        "kind": "shell",
        "tool_use_id": tool_use_id,
        "title": "gh pr checks",
        "status": status,
        "started_at": _OLD_TS,
        "updated_at": _OLD_TS,
    })


def _patch_wake():
    """The two side-effect seams the force stage drives."""
    return (
        patch("app.core.turn_recorder.record_task_settle_frame", new=AsyncMock()),
        patch("app.core.turn_scheduler.on_task_terminal", new=AsyncMock()),
    )


# =============================================================================
# list_stalled_parked_turns
# =============================================================================

def test_lists_only_parks_with_nothing_after_them(mock_db):
    _seed_turn("pw-stalled", "t-parked", "parked")
    # A park followed by ANY later turn is a park that woke.
    _seed_turn("pw-woken", "t-parked-2", "parked")
    _seed_turn("pw-woken", "t-wake", "completed", created_at="2020-01-01T00:00:00+00:00")
    # Not parked at all.
    _seed_turn("pw-running", "t-live", "running")

    rows = list_stalled_parked_turns(60)

    assert {r["session_id"] for r in rows} == {"pw-stalled"}
    assert rows[0]["parked_seconds"] > 60


def test_recent_parks_are_not_stalled(mock_db):
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _seed_turn("pw-fresh", "t-fresh", "parked", ended_at=now, created_at=now)

    assert list_stalled_parked_turns(1800) == []


def test_peer_installs_parks_are_left_alone(mock_db):
    _seed_turn("pw-peer", "t-peer", "parked", instance_id="some-other-install-9999")

    assert list_stalled_parked_turns(60) == []


# =============================================================================
# sweep_stalled_parks — warn stage
# =============================================================================

async def test_warn_stage_reports_once_and_touches_nothing(mock_db):
    _seed_turn("pw-warn", "t-warn", "parked")
    _seed_task("pw-warn", "task-warn")

    with patch("app.core.startup_reconcile.record_error") as rec:
        first = await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=0)
        second = await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=0)

    assert (first, second) == (0, 0)
    # Reported once, not once per five-minute tick…
    assert rec.call_count == 1
    assert rec.call_args.kwargs["context"]["watchdog"] == "park_watchdog"
    # …and nothing was settled: a long wait may be perfectly healthy.
    assert get_background_task("task-warn", "pw-warn")["status"] == "running"


async def test_the_kill_switch_disables_the_sweep(mock_db, monkeypatch):
    from app.core.config import settings

    _seed_turn("pw-off", "t-off", "parked")
    monkeypatch.setattr(settings, "park_watchdog_enabled", False)

    with patch("app.core.startup_reconcile.record_error") as rec:
        assert await sr.sweep_stalled_parks() == 0
    rec.assert_not_called()


# =============================================================================
# sweep_stalled_parks — force stage
# =============================================================================

async def test_force_stage_settles_the_task_and_wakes_the_session(mock_db):
    _seed_turn("pw-force", "t-force", "parked")
    _seed_task("pw-force", "task-force", tool_use_id="toolu_p")

    frame_patch, wake_patch = _patch_wake()
    with frame_patch as frame, wake_patch as wake:
        forced = await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=120)

    assert forced == 1
    assert get_background_task("task-force", "pw-force")["status"] == "killed"
    frame.assert_awaited_once()
    assert frame.await_args.args[2] == "killed"
    assert frame.await_args.kwargs["reason"] == "park_watchdog"
    assert frame.await_args.kwargs["tool_use_id"] == "toolu_p"
    # The frame settles the chip; the scheduler hand-off is what actually
    # wakes the parked session.
    wake.assert_awaited_once()
    woken_frame = wake.await_args.args[1]
    assert woken_frame["task_id"] == "task-force"
    assert woken_frame["status"] == "killed"
    assert "was not a failure" in woken_frame["summary"]


async def test_force_stage_unsticks_a_park_with_no_tasks_left(mock_db):
    # 'parked' is terminal in the turn state machine: with the work already
    # gone, nothing else ever moves the row and the session reads as parked
    # forever.
    _seed_turn("pw-empty", "t-empty", "parked")

    frame_patch, wake_patch = _patch_wake()
    with frame_patch as frame, wake_patch as wake:
        forced = await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=120)

    assert forced == 1
    frame.assert_not_awaited()
    wake.assert_not_awaited()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT state FROM turns WHERE id=%s", ("t-empty",))
        assert cur.fetchone()["state"] == "completed"


async def test_forcing_admits_the_task_to_the_own_wake_lane(mock_db):
    """Without OWN-lane admission on_task_terminal drops the frame as not-own
    work — and the park this sweep exists to break survives the sweep."""
    _seed_turn("pw-lane", "t-lane", "parked")
    _seed_task("pw-lane", "task-lane")

    frame_patch, wake_patch = _patch_wake()
    with frame_patch, wake_patch:
        await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=120)

    assert task_registry.is_own_background_task("pw-lane", "task-lane")


async def test_force_ceiling_of_zero_reports_without_forcing(mock_db):
    _seed_turn("pw-noforce", "t-noforce", "parked")
    _seed_task("pw-noforce", "task-noforce")

    frame_patch, wake_patch = _patch_wake()
    with frame_patch as frame, wake_patch as wake:
        forced = await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=0)

    assert forced == 0
    frame.assert_not_awaited()
    wake.assert_not_awaited()
    assert get_background_task("task-noforce", "pw-noforce")["status"] == "running"
