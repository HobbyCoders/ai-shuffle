"""Server key adoption preserves ciphertext and survives real custody failures."""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.core import credential_keys as keys, encryption
from app.db import database as db

PASSWORD = 'legacy-only-test-password'
RECOVERY = 'portable-recovery-passphrase'


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(keys, 'settings', SimpleNamespace(effective_data_dir=tmp_path))
    monkeypatch.setattr(encryption, '_encryption_key', None)
    monkeypatch.setattr(encryption, '_key_salt', None)
    monkeypatch.setattr(encryption, '_credential_status', {'ready': False, 'state': 'unavailable'})
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)
    from app.core.config import settings
    monkeypatch.setattr(settings, 'admin_password', None)


def metadata():
    return json.loads(db.get_system_setting(keys.STATE_KEY))


def stored_values():
    with db.get_db() as conn:
        return keys._encrypted_values(conn.cursor())


def seed():
    encryption.set_encryption_key(PASSWORD, db)
    enc = encryption.encrypt_value
    db.set_system_setting('orphaned_credential', enc('settings-secret'))
    db.set_system_setting('mobile_managed_credential:keep', 'installation:v1:unchanged')
    db.create_automation(automation_id='a', name='a', trigger_type='webhook',
        trigger_config={'secret': enc('old-trigger')},
        triggers=[{'config': {'nested': [enc('new-trigger'), 'ordinary text']}}],
        prompt='p', profile_id='default')
    db.create_connector(connector_id='c', connector_type='telegram', name='c',
        config={'bot_token': enc('connector-secret')}, events=[])
    db.create_api_user(user_id='u', name='u', api_key_hash='hash')
    db.set_user_credential('u', 'github_token', enc('integration-secret'))
    db.create_claude_account(account_id='claude', name='Claude', credentials=enc('claude-secret'),
        credential_fingerprint='unchanged', expires_at=123, has_refresh_token=True)
    db.create_model_provider(provider_id='p', name='p', provider_type='custom',
        base_url='https://example.com', auth_token=enc('provider-secret'))
    db.set_codex_token_bundle('p', access_token=enc('oauth-access'), refresh_token=enc('oauth-refresh'),
        account_id='account', id_token_expires_at='2030-01-01T00:00:00+00:00',
        last_refresh='2026-01-01T00:00:00+00:00')


def test_fresh_local_database_needs_no_password_or_account_and_reloads():
    assert db.get_admin() is None
    assert encryption.initialize_server_key(db)
    original_key = encryption._encryption_key
    original_state = db.get_system_setting(keys.STATE_KEY)
    assert db.get_system_setting('encryption_salt') is None
    secret = encryption.encrypt_value('fresh-secret')
    db.set_system_setting('fresh', secret)
    assert original_key.decode() not in original_state
    encryption.clear_encryption_key()
    assert encryption.initialize_server_key(db, legacy_password='wrong-unused-password')
    assert encryption._encryption_key == original_key
    assert encryption.decrypt_value(secret) == 'fresh-secret'
    assert db.get_system_setting(keys.STATE_KEY) == original_state


def test_adopts_every_legacy_store_without_rewriting_ciphertext_or_metadata():
    seed()
    before = stored_values()
    old_key = encryption._encryption_key
    provider = db.get_model_provider('p', include_token=True)
    assert len(before) == 9  # eight locations; both automation columns
    assert encryption.initialize_server_key(db, PASSWORD)
    assert encryption._encryption_key == old_key
    assert stored_values() == before
    assert db.get_model_provider('p', include_token=True) == provider
    assert db.get_system_setting('mobile_managed_credential:keep') == 'installation:v1:unchanged'
    encryption.clear_encryption_key()
    assert encryption.initialize_server_key(db)
    assert {encryption.decrypt_value(value) for value in before} == {
        'settings-secret', 'old-trigger', 'new-trigger', 'connector-secret', 'integration-secret',
        'claude-secret', 'provider-secret', 'oauth-access', 'oauth-refresh'}


def test_empty_encrypted_plaintext_is_valid_during_adoption_and_restart():
    encryption.set_encryption_key(PASSWORD, db)
    value = encryption.encrypt_value('')
    db.set_system_setting('legacy-empty-secret', value)
    assert encryption.initialize_server_key(db, PASSWORD)
    assert encryption.decrypt_value(value) == ''
    encryption.clear_encryption_key()
    assert encryption.initialize_server_key(db)
    assert db.get_system_setting('legacy-empty-secret') == value
    assert encryption.decrypt_value(value) == ''


@pytest.mark.parametrize('problem', ['no-password', 'wrong-password', 'missing-salt', 'corrupt-salt', 'corrupt-secret'])
def test_legacy_failures_never_activate_or_rewrite(problem, test_db):
    seed()
    password = PASSWORD
    if problem == 'no-password': password = None
    if problem == 'wrong-password': password = 'wrong'
    if problem == 'missing-salt': test_db.execute("DELETE FROM system_settings WHERE key='encryption_salt'")
    if problem == 'corrupt-salt': db.set_system_setting('encryption_salt', 'invalid')
    if problem == 'corrupt-secret': db.set_system_setting('orphaned_credential', 'enc:invalid')
    before = stored_values()
    assert not encryption.initialize_server_key(db, password)
    assert not encryption.is_encryption_ready()
    assert metadata()['wrapped_dek'] is None
    assert stored_values() == before
    assert not (keys.settings.effective_data_dir / '.credential-keys').exists()


@pytest.mark.parametrize('table', [entry[0] for entry in keys._TABLES])
def test_any_store_query_failure_refuses_activation(table, monkeypatch):
    from tests.pg_shim import ShimCursor
    seed()
    original = ShimCursor.execute
    def execute(self, sql, params=None):
        if sql.startswith('SELECT ') and sql.endswith(' FROM ' + table):
            raise RuntimeError('read failed')
        return original(self, sql, params)
    monkeypatch.setattr(ShimCursor, 'execute', execute)
    assert not encryption.initialize_server_key(db, PASSWORD)
    assert metadata()['wrapped_dek'] is None
    assert not encryption.is_encryption_ready()


@pytest.mark.parametrize('damage', ['missing-file', 'corrupt-file', 'empty-state', 'corrupt-envelope'])
def test_active_database_never_falls_back_to_password_or_replaces_custody(damage):
    seed()
    assert encryption.initialize_server_key(db, PASSWORD)
    state = metadata()
    path = keys.key_path(state['database_id'])
    if damage == 'missing-file': path.unlink()
    if damage == 'corrupt-file': path.write_bytes(b'invalid')
    if damage == 'empty-state': db.set_system_setting(keys.STATE_KEY, '')
    if damage == 'corrupt-envelope':
        state['wrapped_dek'] = 'invalid'
        db.set_system_setting(keys.STATE_KEY, json.dumps(state))
    before = db.get_system_setting(keys.STATE_KEY)
    assert not encryption.initialize_server_key(db, PASSWORD)
    assert not encryption.is_encryption_ready()
    assert db.get_system_setting(keys.STATE_KEY) == before
    if damage == 'missing-file': assert not path.exists()


@pytest.mark.parametrize('failure', ['publication', 'directory-sync', 'database-activation'])
def test_interrupted_adoption_reuses_database_identity_and_complete_key(failure, monkeypatch):
    seed()
    before = stored_values()
    with monkeypatch.context() as patch:
        if failure in ('publication', 'directory-sync'):
            target = '_publish_key' if failure == 'publication' else '_sync_key_location'
            patch.setattr(keys.custody, target, lambda *a: (_ for _ in ()).throw(OSError('interrupted')))
        else:
            original = db.get_db
            calls = 0
            @contextmanager
            def interrupted():
                nonlocal calls
                calls += 1
                with original() as conn:
                    yield conn
                    if calls == 2: raise OSError('before commit')
            patch.setattr(db, 'get_db', interrupted)
        assert not encryption.initialize_server_key(db, PASSWORD)
    pending = metadata()
    assert pending['wrapped_dek'] is None
    path = keys.settings.effective_data_dir / '.credential-keys' / (pending['database_id'] + '.key')
    old_bytes = path.read_bytes() if path.exists() else None
    assert stored_values() == before
    assert encryption.initialize_server_key(db, PASSWORD)
    assert metadata()['database_id'] == pending['database_id']
    if old_bytes: assert path.read_bytes() == old_bytes
    assert stored_values() == before


def test_portable_recovery_provisions_another_data_directory_and_is_idempotent(tmp_path, monkeypatch):
    seed()
    assert encryption.initialize_server_key(db, PASSWORD)
    original_key = encryption._encryption_key
    package = tmp_path / 'portable.backup'
    keys.export_recovery(db, package, RECOVERY)
    assert original_key not in package.read_bytes()
    another = tmp_path / 'another-install'
    another.mkdir()
    monkeypatch.setattr(keys.settings, 'effective_data_dir', another)
    assert not encryption.initialize_server_key(db, PASSWORD)
    keys.import_recovery(db, package, RECOVERY)
    keys.import_recovery(db, package, RECOVERY)
    assert encryption.initialize_server_key(db)
    assert encryption._encryption_key == original_key


@pytest.mark.parametrize('damage', ['wrong-passphrase', 'wrong-database', 'existing-different-key'])
def test_recovery_rejects_wrong_identity_or_overwrite(damage, tmp_path, monkeypatch):
    assert encryption.initialize_server_key(db)
    package = tmp_path / 'portable.backup'
    keys.export_recovery(db, package, RECOVERY)
    state = metadata()
    path = keys.key_path(state['database_id'])
    if damage == 'wrong-database':
        state['database_id'] = '22222222-2222-4222-8222-222222222222'
        db.set_system_setting(keys.STATE_KEY, json.dumps(state))
    if damage == 'existing-different-key':
        path.unlink()
        keys._publish(state['database_id'], encryption.Fernet.generate_key())
    previous = path.read_bytes()
    with pytest.raises(keys.CredentialKeyError):
        keys.import_recovery(db, package, 'wrong-passphrase' if damage == 'wrong-passphrase' else RECOVERY)
    assert path.read_bytes() == previous


def test_legacy_key_changers_are_disabled_after_adoption(monkeypatch):
    assert encryption.initialize_server_key(db)
    monkeypatch.setenv('ADMIN_PASSWORD', 'unused-new-value')
    for call in (lambda: encryption.set_encryption_key('new', db),
                 lambda: encryption.re_encrypt_all_secrets('old', 'new', db),
                 lambda: encryption.init_encryption_from_env(db)):
        with pytest.raises(RuntimeError, match='cannot be password-rekeyed'):
            call()
    from scripts.rekey import rekey
    assert rekey('old', 'new', dry_run=False, update_admin_hash=False) == 2


@pytest.mark.parametrize('writer', ['settings', 'connector', 'connector-manager', 'automation'])
def test_locked_secret_writers_never_return_or_persist_plaintext(writer):
    from app.api.settings_crypto import set_encrypted_api_key
    from app.core.connector_service import encrypt_config
    from app.core.connectors.manager import ConnectorManager
    from app.core.automation_service import encrypt_webhook_secret
    value = {'bot_token': 'never-persist-this'}
    call = {
        'settings': lambda: set_encrypted_api_key('new-secret', 'never-persist-this'),
        'connector': lambda: encrypt_config(value, 'telegram'),
        'connector-manager': lambda: ConnectorManager._encrypt_config(value, 'telegram'),
        'automation': lambda: encrypt_webhook_secret({'secret': 'never-persist-this'}),
    }[writer]
    with pytest.raises((RuntimeError, ValueError), match='local recovery'):
        call()
    assert db.get_system_setting('new-secret') is None
    assert value == {'bot_token': 'never-persist-this'}


def test_cli_status_is_read_only_and_adopt_prompts_only_for_legacy(monkeypatch):
    from scripts import credential_keys as cli
    assert cli.inspect_status() == {'ready': False, 'state': 'uninitialized'}
    assert db.get_system_setting(keys.STATE_KEY) is None
    monkeypatch.setattr('scripts.rekey._server_appears_up', lambda: None)
    prompts = []
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: prompts.append(prompt) or PASSWORD)
    seed()
    assert cli.execute(SimpleNamespace(command='adopt'))['ready']
    assert len(prompts) == 1
    assert cli.execute(SimpleNamespace(command='adopt'))['ready']
    assert len(prompts) == 1


def test_cli_portable_export_import_and_live_adoption_refusal(monkeypatch, tmp_path):
    from scripts import credential_keys as cli
    monkeypatch.setattr('scripts.rekey._server_appears_up', lambda: 'up')
    with pytest.raises(RuntimeError, match='Stop the app'):
        cli.execute(SimpleNamespace(command='adopt'))
    assert db.get_system_setting(keys.STATE_KEY) is None
    assert encryption.initialize_server_key(db)
    package = tmp_path / 'cli.export'
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: RECOVERY)
    args = SimpleNamespace(command='export', file=package)
    assert cli.execute(args)['ready']
    path = keys.key_path(metadata()['database_id'])
    path.unlink()
    monkeypatch.setattr('scripts.rekey._server_appears_up', lambda: None)
    args.command = 'import'
    assert cli.execute(args)['ready']


def test_file_sync_failure_never_publishes_a_key_or_activates_db(monkeypatch):
    seed()
    with monkeypatch.context() as patch:
        patch.setattr(keys.os, 'fsync', lambda *args: (_ for _ in ()).throw(OSError('file sync failure')))
        assert not encryption.initialize_server_key(db, PASSWORD)
    state = metadata()
    assert state['wrapped_dek'] is None
    assert not keys.key_path(state['database_id']).exists()
    assert encryption.initialize_server_key(db, PASSWORD)


def test_fresh_interpreter_reloads_database_and_key_without_password(test_db, tmp_path):
    seed()
    assert encryption.initialize_server_key(db, PASSWORD)
    file = tmp_path / 'database.sqlite'
    test_db.commit()
    with sqlite3.connect(file) as target:
        test_db.backup(target)
    program = r'''
import os,sqlite3,sys
from contextlib import contextmanager
from types import SimpleNamespace
from tests.pg_shim import ShimConnection
from app.core import encryption,credential_keys
from app.db import database
connection=sqlite3.connect(sys.argv[1],check_same_thread=False)
connection.row_factory=sqlite3.Row
@contextmanager
def database_connection():
    yield ShimConnection(connection)
    connection.commit()
database.get_db=database_connection
credential_keys.settings=SimpleNamespace(effective_data_dir=__import__('pathlib').Path(sys.argv[2]))
assert encryption._encryption_key is None
assert 'ADMIN_PASSWORD' not in os.environ
assert encryption.initialize_server_key(database)
with database.get_db() as conn:
    values=credential_keys._encrypted_values(conn.cursor())
assert len(values)==9
assert all(encryption.decrypt_value(v) for v in values)
print('fresh-process-recovery-ok')
'''
    env = {key: value for key, value in os.environ.items() if key != 'ADMIN_PASSWORD'}
    result = subprocess.run([sys.executable, '-c', program, str(file), str(tmp_path)],
                            capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'fresh-process-recovery-ok'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX ownership/mode contract')
def test_world_readable_or_symlink_key_is_rejected(tmp_path):
    assert encryption.initialize_server_key(db)
    path = keys.key_path(metadata()['database_id'])
    path.chmod(0o644)
    assert not encryption.initialize_server_key(db)
    path.chmod(0o600)
    renamed = path.with_suffix('.saved')
    path.rename(renamed)
    path.symlink_to(renamed)
    assert not encryption.initialize_server_key(db)
