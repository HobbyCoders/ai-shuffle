# ruff: noqa: F811
"""Peer-only bootstrap, offline catch-up, and zero cloud data traffic."""
import json

import httpx
import pytest

from app.db import network as store
from app.network import engine, hub, peer_snapshot, transport
from tests.core.network.harness import join, owner_pref, set_owner_pref
from tests.core.network.test_transport import (  # noqa: F401
    ServedBy,
    workers,
)


async def test_state_recipient_forwards_to_offline_peer_without_original_log(workers):
    a, b, c = [workers(name) for name in ('a', 'b', 'c')]
    with a.active():
        engine.create_network('Home')
    join(a, b)
    join(a, c)
    set_owner_pref(a, 'while_offline', 'kept')
    served_a = ServedBy(a, 'https://a.example.com')
    with b.active():
        async with httpx.AsyncClient(transport=served_a.transport()) as client:
            await transport.PeerLink(a.device_id, [served_a.base_url]).receive_state(client, engine.context())
    assert owner_pref(b, 'while_offline') == 'kept'
    # A is no longer reachable. B has A's state, but not its original ops.
    served_b = ServedBy(b, 'https://b.example.com')
    set_owner_pref(c, 'from_c', 'offline edit')
    with c.active():
        async with httpx.AsyncClient(transport=served_b.transport()) as client:
            result = await transport.PeerLink(b.device_id, [served_b.base_url]).sync_once(client, engine.context())
            assert result['received'] > 0
    assert owner_pref(c, 'while_offline') == 'kept'
    assert owner_pref(b, 'from_c') == 'offline edit'
    assert owner_pref(c, 'from_c') == 'offline edit'


def test_incomplete_or_tampered_state_never_advances_frontier(workers, tmp_path):
    a, b = workers('a'), workers('b')
    with a.active():
        engine.create_network('Home')
    join(a, b)
    set_owner_pref(a, 'new_value', 42)
    with a.active():
        manifest = peer_snapshot.capture(b.device_id)
        copied = tmp_path / 'received'
        copied.mkdir()
        for index in range(len(manifest['pages'])):
            (copied / f'{index}.json').write_bytes(peer_snapshot.page(b.device_id, manifest['transfer'], index))
    with b.active():
        before = store.local_watermarks()
        last = copied / f"{len(manifest['pages']) - 1}.json"
        raw = last.read_bytes()
        last.write_bytes(raw + b' ')
        with pytest.raises(ValueError, match='verify'):
            peer_snapshot.apply(manifest, copied, a.device_id)
        assert store.local_watermarks() == before
        assert owner_pref(b, 'new_value') is None
        last.write_bytes(raw)
        peer_snapshot.apply(manifest, copied, a.device_id)
        assert owner_pref(b, 'new_value') == 42


async def test_peer_mode_never_uploads_or_polls_cloud_data(workers, monkeypatch):
    a = workers('a')
    calls = []
    with a.active():
        engine.create_network('Home')
        monkeypatch.setattr(hub, '_binding', lambda: {'hub_url': 'https://hub.example', 'server_id': 'a'})
        hub._ensure_hub_scope()
        hub._save_hub_state(registered=True, registered_name=store.get_member(a.device_id)['name'])
        monkeypatch.setattr(hub, '_call', lambda method, route, *args, **kwargs: calls.append((method, route)) or {'devices': []})
        monkeypatch.setattr(hub, '_recovery_upload_due', False)
        for _ in range(60):
            await hub._loop_body()
        assert calls == [('GET', '')]
        assert hub.status()['sync_mode'] == 'peers'
        assert 'backup_enabled' not in hub.status()
        # A legacy enabled preference cannot reactivate removed transports.
        hub._save_hub_state(backup_enabled=True)
        await hub._loop_body()
        assert calls == [('GET', '')]



async def test_account_bootstrap_waits_then_copies_peer_without_cloud_feed(workers, monkeypatch):
    a, b = workers('a'), workers('b')
    with a.active():
        engine.create_network('Home')
        response = engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                                      encryption_public=b.identity.encryption_public, name='B')
    response['issuer']['endpoints'] = ['https://a.example.com']
    set_owner_pref(a, 'account_setting', 7)
    served = ServedBy(a, 'https://a.example.com')
    real_client = httpx.AsyncClient
    calls = []
    monkeypatch.setattr(hub, '_binding', lambda: {'hub_url': 'https://hub.example', 'server_id': 'b'})
    monkeypatch.setattr(hub, '_call', lambda method, route, *args, **kwargs: calls.append((method, route)) or {'status': 'active'})
    with b.active():
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kwargs))
        with pytest.raises(hub.HubError, match='Waiting for another worker'):
            await hub._bootstrap_from_hub(response)
        assert engine.context().bootstrap_state == engine.BOOTSTRAP_SYNCING
        engine.load_context()  # Restart while the admitting worker is offline.
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=served.transport(), **kwargs))
        assert await hub._bootstrap_from_hub()
        assert engine.context().bootstrap_state == engine.BOOTSTRAP_READY
        assert owner_pref(b, 'account_setting') == 7
        assert all(route == '' for _, route in calls)


async def test_peer_state_after_key_rotation_and_revoked_transfer_denial(workers):
    a, b, removed = [workers(name) for name in ('a', 'b', 'removed')]
    with a.active():
        engine.create_network('Home')
    join(a, b)
    join(a, removed)
    with a.active():
        stale = peer_snapshot.capture(removed.device_id)
        engine.revoke_device(removed.device_id)
        with pytest.raises(ValueError):
            peer_snapshot.page(removed.device_id, stale['transfer'], 0)
    set_owner_pref(a, 'after_rotation', 9)
    served = ServedBy(a, 'https://a.example.com')
    with b.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            await transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context())
        assert owner_pref(b, 'after_rotation') == 9
        assert engine.context().member(removed.device_id).status == 'revoked'
        assert engine.context().network_key == a.context.network_key


async def test_direct_attachments_are_encrypted_and_survive_source_offline(workers, monkeypatch):
    from app.db import database
    from app.network import blobs, peer_files
    a, b = workers('a'), workers('b')
    monkeypatch.setattr(peer_files, 'PART_BYTES', 4)
    with a.active():
        engine.create_network('Home')
        database.create_note(note_id='note', title='Peer file', content='body')
        database.create_note_attachment(attachment_id='file', note_id='note', filename='x.txt',
                                        file_path='/local/x.txt', data=b'secret bytes', size_bytes=12)
        engine.scan_all()
    join(a, b)
    digest = blobs.sha256(b'secret bytes')
    with a.active():
        sealed = peer_files.part(digest, 0, b.device_id)
        assert b'secr' not in json.dumps(sealed).encode()
    served = ServedBy(a, 'https://a.example.com')
    with b.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            data = await peer_files.fetch(client, transport.PeerLink(a.device_id, [served.base_url]), digest)
        assert data == b'secret bytes'
        assert blobs.store_fetched(digest, data)
        assert blobs.read_local(digest) == data
        # Cloud fallback is forbidden unless this worker explicitly enables it.
        monkeypatch.setattr(hub, '_call', lambda *a, **k: pytest.fail('Unexpected cloud request'))
        assert not await blobs.fetch_one('0' * 64)


async def test_snapshot_merge_keeps_conflicting_offline_edit(workers):
    a, b = workers('a'), workers('b')
    with a.active():
        engine.create_network('Home')
    join(a, b)
    set_owner_pref(a, 'same_setting', 'a edit')
    set_owner_pref(b, 'same_setting', 'b edit')
    served = ServedBy(a, 'https://a.example.com')
    with b.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            await transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context())
        conflicts = store.list_conflicts()
        assert any('same_setting' in row['record_id'] for row in conflicts)
        assert owner_pref(b, 'same_setting') in ('a edit', 'b edit')
