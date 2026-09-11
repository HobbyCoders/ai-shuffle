# ruff: noqa: F811  # Pytest fixtures are intentionally imported and requested by name.
import pytest

from app.db import database
from app.db import network as store
from app.network import blobs, cloud_blobs, engine, hub, object_sync, recovery
from tests.core.network.harness import join
from tests.core.network.test_checkpoints import checkpoint
from tests.core.network.test_hub import _signed_in
from tests.core.network.test_object_sync import (  # noqa: F401
    activate,
    fake,
    objects,
    workers,
)


def backup_files():
    scope = hub._ensure_hub_scope()
    for _ in range(20):
        if not cloud_blobs.publish_once(hub, scope):
            return
    pytest.fail('File backup did not finish')


async def test_offline_attachment_recovery_and_no_repeated_file_upload(workers, fake, objects, monkeypatch):
    a, b = workers('a'), workers('b')
    monkeypatch.setattr(cloud_blobs, 'PART_BYTES', 3)
    with a.active():
        engine.create_network('Home')
        activate(fake, a)
        database.create_note(note_id='note', title='Files', content='body')
        database.create_note_attachment(attachment_id='file', note_id='note', filename='x.txt',
                                        file_path='/local/x.txt', data=b'hello world', size_bytes=11)
        digest = blobs.sha256(b'hello world')
        assert store.get_blob(digest)['present']
        objects[0]['lose_ack'] = True
        assert cloud_blobs.publish_once(hub, hub._ensure_hub_scope())
        with pytest.raises(Exception, match='acknowledgement lost'):
            cloud_blobs.publish_once(hub, hub._ensure_hub_scope())
        hub.reset_for_tests()
        engine.load_context()
        backup_files()
        before = objects[0]['uploads']
        backup_files()
        assert objects[0]['uploads'] == before
        key = recovery.create()['recovery_key']
    checkpoint(a, fake)
    with b.active():
        _signed_in(fake, b)
        await hub.restore_with_recovery(key)
        assert database.get_note_attachment_data('file') == b'hello world'
        assert store.missing_blobs() == []
        assert cloud_blobs.receipt(hub._ensure_hub_scope(), digest)


def test_cloud_fallback_fetches_attachment_with_original_worker_offline(workers, fake, objects):
    a, b = workers('a'), workers('b')
    with a.active():
        engine.create_network('Home')
        database.create_note(note_id='note', title='Files', content='body')
        database.create_note_attachment(attachment_id='file', note_id='note', filename='x.txt',
                                        file_path='/local/x.txt', data=b'offline bytes', size_bytes=13)
        engine.scan_all()
    join(a, b)
    with a.active():
        activate(fake, a)
        backup_files()
    with b.active():
        activate(fake, b)
        digest = blobs.sha256(b'offline bytes')
        assert blobs.read_local(digest) is None
        assert cloud_blobs.fetch_one(hub, hub._ensure_hub_scope(), digest)
        assert blobs.read_local(digest) == b'offline bytes'
        manifest = cloud_blobs.receipt(hub._ensure_hub_scope(), digest)
        objects[2][manifest['parts'][0]] = b'corrupt'
        with pytest.raises(object_sync.ObjectError):
            cloud_blobs.open_file(hub, manifest, engine.require_context())
