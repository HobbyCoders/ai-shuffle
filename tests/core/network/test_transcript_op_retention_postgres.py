"""Real PostgreSQL producers, authenticated exchange, retention and state-copy recovery.

One private cluster, independent schemas/identities for every worker; never the
configured application database. The HTTP adapter runs the real peer router.
"""
import asyncio
import json
import threading
import time
from contextlib import closing, contextmanager
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from app.db import base, database, retention
from app.db import network as store
from app.db import session_events as events_db
from app.db.schema_postgres import create_postgres_schema
from app.network import (
    catalog,
    engine,
    hub,
    identity,
    peer_snapshot,
    transcripts,
    transport,
)
from tests.core.network.harness import _ACTIVE, Worker
from tests.core.network.test_transport import ServedBy
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("requires PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("sync-retention-pg")) as descriptor:
        yield descriptor


@pytest.fixture
def pg_workers(cluster, tmp_path, monkeypatch):
    schemas, made = [], []
    monkeypatch.setattr(peer_snapshot, "_jobs", {})
    monkeypatch.setattr(hub, "_binding", lambda: None)
    transcripts.reset_for_tests()

    class PgWorker(Worker):
        connect = None

        @contextmanager
        def active(self):
            with super().active():
                if self.connect is None:
                    yield self
                else:
                    # Context propagates through to_thread and nested HTTP calls.
                    with patch.object(base, "get_connection", lambda: (_ACTIVE.get() or self).connect()), \
                            patch.object(database, "get_connection", lambda: (_ACTIVE.get() or self).connect()), \
                            patch.object(base, "_close_connection", lambda conn: conn.close()):
                        yield self

        def sql(self, query, params=()):
            with self.active(), base.get_db() as conn:
                cur = conn.cursor()
                cur.execute(query, params)
                return [dict(row) for row in cur.fetchall()] if cur.description else []

    def make(name):
        worker = PgWorker(name, tmp_path)
        made.append(worker)
        schema = "sync_retention_" + uuid4().hex
        schemas.append(schema)
        with closing(pg_connect(cluster)) as admin:
            admin.cursor().execute("CREATE SCHEMA " + schema)

        def connect():
            conn = pg_connect(cluster)
            conn.cursor().execute("SET search_path TO " + schema)
            conn.cursor().execute("SET statement_timeout TO 10000")
            conn.autocommit = False
            return conn

        with closing(connect()) as conn, conn:
            create_postgres_schema(conn.cursor())
        worker.connect = connect
        with worker.active():
            database.create_profile(profile_id="default", name="Default Profile", description=None, config={})
        return worker

    try:
        yield make
    finally:
        for worker in made:
            worker.close()
        with closing(pg_connect(cluster)) as admin:
            for schema in schemas:
                admin.cursor().execute("DROP SCHEMA " + schema + " CASCADE")
        engine.reset_for_tests()
        transport.reset_for_tests()
        transcripts.reset_for_tests()


def publish(worker, count, *, create=False):
    with worker.active():
        if create:
            database.create_session(session_id="chat", profile_id="default", project_id=None, title="Chat")
        first = events_db.get_max_seq("chat") + 1
        events_db.append_events("chat", [
            {"type": "message.user", "payload": {"content": f"message {n}"}}
            for n in range(first, first + count)])
        catalog.publish_owned()


def admit(source, target):
    with source.active():
        response = engine.admit_device(device_id=target.device_id,
            signing_public=target.identity.signing_public,
            encryption_public=target.identity.encryption_public, name=target.name)
        initial = store.ops_since({}, limit=10000)
    with target.active():
        engine.enroll_from_response(response, device_name=target.name)
        engine.finish_bootstrap()
        applied = engine.apply_batch(initial)
        assert applied.rejected == applied.pending == applied.retry == 0


def old_ops(worker):
    worker.sql("UPDATE sync_ops SET created_at = '2000-01-01' WHERE collection = 'session_events'")


def op_count(worker):
    return worker.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE collection = 'session_events'")[0]["n"]


def transcript(worker):
    return worker.sql("SELECT seq, id, payload FROM session_events WHERE session_id = 'chat' ORDER BY seq")


def protected_rows(worker):
    # Canonical stores, including stores not participating in the sync log.
    return {table: sorted(json.dumps(row, sort_keys=True, default=str) for row in worker.sql("SELECT * FROM " + table))
            for table in ("session_events", "fluid_sessions", "fluid_session_nodes", "fluid_session_events",
                          "session_store_entries", "session_store_summaries", "sync_records")}


async def prune_once(worker, monkeypatch):
    # Drive the existing scheduled maintenance, not a test-only pruning route.
    calls = 0

    async def sleep(_delay):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError

    with worker.active(), monkeypatch.context() as scoped:
        scoped.setattr(transport.asyncio, "sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await transport.prune_loop()


async def sync(target, served):
    with target.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            return await transport.PeerLink(served.server.device_id, [served.base_url]).sync_once(client, engine.context())


@pytest.mark.parametrize("run", range(3))
async def test_transcript_op_count_is_bounded_without_deleting_content(pg_workers, monkeypatch, run):
    source = pg_workers("source")
    with source.active():
        engine.create_network("Retention")
    publish(source, 12, create=True)
    # Nonempty Fluid stores are sentinels: pruning is not transcript compaction.
    source.sql("INSERT INTO fluid_sessions VALUES ('fluid-chat', 'node', 1, NULL, NULL)")
    source.sql("INSERT INTO fluid_session_nodes VALUES ('fluid-chat', 'node', NULL, 1, %s)",
               (json.dumps({"content": "keep"}),))
    source.sql("INSERT INTO fluid_session_events VALUES ('fluid-chat', 1, %s)",
               (json.dumps({"content": "keep"}),))
    old_ops(source)
    before = protected_rows(source)
    assert op_count(source) == 12
    started = time.perf_counter()
    await prune_once(source, monkeypatch)
    elapsed = time.perf_counter() - started
    print(f"retention_lab run={run} before=12 after={op_count(source)} seconds={elapsed:.6f}")
    assert op_count(source) == 1  # only the origin/incarnation head anchor
    assert protected_rows(source) == before
    with source.active(), store.get_db() as conn:
        cur = conn.cursor()
        assert store.next_origin_seq(cur, source.device_id, source.identity.incarnation) > 12
    await prune_once(source, monkeypatch)
    assert op_count(source) == 1


async def test_long_offline_peer_automatically_recovers_then_resumes_op_sync(pg_workers, monkeypatch):
    source, online, offline = (pg_workers(name) for name in ("source", "online", "offline"))
    with source.active():
        engine.create_network("Retention")
    publish(source, 3, create=True)
    admit(source, online)
    admit(source, offline)
    served = ServedBy(source, "https://source.example.com")
    await sync(online, served)
    await sync(offline, served)
    offline_prefix = transcript(offline)
    publish(source, 12)
    await sync(online, served)
    # Only the NEXT request advertises the newly committed receive frontier.
    await sync(online, served)
    old_ops(source)
    # A real past authenticated contact, not last_seen (directory refreshes that).
    source.sql("INSERT INTO sync_peers(device_id, last_contact, updated_at) VALUES (%s, '2000-01-01', '2000-01-01')",
               (offline.device_id,))
    transport._peer_watermarks.pop(offline.device_id, None)
    before = protected_rows(source)
    await prune_once(source, monkeypatch)
    assert op_count(source) <= 1
    assert transcript(offline) == offline_prefix
    assert protected_rows(source) == before
    with source.active():
        assert store.requires_snapshot({source.device_id: {"1": 0}})
    # No receive_state call from the test: the ordinary exchange must select it.
    recovered = await sync(offline, served)
    assert recovered["snapshot_required"]
    monkeypatch.setattr(transcripts, "_link", lambda device_id: transport.PeerLink(
        device_id, [served.base_url] if device_id == source.device_id else []))
    with offline.active():
        assert transcripts.pending()
        async with httpx.AsyncClient(transport=served.transport()) as client:
            assert await transcripts.run_once(client)
        assert not transcripts.pending()
    assert transcript(offline) == transcript(source)
    assert [r["seq"] for r in transcript(offline)] == list(range(1, 16))
    assert len({r["id"] for r in transcript(offline)}) == 15
    assert any(path.endswith("/state") for _, path in served.requests)
    assert any(path.endswith("/transcripts") for _, path in served.requests)
    # LWW apply is idempotent and subsequent writes take the normal op path.
    served.requests.clear()
    publish(source, 2)
    assert not (await sync(offline, served)).get("snapshot_required")
    await sync(online, served)
    assert transcript(source) == transcript(online) == transcript(offline)
    assert len(transcript(offline)) == 17
    assert not any(path.endswith("/state") for _, path in served.requests)
    publish(source, 1)
    await prune_once(source, monkeypatch)
    assert op_count(source) <= 4  # one anchor plus writes younger than 30 days


async def test_recent_unknown_peer_pins_but_signed_revocation_unpins(pg_workers, monkeypatch):
    source, peer = pg_workers("source"), pg_workers("peer")
    with source.active():
        engine.create_network("Retention")
    admit(source, peer)
    publish(source, 12, create=True)
    old_ops(source)
    # No exchange since startup: missing in-memory frontiers fail closed.
    await prune_once(source, monkeypatch)
    assert op_count(source) == 12
    await prune_once(source, monkeypatch)
    assert op_count(source) == 12  # maintenance must not reset the grace baseline
    with source.active():
        engine.revoke_device(peer.device_id)
    before = protected_rows(source)
    await prune_once(source, monkeypatch)
    assert op_count(source) <= 1
    assert protected_rows(source) == before


async def test_time_floor_and_kill_switch(pg_workers, monkeypatch):
    source = pg_workers("source")
    with source.active():
        engine.create_network("Retention")
    publish(source, 6, create=True)
    await prune_once(source, monkeypatch)
    assert op_count(source) == 6  # recent ops are never eligible
    old_ops(source)
    monkeypatch.setenv("AI_SHUFFLE_PERF_TRANSCRIPT_OP_RETENTION", "0")
    await prune_once(source, monkeypatch)
    assert op_count(source) == 6
    monkeypatch.delenv("AI_SHUFFLE_PERF_TRANSCRIPT_OP_RETENTION")
    await prune_once(source, monkeypatch)
    assert op_count(source) == 1


async def test_recent_peer_minimum_and_restart_fail_closed(pg_workers, monkeypatch):
    source, online, lagging = (pg_workers(name) for name in ("source", "online", "lagging"))
    with source.active():
        engine.create_network("Retention")
    publish(source, 3, create=True)
    admit(source, online)
    admit(source, lagging)
    served = ServedBy(source, "https://source.example.com")
    await sync(lagging, served)
    publish(source, 12)
    await sync(online, served)
    await sync(online, served)
    old_ops(source)
    await prune_once(source, monkeypatch)
    assert op_count(source) == 12  # recent lagging peer pins its missing tail
    baseline = source.sql("SELECT * FROM sync_peers ORDER BY device_id")
    transport.forget_peers()  # process restart loses observations, not contact times
    await prune_once(source, monkeypatch)
    assert op_count(source) == 12
    assert source.sql("SELECT * FROM sync_peers ORDER BY device_id") == baseline
    # Directory activity is not authenticated exchange, and cannot pin forever.
    source.sql("UPDATE sync_peers SET last_contact = '2000-01-01'")
    source.sql("UPDATE network_members SET last_seen = NOW()")
    await prune_once(source, monkeypatch)
    assert op_count(source) == 1


def test_bounded_committed_batches_cancellation_and_metrics(pg_workers, monkeypatch, caplog):
    source = pg_workers("source")
    with source.active():
        engine.create_network("Retention")
    publish(source, 13, create=True)
    old_ops(source)
    before = protected_rows(source)
    stop = threading.Event()
    monkeypatch.setattr(retention, "BATCH_SIZE", 3)
    get_db = retention.get_db
    released = []

    @contextmanager
    def stop_after_commit():
        with get_db() as conn:
            yield conn
        released.append(conn.closed)
        stop.set()

    with source.active(), monkeypatch.context() as scoped, caplog.at_level("INFO", logger=store.__name__):
        scoped.setattr(retention, "get_db", stop_after_commit)
        assert store.prune_transcript_ops(source.device_id, {}, stop=stop) == 3
    assert released == [1]  # connection was committed/released before cancellation
    assert op_count(source) == 10
    metrics = json.loads(next(record.getMessage().removeprefix("Transcript sync operation retention ")
                              for record in caplog.records
                              if record.getMessage().startswith("Transcript sync operation retention ")))
    assert metrics["deleted_ops"] == 3 and metrics["bytes_estimate"] > 0
    assert metrics["peer_frontiers"] == {}
    assert metrics["retained_frontiers"][source.device_id]["1"] > 0
    with source.active():
        assert store.prune_transcript_ops(source.device_id, {}) == 9
    assert op_count(source) == 1
    assert protected_rows(source) == before


def test_missing_snapshot_materialization_is_not_pruned(pg_workers):
    source = pg_workers("source")
    with source.active():
        engine.create_network("Retention")
    publish(source, 4, create=True)
    old_ops(source)
    # Damaged/incomplete metadata is not evidence that an envelope is redundant.
    source.sql("DELETE FROM sync_records WHERE collection = 'session_events' AND record_id = 'chat|1'")
    source.sql("UPDATE sync_records SET order_seq = NULL WHERE collection = 'session_events' AND record_id = 'chat|2'")
    with source.active():
        assert store.prune_transcript_ops(source.device_id, {}) == 1
    assert op_count(source) == 3  # two unreconstructable records plus the head
    assert len(transcript(source)) == 4


async def test_new_peer_can_take_legacy_full_state_after_retention(pg_workers, monkeypatch):
    source, newcomer = pg_workers("source"), pg_workers("newcomer")
    with source.active():
        engine.create_network("Retention")
    publish(source, 8, create=True)
    old_ops(source)
    await prune_once(source, monkeypatch)
    assert op_count(source) == 1
    with source.active():
        response = engine.admit_device(device_id=newcomer.device_id,
            signing_public=newcomer.identity.signing_public,
            encryption_public=newcomer.identity.encryption_public, name="Newcomer")
    with newcomer.active():
        engine.enroll_from_response(response, device_name="Newcomer")
    served = ServedBy(source, "https://source.example.com")

    class LegacyLink(transport.PeerLink):
        async def _request_state(self, client, ctx, *, on_wait=None):
            # An old recipient asks for the pre-existing full-copy format.
            return await self.call(client, ctx, "POST", "/api/v1/network/peer/state", json_body={},
                                   params={"format": "1"})

    with newcomer.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            await LegacyLink(source.device_id, [served.base_url]).receive_state(client, engine.context())
        engine.finish_bootstrap()
        assert not transcripts.pending()
    assert transcript(newcomer) == transcript(source)
    assert len(transcript(newcomer)) == 8


async def test_never_contacted_peer_grace_expires_and_invalid_config_fails_closed(pg_workers, monkeypatch):
    source, peer = pg_workers("source"), pg_workers("peer")
    with source.active():
        engine.create_network("Retention")
    admit(source, peer)
    publish(source, 6, create=True)
    old_ops(source)
    await prune_once(source, monkeypatch)
    assert op_count(source) == 6
    source.sql("UPDATE sync_peers SET updated_at = '2000-01-01'")
    for invalid in ("0", "invalid"):
        monkeypatch.setenv("AI_SHUFFLE_SYNC_OP_PEER_GRACE_DAYS", invalid)
        await prune_once(source, monkeypatch)
        assert op_count(source) == 6
    monkeypatch.setenv("AI_SHUFFLE_SYNC_OP_PEER_GRACE_DAYS", "1")
    await prune_once(source, monkeypatch)
    assert op_count(source) == 1


async def test_legacy_prune_cannot_remove_a_superseded_transcript_stream_anchor(pg_workers, monkeypatch):
    source = pg_workers("source")
    with source.active():
        engine.create_network("Retention")
    publish(source, 2, create=True)
    with source.active(), store.get_db() as conn:
        cur = conn.cursor()
        next_seq = store.next_origin_seq(cur, source.device_id, 1)
        cur.execute("SELECT envelope FROM sync_ops WHERE collection = 'session_events' ORDER BY seq DESC LIMIT 1")
        env = dict(cur.fetchone()["envelope"])
        # Synthetic forwarding-log collision, not a wire/authentication fixture:
        # another stream can supersede the record that anchors this stream.
        env.update(origin_device_id="another-origin", origin_seq=1, clock=env["clock"] + 1)
        store.insert_op(cur, env, store.LANE_HISTORY)
    old_ops(source)
    before = protected_rows(source)
    await prune_once(source, monkeypatch)
    assert op_count(source) == 2  # one retained envelope per stream, not per record
    assert protected_rows(source) == before
    with source.active(), store.get_db() as conn:
        assert store.next_origin_seq(conn.cursor(), source.device_id, 1) == next_seq
    publish(source, 1)
    assert len(transcript(source)) == 3


async def test_superseded_non_transcript_head_preserves_signed_rollback_proof(pg_workers, monkeypatch):
    source, relay = pg_workers("source"), pg_workers("relay")
    with source.active():
        engine.create_network("Retention")
    admit(source, relay)
    publish(source, 3, create=True)
    with source.active(), store.get_db() as conn:
        backup_frontier = store.own_watermark(conn.cursor(), source.device_id, 1)

    # Later transcript events are followed by a signed session-header update.
    # That non-transcript envelope, not an event, is now A's stream head.
    publish(source, 4)
    with source.active():
        database.update_session("chat", title="Source header")
        catalog.publish_owned()
    head = source.sql(
        "SELECT seq, collection FROM sync_ops WHERE origin_device_id=%s "
        "AND incarnation=1 ORDER BY seq DESC LIMIT 1", (source.device_id,))[0]
    assert head["collection"] == "sessions" and head["seq"] > backup_frontier
    await sync(relay, ServedBy(source, "https://source.example.com"))
    with relay.active():
        database.update_session("chat", title="Newer replica header")
        with store.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM sessions WHERE id='chat'")
            newer = engine.record(cur, "sessions", "chat", cur.fetchone())
            assert newer and newer["origin_device_id"] == relay.device_id
        peer_heads = store.local_watermarks()
    relay.sql("UPDATE sync_ops SET created_at='2000-01-01' WHERE origin_device_id=%s", (source.device_id,))
    relay.sql("INSERT INTO sync_peers(device_id, last_contact) VALUES (%s, '2000-01-01') "
              "ON CONFLICT(device_id) DO UPDATE SET last_contact=EXCLUDED.last_contact", (source.device_id,))
    transport._peer_watermarks.pop(source.device_id, None)
    before = protected_rows(relay)
    await prune_once(relay, monkeypatch)
    assert protected_rows(relay) == before
    assert relay.sql("SELECT COUNT(*) AS n FROM sync_ops WHERE collection='session_events'")[0]["n"] == 0
    with relay.active(), store.get_db() as conn:
        cur = conn.cursor()
        proof = store.get_op(cur, source.device_id, 1, backup_frontier + 1, at_or_after=True)
        assert proof and proof["origin_seq"] == head["seq"]
        assert proof["collection"] == "sessions"
        assert store.next_origin_seq(cur, source.device_id, 1) == head["seq"] + 1
        assert store.local_watermarks(conn)[source.device_id] == peer_heads[source.device_id]

    # Simulate restoring A's outbox to the older backup frontier. Its identity
    # and canonical transcript rows stay intact; only test-owned log copies are
    # rewound, as in the existing transport restore-from-backup regression.
    source.sql("DELETE FROM sync_ops WHERE origin_device_id=%s AND incarnation=1 AND seq>%s",
               (source.device_id, backup_frontier))
    served = ServedBy(relay, "https://relay.example.com")
    with source.active():
        with store.get_db() as conn:
            assert store.own_watermark(conn.cursor(), source.device_id, 1) == backup_frontier
        async with httpx.AsyncClient(transport=served.transport()) as client:
            link = transport.PeerLink(relay.device_id, [served.base_url])
            assert await link._check_rollback(client, engine.context(), peer_heads)
        assert identity.load().incarnation == 2
        assert engine.context().identity.incarnation == 2
        with store.get_db() as conn:
            # Rollback recovery publishes presence and the local member name.
            assert store.next_origin_seq(conn.cursor(), source.device_id, 2) == 3
        database.set_system_setting("tts_model", "after-restore")
        engine.scan_collection("system_settings")
        with store.get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT seq, envelope FROM sync_ops WHERE origin_device_id=%s AND incarnation=2 ORDER BY seq",
                        (source.device_id,))
            fresh = [dict(row) for row in cur.fetchall()]
    assert [row["seq"] for row in fresh] == [1, 2, 3]
    assert [row["envelope"]["collection"] for row in fresh] == ["presence", "member_names", "system_settings"]
    summary = await sync(relay, ServedBy(source, "https://source.example.com"))
    assert summary["received"] == 3
    with relay.active():
        assert database.get_system_setting("tts_model") == "after-restore"
        assert store.local_watermarks()[source.device_id]["2"] == 3
    assert len(transcript(relay)) == 7
