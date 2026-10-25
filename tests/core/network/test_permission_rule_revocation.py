"""Permission revocations are tombstoned before a stale peer can answer back."""
from collections.abc import Callable

import pytest

from app.db import database
from app.db import network as store
from app.network import engine
from tests.core.network.harness import Worker, converge, exchange, join


@pytest.mark.parametrize("delete_all", [False, True])
def test_revocation_sync_and_stale_replay(
    workers: Callable[[str], Worker], delete_all: bool,
) -> None:
    a, b = workers("approval-owner"), workers("stale-peer")
    with a.active():
        engine.create_network("permission revocations")
        rules = [database.add_permission_rule("default", tool, None, "deny")
                 for tool in ("Bash", "Read")]
        engine.scan_all()
        stale_ops = store.ops_since({})
        stale_snapshot = engine.snapshot_chunk("permission_rules", 0)
    join(a, b)
    converge([a, b])
    # Revoke on the replica that did NOT originate the rules, without a scan.
    with b.active():
        if delete_all:
            assert database.delete_profile_permission_rules("default") == 2
        else:
            assert database.delete_permission_rule(rules[0]["id"], profile_id="default")
        with database.get_db() as conn:
            tombstone = store.get_record(conn.cursor(), "permission_rules", rules[0]["id"])
        assert tombstone["deleted"]
        engine.apply_batch(stale_ops)
        engine.apply_snapshot_chunk(stale_snapshot)
        assert database.get_permission_rule(rules[0]["id"]) is None
    exchange(b, a)
    converge([a, b])
    for worker in (a, b):
        with worker.active():
            remaining = database.get_permission_rules(profile_id="default")
            assert {r["id"] for r in remaining} == (set() if delete_all else {rules[1]["id"]})
