"""Credential merge SQL against a disposable PostgreSQL, never the app DB."""
import base64
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, nullcontext
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core import codex_token_refresh, encryption, xai_token_refresh
from app.db import database, model_providers
from app.db import network as store
from app.db.schema_postgres import create_postgres_schema
from app.network import engine, policy
from tests.core.network.harness import join
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres
from tests.fakes.http import mock_transport_client


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("requires PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("credential-merge-pg")) as descriptor:
        with closing(pg_connect(descriptor)) as conn, conn:
            create_postgres_schema(conn.cursor())
        yield descriptor


def bundle(provider, stamp, generation):
    return {"id": provider, "name": "Original", "provider_type": "openai", "base_url": "https://example.com",
            "auth_mode": "chatgpt", "codex_last_refresh": stamp, "codex_access_token": "access-" + generation,
            "codex_refresh_token": "refresh-" + generation, "codex_account_id": "account-" + generation,
            "codex_id_token_expires_at": stamp, "needs_reauth": False}


@pytest.mark.parametrize("losing_row", [False, True])
def test_timestamp_comparison_and_atomic_write(cluster, losing_row):
    coll = policy.collection("model_providers")
    provider = "pg-losing" if losing_row else "pg-winning"
    # Lexically later, but chronologically older. PostgreSQL must compare
    # instants, not strings (the in-memory network harness cannot prove this).
    old = bundle(provider, "2026-10-19T14:00:00+04:00", "old")
    newer = bundle(provider, "2026-10-19T12:00:00+00:00", "new")
    with closing(pg_connect(cluster)) as initial:
        engine.materialize(initial.cursor(), coll, provider, old)
    started = threading.Event()

    def incoming_write():
        with closing(pg_connect(cluster)) as conn:
            conn.cursor().execute("SET statement_timeout TO 10000")
            started.set()
            if losing_row:
                assert engine._merge_losing_provider_credentials(
                    conn.cursor(), coll, provider, newer, {"deleted": False})
                return engine._read_row(conn.cursor(), coll, provider)
            return engine.materialize(conn.cursor(), coll, provider, {**old, "name": "Edited"})

    with closing(pg_connect(cluster)) as rotating, ThreadPoolExecutor(max_workers=1) as pool:
        rotating.autocommit = False
        # Hold a real row lock while the incoming operation starts. The
        # guarded write must evaluate against the committed row, not a stale
        # read from before the rotation.
        rotating.cursor().execute("SELECT id FROM model_providers WHERE id = %s FOR UPDATE", (provider,))
        if not losing_row:
            engine.materialize(rotating.cursor(), coll, provider, newer)
        future = pool.submit(incoming_write)
        try:
            assert started.wait(5)
        finally:
            rotating.commit()
        row = future.result(timeout=15)
    assert row["codex_refresh_token"] == "refresh-new"
    assert row["codex_access_token"] == "access-new"
    assert row["codex_account_id"] == "account-new"
    assert row["codex_last_refresh"] == datetime(2026, 10, 19, 12, tzinfo=UTC)
    assert row["name"] == ("Original" if losing_row else "Edited")

@pytest.mark.parametrize("delivery", ["envelope", "snapshot"])
@pytest.mark.parametrize("session_zone", ["UTC", "America/New_York"])
def test_applied_bundle_does_not_echo_across_timezones(cluster, workers, monkeypatch, delivery, session_zone):
    coll = policy.collection("model_providers")
    provider = f"pg-echo-{delivery}-{session_zone}"
    sender = workers("sender")
    with sender.active():
        ctx = engine.create_network("Timezone regression")
        with closing(pg_connect(cluster)) as source, source:
            source.cursor().execute("SET TIME ZONE 'UTC'")
            row = engine.materialize(source.cursor(), coll, provider,
                                     bundle(provider, "2026-10-19T12:00:00.123456+00:00", "new"))
            record = engine.serialize_row(coll, row)
        with engine.get_db() as conn:
            env = engine.record(conn.cursor(), coll.name, provider, row, ctx=ctx)
        assert env is not None
        env = engine.Envelope.from_dict(env)
        with closing(pg_connect(cluster)) as conn, conn:
            cursor = conn.cursor()
            cursor.execute("SELECT set_config('TimeZone', %s, false)", (session_zone,))
            store.create_state(network_id=ctx.network_id, name=ctx.name,
                               founder_device_id=ctx.founder_device_id, keyring={}, _conn=conn)
            monkeypatch.setattr(engine, "get_db", lambda: nullcontext(conn))
            if delivery == "envelope":
                result = engine.ApplyResult()
                assert engine._apply_in_order(cursor, ctx, env, ctx.member(ctx.device_id), result) == "applied"
            else:
                applied, _ = engine.apply_snapshot_items(cursor, coll, [{
                    "record_id": provider, "record": record, "clock": env.clock,
                    "origin_device_id": env.origin_device_id, "deleted": False,
                }], ctx=ctx)
                assert applied == 1
            assert engine.scan_collection(coll.name, ctx=ctx, record_ids=[provider]) == 0


@pytest.mark.parametrize("initial_reason", ["last_refresh_missing", "8_day", "exp_within_skew"])
@pytest.mark.parametrize("worker,mode", [(codex_token_refresh, "chatgpt"), (xai_token_refresh, "grok")])
@pytest.mark.parametrize("delivery", ["envelope", "snapshot"])
@pytest.mark.parametrize("session_zone", ["UTC", "America/New_York"])
async def test_rotation_survives_next_sweeps_and_peer_echo(
    cluster, workers, monkeypatch, caplog, worker, mode, delivery, session_zone, initial_reason,
):
    """Real TIMESTAMPTZ reads, rotation/lease SQL and sync, not shim strings."""
    coll = policy.collection("model_providers")
    provider = f"pg-sweep-{mode}-{delivery}-{session_zone}-{initial_reason}"
    authority, sender = workers("refresh-authority"), workers("echo-peer")
    with authority.active():
        engine.create_network("Refresh cadence regression")
    join(authority, sender)
    with authority.active():
        ctx = engine.context()
    # Membership is supplied by the network harness; the authority decision
    # itself is real. All provider reads/writes below use private PostgreSQL.
    monkeypatch.setattr(engine, "load_context", lambda **kwargs: ctx)
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    worker._locks.clear()
    caplog.set_level(logging.INFO, logger=worker.__name__)
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    monkeypatch.setattr(worker, "_now", lambda: now)

    @contextmanager
    def provider_db():
        with closing(pg_connect(cluster)) as conn:
            conn.autocommit = False
            with conn:
                conn.cursor().execute("SELECT set_config('TimeZone', %s, false)", (session_zone,))
                yield conn

    monkeypatch.setattr(model_providers, "get_db", provider_db)
    monkeypatch.setattr(engine, "get_db", provider_db)

    def jwt(exp):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp.timestamp())}).encode()).rstrip(b"=").decode()
        return f"e30.{payload}.sig"

    with provider_db() as conn:
        # Isolate each parametrized sweep from prior cases in this cluster.
        conn.cursor().execute("UPDATE model_providers SET enabled = FALSE")
        store.create_state(network_id=ctx.network_id, name=ctx.name,
                           founder_device_id=ctx.founder_device_id, keyring={}, _conn=conn)
    model_providers.create_model_provider(provider, "Sweep regression", "grok" if mode == "grok" else "openai",
                                          "https://example.com")
    model_providers.set_codex_token_bundle(
        provider, access_token=encryption.encrypt_value(jwt(now + timedelta(minutes=10))),
        refresh_token=encryption.encrypt_value("fake-refresh-0"), account_id="fake-account",
        last_refresh=(now - (timedelta(days=8) if initial_reason == "8_day" else timedelta(hours=1))).isoformat(),
        auth_mode=mode,
    )
    if initial_reason == "last_refresh_missing":
        # Legacy bundles may lack a stamp; the rotation must repair it.
        with provider_db() as conn:
            conn.cursor().execute("UPDATE model_providers SET codex_last_refresh = NULL WHERE id = %s", (provider,))
    requests = []

    def endpoint(request):
        requests.append(request)
        return httpx.Response(200, json={
            "access_token": jwt(now + timedelta(hours=1)),
            "refresh_token": f"fake-refresh-{len(requests)}",
        })

    async with mock_transport_client(endpoint) as client:
        assert await worker.refresh_due_providers(client=client) == 1
        assert len(requests) == 1
        assert f"reason={initial_reason}" in caplog.text
        rotated = model_providers.get_codex_tokens(provider)
        assert isinstance(rotated["codex_last_refresh"], datetime)
        assert rotated["codex_last_refresh"] == now
        assert not rotated["needs_reauth"]
        assert encryption.decrypt_value(rotated["codex_refresh_token"]) == "fake-refresh-1"
        assert worker._access_token_exp(encryption.decrypt_value(rotated["codex_access_token"])) == now + timedelta(hours=1)

        assert not worker._needs_refresh(rotated, encryption.decrypt_value(rotated["codex_access_token"]))

        # A peer republishes the row produced by the real rotation. Record the
        # echo in the harness DB, so the receiving PostgreSQL has not seen it.
        with provider_db() as conn:
            row = engine._read_row(conn.cursor(), coll, provider)
            record = engine.serialize_row(coll, row)
        with sender.active(), database.get_db() as conn:
            env = engine.Envelope.from_dict(engine.record(conn.cursor(), coll.name, provider, row, ctx=sender.context))
        assert env.origin_device_id != ctx.device_id
        with provider_db() as conn:
            if delivery == "envelope":
                assert engine._apply_in_order(conn.cursor(), ctx, env, ctx.member(env.origin_device_id), engine.ApplyResult()) == "applied"
            else:
                applied, _ = engine.apply_snapshot_items(conn.cursor(), coll, [{
                    "record_id": provider, "record": record, "clock": env.clock,
                    "origin_device_id": env.origin_device_id, "deleted": False,
                }], ctx=ctx)
                assert applied == 1
        echoed = model_providers.get_codex_tokens(provider)
        for field in rotated:
            if field in ("codex_access_token", "codex_refresh_token"):
                assert encryption.decrypt_value(echoed[field]) == encryption.decrypt_value(rotated[field])
            else:
                assert echoed[field] == rotated[field]
        assert engine.scan_collection(coll.name, ctx=ctx, record_ids=[provider]) == 0

        for minutes in range(5, 45, 5):
            now = datetime(2026, 10, 20, 12, tzinfo=UTC) + timedelta(minutes=minutes)
            stored = model_providers.get_codex_tokens(provider)
            assert not worker._needs_refresh(stored, encryption.decrypt_value(stored["codex_access_token"]))
            assert await worker.refresh_due_providers(client=client) == 0
            assert len(requests) == 1
        now = datetime(2026, 10, 20, 12, 45, tzinfo=UTC)
        assert await worker.refresh_due_providers(client=client) == 1
        assert len(requests) == 2
        now += timedelta(minutes=5)
        assert await worker.refresh_due_providers(client=client) == 0
        assert len(requests) == 2
    assert "reason=exp_within_skew" in caplog.text
    assert "fake-refresh-" not in caplog.text
