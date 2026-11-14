"""Stop on a turn parked on background work reads as Stopped (#1340).

The turn already replied, so Stop settles it to ``completed``/``parked_settled``
rather than aborting it. The settlement Stop causes still has to say it was a
Stop, or the sidebar reads Done once the background work is gone.
"""

from __future__ import annotations

from app.core.owner_services import session_stopped
from app.core.query_session_lifecycle import enter_user_stop
from app.core.task_registry import task_registry
from app.db import database, turns
from tests.shared_fixtures import clean_task_registry  # noqa: F401  (autouse fixture)


def _parked(session_id: str) -> dict:
    database.create_session(session_id=session_id, profile_id="default", title="t")
    row = turns.enqueue_turn(session_id, "user", prompt="watch CI")
    turns.start_turn(row["id"])
    turns.park_turn(row["id"], task_ids=["monitor-1"])
    return row


async def test_stop_on_a_parked_turn_reads_as_stopped(mock_db):
    row = _parked("s-park-stop")

    await enter_user_stop("s-park-stop")

    settled = turns.get_turn(row["id"])
    # The reply stands: the row completes as a park settlement, marked as a Stop.
    assert (settled["state"], settled["stop_reason"], settled["interrupt_mode"]) == (
        "completed", "parked_settled", "stop")
    assert turns.latest_turn_outcomes(["s-park-stop"]) == {"s-park-stop": "stopped"}
    # A reload reads the same rows; after a reboot the Stop still holds.
    task_registry.clear_user_stop("s-park-stop")  # a reboot drops the live latch
    assert session_stopped("s-park-stop")


async def test_stop_keeps_its_mark_when_monitor_cleanup_settles_the_park_first(mock_db):
    """The Stop names the park, then the post-park Monitor check settles it before
    the Stop's own cleanup runs. The row still reads Stopped."""
    from app.core.query_session_lifecycle import _begin_user_stop
    from app.core.turn_finalize import settle_stopped_monitor_parks

    row = _parked("s-park-race")
    stop = _begin_user_stop("s-park-race")
    receipt = await stop.bind()
    assert [p["id"] for p in receipt["parks"]] == [row["id"]]

    # The competing settlement lands first, without the Stop's mark.
    assert await settle_stopped_monitor_parks("s-park-race") is True
    assert turns.get_turn(row["id"])["interrupt_mode"] is None

    await enter_user_stop("s-park-race", stop=stop)

    settled = turns.get_turn(row["id"])
    assert (settled["state"], settled["stop_reason"], settled["interrupt_mode"]) == (
        "completed", "parked_settled", "stop")
    assert turns.latest_turn_outcomes(["s-park-race"]) == {"s-park-race": "stopped"}


async def test_a_stop_does_not_mark_a_park_settled_before_it(mock_db):
    """A park ordinary cleanup settled before the Stop is not in its receipt."""
    row = _parked("s-park-before")
    assert turns.settle_parked_turns("s-park-before") == 1

    await enter_user_stop("s-park-before")

    assert turns.get_turn(row["id"])["interrupt_mode"] is None


def test_a_park_that_settles_on_its_own_is_not_a_stop(mock_db):
    row = _parked("s-park-done")

    assert turns.settle_parked_turns("s-park-done") == 1

    settled = turns.get_turn(row["id"])
    assert (settled["state"], settled["stop_reason"], settled["interrupt_mode"]) == (
        "completed", "parked_settled", None)
    assert turns.latest_turn_outcomes(["s-park-done"]) == {}
