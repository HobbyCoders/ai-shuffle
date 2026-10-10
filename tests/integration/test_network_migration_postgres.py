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


def test_peer_schema_upgrade_preserves_ops_pending_and_sequence_scope(pg):
    from app.db import network as store

    new_schema, connect, _ = pg
    schema = new_schema()

    def envelope(seq, incarnation=1):
        return {"origin_device_id": "origin", "origin_incarnation": incarnation, "origin_seq": seq,
                "collection": "test", "record_id": str(seq), "op": "upsert", "clock": seq}

    with connect(schema) as conn:
        cursor = conn.cursor()
        store.insert_op(cursor, envelope(1), "settings")
        store.set_watermark(cursor, "origin", 1, 1)
        store.set_watermark(cursor, "origin", 2, 3)
        store.set_watermark(cursor, "other", 1, 7)
        store.stash_pending(cursor, envelope(5))
        # Recreate an absent index as on an older installation, without losing queued work.
        cursor.execute("DROP INDEX idx_sync_ops_record")
        store.create_schema(cursor)
        store.create_schema(cursor)
    with connect(schema) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT to_regclass('idx_sync_ops_record') AS name")
        assert cursor.fetchone()["name"] == "idx_sync_ops_record"
        cursor.execute("SELECT envelope FROM sync_ops")
        assert cursor.fetchone()["envelope"] == envelope(1)
        assert store.local_watermarks(conn) == {"origin": {"1": 1, "2": 3}, "other": {"1": 7}}
        assert store.get_watermark(cursor, "origin", 3) == 0
        cursor.execute("SELECT envelope FROM sync_pending")
        assert cursor.fetchone()["envelope"] == envelope(5)

    with pytest.raises(RuntimeError, match="after frontier"), connect(schema) as conn:
        cursor = conn.cursor()
        store.insert_op(cursor, envelope(2), "settings")
        store.set_watermark(cursor, "origin", 1, 2)
        raise RuntimeError("after frontier")
    with connect(schema) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM sync_ops")
        assert cursor.fetchone()["n"] == 1
        assert store.local_watermarks(conn) == {"origin": {"1": 1, "2": 3}, "other": {"1": 7}}


def test_concurrent_hub_registration_updates_preserve_unrelated_fields(pg, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.db import network as store
    from app.network import hub

    new_schema, connect, _ = pg
    schema = new_schema()
    with connect(schema) as conn:
        store.create_state(network_id="test-network", name="Test", founder_device_id="origin", keyring={}, _conn=conn)
        store.update_state(_conn=conn, hub={"binding_scope": "scope"})

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

    def update(fields):
        start.wait(timeout=5)
        return hub._save_hub_state(_expected_scope="scope", **fields)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(update, [{"registered": True, "registered_name": "Worker"},
                                   {"status": "active", "network_hub_id": "account-network"}]))
    expected = {"binding_scope": "scope", "registered": True, "registered_name": "Worker",
                "status": "active", "network_hub_id": "account-network"}
    assert hub._hub_state() == expected
    # Registration fields are current values, not monotonic transport cursors.
    hub._save_hub_state(_expected_scope="scope", registered=False, status="conflict")
    assert hub._hub_state() == {**expected, "registered": False, "status": "conflict"}


def test_hub_registration_update_rolls_back_and_rejects_stale_scope(pg, monkeypatch):
    from app.db import network as store
    from app.network import hub

    new_schema, connect, _ = pg
    schema = new_schema()
    initial = {"binding_scope": "scope", "registered": False}
    with connect(schema) as conn:
        store.create_state(network_id="test", name="Test", founder_device_id="origin", keyring={}, _conn=conn)
        store.update_state(_conn=conn, hub=initial)

    @contextmanager
    def isolated_db():
        conn = connect(schema)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(store, "get_db", isolated_db)
    real_update = store.update_state

    def interrupted(**kwargs):
        real_update(**kwargs)
        raise RuntimeError("process interrupted after registration update")

    monkeypatch.setattr(store, "update_state", interrupted)
    with pytest.raises(RuntimeError, match="process interrupted"):
        hub._save_hub_state(_expected_scope="scope", registered=True, registered_name="Worker")
    assert hub._hub_state() == initial
    monkeypatch.setattr(store, "update_state", real_update)
    assert hub._save_hub_state(_expected_scope="scope", registered=True) == {**initial, "registered": True}
    hub._save_hub_state(binding_scope="new-account", registered=False)
    with pytest.raises(hub.HubError, match="Account network changed"):
        hub._save_hub_state(_expected_scope="scope", registered=True, registered_name="Old account")
    assert hub._hub_state() == {"binding_scope": "new-account", "registered": False}


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


def test_peer_apply_rolls_back_records_and_frontier_together(pg, monkeypatch, tmp_path):
    import copy

    from app.db import network as store
    from app.network import engine, peer_snapshot
    from tests.core.network.harness import Worker, join

    # Real signed and encrypted input from two independent device identities.
    a, b = Worker('source', tmp_path), Worker('receiver', tmp_path)
    try:
        with a.active():
            engine.create_network('Test')
        join(a, b)
        with a.active():
            from app.db import database
            database.set_system_setting('tts_model', 'peer-value')
            engine.scan_all()
            manifest = peer_snapshot.capture(b.device_id)
            directory = tmp_path / 'received'
            directory.mkdir()
            for index in range(len(manifest['pages'])):
                (directory / f'{index}.json').write_bytes(peer_snapshot.page(b.device_id, manifest['transfer'], index))
        receiver = copy.deepcopy(b.context)
    finally:
        a.close()
        b.close()

    new_schema, connect, _ = pg
    schema = new_schema()
    with connect(schema) as conn:
        store.create_state(network_id=receiver.network_id, name='Test', founder_device_id=receiver.founder_device_id,
                           keyring={}, _conn=conn)

    @contextmanager
    def get_db():
        conn = connect(schema)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(store, 'get_db', get_db)
    monkeypatch.setattr(engine, 'require_context', lambda: receiver)
    monkeypatch.setattr(engine, 'load_context', lambda conn=None, **_: receiver)
    monkeypatch.setattr(engine, '_notify', lambda _: None)
    original = engine.apply_snapshot_chunk

    def interrupted(chunk, **kwargs):
        count = original(chunk, **kwargs)
        if chunk['collection'] == 'system_settings':
            raise RuntimeError('lost receiver process')
        return count

    monkeypatch.setattr(engine, 'apply_snapshot_chunk', interrupted)
    with pytest.raises(RuntimeError, match='lost receiver process'):
        peer_snapshot.apply(manifest, directory, a.device_id)
    with connect(schema) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS n FROM system_settings WHERE key='tts_model'")
        assert cursor.fetchone()['n'] == 0
        assert store.local_watermarks(conn) == {}


@pytest.mark.parametrize("page_limit", [8 * 1024 * 1024, 4096])
def test_peer_capture_keeps_concurrent_changes_out_of_its_signed_frontier(pg, monkeypatch, tmp_path, page_limit):
    import json
    from types import SimpleNamespace

    from app.core.config import settings
    from app.db import network as store
    from app.network import crypto, engine, peer_snapshot

    new_schema, connect, _ = pg
    schema = new_schema()
    key = crypto.generate_network_key()
    recipient_private = crypto.generate_encryption_key()
    ctx = SimpleNamespace(network_id='peer-test', device_id='origin', generation=1, key_version=1,
                          bootstrap_state=engine.BOOTSTRAP_READY, network_key=key, keyring={1: key},
                          key_candidates={}, key_recipients={}, key_generations={},
                          identity=SimpleNamespace(signing_private=crypto.generate_signing_key()),
                          member=lambda _: SimpleNamespace(status='active', encryption_public=crypto.encryption_public_key(recipient_private)))
    with connect(schema) as conn:
        store.create_state(network_id=ctx.network_id, name='Peer test', founder_device_id='origin',
                           keyring={}, _conn=conn)
        for index in range(101):
            name = f'tag-{index:03}'
            conn.cursor().execute('INSERT INTO tags(id,name,color) VALUES(%s,%s,%s)', (name, 'Before', 'blue'))
            store.put_record(conn.cursor(), 'tags', name, clock=1, origin_device_id='origin', content_hash='hash', deleted=False)

    @contextmanager
    def get_db():
        conn = connect(schema)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    monkeypatch.setattr(store, 'get_db', get_db)
    monkeypatch.setattr(engine, 'require_context', lambda: ctx)
    monkeypatch.setattr(engine, 'load_context', lambda conn, **kwargs: ctx)
    monkeypatch.setattr(engine, 'scan_all', lambda: None)
    monkeypatch.setattr(peer_snapshot, 'MAX_PAGE', page_limit)
    original = engine.snapshot_page
    changed = False

    def snapshot_chunk(collection, offset, **kwargs):
        nonlocal changed
        result = original(collection, offset, **kwargs)
        if collection == 'tags' and offset == 0 and not changed:
            with connect(schema) as writer:
                # The old capture held this lock until every page was written,
                # timing out real session creation and catalog publishers.
                store.lock_state(writer.cursor())
                writer.cursor().execute("UPDATE network_state SET clock=clock+1 WHERE id=1")
                writer.cursor().execute("UPDATE tags SET name='After' WHERE id='tag-100'")
            changed = True
        return result

    monkeypatch.setattr(engine, 'snapshot_page', snapshot_chunk)
    manifest = peer_snapshot.capture('recipient')
    assert changed
    items = []
    for index, page in enumerate(manifest['pages']):
        if page['collection'] != 'tags':
            continue
        assert page['bytes'] <= page_limit
        chunk = json.loads(peer_snapshot.page('recipient', manifest['transfer'], index))
        aad = crypto.canonical_json({'collection': 'tags', 'offset': page['offset'], 'network_id': ctx.network_id})
        body = json.loads(crypto.decrypt_payload(key, chunk['ciphertext'], aad))
        items.extend(body['items'])
    assert [item['record_id'] for item in items] == [f'tag-{i:03}' for i in range(101)]
    assert all(item['record']['name'] == 'Before' for item in items)
