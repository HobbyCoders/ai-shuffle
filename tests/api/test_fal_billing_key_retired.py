"""The separate fal.ai Admin billing key is gone; the ordinary key reads the balance."""
import pytest

from app.core import encryption
from app.db import database as db
from app.db.base import get_db


@pytest.mark.parametrize("method", ["post", "delete"])
def test_billing_key_routes_are_removed(authenticated_client, method):
    kwargs = {"json": {"api_key": "billing-only"}} if method == "post" else {}
    response = getattr(authenticated_client, method)("/api/v1/settings/integrations/fal-billing", **kwargs)
    assert response.status_code in (404, 405)
    assert db.get_system_setting("fal_billing_api_key") is None


def test_integrations_no_longer_reports_a_billing_key(authenticated_client):
    db.set_system_setting("fal_api_key", encryption.encrypt_value("media-key-0123456789abcdef"))
    body = authenticated_client.get("/api/v1/settings/integrations").json()
    assert body["fal_api_key_set"] is True
    assert "fal_billing_api_key_set" not in body


def _fal_snapshot(cursor):
    cursor.execute(
        "INSERT INTO provider_usage_snapshots (provider_id, provider_type, windows, credits, updated_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        ("fal-ai", "fal", "[]", '{"balance_remaining": 1}', "old"),
    )


def _snapshot_count(cursor):
    cursor.execute("SELECT COUNT(*) AS n FROM provider_usage_snapshots WHERE provider_id = 'fal-ai'")
    return int(cursor.fetchone()["n"])


def test_upgrade_deletes_the_stored_billing_key_and_its_balance(mock_db):
    db.set_system_setting("fal_api_key", "media-key")
    db.set_system_setting("fal_billing_api_key", "enc:old-admin-key")
    with get_db() as conn:
        cursor = conn.cursor()
        _fal_snapshot(cursor)
        assert db._retire_fal_billing_key(cursor) == ["system_settings.fal_billing_api_key"]
        assert _snapshot_count(cursor) == 0
        # Idempotent: nothing left to retire on the next pass.
        assert db._retire_fal_billing_key(cursor) == []
    assert db.get_system_setting("fal_billing_api_key") is None
    assert db.get_system_setting("fal_api_key") == "media-key"


def test_upgrade_without_a_billing_key_keeps_the_balance(mock_db):
    with get_db() as conn:
        cursor = conn.cursor()
        _fal_snapshot(cursor)
        assert db._retire_fal_billing_key(cursor) == []
        assert _snapshot_count(cursor) == 1


def test_saving_or_removing_the_media_key_drops_the_cached_balance(mock_db):
    def cached():
        with get_db() as conn:
            return _snapshot_count(conn.cursor())

    with get_db() as conn:
        _fal_snapshot(conn.cursor())
    db.set_system_setting("fal_api_key", "media-key-2")
    assert cached() == 0
    with get_db() as conn:
        _fal_snapshot(conn.cursor())
    db.delete_system_setting("fal_api_key")
    assert cached() == 0
