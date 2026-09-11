"""Native remote-access setup uses verified downloads and works off PATH."""
import hashlib
import io
import tarfile
from types import SimpleNamespace

import pytest

from app.core import cloudflared as client


@pytest.fixture
def local(monkeypatch, tmp_path):
    path = tmp_path / 'bin' / 'cloudflared.exe'
    monkeypatch.setattr(client.shutil, 'which', lambda _: None)
    monkeypatch.setattr(client, '_local_path', lambda: path)
    return path


@pytest.mark.parametrize('archive', [False, True])
def test_verified_install_is_discovered_and_reused_offline(local, monkeypatch, archive):
    binary = b'fake tunnel executable'
    raw = binary
    asset = 'test.tgz' if archive else 'test.exe'
    if archive:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as bundle:
            member = tarfile.TarInfo('cloudflared')
            member.size = len(binary)
            bundle.addfile(member, io.BytesIO(binary))
        raw = buffer.getvalue()
    monkeypatch.setattr(client, '_asset', lambda: asset)
    monkeypatch.setitem(client._DIGESTS, asset, hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(client.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(raw))
    assert client.install() == str(local)
    assert local.read_bytes() == binary
    monkeypatch.setattr(client.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('unexpected download'))
    assert client.install() == client.find_executable() == str(local)
    assert list(local.parent.iterdir()) == [local]


def test_bad_checksum_never_publishes_executable(local, monkeypatch):
    monkeypatch.setattr(client, '_asset', lambda: 'test.exe')
    monkeypatch.setitem(client._DIGESTS, 'test.exe', '0' * 64)
    monkeypatch.setattr(client.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'corrupt'))
    with pytest.raises(RuntimeError, match='checksum'):
        client.install()
    assert not local.exists()
    assert not list(local.parent.iterdir())


def test_existing_system_client_needs_no_download(local, monkeypatch):
    monkeypatch.setattr(client.shutil, 'which', lambda _: '/usr/bin/cloudflared')
    monkeypatch.setattr(client.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('unexpected download'))
    assert client.install() == '/usr/bin/cloudflared'
    assert not local.parent.exists()


def test_bundle_ignores_path_and_replaces_stale_local_client(local, monkeypatch):
    binary = b'verified bundled executable'
    monkeypatch.setattr(client.shutil, 'which', lambda _: '/usr/bin/cloudflared')
    local.parent.mkdir()
    local.write_bytes(b'stale client')
    monkeypatch.setattr(client, '_asset', lambda: 'test.exe')
    digest = hashlib.sha256(binary).hexdigest()
    monkeypatch.setitem(client._DIGESTS, 'test.exe', digest)
    monkeypatch.setattr(client.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(binary))
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        assert local.read_bytes() == binary
        return SimpleNamespace(stdout=f'cloudflared version {client.VERSION} (built today)\n')
    monkeypatch.setattr(client.subprocess, 'run', run)

    manifest = client.bundle()

    assert manifest == {
        'version': client.VERSION, 'asset': 'test.exe', 'archiveSha256': digest,
        'sha256': digest, 'executable': str(local),
    }
    assert calls[0][0] == [str(local), '--version']
    assert calls[0][1]['check'] is True
    assert calls[0][1]['timeout'] == 15


def test_bundle_bad_download_fails_even_with_existing_clients(local, monkeypatch):
    monkeypatch.setattr(client.shutil, 'which', lambda _: '/usr/bin/cloudflared')
    local.parent.mkdir()
    local.write_bytes(b'existing local client')
    monkeypatch.setattr(client, '_asset', lambda: 'test.exe')
    monkeypatch.setitem(client._DIGESTS, 'test.exe', '0' * 64)
    monkeypatch.setattr(client.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(b'corrupt'))
    monkeypatch.setattr(client.subprocess, 'run', lambda *a, **kw: pytest.fail('executed an unverified download'))

    with pytest.raises(RuntimeError, match='checksum'):
        client.bundle()
    assert local.read_bytes() == b'existing local client'
    assert list(local.parent.iterdir()) == [local]


def test_source_build_reuses_only_a_verified_cached_binary(local, monkeypatch):
    binary = b'verified cached executable'
    local.parent.mkdir()
    local.write_bytes(binary)
    monkeypatch.setattr(client, '_asset', lambda: 'test.exe')
    monkeypatch.setitem(client._DIGESTS, 'test.exe', hashlib.sha256(binary).hexdigest())
    monkeypatch.setattr(client.urllib.request, 'urlopen', lambda *a, **kw: pytest.fail('unexpected download'))
    monkeypatch.setattr(client.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=f'cloudflared version {client.VERSION} (cached)'))
    assert client.bundle(reuse_verified=True)['executable'] == str(local)


def test_source_build_does_not_reuse_a_tampered_cache(local, monkeypatch):
    local.parent.mkdir()
    local.write_bytes(b'tampered')
    verified = b'correct download'
    monkeypatch.setattr(client, '_asset', lambda: 'test.exe')
    monkeypatch.setitem(client._DIGESTS, 'test.exe', hashlib.sha256(verified).hexdigest())
    monkeypatch.setattr(client.urllib.request, 'urlopen', lambda *a, **kw: io.BytesIO(verified))
    monkeypatch.setattr(client.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=f'cloudflared version {client.VERSION} (fresh)'))
    client.bundle(reuse_verified=True)
    assert local.read_bytes() == verified


def test_bundle_rejects_wrong_reported_version(local, monkeypatch):
    monkeypatch.setattr(client, 'install', lambda **kwargs: str(local))
    monkeypatch.setattr(client.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='cloudflared version 2020.1.1 (built long ago)'))
    with pytest.raises(RuntimeError, match='pinned version'):
        client.bundle()


def test_packaged_worker_discovers_client_with_empty_path(tmp_path, monkeypatch):
    server = tmp_path / 'resources' / 'worker' / 'server'
    monkeypatch.setattr(client, '__file__', str(server / 'app' / 'core' / 'cloudflared.py'))
    monkeypatch.setenv('PATH', '')
    executable = server / 'downloads' / 'cloudflared' / client.VERSION / (
        'cloudflared.exe' if client.sys.platform == 'win32' else 'cloudflared'
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'packaged executable')
    executable.chmod(0o755)
    assert client.find_executable() == str(executable)


def test_setup_failure_keeps_local_mode_available(monkeypatch):
    import run_local
    monkeypatch.setattr(client, 'find_executable', lambda: None)
    def fail():
        raise OSError('offline')
    monkeypatch.setattr(client, 'install', fail)
    messages = []
    monkeypatch.setattr(run_local, 'print_status', lambda message, *_: messages.append(message))
    assert run_local.setup_remote_access() is False
    assert any('python -m app.core.cloudflared' in message for message in messages)
