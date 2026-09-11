"""Real PostgreSQL migration contracts, opt-in and isolated from app configuration."""
import os
import uuid
from contextlib import contextmanager

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("NETWORK_POSTGRES_TEST") != "1", reason="requires disposable network PostgreSQL opt-in"),
]


@pytest.fixture
def _auto_db_shim():
    yield


@pytest.fixture
def pg(monkeypatch):
    if os.environ.get("TESTING") != "1" or os.environ.get("ALLOW_REAL_PG") != "1":
        pytest.fail("Explicit test-only PostgreSQL opt-in required")
    port = os.environ.get("NETWORK_PG_PORT", "5432")
    if port not in {"5432", "55456"}:
        pytest.fail("Unexpected disposable PostgreSQL port")
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor
    from pydantic_settings import DotEnvSettingsSource

    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_files", lambda self: {})
    from app.db import base
    from app.db.schema_postgres import create_postgres_schema
    from app.network import engine

    schemas = []

    def connect(schema=None):
        return psycopg2.connect(
            host="127.0.0.1", hostaddr="127.0.0.1", port=int(port),
            dbname="shuffle_network_test", user="shuffle_network_test", password="network-test-only",
            connect_timeout=5, sslmode="disable", cursor_factory=RealDictCursor,
            options="-c statement_timeout=10000 -c lock_timeout=5000 "
                    "-c idle_in_transaction_session_timeout=15000" + (f" -c search_path={schema}" if schema else ""),
        )

    def new_schema():
        schema = "network_test_" + uuid.uuid4().hex
        with connect() as conn:
            conn.cursor().execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        schemas.append(schema)
        with connect(schema) as conn:
            create_postgres_schema(conn.cursor())
        return schema

    @contextmanager
    def active(schema):
        @contextmanager
        def get_db():
            conn = connect(schema)
            try:
                with conn:
                    yield conn
            finally:
                conn.close()
        with monkeypatch.context() as scoped:
            scoped.setattr(base, "get_db", get_db)
            scoped.setattr(engine, "context", lambda: None)
            yield

    yield new_schema, connect, active
    with connect() as conn:
        for schema in schemas:
            conn.cursor().execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_export_uses_one_repeatable_read_snapshot(pg, monkeypatch):
    from scripts import network_migrate
    new_schema, connect, active = pg
    source = new_schema()
    with connect(source) as conn:
        conn.cursor().execute("INSERT INTO sessions (id,instance_id,title) VALUES ('session','lab','Before')")
    real_rows = network_migrate._rows
    changed = False

    def concurrent_rows(cursor, table, column, ids):
        nonlocal changed
        rows = real_rows(cursor, table, column, ids)
        if table == "sessions" and not changed:
            changed = True
            with connect(source) as writer:
                writer.cursor().execute("UPDATE sessions SET title='After' WHERE id='session'")
                writer.cursor().execute("INSERT INTO queued_messages (id,session_id,content) VALUES ('new','session','After')")
        return rows

    monkeypatch.setattr(network_migrate, "_rows", concurrent_rows)
    with active(source):
        bundle = network_migrate.export_bundle("lab")
    assert changed
    assert bundle["tables"]["sessions"][0]["title"] == "Before"
    assert bundle["tables"]["queued_messages"] == []
    with connect(source) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM queued_messages")
        assert cursor.fetchone()["n"] == 1


def test_postgres_migration_preserves_bytea_and_resume_state(pg):
    from scripts import network_migrate
    new_schema, connect, active = pg
    source, target = new_schema(), new_schema()
    with connect(source) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO sessions (id,instance_id,sdk_session_id,title) VALUES ('session','lab','sdk-lab','Source')")
        cursor.execute("INSERT INTO queued_messages (id,session_id,content) VALUES ('q','session','Queued')")
        cursor.execute("INSERT INTO connectors (id,type,name,config,events) VALUES ('c','telegram','C','{}','[]')")
        cursor.execute("INSERT INTO connector_sessions (id,connector_id,platform_chat_id,session_id) VALUES ('route','c','chat','session')")
        cursor.execute("INSERT INTO upload_blobs (id,session_id,source_path,filename,size,data) VALUES ('u','session','/old/a.txt','a.txt',3,%s)", (b"abc",))
    with active(source):
        bundle = network_migrate.export_bundle("lab")
    with connect(target) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO sessions (id,title) VALUES ('session','Synced title')")
        cursor.execute("INSERT INTO connectors (id,type,name,config,events) VALUES ('c','telegram','C','{}','[]')")
    with active(target):
        network_migrate.import_bundle(bundle)
        assert network_migrate.import_bundle(bundle)["queued_messages"] == 0
    with connect(target) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT sdk_session_id,title FROM sessions WHERE id='session'")
        assert dict(cursor.fetchone()) == {"sdk_session_id": "sdk-lab", "title": "Synced title"}
        cursor.execute("SELECT data FROM upload_blobs WHERE id='u'")
        assert bytes(cursor.fetchone()["data"]) == b"abc"
        cursor.execute("SELECT platform_chat_id FROM connector_sessions WHERE id='route'")
        assert cursor.fetchone()["platform_chat_id"] == "chat"


def test_cloud_receipt_upgrade_atomicity_and_exact_sequence_scope(pg, monkeypatch):
    from app.db import network as store

    new_schema, connect, _ = pg
    schema = new_schema()

    def envelope(seq):
        return {"origin_device_id": "origin", "origin_incarnation": 1, "origin_seq": seq,
                "collection": "test", "record_id": str(seq), "op": "upsert", "clock": seq}

    with connect(schema) as conn:
        cursor = conn.cursor()
        store.insert_op(cursor, envelope(1), "settings")
        # Simulate an existing installation, then run the additive migration twice.
        cursor.execute("DROP TABLE sync_hub_receipts")
        store.create_schema(cursor)
        store.create_schema(cursor)
        cursor.execute("SELECT COUNT(*) AS n FROM sync_ops")
        assert cursor.fetchone()["n"] == 1
    with pytest.raises(RuntimeError, match="after receipt"), connect(schema) as conn:
        cursor = conn.cursor()
        store.insert_op(cursor, envelope(2), "settings")
        store.record_hub_receipt(cursor, "hub-a", envelope(2))
        raise RuntimeError("after receipt")
    with connect(schema) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM sync_hub_receipts")
        assert cursor.fetchone()["n"] == 0
        cursor.execute("SELECT COUNT(*) AS n FROM sync_ops")
        assert cursor.fetchone()["n"] == 1
        # A downloaded successor can be pending before LAN supplies its prefix.
        store.record_hub_receipt(cursor, "hub-a", envelope(5))
    with connect(schema) as conn:
        store.insert_op(conn.cursor(), envelope(5), "settings")

    @contextmanager
    def isolated_db():
        conn = connect(schema)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(store, "get_db", isolated_db)
    assert [env for _, env in store.hub_ops_after_ingest(0, "hub-a")] == [envelope(1), None]
    assert [env for _, env in store.hub_ops_after_ingest(0, "hub-b")] == [envelope(1), envelope(5)]


def test_concurrent_hub_state_updates_preserve_cursors_and_cooldown(pg, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.db import network as store
    from app.network import hub

    new_schema, connect, _ = pg
    schema = new_schema()
    with connect(schema) as conn:
        store.create_state(network_id="test-network", name="Test", founder_device_id="origin", keyring={}, _conn=conn)

    @contextmanager
    def isolated_db():
        conn = connect(schema)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(store, "get_db", isolated_db)
    start = threading.Barrier(2)
    blocked = {"reason": "daily_upload", "retry_at": 123456789}

    def update(fields):
        start.wait(timeout=5)
        return hub._save_hub_state(**fields)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(update, [{"cursor": 2, "quota": {"ops": 2}},
                                   {"cursor": 1, "upload_blocked": blocked, "pushed_ingest_id": 10}]))
    assert hub._hub_state() == {"cursor": 2, "quota": {"ops": 2}, "upload_blocked": blocked, "pushed_ingest_id": 10}


def test_object_acknowledgement_and_outbox_removal_are_atomic(pg, monkeypatch):
    import json
    from types import SimpleNamespace

    from app.db import network as store
    from app.network import engine, object_sync

    new_schema, connect, _ = pg
    schema = new_schema()
    descriptor = {'object_id': 'test', 'op_count': 1}
    with connect(schema) as conn:
        store.create_state(network_id='test', name='Test', founder_device_id='origin', keyring={}, _conn=conn)
        store.update_state(_conn=conn, hub={'binding_scope': 'scope', 'pushed_ingest_id': 0})
        store.create_schema(conn.cursor())  # Upgrade remains repeatable with queued work.
        conn.cursor().execute('INSERT INTO sync_object_outbox(hub_scope,object_id,descriptor,payload,last_ingest_id) VALUES(%s,%s,%s,%s,%s)',
                              ('scope', 'test', json.dumps(descriptor), 'dGVzdA==', 7))
        store.create_schema(conn.cursor())

    @contextmanager
    def isolated_db():
        conn = connect(schema)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(store, 'get_db', isolated_db)
    monkeypatch.setattr(engine, 'require_context', lambda: None)
    client = SimpleNamespace(_call=lambda *args, **kw: {'committed': True, 'descriptor': descriptor})
    real_update = store.update_state

    def interrupted(**kwargs):
        real_update(**kwargs)
        raise RuntimeError('process interrupted after cursor update')

    monkeypatch.setattr(store, 'update_state', interrupted)
    with pytest.raises(RuntimeError, match='process interrupted'):
        object_sync.push(client, 'scope', {'object_sync': {'writes': True}})
    assert object_sync._outbox('scope')
    assert store.get_state()['hub']['pushed_ingest_id'] == 0
    monkeypatch.setattr(store, 'update_state', real_update)
    assert object_sync.push(client, 'scope', {'object_sync': {'writes': True}}) == 1
    assert object_sync._outbox('scope') is None
    assert store.get_state()['hub']['pushed_ingest_id'] == 7


def test_checkpoint_pages_share_the_same_postgres_snapshot(pg):
    import json
    from types import SimpleNamespace

    from app.db import network as store
    from app.network import crypto, engine

    new_schema, connect, _ = pg
    schema = new_schema()
    with connect(schema) as conn:
        for key in ('a', 'b'):
            conn.cursor().execute('INSERT INTO tags(id,name,color) VALUES(%s,%s,%s)', (key, 'Before', 'blue'))
            store.put_record(conn.cursor(), 'tags', key, clock=1, origin_device_id='origin', content_hash='hash', deleted=False)
    ctx = SimpleNamespace(network_id='test', device_id='origin', generation=1, key_version=1,
                          network_key=crypto.generate_network_key(), identity=SimpleNamespace(signing_private=crypto.generate_signing_key()))
    with connect(schema) as reader:
        reader.cursor().execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        first = engine.snapshot_chunk('tags', 0, ctx=ctx, limit=1, _conn=reader)
        with connect(schema) as writer:
            writer.cursor().execute("UPDATE tags SET name='After' WHERE id='b'")
        second = engine.snapshot_chunk('tags', 1, ctx=ctx, limit=1, _conn=reader)
    aad = crypto.canonical_json({'collection': 'tags', 'offset': 1, 'network_id': 'test'})
    body = json.loads(crypto.decrypt_payload(ctx.network_key, second['ciphertext'], aad))
    assert first['more'] and not second['more']
    assert body['items'][0]['record']['name'] == 'Before'
