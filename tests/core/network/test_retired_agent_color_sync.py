"""The retired agent ``avatar_color`` column across mixed-version peers.

The avatar feature was removed: this build never reads or writes the column,
but older peers still send it on agent rows, and a peer build may also leave it
out. Agent rows apply cleanly either way, in both directions.
"""
from __future__ import annotations

import dataclasses

from app.db import database
from app.db.base import get_db
from app.network import engine, policy
from tests.core.network.harness import converge, exchange, join


def _pair(workers):
    new, old = workers("updated"), workers("older")
    with new.active():
        engine.create_network("Home", device_name="Updated")
    join(new, old)
    return new, old


def _clean(result: engine.ApplyResult) -> None:
    assert result.retry == result.rejected == result.pending == 0, result


def _sql(worker, query, params=()):
    with worker.active(), get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()] if cursor.description else []


def _color(worker, agent_id):
    rows = _sql(worker, "SELECT avatar_color FROM agents WHERE id = %s", (agent_id,))
    return rows[0]["avatar_color"] if rows else "missing"


def test_an_older_peers_coloured_agent_applies_and_keeps_its_colour(workers):
    new, old = _pair(workers)
    with old.active():
        database.create_profile("painter", "Painter", None, {"max_turns": 7})
    # An older release still writes the colour it lets the owner pick.
    _sql(old, "UPDATE agents SET avatar_color = 'blue' WHERE id = 'painter'")
    with old.active():
        engine.scan_all()
    _clean(exchange(old, new))
    with new.active():
        assert database.get_profile("painter")["config"]["max_turns"] == 7
    assert _color(new, "painter") == "blue"

    # An edit on the updated worker travels back without wiping the old peer's colour.
    with new.active():
        database.update_profile("painter", name="Painter renamed")
        engine.scan_all()
    _clean(exchange(new, old))
    with old.active():
        assert database.get_profile("painter")["name"] == "Painter renamed"
    assert _color(old, "painter") == "blue"


def test_an_agent_row_without_the_colour_applies_cleanly(workers, monkeypatch):
    new, old = _pair(workers)
    coll = policy.collection("agents")
    without = dataclasses.replace(coll, fields=tuple(f for f in coll.fields if f != "avatar_color"))
    with monkeypatch.context() as other_build:
        other_build.setitem(policy.COLLECTIONS, "agents", without)
        with old.active():
            database.create_profile("plain", "Plain", None, {"max_turns": 3})
            engine.scan_all()
    _clean(exchange(old, new))
    with new.active():
        assert database.get_profile("plain")["name"] == "Plain"
    assert _color(new, "plain") is None

    # And the updated worker's own rows reach that peer cleanly too.
    with new.active():
        database.create_profile("fresh", "Fresh", None, {})
        engine.scan_all()
    with monkeypatch.context() as other_build:
        other_build.setitem(policy.COLLECTIONS, "agents", without)
        _clean(exchange(new, old))
    with old.active():
        assert database.get_profile("fresh")["name"] == "Fresh"
    converge([new, old])
