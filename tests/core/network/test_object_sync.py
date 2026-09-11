# ruff: noqa: F811  # Pytest fixtures are intentionally imported and requested by name.
"""Object-store interruptions run through the real PostgreSQL-shaped outbox and engine."""
import copy
import gzip
import json

import pytest
from fastapi import HTTPException

from app.core import account_hub
from app.db import database
from app.db import network as store
from app.network import engine, hub, object_sync
from tests.core.network.harness import exchange, join
from tests.core.network.test_hub import _signed_in, fake, workers  # noqa: F401


@pytest.fixture
def objects(fake, monkeypatch):
    original = account_hub.call
    saved, uploaded, entries, committed_objects = {}, {}, [], {}
    counters = {"uploads": 0, "commits": 0, "fail_upload": False, "lose_ack": False}

    def call(binding, method, suffix, body=None, **kwargs):
        path = suffix.split('?')[0]
        body = body or {}
        if path == 'network/objects/archive-page':
            last = body['cursor']
            ops = fake.feed[last:last + 200]
            return {'ops': copy.deepcopy(ops), 'source_start': last + 1, 'cursor': last + len(ops)}
        if path == 'network/objects/checkpoint':
            roots = [d for d in committed_objects.values() if d['kind'] == 'checkpoint']
            return {"checkpoint": copy.deepcopy(roots[-1]) if roots else None}
        if path == 'network/objects/blob':
            roots = [d for d in committed_objects.values() if d['kind'] == 'blob' and d['blob_hash'] == body['hash']]
            return {"file": copy.deepcopy(roots[-1]) if roots else None}
        if path == 'network/objects/known':
            return {"known": [item for item in body['identities'] if tuple(item) in fake.seen]}
        if path == 'network/objects/reserve':
            d = copy.deepcopy(body['descriptor'])
            saved.setdefault(d['object_id'], d)
            committed = committed_objects.get(d['object_id'])
            return {"committed": bool(committed), "descriptor": committed, "upload": {"id": d['object_id']}}
        if path.startswith('network/objects/'):
            object_id = path.split('/')[2]
            if path.endswith('/commit'):
                if object_id not in uploaded:
                    raise HTTPException(503, 'Missing object')
                existing = committed_objects.get(object_id)
                if existing is None:
                    d = copy.deepcopy(saved[object_id])
                    if d['kind'] == 'batch':
                        d.update(cursor_start=len(fake.feed) + 1, cursor_end=len(fake.feed) + d['op_count'])
                        ops = json.loads(gzip.decompress(uploaded[object_id]))['ops']
                        fake.push(binding['server_id'], {"ops": ops}, {})
                        entries.append({"cursor": d['cursor_end'], "object": d})
                    else:
                        d.update(cursor_start=None, cursor_end=d.get('base_cursor'))
                    committed_objects[object_id] = d
                    counters['commits'] += 1
                    existing = d
                if counters['lose_ack']:
                    counters['lose_ack'] = False
                    raise HTTPException(503, 'Commit acknowledgement lost')
                return {"committed": True, "descriptor": copy.deepcopy(existing)}
            return {"descriptor": copy.deepcopy(committed_objects[object_id]),
                    "download": {"id": object_id}}
        result = original(binding, method, suffix, body, **kwargs)
        if path == 'network/feed':
            cursor = int(dict(part.split('=') for part in suffix.split('?')[1].split('&'))['cursor'])
            result.update(object_sync={"v": 2, "writes": True, "direct_read": True}, entries=copy.deepcopy([e for e in entries if e['cursor'] > cursor]))
        return result

    def transfer(grant, payload=None):
        if payload is not None:
            if counters['fail_upload']:
                raise object_sync.ObjectError('Synthetic interrupted upload')
            if grant['id'] not in uploaded:
                uploaded[grant['id']] = payload
                counters['uploads'] += 1
            return b''
        return uploaded[grant['id']]

    monkeypatch.setattr(account_hub, 'call', call)
    monkeypatch.setattr(object_sync, 'transfer', transfer)
    return counters, saved, uploaded, entries, committed_objects


def activate(fake, worker):
    _signed_in(fake, worker)
    hub.register()
    hub._ensure_hub_scope()
    hub._save_hub_state(object_sync={"v": 2, "writes": True}, object_batch_due=0)


def test_nonoverlapping_uptime_and_no_empty_or_duplicate_uploads(workers, fake, objects):
    a, b = workers('a'), workers('b')
    with a.active():
        engine.create_network('Home')
    join(a, b)
    with a.active():
        activate(fake, a)
        database.set_system_setting('tts_model', 'object-offline')
        engine.scan_all()
        assert hub.push_once() > 0
        for _ in range(20):
            hub.push_once()
        assert objects[0]['uploads'] == 1
    # A never runs during B's catch-up.
    with b.active():
        activate(fake, b)
        assert hub.pull_once() > 0
        assert database.get_system_setting('tts_model') == 'object-offline'
        hub.push_once()  # skips the exact cloud receipts including other origins
        assert objects[0]['uploads'] == 1
    assert exchange(a, b).applied == 0


@pytest.mark.parametrize('failure', ['fail_upload', 'lose_ack'])
def test_restart_reuses_prepared_bytes_and_only_acknowledges_durable_commit(workers, fake, objects, failure):
    a = workers('a')
    with a.active():
        engine.create_network('Home')
        activate(fake, a)
        objects[0][failure] = True
        with pytest.raises(hub.HubError):
            hub.push_once()
        before = object_sync._outbox(hub._ensure_hub_scope())
        assert before and hub._hub_state().get('pushed_ingest_id', 0) == 0
        objects[0][failure] = False
        hub.reset_for_tests()
        engine.load_context()
        assert hub.push_once() > 0
        assert object_sync._outbox(hub._ensure_hub_scope()) is None
        assert objects[0]['uploads'] == objects[0]['commits'] == 1
        assert next(iter(objects[1])) == before['object_id']


def test_batch_delay_is_durable_and_cloud_failure_does_not_block_peer_sync(workers, fake, objects, monkeypatch):
    a, b = workers('a'), workers('b')
    with a.active():
        engine.create_network('Home')
    join(a, b)
    with a.active():
        activate(fake, a)
        hub._save_hub_state(object_batch_due=None)
        database.set_system_setting('tts_model', 'lan-immediate')
        engine.scan_all()
        monkeypatch.setattr(object_sync.time, 'time', lambda: 1000)
        assert hub.push_once() == 0
        assert hub._hub_state()['object_batch_due'] == 1060
        hub.reset_for_tests()
        engine.load_context()
        assert hub.push_once() == 0
    assert exchange(a, b).applied > 0
    with a.active():
        monkeypatch.setattr(object_sync.time, 'time', lambda: 1060)
        assert hub.push_once() > 0
        assert objects[0]['uploads'] == 1


def test_tampered_bundle_keeps_cursor_and_out_of_order_ops_do_not_acknowledge_holes(workers, fake, objects):
    a, b = workers('a'), workers('b')
    with a.active():
        engine.create_network('Home')
    join(a, b)
    with a.active():
        activate(fake, a)
        database.set_system_setting('tts_model', 'safe-value')
        engine.scan_all()
        hub.push_once()
    object_id = next(iter(objects[2]))
    valid = objects[2][object_id]
    objects[2][object_id] = valid[:-1] + bytes([valid[-1] ^ 1])
    with b.active():
        activate(fake, b)
        with pytest.raises(hub.HubError, match='cursor retained'):
            hub.pull_once()
        assert hub._hub_state().get('cursor', 0) == 0
        objects[2][object_id] = valid
        hub.pull_once()
        assert database.get_system_setting('tts_model') == 'safe-value'


def test_descriptor_and_payload_validation(workers):
    a = workers('a')
    with a.active():
        ctx = engine.create_network('Home')
        ops = store.ops_since({})
        d, payload = object_sync.encode(ops, ctx)
        assert object_sync.decode(d, payload, ctx) == ops
        for field, value in [('publisher', 'dev_' + '0' * 24), ('network_id', 'other'), ('bytes', len(payload) + 1)]:
            with pytest.raises(object_sync.ObjectError):
                object_sync.decode({**d, field: value}, payload, ctx)
        with pytest.raises(object_sync.ObjectError):
            object_sync.decode(d, payload + payload, ctx)
        assert b'Home' not in payload  # record contents remain sealed before compression


def test_feature_rollback_drains_prepared_outbox_without_false_ack(workers, fake, objects):
    a = workers('a')
    with a.active():
        engine.create_network('Home')
        activate(fake, a)
        objects[0]['fail_upload'] = True
        with pytest.raises(hub.HubError):
            hub.push_once()
        assert object_sync._outbox(hub._ensure_hub_scope())
        hub._save_hub_state(object_sync={'v': 2, 'writes': False})
        assert hub.push_once() > 0
        assert object_sync._outbox(hub._ensure_hub_scope()) is None
        assert len(fake.feed) > 0 and objects[0]['uploads'] == 0


def test_archive_retries_same_bytes_and_retains_legacy_history(workers, fake, objects):
    a = workers('a')
    with a.active():
        engine.create_network('Home')
        activate(fake, a)
        hub._save_hub_state(object_sync=None)
        hub.push_once()
        before = copy.deepcopy(fake.feed)
        objects[0]['lose_ack'] = True
        with pytest.raises(hub.HubError, match='acknowledgement lost'):
            object_sync.archive_once(hub, hub._ensure_hub_scope())
        assert not hub._hub_state().get('archive_cursor')
        hub.reset_for_tests()
        engine.load_context()
        assert object_sync.archive_once(hub, hub._ensure_hub_scope())
        assert hub._hub_state()['archive_cursor'] == len(before)
        assert fake.feed == before
        assert objects[0]['uploads'] == 1


def test_download_cursor_is_independent_of_applied_sequence_watermarks(workers, fake, objects):
    a, b = workers('a'), workers('b')
    with a.active():
        engine.create_network('Home')
    join(a, b)
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        database.set_system_setting('tts_model', 'first')
        engine.scan_all()
        database.set_system_setting('tts_model', 'second')
        engine.scan_all()
        ops = store.ops_since(marks, exclude_origin=b.device_id)
        assert len(ops) >= 2
    with b.active():
        activate(fake, b)
        scope = hub._ensure_hub_scope()
        object_sync.pull_entry(hub, scope, {'cursor': 1, 'ops': ops[1:]})
        assert store.pending_holes()
        assert store.local_watermarks() == marks
        object_sync.pull_entry(hub, scope, {'cursor': 2, 'ops': ops[:1]})
        assert store.pending_holes() == []
        assert database.get_system_setting('tts_model') == 'second'


def test_legacy_bridge_retains_unknown_members_until_later_membership_arrives(workers, fake, objects, monkeypatch):
    a, b, c = workers('a'), workers('b'), workers('c')
    with a.active():
        engine.create_network('Home')
    join(a, b)
    with b.active():
        marks = store.local_watermarks()
    join(a, c)
    with c.active():
        database.set_system_setting('tts_model', 'late-membership')
        engine.scan_all()
        unknown = [op for op in store.ops_since(marks) if op['origin_device_id'] == c.device_id]
        assert unknown
    with a.active():
        activate(fake, a)
        membership_ops = store.ops_since(marks, exclude_origin=b.device_id)
    with b.active():
        activate(fake, b)
        pages = iter([
            {'ops': unknown, 'cursor': 1, 'more': True},
            {'ops': membership_ops, 'cursor': 2, 'more': False},
        ])

        def bridge(*args, **kwargs):
            return {**next(pages), 'object_sync': {'v': 2, 'writes': False, 'direct_read': False}}

        monkeypatch.setattr(hub, '_call', bridge)
        hub.pull_once()
        assert hub._hub_state()['cursor'] == 1
        assert object_sync.inbox_count(hub._ensure_hub_scope()) == len(unknown)
        hub.pull_once()
        assert object_sync.inbox_count(hub._ensure_hub_scope()) == 0
        assert database.get_system_setting('tts_model') == 'late-membership'


@pytest.fixture(autouse=True)
def explicit_cloud_backup(monkeypatch):
    """These tests exercise the optional cloud transport."""
    from app.network import backup
    monkeypatch.setattr(backup, "enabled", lambda: True)
