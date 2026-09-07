from unittest.mock import patch
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import preferences
from app.core.global_events import global_events
from app.db import settings
from app.db import database as db


def test_two_clients_cannot_overwrite_a_newer_preference_snapshot():
    row = settings.set_user_preference('account', 'hub#owner', 'composer.effort', 'high', expected_version=None)
    settings.set_user_preference('account', 'hub#owner', 'composer.effort', 'max', expected_version=row['revision'])
    with patch.object(global_events, 'preference_changed') as changed:
        with pytest.raises(settings.PreferenceConflict):
            settings.set_user_preference('account', 'hub#owner', 'composer.effort', 'low', expected_version=row['revision'])
        changed.assert_not_called()
    assert settings.get_user_preference('account', 'hub#owner', 'composer.effort')['value'] == 'max'
    # A different account has an independent row and version.
    assert settings.set_user_preference('account', 'hub#other', 'composer.effort', 'low', expected_version=None)['value'] == 'low'


def test_http_versions_and_explicit_null_creation_are_atomic():
    app = FastAPI()
    app.include_router(preferences.router)
    app.dependency_overrides[preferences.require_auth] = lambda: 'authenticated'
    with patch.object(preferences, 'get_user_identity', return_value=('account', 'hub#alice')), TestClient(app) as client:
        first = client.put('/api/v1/preferences/composer.profile', json={'key':'composer.profile', 'value':'a', 'expected_version':None})
        assert first.status_code == 200
        snapshot = client.get('/api/v1/preferences/all').json()
        assert snapshot['versions']['composer.profile'] == first.json()['version']
        assert client.put('/api/v1/preferences/composer.profile', json={'key':'composer.profile', 'value':'b', 'expected_version':None}).status_code == 409
        assert client.post('/api/v1/preferences/composer.profile', json={'key':'composer.profile', 'value':None, 'expected_version':first.json()['version']}).status_code == 200


def test_revisions_change_even_when_every_write_has_the_same_timestamp(monkeypatch):
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, tzinfo=timezone.utc)

    monkeypatch.setattr(settings, 'datetime', FrozenClock)
    first = settings.set_user_preference('account', 'owner', 'composer.effort', 'high')
    seen = {first['revision']}
    previous = first
    for index in range(30):
        current = settings.set_user_preference('account', 'owner', 'composer.effort', index,
                                               expected_version=previous['revision'])
        UUID(current['revision'])
        assert current['updated_at'] == first['updated_at']
        assert current['revision'] not in seen
        seen.add(current['revision'])
        previous = current
    with pytest.raises(settings.PreferenceConflict):
        settings.set_user_preference('account', 'owner', 'composer.effort', 'stale',
                                     expected_version=first['revision'])


def test_legacy_writes_rotate_revisions_and_missing_only_keeps_current_version():
    first = settings.set_user_preference('admin', 'admin', 'theme', 'dark', expected_version=None)
    imported = settings.set_user_preference('admin', 'admin', 'theme', 'light', only_if_missing=True)
    assert imported['revision'] == first['revision']
    assert imported['value'] == 'dark'
    legacy = settings.set_user_preference('admin', 'admin', 'theme', 'light')
    assert legacy['revision'] != first['revision']
    with pytest.raises(settings.PreferenceConflict):
        settings.set_user_preference('admin', 'admin', 'theme', 'stale', expected_version=first['revision'])


def test_delete_recreate_cannot_reuse_a_stale_version():
    old = settings.set_user_preference('account', 'a', 'choice', 'old')
    settings.delete_user_preference('account', 'a', 'choice')
    replacement = settings.set_user_preference('account', 'a', 'choice', 'new', expected_version=None)
    assert replacement['revision'] != old['revision']
    with pytest.raises(settings.PreferenceConflict):
        settings.set_user_preference('account', 'a', 'choice', 'stale', expected_version=old['revision'])


def test_revision_migration_is_idempotent_and_preserves_legacy_values(mock_db):
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('ALTER TABLE user_preferences DROP COLUMN revision')
        cursor.execute('''INSERT INTO user_preferences (user_type, user_id, key, value, updated_at)
            VALUES (%s, %s, %s, %s, %s)''', ('admin', 'admin', 'theme', '"dark"', '2026-01-01'))
        db._migrate_preference_revisions(cursor)
    first = settings.get_user_preference('admin', 'admin', 'theme')
    UUID(first['revision'])
    assert first['value'] == 'dark'
    assert first['updated_at'] == '2026-01-01'
    with db.get_db() as conn:
        db._migrate_preference_revisions(conn.cursor())
    assert settings.get_user_preference('admin', 'admin', 'theme')['revision'] == first['revision']
