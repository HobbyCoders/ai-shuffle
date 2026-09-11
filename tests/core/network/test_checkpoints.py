# ruff: noqa: F811  # Pytest fixtures are intentionally imported and requested by name.
import copy

import pytest

from app.db import database
from app.db import network as store
from app.network import checkpoints, engine, hub, object_sync, recovery
from tests.core.network.harness import join
from tests.core.network.test_hub import _signed_in
from tests.core.network.test_object_sync import (  # noqa: F401
    activate,
    fake,
    objects,
    workers,
)


def checkpoint(a, fake):
    with a.active():
        activate(fake, a)
        engine.scan_all()
        while hub.push_once():
            hub._save_hub_state(object_batch_due=0)
        hub.pull_once()
        assert checkpoints.capture(hub, hub._ensure_hub_scope())
        for _ in range(100):
            if not checkpoints.publish_once(hub, hub._ensure_hub_scope()):
                break
        assert hub._hub_state()['checkpoint_id']


async def test_recovery_checkpoint_restores_new_device_with_all_originals_offline(workers, fake, objects):
    a, b = workers('a'), workers('b')
    with a.active():
        engine.create_network('Home')
        database.set_system_setting('tts_model', 'checkpoint-restored')
        engine.scan_all()
        recovery_key = recovery.create()['recovery_key']
    checkpoint(a, fake)
    # B has no identity, membership, keys or snapshots from A. Only account
    # access and the user's recovery key are available while A stays offline.
    with b.active():
        _signed_in(fake, b)
        result = await hub.restore_with_recovery(recovery_key)
        assert result['restored']
        assert database.get_system_setting('tts_model') == 'checkpoint-restored'
        assert engine.context().bootstrap_state == engine.BOOTSTRAP_READY
        assert hub._hub_state()['restored_checkpoint']


def test_failed_checkpoint_replacement_retains_last_valid_root_and_reuses_parts(workers, fake, objects):
    a = workers('a')
    with a.active():
        engine.create_network('Home')
    checkpoint(a, fake)
    with a.active():
        first = hub._hub_state()['checkpoint_id']
        database.set_system_setting('tts_model', 'next-checkpoint')
        engine.scan_all()
        hub._save_hub_state(object_batch_due=0)
        hub.push_once(); hub.pull_once()
        assert checkpoints.capture(hub, hub._ensure_hub_scope())
        prepared = copy.deepcopy(checkpoints._pending(hub._ensure_hub_scope(), all_rows=True))
        objects[0]['fail_upload'] = True
        with pytest.raises(object_sync.ObjectError):
            checkpoints.publish_once(hub, hub._ensure_hub_scope())
        assert hub._hub_state()['checkpoint_id'] == first
        assert checkpoints._pending(hub._ensure_hub_scope(), all_rows=True) == prepared
        objects[0]['fail_upload'] = False
        hub.reset_for_tests(); engine.load_context()
        while checkpoints.publish_once(hub, hub._ensure_hub_scope()):
            pass
        assert hub._hub_state()['checkpoint_id'] != first
        assert first in objects[4]


async def test_tampered_checkpoint_part_does_not_finish_bootstrap_or_advance_cursor(workers, fake, objects):
    a, b = workers('a'), workers('b')
    with a.active():
        engine.create_network('Home')
        key = recovery.create()['recovery_key']
    checkpoint(a, fake)
    part = next(d for d in objects[4].values() if d['kind'] == 'checkpoint_part')
    objects[2][part['object_id']] = b'corrupt'
    with b.active():
        _signed_in(fake, b)
        with pytest.raises(object_sync.ObjectError):
            await hub.restore_with_recovery(key)
        assert engine.context().bootstrap_state == engine.BOOTSTRAP_SYNCING
        assert hub._hub_state().get('cursor', 0) == 0


def test_checkpoint_refuses_sequence_holes(workers, fake, objects):
    a = workers('a')
    with a.active():
        engine.create_network('Home')
        activate(fake, a)
        hub.push_once(); hub.pull_once()
        with store.get_db() as conn:
            op = store.ops_since({})[0]
            op = {**op, 'origin_seq': 1000}
            store.stash_pending(conn.cursor(), op)
        assert checkpoints.capture(hub, hub._ensure_hub_scope()) is False
        assert checkpoints._pending(hub._ensure_hub_scope(), all_rows=True) == []


async def test_rotation_replaces_stale_prepared_checkpoint_and_recovers_history(workers, fake, objects):
    a, revoked, recovered = workers('a'), workers('revoked'), workers('recovered')
    with a.active():
        engine.create_network('Home')
    join(a, revoked)
    checkpoint(a, fake)
    with a.active():
        assert checkpoints.capture(hub, hub._ensure_hub_scope())
        stale = store._load(checkpoints._pending(hub._ensure_hub_scope(), all_rows=True)[-1]['descriptor'])
        engine.revoke_device(revoked.device_id)
        database.set_system_setting('tts_model', 'after-rotation')
        engine.scan_all()
        hub._save_hub_state(object_batch_due=0)
        hub.push_once()
        hub.pull_once()
        key = recovery.create()['recovery_key']
        hub._upload_recovery()
        while checkpoints.publish_once(hub, hub._ensure_hub_scope()):
            pass
        latest = objects[4][hub._hub_state()['checkpoint_id']]
        assert latest['key_digest'] != stale['key_digest']
    with recovered.active():
        _signed_in(fake, recovered)
        await hub.restore_with_recovery(key)
        assert database.get_system_setting('tts_model') == 'after-rotation'
        assert engine.context().member(revoked.device_id).status == 'revoked'
