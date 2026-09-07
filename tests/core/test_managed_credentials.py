import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.core import account_hub, config, encryption, managed_connection, managed_credentials
from app.db import database, mobile_hub


SECRET = 'a' * 64
SERVER = '11111111-1111-1111-1111-111111111111'
DEVICE = '22222222-2222-2222-2222-222222222222'


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(encryption, '_encryption_key', encryption.Fernet.generate_key())
    monkeypatch.setattr(encryption, '_key_salt', None)
    settings = SimpleNamespace(effective_instance_id='this-install', effective_data_dir=tmp_path,
                               port=8000, admin_password=None)
    monkeypatch.setattr(managed_credentials, 'settings', settings)
    monkeypatch.setattr(managed_connection, 'settings', settings)
    monkeypatch.setattr(config, 'settings', settings)
    monkeypatch.setattr(managed_connection, '_protected_listener_ready', lambda: True)
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)


def bind():
    return mobile_hub.bind(account_hub.configured_origin(), SERVER,
                           encryption.encrypt_value(SECRET), 'managed:this-install')


def connection(monkeypatch, reply):
    conn = Mock()
    response = conn.getresponse.return_value
    response.status = 200
    response.read.return_value = json.dumps(reply).encode()
    monkeypatch.setattr(account_hub, '_PinnedHTTPS', Mock(return_value=conn))
    return conn


def test_existing_managed_binding_migrates_without_replacing_original_secret():
    binding = bind()
    original = binding['encrypted_secret']
    assert managed_credentials.secret_for(binding) == SECRET
    stored = database.get_system_setting(managed_credentials._setting_key())
    assert stored.startswith('installation:v1:')
    assert SECRET not in stored
    assert mobile_hub.get_binding()['encrypted_secret'] == original
    assert encryption.decrypt_value(original) == SECRET
    key_file = managed_credentials._key_path()
    assert key_file.is_file()
    if os.name == 'nt':
        assert key_file.read_bytes().startswith(b'dpapi:')
        assert managed_credentials._key() not in key_file.read_bytes()
    else:
        assert key_file.stat().st_mode & 0o777 == 0o600
        assert key_file.parent.stat().st_mode & 0o777 == 0o700


def test_cleared_process_keys_reconnect_and_check_phone_authorization_without_admin_password(monkeypatch):
    binding = bind()
    # Initial server activation uses the real backchannel, which durably migrates
    # the credential. Only the network is substituted; no secret resolver mock.
    conn = connection(monkeypatch, {'tunnel_token': 'connector-token' * 8, 'url': 'https://worker.example.com', 'remote_port':8001})
    assert managed_connection._credentials() == ('connector-token' * 8, 'https://worker.example.com')
    assert mobile_hub.get_binding()['active']
    encryption.clear_encryption_key()
    monkeypatch.setattr(encryption, '_key_salt', None)
    assert encryption.init_encryption_from_env(database) is False
    # No process key cache exists: this reloads the persistent installation key.
    assert managed_connection._credentials() == ('connector-token' * 8, 'https://worker.example.com')
    assert conn.request.call_args.args[0:2] == ('POST', '/v1/servers/' + SERVER + '/connector')
    assert conn.request.call_args.kwargs['headers']['Authorization'] == 'Bearer ' + SECRET
    conn.getresponse.return_value.read.return_value = json.dumps({'active': True, 'account_id': SERVER, 'device_id': DEVICE}).encode()
    account_hub.require_active({'server_id': SERVER, 'account_id': SERVER, 'hub_device_id': DEVICE})
    assert conn.request.call_args.args[1].endswith('/devices/' + DEVICE)
    assert not encryption.is_encryption_ready()
    with pytest.raises(RuntimeError):
        encryption.decrypt_value(binding['encrypted_secret'])
    # Retained local credentials never substitute for current hub authorization.
    conn.getresponse.return_value.status = 401
    with pytest.raises(HTTPException):
        managed_connection._check_authorized()
    with pytest.raises(HTTPException):
        account_hub.require_active({'server_id': SERVER, 'account_id': SERVER, 'hub_device_id': DEVICE})


def test_unmigrated_binding_waits_for_unlock_without_creating_an_unprotected_copy():
    binding = bind()
    key = encryption._encryption_key
    encryption.clear_encryption_key()
    with pytest.raises(RuntimeError):
        managed_credentials.secret_for(binding)
    assert database.get_system_setting(managed_credentials._setting_key()) is None
    assert not (managed_credentials.settings.effective_data_dir / '.managed-connection').exists()
    encryption._encryption_key = key
    assert managed_credentials.secret_for(binding) == SECRET
    encryption.clear_encryption_key()
    assert managed_credentials.secret_for(binding) == SECRET


def test_copy_cannot_authorize_another_binding_or_installation(monkeypatch):
    binding = bind()
    managed_credentials.secret_for(binding)
    encryption.clear_encryption_key()
    for changed in ({**binding, 'server_id': DEVICE}, {**binding, 'hub_url': 'https://other.example.com'}):
        with pytest.raises(ValueError, match='another binding'):
            managed_credentials.secret_for(changed)
    with pytest.raises(RuntimeError):
        managed_credentials.secret_for({**binding, 'linked_by': 'paired-controller'})
    monkeypatch.setattr(managed_credentials.settings, 'effective_instance_id', 'another-install')
    with pytest.raises(RuntimeError):
        managed_credentials.secret_for(binding)


@pytest.mark.parametrize('damage', ['missing-key', 'corrupt-key', 'corrupt-copy', 'empty-copy'])
def test_missing_or_corrupt_installation_state_is_never_silently_replaced(damage):
    binding = bind()
    managed_credentials.secret_for(binding)
    key_file = managed_credentials._key_path()
    if damage == 'missing-key':
        key_file.unlink()
    elif damage == 'corrupt-key':
        key_file.write_bytes(b'invalid')
    elif damage == 'empty-copy':
        database.set_system_setting(managed_credentials._setting_key(), '')
    else:
        database.set_system_setting(managed_credentials._setting_key(), 'installation:v1:invalid')
    stored = database.get_system_setting(managed_credentials._setting_key())
    # Even an unlocked legacy key cannot silently replace different installation
    # state. Explicit administrative recovery can use the retained original.
    with pytest.raises(Exception):
        managed_credentials.secret_for(binding)
    assert database.get_system_setting(managed_credentials._setting_key()) == stored
    assert encryption.decrypt_value(mobile_hub.get_binding()['encrypted_secret']) == SECRET
    if damage == 'missing-key':
        assert not key_file.exists()


def test_password_rotation_preserves_both_credential_paths():
    binding = bind()
    managed_credentials.secret_for(binding)
    installation_copy = database.get_system_setting(managed_credentials._setting_key())
    new_key = encryption.Fernet.generate_key()
    plan, failures = encryption.build_reencryption_plan(encryption._encryption_key, new_key, database)
    assert not failures
    assert any(item.ref == 'mobile_account_hub_secret' for item in plan)
    assert all(item.ref != managed_credentials._setting_key() for item in plan)
    encryption.apply_reencryption_plan(plan, database)
    encryption._encryption_key = new_key
    assert encryption.decrypt_value(mobile_hub.get_binding()['encrypted_secret']) == SECRET
    encryption.clear_encryption_key()
    assert managed_credentials.secret_for(mobile_hub.get_binding()) == SECRET
    assert database.get_system_setting(managed_credentials._setting_key()) == installation_copy


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission enforcement')
def test_key_with_public_permissions_is_rejected():
    binding = bind()
    managed_credentials.secret_for(binding)
    managed_credentials._key_path().chmod(0o644)
    with pytest.raises(ValueError, match='permissions'):
        managed_credentials.secret_for(binding)


def test_partial_write_never_publishes_a_final_key_and_can_retry(monkeypatch):
    binding = bind()
    original_fdopen = managed_credentials.os.fdopen

    class InterruptedWrite:
        def __init__(self, fd):
            self.stream = original_fdopen(fd, 'wb')
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def write(self, data):
            self.stream.write(data[:3])
            self.stream.flush()
            raise OSError('simulated interrupted write')

    monkeypatch.setattr(managed_credentials.os, 'fdopen',
                        lambda fd, mode: InterruptedWrite(fd) if mode == 'wb' else original_fdopen(fd, mode))
    with pytest.raises(OSError, match='interrupted write'):
        managed_credentials.secret_for(binding)
    assert not managed_credentials._key_path().exists()
    assert database.get_system_setting(managed_credentials._setting_key()) is None
    monkeypatch.setattr(managed_credentials.os, 'fdopen', original_fdopen)
    assert managed_credentials.secret_for(binding) == SECRET


@pytest.mark.parametrize('failure', ['file-sync', 'key-directory-sync', 'data-directory-sync'])
def test_sync_failure_cannot_commit_credential_and_retry_flushes_recovered_key(monkeypatch, failure):
    binding = bind()
    path = managed_credentials._key_path()
    real_sync = managed_credentials.os.fsync
    real_directory_sync = managed_credentials._sync_directory
    synced = []

    def directory_sync(directory):
        synced.append(directory)
        target = path.parent if failure == 'key-directory-sync' else path.parent.parent
        if directory == target:
            raise OSError('simulated directory sync failure')
        real_directory_sync(directory)

    if failure == 'file-sync':
        monkeypatch.setattr(managed_credentials.os, 'fsync', Mock(side_effect=OSError('simulated file sync failure')))
    else:
        monkeypatch.setattr(managed_credentials, '_sync_directory', directory_sync)
    with pytest.raises(OSError, match='sync failure'):
        managed_credentials.secret_for(binding)
    assert database.get_system_setting(managed_credentials._setting_key()) is None
    published = path.read_bytes() if path.exists() else None
    assert (published is None) == (failure == 'file-sync')
    monkeypatch.setattr(managed_credentials.os, 'fsync', real_sync)
    synced.clear()

    def successful_sync(directory):
        synced.append(directory)
        real_directory_sync(directory)

    monkeypatch.setattr(managed_credentials, '_sync_directory', successful_sync)
    assert managed_credentials.secret_for(binding) == SECRET
    assert synced == [path.parent, path.parent.parent]
    if published is not None:
        assert path.read_bytes() == published


def test_atomic_publication_never_overwrites_a_concurrent_key(monkeypatch):
    path = managed_credentials._key_path()
    original_publish = managed_credentials._publish_key
    other_key = encryption.Fernet.generate_key()
    raw = (b'dpapi:' + managed_credentials.base64.b64encode(managed_credentials._windows_protect(other_key))
           if os.name == 'nt' else other_key)

    def competing_publish(temporary, final):
        fd = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        original_publish(temporary, final)

    monkeypatch.setattr(managed_credentials, '_publish_key', competing_publish)
    assert managed_credentials._key(create=True) == other_key
    assert path.read_bytes() == raw
    assert not list(path.parent.glob('.pending-*'))


def test_fresh_interpreter_reconnects_from_persistent_credential():
    binding = bind()
    managed_credentials.secret_for(binding)
    mobile_hub.activate()
    stored = database.get_system_setting(managed_credentials._setting_key())
    script = r'''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from app.core import account_hub, config, encryption, managed_connection, managed_credentials
from app.db import database, mobile_hub
state = json.loads(sys.stdin.read())
settings = SimpleNamespace(effective_instance_id='this-install', effective_data_dir=Path(state['directory']), port=8000, admin_password=None)
config.settings = managed_connection.settings = managed_credentials.settings = settings
assert encryption._encryption_key is None and encryption._key_salt is None
assert 'ADMIN_PASSWORD' not in os.environ
assert encryption.init_encryption_from_env(database) is False
database.get_system_setting = lambda key: state['stored'] if key == managed_credentials._setting_key() else None
def setting_write(key, value):
    assert key == managed_connection._key() and value == ''
database.set_system_setting = setting_write
mobile_hub.get_binding = lambda: state['binding']
mobile_hub.activate = lambda: None
class Connection:
    status = 200
    def __init__(self, *args, **kwargs): pass
    def request(self, method, path, **kwargs):
        assert kwargs['headers']['Authorization'] == 'Bearer ' + 'a' * 64
        self.path = path
    def getresponse(self): return self
    def read(self, limit):
        if self.path.endswith('/connector'):
            return json.dumps({'tunnel_token':'connector-token' * 8, 'url':'https://worker.example.com', 'remote_port':8001}).encode()
        return json.dumps({'active':True, 'account_id':state['binding']['server_id'], 'device_id':state['device']}).encode()
    def close(self): pass
account_hub._PinnedHTTPS = Connection
managed_connection._protected_listener_ready = lambda: True
assert managed_connection._credentials() == ('connector-token' * 8, 'https://worker.example.com')
account_hub.require_active({'server_id':state['binding']['server_id'], 'account_id':state['binding']['server_id'], 'hub_device_id':state['device']})
assert encryption._encryption_key is None
print('Fresh interpreter reconnect and device authorization passed')
'''
    child_env = dict(os.environ)
    child_env.pop('ADMIN_PASSWORD', None)
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[2],
                            env=child_env, input=json.dumps({'directory': str(managed_credentials.settings.effective_data_dir),
                                'binding': mobile_hub.get_binding(), 'stored': stored, 'device': DEVICE}),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'Fresh interpreter reconnect and device authorization passed' in result.stdout
