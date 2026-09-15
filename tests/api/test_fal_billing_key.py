"""Billing credentials are local and never replace media generation credentials."""
from app.core import encryption
from app.db import database as db


def test_separate_fal_billing_key_is_encrypted_and_revoked_independently(authenticated_client):
    db.set_system_setting("fal_api_key", encryption.encrypt_value("media-only"))
    endpoint = "/api/v1/settings/integrations/fal-billing"
    response = authenticated_client.post(endpoint, json={"api_key": "billing-only"})
    assert response.status_code == 200, response.text
    stored = db.get_system_setting("fal_billing_api_key")
    assert encryption.is_encrypted(stored)
    assert encryption.decrypt_value(stored) == "billing-only"
    assert encryption.decrypt_value(db.get_system_setting("fal_api_key")) == "media-only"
    status = authenticated_client.get("/api/v1/settings/integrations")
    assert status.json()["fal_billing_api_key_set"]
    assert "billing-only" not in status.text
    assert authenticated_client.delete(endpoint).status_code == 200
    assert db.get_system_setting("fal_billing_api_key") is None
    assert encryption.decrypt_value(db.get_system_setting("fal_api_key")) == "media-only"


def test_fal_billing_key_is_not_synced():
    from app.network.policy import COLLECTIONS
    assert not COLLECTIONS["system_settings"].shared_row({"key": "fal_billing_api_key"})
