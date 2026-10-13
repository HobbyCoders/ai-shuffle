"""Credential rotation and observation locking on disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from tests.db.postgres_harness import pg_schema, requires_real_pg  # noqa: F401  (pg_schema: autouse fixture)

pytestmark = requires_real_pg


@pytest.mark.parametrize("action", ["rotate", "disconnect", "delete"])
def test_committing_observation_cannot_survive_credential_invalidation(monkeypatch, action):
    from app.api.openrouter_billing import _save
    from app.core.openrouter_usage import SETTING_PREFIX, SNAPSHOT_PREFIX
    from app.db import database as db
    from app.db import provider_usage_snapshots as snapshots

    provider = "router-" + uuid4().hex
    db.create_model_provider(provider, "Router", "openrouter", "https://openrouter.ai/api/v1")
    assert _save(provider, "enc:disposable-fixture-a")
    generation = db.get_system_setting_revision(SETTING_PREFIX + provider)
    locked, release, invalidating = Event(), Event(), Event()
    original = snapshots._lock_usage_generation

    def hold_transaction(*args):
        result = original(*args)
        assert result
        locked.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(snapshots, "_lock_usage_generation", hold_transaction)

    def invalidate():
        invalidating.set()
        if action == "rotate":
            assert _save(provider, "enc:disposable-fixture-b")
        elif action == "disconnect":
            assert db.delete_system_setting(SETTING_PREFIX + provider)
        else:
            assert db.delete_model_provider(provider)

    with ThreadPoolExecutor(max_workers=2) as executor:
        write = executor.submit(db.upsert_provider_usage_snapshot, SNAPSHOT_PREFIX + provider,
                                "openrouter_account", None, [], {"balance_remaining": 10},
                                {"source": "openrouter_management"}, 123,
                                expected_credential_generation=generation)
        try:
            assert locked.wait(10)
            revoke = executor.submit(invalidate)
            assert invalidating.wait(10)
        finally:
            release.set()
        assert write.result(timeout=10)
        revoke.result(timeout=10)
    assert not any(s["provider_id"] == SNAPSHOT_PREFIX + provider
                   for s in db.get_all_provider_usage_snapshots())
    monkeypatch.setattr(snapshots, "_lock_usage_generation", original)
    # Both a late success and a late error from the old generation are rejected.
    assert not db.upsert_provider_usage_snapshot(SNAPSHOT_PREFIX + provider, "openrouter_account",
                                                None, [], {"balance_remaining": 999}, {}, 124,
                                                expected_credential_generation=generation)
    db.record_provider_usage_failure(SNAPSHOT_PREFIX + provider, "openrouter_account", "unavailable",
                                     expected_credential_generation=generation)
    assert not any(s["provider_id"] == SNAPSHOT_PREFIX + provider
                   for s in db.get_all_provider_usage_snapshots())
