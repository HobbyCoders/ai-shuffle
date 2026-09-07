"""Native remote-access setup uses verified downloads and works off PATH."""
import hashlib
import io
import tarfile

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
