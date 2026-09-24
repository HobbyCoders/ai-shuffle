"""Opt-in PostgreSQL contracts; never use the application's configured pool.

Run only this file with --confcutdir=tests/integration and the workflow's
explicit MOBILE_PG_* environment. No existing schema is modified or removed.
"""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MOBILE_POSTGRES_TEST") != "1",
        reason="requires explicit disposable mobile PostgreSQL service opt-in",
    ),
]


@pytest.fixture
def _auto_db_shim():
    """Override the root SQLite autouse fixture for this module only."""
    yield


@pytest.fixture
def pg(monkeypatch, tmp_path):
    expected = {
        "TESTING": "1", "ALLOW_REAL_PG": "1",
        "MOBILE_PG_HOST": "127.0.0.1",
        "MOBILE_PG_DATABASE": "shuffle_mobile_test",
        "MOBILE_PG_USER": "shuffle_mobile_test",
        "MOBILE_PG_PASSWORD": "mobile-test-only",
    }
    port_text = os.environ.get("MOBILE_PG_PORT", "")
    if not port_text.isascii() or not port_text.isdecimal() or not 1024 <= int(port_text) <= 65535:
        pytest.fail("Refusing PostgreSQL: explicit unprivileged loopback test port required")
    port = int(port_text)
    if any(os.environ.get(key) != value for key, value in expected.items()):
        pytest.fail("Refusing PostgreSQL: dedicated localhost test environment required")

    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor
    from pydantic_settings import DotEnvSettingsSource

    # Even if invoked outside the dedicated workflow, do not read any .env.
    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_files", lambda self: {})
    from app.db import mobile, mobile_hub as hub, mobile_tools as queue, pairing
    from app.core import config

    # Isolate the filesystem as well as SQL, but do NOT pin/warm the identity:
    # concurrent bind must exercise real first-use identity publication.
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config.settings, "instance_id", None)
    monkeypatch.setattr(config, "_cached_persisted_instance_id", None)

    schema = "mobile_test_" + uuid.uuid4().hex

    def connect():
        # Only the explicit test port varies; never consult app DB configuration.
        return psycopg2.connect(
            host="127.0.0.1", hostaddr="127.0.0.1", port=port,
            dbname="shuffle_mobile_test", user="shuffle_mobile_test",
            password="mobile-test-only", connect_timeout=5, sslmode="disable",
            options="-c statement_timeout=10000 -c lock_timeout=5000 -c timezone=America/New_York "
                    "-c idle_in_transaction_session_timeout=15000 "
                    f"-c search_path={schema}",
            cursor_factory=RealDictCursor,
        )

    local = threading.local()

    @contextmanager
    def transaction():
        conn = connect()
        try:
            barrier = getattr(local, "barrier", None)
            if barrier is not None:
                local.barrier = None
                local.pid = conn.get_backend_pid()
                barrier.wait(timeout=10)
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(hub, "get_db", transaction)
    monkeypatch.setattr(mobile, "get_db", transaction)
    monkeypatch.setattr(queue, "get_db", transaction)
    monkeypatch.setattr(pairing, "get_db", transaction)
    with transaction() as conn:
        c = conn.cursor()
        c.execute("SELECT current_database() AS db, current_user AS usr, "
                  "inet_server_addr()::text AS addr")
        target = c.fetchone()
        assert target["db"] == target["usr"] == "shuffle_mobile_test"
        assert conn.get_dsn_parameters()["hostaddr"] == "127.0.0.1"
        # No IF NOT EXISTS: a collision must fail, never adopt existing data.
        c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        c.execute("""
            CREATE TABLE system_settings (
                key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMPTZ DEFAULT NOW());
            CREATE TABLE paired_clients (
                id TEXT PRIMARY KEY, device_id TEXT NOT NULL UNIQUE,
                device_name TEXT NOT NULL, platform TEXT NOT NULL,
                client_type TEXT NOT NULL DEFAULT 'browser',
                scopes JSONB NOT NULL DEFAULT '["controller"]',
                session_token_hash TEXT, session_expires_at TIMESTAMPTZ,
                revoked_at TIMESTAMPTZ, last_seen TIMESTAMPTZ,
                paired_at TIMESTAMPTZ DEFAULT NOW(),
                created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW());
            CREATE TABLE mobile_devices (
                id TEXT PRIMARY KEY, device_id TEXT NOT NULL UNIQUE,
                device_name TEXT NOT NULL, platform TEXT NOT NULL,
                api_key_hash TEXT NOT NULL, capabilities JSONB DEFAULT '{}',
                paired_client_id TEXT, api_key_expires_at TIMESTAMPTZ,
                scopes JSONB NOT NULL DEFAULT '["controller"]',
                paired_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT NOW(),
                last_seen TIMESTAMPTZ, revoked_at TIMESTAMPTZ);
            CREATE TABLE paired_client_native_sessions (
                client_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL);
            CREATE TABLE paired_client_web_reservations (
                client_id TEXT PRIMARY KEY REFERENCES paired_clients(id) ON DELETE CASCADE,
                mobile_record_id TEXT NOT NULL, api_key_hash TEXT NOT NULL,
                reservation_hash TEXT NOT NULL UNIQUE, expires_at TIMESTAMPTZ NOT NULL);
            CREATE TABLE pairing_grants (
                id TEXT PRIMARY KEY, token_hash TEXT UNIQUE, purpose TEXT, scopes JSONB,
                target_install_id TEXT, endpoint_url TEXT, device_label TEXT, created_by TEXT,
                created_at TIMESTAMPTZ, expires_at TIMESTAMPTZ, consumed_at TIMESTAMPTZ,
                revoked_at TIMESTAMPTZ, attempt_count INTEGER, max_attempts INTEGER);
        """)
        hub.create_schema(c)
    queue.init_schema()

    def rows(table):
        with transaction() as conn:
            c = conn.cursor()
            c.execute(sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table)))
            return [dict(row) for row in c.fetchall()]

    def race(*calls):
        barrier = threading.Barrier(len(calls))

        def run(call):
            local.barrier = barrier
            value = call()
            return local.pid, value

        with ThreadPoolExecutor(max_workers=len(calls)) as executor:
            futures = [executor.submit(run, call) for call in calls]
            results = [future.result(timeout=30) for future in futures]
        assert len({pid for pid, _ in results}) == len(calls)
        return [value for _, value in results]

    # Leave only our UUID schema behind; the disposable service owns cleanup.
    return SimpleNamespace(hub=hub, mobile=mobile, queue=queue, pairing=pairing, transaction=transaction,
                           rows=rows, race=race, integrity_error=psycopg2.IntegrityError,
                           config=config, id_file=tmp_path / ".instance_id")


def enroll(pg, device="device-one", account="account-one", key="test-key-hash"):
    identity = dict(device_id=device, device_name="Test phone",
                    account_id=account, server_id="server-one")
    info = SimpleNamespace(platform="android", capabilities=SimpleNamespace(model_dump=lambda: {}))
    return pg.hub.enroll(identity, info, key,
                         datetime.now(timezone.utc) + timedelta(hours=1), ["controller"])


def ready_command(pg):
    device = enroll(pg)
    pg.queue.capabilities(device, True, ["get_location"])
    assert pg.queue.claim(device) == []  # real heartbeat, no wall-clock sleeps
    command = pg.queue.enqueue(device, "session-one", "get_location", '{"accuracy":"low"}', 300)
    assert "id" in command
    return device, command["id"]


def test_concurrent_claim_delivers_once(pg):
    device, command = ready_command(pg)
    deliveries = pg.race(*(lambda: pg.queue.claim(device) for _ in range(4)))
    assert [row["id"] for batch in deliveries for row in batch] == [command]
    assert pg.queue.claim(device) == []
    assert pg.rows("mobile_tool_commands")[0]["status"] == "claimed"


def test_duplicate_result_is_idempotent_and_conflicting_result_rejected(pg):
    device, command = ready_command(pg)
    assert len(pg.queue.claim(device)) == 1
    result = '{"latitude":1}'
    assert pg.race(lambda: pg.queue.submit_result(device, command, result),
                   lambda: pg.queue.submit_result(device, command, result)) == [None, None]
    before = pg.rows("mobile_tool_commands")
    assert pg.queue.submit_result(device, command, result) is None
    assert pg.queue.submit_result(device, command, '{"latitude":2}') == "result_conflict"
    assert pg.rows("mobile_tool_commands") == before
    assert before[0]["status"] == "completed"
    assert before[0]["result"] == result
    assert before[0]["arguments"] == "{}"


def test_unchanged_capabilities_preserve_claim_and_heartbeat(pg):
    device, command = ready_command(pg)
    assert len(pg.queue.claim(device)) == 1
    before = pg.rows("mobile_tool_commands")
    heartbeat = pg.queue.state(device)["heartbeat_at"]
    pg.queue.capabilities(device, True, ["get_location"])
    assert pg.rows("mobile_tool_commands") == before
    assert pg.queue.state(device)["heartbeat_at"] == heartbeat
    assert pg.queue.submit_result(device, command, '{}') is None


TABLES = ("mobile_devices", "paired_clients", "mobile_hub_authorizations")


def attempt_enroll(pg, **kwargs):
    try:
        return enroll(pg, **kwargs)
    except (ValueError, pg.integrity_error):
        return "rejected"


def test_concurrent_enrollment_creates_exactly_one_consistent_triple(pg):
    results = pg.race(lambda: attempt_enroll(pg, key="first-test-hash"),
                      lambda: attempt_enroll(pg, key="second-test-hash"))
    assert sorted(results) == ["device-one", "rejected"]
    device, client, auth = [pg.rows(table) for table in TABLES]
    assert len(device) == len(client) == len(auth) == 1
    assert device[0]["device_id"] == client[0]["device_id"] == auth[0]["device_id"] == "device-one"
    assert device[0]["paired_client_id"] == client[0]["id"] == auth[0]["paired_client_id"]
    assert auth[0]["account_id"] == "account-one"
    winner = results.index("device-one")
    assert device[0]["api_key_hash"] == ("first-test-hash", "second-test-hash")[winner]


def test_existing_different_identity_is_not_overwritten(pg):
    enroll(pg)
    before = [pg.rows(table) for table in TABLES]
    assert pg.race(lambda: attempt_enroll(pg, account="other-account", key="other-hash"),
                   lambda: attempt_enroll(pg, account="third-account")) == ["rejected", "rejected"]
    assert [pg.rows(table) for table in TABLES] == before


def test_authorization_conflict_rolls_back_both_preceding_inserts(pg):
    # A retained authorization collides on hub_device_id at the THIRD insert.
    with pg.transaction() as conn:
        conn.cursor().execute("""INSERT INTO mobile_hub_authorizations
            VALUES ('tombstone', 'old-client', 'old-account', 'device-one', 'server-one')""")
    before = pg.rows("mobile_hub_authorizations")
    with pytest.raises(pg.integrity_error):
        enroll(pg)
    assert pg.rows("mobile_devices") == []
    assert pg.rows("paired_clients") == []
    assert pg.rows("mobile_hub_authorizations") == before


def test_concurrent_immutable_bind_preserves_winners_secret(pg):
    assert pg.config._cached_persisted_instance_id is None
    assert not pg.id_file.exists()
    candidates = [
        ("https://first.invalid", "server-first", "enc:fake-first", "account-first"),
        ("https://second.invalid", "server-second", "enc:fake-second", "account-second"),
    ]
    first, second = pg.race(*(lambda args=args: pg.hub.bind(*args) for args in candidates))
    assert first == second == pg.hub.get_binding()
    assert first["instance_id"] == pg.id_file.read_text().strip()
    assert pg.rows("system_settings")[0]["key"] == "mobile_account_hub_secret:" + first["instance_id"]
    winner = next(args for args in candidates if args[1] == first["server_id"])
    assert (first["hub_url"], first["server_id"], first["encrypted_secret"], first["linked_by"]) == winner
    assert len(pg.rows("mobile_worker_bindings")) == len(pg.rows("system_settings")) == 1
    assert pg.hub.bind("https://third.invalid", "server-third", "enc:fake-third", "other") == first


def web_reservation_fixture(pg):
    enroll(pg)
    with pg.transaction() as conn:
        conn.cursor().execute('UPDATE mobile_devices SET scopes=\'["controller", "web:session"]\'')
    device = pg.rows('mobile_devices')[0]
    client_id = device['paired_client_id']
    expiry = datetime.now(timezone.utc) + timedelta(minutes=2)
    assert pg.pairing.set_paired_client_session(client_id, 'original-cookie', expiry, mobile_device=device)
    return device, client_id, expiry


@pytest.mark.parametrize('first', ['A', 'B'])
def test_reserved_web_commit_order_never_allows_older_mint_to_win(pg, monkeypatch, first):
    device, client_id, expiry = web_reservation_fixture(pg)
    for label in ('A', 'B'):
        assert pg.pairing.reserve_paired_client_web_session(client_id, 'reservation-' + label, expiry, mobile_device=device)
    held = threading.Event()
    attempted = threading.Event()
    acquired_second = threading.Event()
    release = threading.Event()
    local = threading.local()
    original = pg.pairing._lock_current_mobile_client

    def controlled(*args):
        if local.label != first:
            attempted.set()
        value = original(*args)
        if local.label == first:
            held.set()
            assert release.wait(timeout=10)
        else:
            acquired_second.set()
        return value

    monkeypatch.setattr(pg.pairing, '_lock_current_mobile_client', controlled)

    def commit(label):
        local.label = label
        try:
            value = pg.pairing.set_paired_client_session(client_id, 'cookie-' + label, expiry,
                mobile_device=device, web_reservation_hash='reservation-' + label)
            assert value
            return 'committed'
        except pg.pairing.WebSessionReservationConflict:
            return 'superseded'

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(commit, first)
        try:
            assert held.wait(timeout=5)
            second = 'B' if first == 'A' else 'A'
            loser = executor.submit(commit, second)
            assert attempted.wait(timeout=5)
            assert not acquired_second.wait(timeout=0.2)
        finally:
            release.set()
        results = {first: winner.result(timeout=10), second: loser.result(timeout=10)}
    assert results == {'A': 'superseded', 'B': 'committed'}
    assert pg.rows('paired_clients')[0]['session_token_hash'] == 'cookie-B'
    assert pg.rows('paired_client_web_reservations') == []


def test_late_reservation_and_replaced_mobile_snapshot_cannot_invalidate_web(pg):
    device, client_id, expiry = web_reservation_fixture(pg)
    assert pg.pairing.reserve_paired_client_web_session(client_id, 'reservation-B', expiry, mobile_device=device)
    assert pg.pairing.set_paired_client_session(client_id, 'cookie-B', expiry,
        mobile_device=device, web_reservation_hash='reservation-B')
    # A cancelled request may reserve late; it must not rotate B's installed cookie.
    assert pg.pairing.reserve_paired_client_web_session(client_id, 'orphan-A', expiry, mobile_device=device)
    assert pg.rows('paired_clients')[0]['session_token_hash'] == 'cookie-B'
    with pg.transaction() as conn:
        conn.cursor().execute('UPDATE mobile_devices SET id=%s, api_key_hash=%s', ('new-mobile-row', 'new-key'))
    current = pg.rows('mobile_devices')[0]
    assert not pg.pairing.reserve_paired_client_web_session(client_id, 'obsolete-reserve', expiry, mobile_device=device)
    assert pg.pairing.set_paired_client_session(client_id, 'obsolete-cookie', expiry,
        mobile_device=device, web_reservation_hash='orphan-A') is None
    with pytest.raises(pg.pairing.WebSessionReservationConflict):
        pg.pairing.set_paired_client_session(client_id, 'invalid-cookie', expiry,
            mobile_device=current, web_reservation_hash='orphan-A')
    assert pg.pairing.reserve_paired_client_web_session(client_id, 'new-reserve', expiry, mobile_device=current)
    assert pg.pairing.set_paired_client_session(client_id, 'new-cookie', expiry,
        mobile_device=current, web_reservation_hash='new-reserve')
    assert pg.rows('paired_clients')[0]['session_token_hash'] == 'new-cookie'


@pytest.mark.parametrize('first', ['account', 'direct'])
@pytest.mark.parametrize('kind', ['browser', 'mobile'])
def test_account_enrollment_and_direct_exchange_serialize_before_identity_checks(pg, monkeypatch, first, kind):
    from app.db import device_identity

    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    pg.pairing.create_pairing_grant(token_hash='controlled-grant', purpose='controller',
        scopes=['controller', 'web:session'], target_install_id='worker-one',
        endpoint_url=None, device_label=None, created_by='fixture', expires_at=expiry)
    held = threading.Event()
    attempted = threading.Event()
    acquired_second = threading.Event()
    release = threading.Event()
    local = threading.local()
    real_lock = device_identity.lock_device_identity

    def controlled_lock(cursor, device):
        if local.role != first:
            attempted.set()
        real_lock(cursor, device)
        if local.role == first:
            held.set()
            assert release.wait(timeout=10)
        else:
            acquired_second.set()

    monkeypatch.setattr(device_identity, 'lock_device_identity', controlled_lock)
    monkeypatch.setattr(pg.pairing, 'lock_device_identity', controlled_lock)

    def run(role):
        local.role = role
        if role == 'account':
            return attempt_enroll(pg)
        shared = dict(token_hash='controlled-grant', device_id='device-one',
                      device_name='Controlled direct client', platform=kind, target_install_id='worker-one')
        if kind == 'browser':
            return pg.pairing.exchange_grant_for_client(**shared, client_type='browser',
                session_token_hash='direct-session', session_expires_at=expiry)
        return pg.pairing.exchange_grant_for_mobile(**shared, capabilities={},
            api_key_hash='direct-mobile-key', api_key_expires_at=expiry)

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(run, first)
        try:
            assert held.wait(timeout=5)
            loser = executor.submit(run, 'direct' if first == 'account' else 'account')
            assert attempted.wait(timeout=5)
            # A real second PostgreSQL transaction cannot pass the identity
            # lock while the first has not yet written any ownership row.
            assert not acquired_second.wait(timeout=0.2)
        finally:
            release.set()
        winner_result, loser_result = winner.result(timeout=10), loser.result(timeout=10)
    assert acquired_second.is_set()
    clients = pg.rows('paired_clients')
    assert len(clients) == 1
    auth = pg.rows('mobile_hub_authorizations')
    if first == 'account':
        assert winner_result == 'device-one'
        assert loser_result is None if kind == 'browser' else loser_result == {'duplicate': True}
        assert clients[0]['client_type'] == 'mobile'
        assert clients[0]['session_token_hash'] is None
        assert len(auth) == 1 and auth[0]['paired_client_id'] == clients[0]['id']
        assert pg.rows('mobile_devices')[0]['api_key_hash'] == 'test-key-hash'
        assert pg.rows('pairing_grants')[0]['consumed_at'] is None
    else:
        assert winner_result and loser_result == 'rejected'
        assert auth == []
        assert clients[0]['client_type'] == kind
        assert pg.rows('pairing_grants')[0]['consumed_at'] is not None

@pytest.fixture
def renewal(pg, monkeypatch):
    import hashlib
    from app.core import encryption

    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    old_hash = hashlib.sha256(b'aim-original-fixture').hexdigest()
    enroll(pg, key=old_hash)
    device = pg.rows('mobile_devices')[0]
    owner = pg.hub.ownership(device['device_id'])
    assert pg.hub.confirmation(owner) == 'pending'
    pg.hub.confirm_device(owner, True, mobile_record_id=device['id'])
    return SimpleNamespace(device=device, owner=owner, old_hash=old_hash)


def test_concurrent_renewals_recover_exactly_one_replacement(pg, renewal):
    import hashlib

    outputs = pg.race(*(lambda key=key: pg.mobile.renew_mobile_key(renewal.device, renewal.old_hash, key)
                       for key in ('aim-candidate-a', 'aim-candidate-b', 'aim-candidate-c', 'aim-candidate-d')))
    assert all(value == outputs[0] for value in outputs)
    key, expiry = outputs[0]
    assert len(pg.rows('mobile_key_renewals')) == 1
    assert pg.rows('mobile_devices')[0]['api_key_hash'] == hashlib.sha256(key.encode()).hexdigest()
    assert timedelta(days=89) < expiry - datetime.now(timezone.utc) <= timedelta(days=90)
    assert pg.mobile.renew_mobile_key(renewal.device, renewal.old_hash, 'aim-retry') == outputs[0]
    recovered = pg.mobile.mobile_key_renewal_device(renewal.old_hash)
    assert recovered and recovered['id'] == renewal.device['id']
    assert pg.hub.confirmation(renewal.owner) == 'approved'


@pytest.mark.parametrize('stop', ['deny', 'revoke'])
@pytest.mark.parametrize('first', ['renew', 'stop'])
def test_renewal_and_worker_denial_or_revocation_share_real_row_lock(pg, renewal, monkeypatch, first, stop):
    held, attempted, acquired_second, release = (threading.Event() for _ in range(4))
    local = threading.local()
    pids = {}

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def execute(self, query, args=None):
            normalized = ' '.join(query.upper().split())
            client_lock = ('FROM PAIRED_CLIENTS' in normalized and 'FOR UPDATE' in normalized) or normalized.startswith('UPDATE PAIRED_CLIENTS SET REVOKED_AT')
            role = getattr(local, 'role', None)
            if client_lock and role and role != first:
                attempted.set()
            result = self.cursor.execute(query, args)
            if client_lock and role:
                if role == first:
                    held.set()
                    assert release.wait(timeout=10)
                else:
                    acquired_second.set()
            return result

    @contextmanager
    def controlled():
        with pg.transaction() as conn:
            role = getattr(local, 'role', None)
            if role:
                pids[role] = conn.get_backend_pid()
            yield SimpleNamespace(cursor=lambda: Cursor(conn.cursor()))

    for module in (pg.mobile, pg.hub, pg.pairing):
        monkeypatch.setattr(module, 'get_db', controlled)

    def run(role):
        local.role = role
        try:
            if role == 'renew':
                return pg.mobile.renew_mobile_key(renewal.device, renewal.old_hash, 'aim-replacement')
            if stop == 'deny':
                return pg.hub.confirm_device(renewal.owner, False, mobile_record_id=renewal.device['id'])
            return pg.pairing.revoke_paired_client(renewal.owner['paired_client_id'])
        except ValueError:
            return 'rejected'

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner = executor.submit(run, first)
        try:
            assert held.wait(timeout=5)
            second = 'stop' if first == 'renew' else 'renew'
            loser = executor.submit(run, second)
            assert attempted.wait(timeout=5)
            assert not acquired_second.wait(timeout=0.2)
        finally:
            release.set()
        results = {first: winner.result(timeout=10), second: loser.result(timeout=10)}
    assert len(set(pids.values())) == 2
    assert acquired_second.is_set()
    assert results['stop'] == ('denied' if stop == 'deny' else True)
    if first == 'stop':
        assert results['renew'] == 'rejected'
        assert pg.rows('mobile_key_renewals') == []
    else:
        assert results['renew'][0] == 'aim-replacement'
        assert len(pg.rows('mobile_key_renewals')) == 1
    # A retry cannot restore authority, whichever transaction won the race.
    current = pg.rows('mobile_devices')[0]
    for key_hash in (renewal.old_hash, current['api_key_hash']):
        with pytest.raises(ValueError):
            pg.mobile.renew_mobile_key(current, key_hash, 'aim-after-revocation')
    if stop == 'deny':
        assert pg.hub.confirmation(renewal.owner) == 'denied'
        with pytest.raises(ValueError):
            pg.hub.confirm_device(renewal.owner, True, mobile_record_id=renewal.device['id'])
    else:
        assert current['revoked_at'] is not None


@pytest.mark.parametrize('legacy', [False, True])
def test_existing_key_migration_and_repair_never_infer_approval(pg, monkeypatch, legacy):
    from app.core import encryption
    from app.core.device_keys import key_expiry

    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    enroll(pg)
    created = datetime.now(timezone.utc) - timedelta(days=100)
    expiry = created + timedelta(days=365)
    with pg.transaction() as conn:
        c = conn.cursor()
        c.execute('UPDATE mobile_devices SET created_at=%s, api_key_expires_at=%s',
                  (created, None if legacy else expiry))
        pg.hub.create_schema(c)
        pg.hub.create_schema(c)
    device = pg.rows('mobile_devices')[0]
    owner = pg.hub.ownership(device['device_id'])
    assert key_expiry(device) == expiry
    assert pg.hub.confirmation(owner) == 'pending'
    pg.hub.confirm_device(owner, True, mobile_record_id=device['id'])
    assert pg.mobile.renew_mobile_key(device, device['api_key_hash'], 'aim-unused') == (None, expiry)
    # The repair producing path replaces this record; its prior decision must
    # not transfer, and an in-flight UI decision naming the old row must fail.
    with pg.transaction() as conn:
        conn.cursor().execute('UPDATE mobile_devices SET id=%s, api_key_hash=%s', ('repaired-record', 'repaired-key'))
    assert pg.hub.confirmation(owner) == 'pending'
    with pytest.raises(ValueError):
        pg.hub.confirm_device(owner, True, mobile_record_id=device['id'])
    pg.hub.confirm_device(owner, True, mobile_record_id='repaired-record')
    assert pg.hub.confirmation(owner) == 'approved'


def test_expired_previous_key_receipt_never_extends_deadline(pg, renewal):
    result = pg.mobile.renew_mobile_key(renewal.device, renewal.old_hash, 'aim-replacement')
    with pg.transaction() as conn:
        conn.cursor().execute("UPDATE mobile_key_renewals SET previous_expires_at='2000-01-01T00:00:00+00:00'")
    assert pg.mobile.mobile_key_renewal_device(renewal.old_hash) is None
    with pytest.raises(ValueError):
        pg.mobile.renew_mobile_key(renewal.device, renewal.old_hash, 'aim-expired-retry')
    assert pg.rows('mobile_devices')[0]['api_key_expires_at'] == result[1]
