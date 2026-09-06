"""Access controls must honor the same dotenv source as the rest of Settings."""
import json
import os
from pathlib import Path
import subprocess
import sys

from starlette.requests import Request

from app.core import owner_auth, account_hub
from app.core.config import Settings
from app.db import mobile_tool_identity


def request(host):
    return Request({'type': 'http', 'method': 'GET', 'scheme': 'http', 'path': '/',
        'server': ('127.0.0.1', 8000), 'client': ('192.168.1.50', 50000),
        'shuffle.ingress': 'local', 'headers': [(b'host', host.encode())]})


def test_dotenv_hostname_and_service_origins(tmp_path, monkeypatch):
    for name in ('SHUFFLE_LOCAL_ACCESS', 'SHUFFLE_LOCAL_HOSTNAMES', 'ACCOUNT_BROWSER_ORIGIN', 'MOBILE_ACCOUNT_HUB_URL'):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / '.env'
    config.write_text('SHUFFLE_LOCAL_ACCESS=true\nSHUFFLE_LOCAL_HOSTNAMES=workstation.lan\n'
                      'ACCOUNT_BROWSER_ORIGIN=https://connect.example.com\nMOBILE_ACCOUNT_HUB_URL=https://hub.example.com\n')
    loaded = Settings(_env_file=config, data_dir=tmp_path, workspace_dir=tmp_path)
    monkeypatch.setattr(owner_auth, 'settings', loaded)
    monkeypatch.setattr(account_hub, 'settings', loaded)
    assert owner_auth.local_access(request('workstation.lan:8000'))
    assert not owner_auth.local_access(request('attacker.example:8000'))
    assert owner_auth.browser_origin() == 'https://connect.example.com'
    assert account_hub.configured_origin() == 'https://hub.example.com'
    monkeypatch.setenv('SHUFFLE_LOCAL_ACCESS', 'false')
    overridden = Settings(_env_file=config, data_dir=tmp_path, workspace_dir=tmp_path)
    monkeypatch.setattr(owner_auth, 'settings', overridden)
    assert not owner_auth.local_access(request('workstation.lan:8000'))
    assert not mobile_tool_identity.admin_active(owner_auth.digest('local-transport:' + overridden.effective_instance_id))


def test_referenced_dotenv_disables_access_in_fresh_process(tmp_path):
    config = tmp_path / 'operator.env'
    config.write_text('SHUFFLE_LOCAL_ACCESS=false\nSHUFFLE_LOCAL_HOSTNAMES=workstation.lan\n')
    source = '''
import json
from starlette.requests import Request
from app.core.owner_auth import local_access, settings
r=Request({'type':'http','method':'GET','scheme':'http','path':'/',
 'client':('192.168.1.50',50000),'server':('127.0.0.1',8000),
 'shuffle.ingress':'local','headers':[(b'host',b'workstation.lan:8000')]})
print(json.dumps({'enabled':settings.shuffle_local_access,'hosts':settings.shuffle_local_hostnames,'authorized':local_access(r)}))
'''
    env = {name: value for name, value in os.environ.items() if name in ('PATH', 'SystemRoot', 'SYSTEMROOT', 'TEMP', 'TMP')}
    env.update(AI_SHUFFLE_ENV_FILE=str(config), DATA_DIR=str(tmp_path / 'data'), WORKSPACE_DIR=str(tmp_path / 'workspace'))
    result = subprocess.run([sys.executable, '-c', source], cwd=Path(__file__).resolve().parents[2],
                            env=env, text=True, capture_output=True, timeout=30, check=True)
    assert json.loads(result.stdout.splitlines()[-1]) == {'enabled': False, 'hosts': 'workstation.lan', 'authorized': False}
