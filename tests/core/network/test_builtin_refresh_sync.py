"""Start-up refresh of built-in agent text vs peer sync (PR #1872 review).

The refresh runs before this worker has pulled from its peers, so it must
never publish: a stale replica stamped with a fresh clock would beat an
owner's edit made on another worker that has not arrived here yet.
"""
import json

import pytest

from app.db import database
from app.db import network as store
from app.network import engine
from tests.core.network.harness import converge, join


def _store_pre_pr_row():
    """Shape General Purpose like a pre-#1861 install: old text, no record."""
    from tests.api.test_builtin_seeding import _OLD_GP_DESCRIPTION, _old_gp_prompt

    with database.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT config FROM agents WHERE id = %s", ("general-purpose",))
        config = cur.fetchone()["config"]
        config = json.loads(config) if isinstance(config, str) else dict(config)
        config.pop("builtin_defaults", None)
        config["system_prompt"] = {"type": "custom", "content": _old_gp_prompt()}
        cur.execute("UPDATE agents SET description = %s, config = %s WHERE id = %s",
                    (_OLD_GP_DESCRIPTION, json.dumps(config), "general-purpose"))


def _op_count(worker):
    return worker.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE origin_device_id = ?",
                      (worker.device_id,))[0]["n"]


def _paired_on_old_text(workers):
    from app.core.builtin_subagents import seed_builtin_subagents

    a, b = workers("serverworker"), workers("desktopworker")
    with a.active():
        engine.create_network("Refresh")
        seed_builtin_subagents()
        _store_pre_pr_row()
        engine.scan_all()
    with b.active():
        seed_builtin_subagents()
    join(a, b)
    converge([a, b])
    with b.active():
        assert "Explore" in database.get_subagent("general-purpose")["description"]
    return a, b


@pytest.mark.parametrize("stale_clock_lead", [0, 50])
def test_stale_worker_refresh_never_beats_an_unreplicated_peer_edit(workers, stale_clock_lead):
    from app.core.builtin_subagents import GENERAL_PURPOSE_PROMPT, seed_builtin_subagents

    a, b = _paired_on_old_text(workers)

    # B is offline. A updates and refreshes; the owner then edits on A.
    with a.active():
        assert seed_builtin_subagents()["general-purpose"] == "refreshed"
        engine.scan_all()
        database.update_subagent("general-purpose", model="owner-choice")
        engine.scan_all()

    # B was busy while offline (its Lamport clock ran ahead), then boots the
    # new build before it has pulled from A.
    with b.active():
        with database.get_db() as conn:
            for _ in range(stale_clock_lead):
                store.bump_clock(conn.cursor())
        before = _op_count(b)
        assert seed_builtin_subagents()["general-purpose"] == "refreshed"
        engine.scan_all()
        assert _op_count(b) == before  # the refresh is not published
        assert database.get_subagent("general-purpose")["prompt"] == GENERAL_PURPOSE_PROMPT

    converge([a, b])
    for worker in (a, b):
        with worker.active():
            agent = database.get_subagent("general-purpose")
            assert agent["model"] == "owner-choice", worker.name
            assert agent["prompt"] == GENERAL_PURPOSE_PROMPT, worker.name


def test_refresh_on_every_worker_publishes_nothing_and_converges(workers):
    from app.core.builtin_subagents import GENERAL_PURPOSE_PROMPT, seed_builtin_subagents

    a, b = _paired_on_old_text(workers)
    counts = {}
    for worker in (a, b):
        with worker.active():
            counts[worker.name] = _op_count(worker)
            assert seed_builtin_subagents()["general-purpose"] == "refreshed"
            engine.scan_all()
            assert seed_builtin_subagents()["general-purpose"] == "exists"
    converge([a, b])
    for worker in (a, b):
        with worker.active():
            assert _op_count(worker) == counts[worker.name]
            assert database.get_subagent("general-purpose")["prompt"] == GENERAL_PURPOSE_PROMPT


def test_owner_edit_after_a_local_refresh_publishes_the_refreshed_row(workers):
    from app.core.builtin_subagents import GENERAL_PURPOSE_PROMPT, seed_builtin_subagents

    a, b = _paired_on_old_text(workers)
    with a.active():
        seed_builtin_subagents()
        engine.scan_all()
        database.update_subagent("general-purpose", model="owner-choice")
        engine.scan_all()
    converge([a, b])
    with b.active():  # B has not restarted on the new build yet
        agent = database.get_subagent("general-purpose")
        assert agent["model"] == "owner-choice"
        assert agent["prompt"] == GENERAL_PURPOSE_PROMPT
