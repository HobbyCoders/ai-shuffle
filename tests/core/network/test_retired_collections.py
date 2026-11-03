"""Peers on older builds still send retired collections; this build drops them quietly."""

import pytest

from app.db import network as store
from app.network import engine, envelope as envelope_mod, peer_snapshot, policy
from tests.core.network.harness import exchange, join, owner_pref, set_owner_pref


CHIP_ROW = {"id": "chip-1", "session_id": "chat-1", "origin_session_id": "chat-1", "origin_seq": 0,
            "request_id": "req-1", "title": "Old", "summary": "Old", "prompt": "Old", "base": "default",
            "model": "opus", "state": "pending", "attempt": 0}
OLD_BUILD_OPS = [("claude_accounts", "acct-1", {"id": "acct-1", "name": "Old", "credentials": "sealed"}),
                 ("session_task_chips", "chip-1", CHIP_ROW)]


def _publish_old_build_op(worker, collection, record_id, record):
    """What an older build publishes when a row of a collection retired here changes."""
    with worker.active():
        ctx = engine.context()
        with store.get_db() as conn:
            cursor = conn.cursor()
            store.lock_state(cursor)
            clock = store.bump_clock(cursor)
            seq = store.next_origin_seq(cursor, ctx.device_id, ctx.identity.incarnation)
            env = envelope_mod.build(
                network_id=ctx.network_id, collection=collection, record_id=record_id,
                op=engine.OP_UPSERT, clock=clock, origin_device_id=ctx.device_id,
                origin_incarnation=ctx.identity.incarnation, origin_seq=seq, schema_version=1,
                key_version=ctx.key_version, network_key=ctx.network_key,
                signing_private=ctx.identity.signing_private,
                record=record, order_seq=None)
            store.insert_op(cursor, env.to_dict(), store.LANE_SETTINGS)


@pytest.mark.parametrize("collection", ["claude_accounts", "session_task_chips"])
def test_retired_collection_is_not_registered(collection):
    assert collection in policy.RETIRED_COLLECTIONS
    assert collection not in policy.COLLECTIONS
    assert collection not in policy.CATALOG_COLLECTIONS


def test_a_retired_sign_in_key_is_not_shared():
    assert policy.is_shared_setting_key("claude_cli_relogin_notice:desk-1234") is False


@pytest.mark.parametrize("collection, record_id, record", OLD_BUILD_OPS, ids=["claude-accounts", "task-chips"])
def test_old_build_ops_are_consumed_without_storing_or_forwarding(workers, collection, record_id, record):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _publish_old_build_op(a, collection, record_id, record)
    set_owner_pref(a, "after", 7)

    result = exchange(a, b)

    assert result.rejected == 0
    assert owner_pref(b, "after") == 7  # the stream moved past the retired op
    assert not b.sql("SELECT * FROM sync_ops WHERE collection = ?", (collection,))
    assert not b.sql("SELECT * FROM sync_records WHERE collection = ?", (collection,))
    assert not b.sql("SELECT * FROM sync_pending")
    # An old peer's chip adds no row here, so it can raise no attention.
    assert not b.sql("SELECT * FROM session_task_chips")


@pytest.mark.parametrize("collection", ["claude_accounts", "session_task_chips"])
def test_snapshot_pages_for_retired_or_unknown_collections_apply_nothing(workers, collection):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        retired = engine.snapshot_chunk(collection, 0)
    assert retired["count"] == 0 and retired["more"] is False
    with b.active():
        assert engine.apply_snapshot_chunk(retired) == 0


def test_state_copy_serves_an_empty_retired_page_and_accepts_it(workers, tmp_path):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    set_owner_pref(a, "copied", 3)
    with a.active():
        manifest = peer_snapshot.capture(b.device_id)
        copied = tmp_path / "received"
        copied.mkdir()
        for index in range(len(manifest["pages"])):
            (copied / f"{index}.json").write_bytes(peer_snapshot.page(b.device_id, manifest["transfer"], index))
    for collection in policy.RETIRED_COLLECTIONS:
        pages = [p for p in manifest["pages"] if p["collection"] == collection]
        assert pages == [dict(pages[0], count=0, more=False, offset=0)]
    with b.active():
        peer_snapshot.apply(manifest, copied, a.device_id)
    assert owner_pref(b, "copied") == 3
