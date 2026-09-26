"""Peers on older builds still send ``claude_accounts``; this build drops it quietly."""

from app.db import network as store
from app.network import engine, envelope as envelope_mod, peer_snapshot, policy
from tests.core.network.harness import exchange, join, owner_pref, set_owner_pref


def _publish_old_build_op(worker):
    """What an older build publishes when a Claude account row changes."""
    with worker.active():
        ctx = engine.context()
        with store.get_db() as conn:
            cursor = conn.cursor()
            store.lock_state(cursor)
            clock = store.bump_clock(cursor)
            seq = store.next_origin_seq(cursor, ctx.device_id, ctx.identity.incarnation)
            env = envelope_mod.build(
                network_id=ctx.network_id, collection="claude_accounts", record_id="acct-1",
                op=engine.OP_UPSERT, clock=clock, origin_device_id=ctx.device_id,
                origin_incarnation=ctx.identity.incarnation, origin_seq=seq, schema_version=1,
                key_version=ctx.key_version, network_key=ctx.network_key,
                signing_private=ctx.identity.signing_private,
                record={"id": "acct-1", "name": "Old", "credentials": "sealed"}, order_seq=None)
            store.insert_op(cursor, env.to_dict(), store.LANE_SETTINGS)


def test_retired_collection_is_not_registered():
    assert "claude_accounts" in policy.RETIRED_COLLECTIONS
    assert "claude_accounts" not in policy.COLLECTIONS
    assert policy.is_shared_setting_key("claude_cli_relogin_notice:desk-1234") is False


def test_old_build_ops_are_consumed_without_storing_or_forwarding(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _publish_old_build_op(a)
    set_owner_pref(a, "after", 7)

    result = exchange(a, b)

    assert result.rejected == 0
    assert owner_pref(b, "after") == 7  # the stream moved past the retired op
    assert not b.sql("SELECT * FROM sync_ops WHERE collection = 'claude_accounts'")
    assert not b.sql("SELECT * FROM sync_records WHERE collection = 'claude_accounts'")
    assert not b.sql("SELECT * FROM sync_pending")


def test_snapshot_pages_for_retired_or_unknown_collections_apply_nothing(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        retired = engine.snapshot_chunk("claude_accounts", 0)
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
    pages = [p for p in manifest["pages"] if p["collection"] == "claude_accounts"]
    assert pages == [dict(pages[0], count=0, more=False, offset=0)]
    with b.active():
        peer_snapshot.apply(manifest, copied, a.device_id)
    assert owner_pref(b, "copied") == 3
