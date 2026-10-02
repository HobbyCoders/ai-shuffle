"""PUT /settings/integrations/fal-ai/enabled and fal_enabled on GET /integrations."""
from unittest.mock import patch

import pytest

from app.api.auth import hash_api_key
from app.api.settings_crypto import set_encrypted_api_key
from app.core import fal_credential
from app.db import database

BASE = "/api/v1/settings"
URL = f"{BASE}/integrations/fal-ai/enabled"
FAL_KEY = "fal-0123456789abcdef:0123456789abcdef0123456789abcdef"
USER_KEY = "aih_fal_enabled_user_key"


@pytest.fixture
def c(authenticated_client):
    return authenticated_client


def test_integrations_reports_fal_enabled_true_by_default(c):
    body = c.get(f"{BASE}/integrations").json()
    assert body["fal_enabled"] is True


def test_put_turns_fal_off_and_on(c):
    set_encrypted_api_key("fal_api_key", FAL_KEY)
    r = c.put(URL, json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"fal_enabled": False, "fal_api_key_set": True}
    assert fal_credential.is_enabled() is False
    body = c.get(f"{BASE}/integrations").json()
    assert body["fal_enabled"] is False
    # Off keeps the key.
    assert body["fal_api_key_set"] is True

    r = c.put(URL, json={"enabled": True})
    assert r.json() == {"fal_enabled": True, "fal_api_key_set": True}
    assert c.get(f"{BASE}/integrations").json()["fal_enabled"] is True


def test_put_without_a_key_reports_it(c):
    assert c.put(URL, json={"enabled": False}).json() == {"fal_enabled": False, "fal_api_key_set": False}


def test_put_never_returns_key_material(c):
    set_encrypted_api_key("fal_api_key", FAL_KEY)
    assert FAL_KEY not in c.put(URL, json={"enabled": False}).text


def test_put_fires_the_settings_resource_event(c):
    from app.core.global_events import global_events

    with patch.object(global_events, "publish") as publish:
        assert c.put(URL, json={"enabled": False}).status_code == 200
    resources = [call.args[0]["resources"] for call in publish.call_args_list]
    assert resources and all("settings" in r for r in resources)


@pytest.mark.parametrize("body", [{}, {"enabled": "maybe"}, {"enabled": None}])
def test_put_validates_the_body(c, body):
    assert c.put(URL, json=body).status_code == 422
    assert fal_credential.is_enabled() is True


def test_put_is_owner_only(client):
    database.create_api_user("fal-enabled-user", "Fal Enabled User", hash_api_key(USER_KEY))
    r = client.put(URL, json={"enabled": False}, headers={"Authorization": f"Bearer {USER_KEY}"})
    assert r.status_code == 403
    assert fal_credential.is_enabled() is True


def test_put_requires_authentication(client):
    assert client.put(URL, json={"enabled": False}).status_code in (401, 403)
    assert fal_credential.is_enabled() is True


def test_catalog_refresh_runs_without_the_key_while_off(c, monkeypatch):
    from app.services.fal_catalog import catalog_service

    set_encrypted_api_key("fal_api_key", FAL_KEY)
    seen = []

    async def refresh_with_status(api_key):
        seen.append(api_key)
        return 0, None

    monkeypatch.setattr(catalog_service, "refresh_with_status", refresh_with_status)
    c.post("/api/v1/catalog/refresh")
    c.put(URL, json={"enabled": False})
    c.post("/api/v1/catalog/refresh")
    assert seen == [FAL_KEY, None]


async def test_voice_lists_and_funds_fal_only_while_on(c):
    from fastapi import HTTPException
    from app.api import voice
    from app.core.voice_models import FAL_PROVIDER_ID

    set_encrypted_api_key("fal_api_key", FAL_KEY)
    assert FAL_PROVIDER_ID in [p["id"] for p in voice.list_eligible_voice_providers()]
    row = voice._eligible_provider_row(FAL_PROVIDER_ID)
    assert await voice._provider_api_key(row, api_user=None) == FAL_KEY

    fal_credential.set_enabled(False)
    assert FAL_PROVIDER_ID not in [p["id"] for p in voice.list_eligible_voice_providers()]
    # The row stays eligible so a call on it explains why it cannot start.
    row = voice._eligible_provider_row(FAL_PROVIDER_ID)
    with pytest.raises(HTTPException) as exc_info:
        await voice._provider_api_key(row, api_user=None)
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == fal_credential.OFF_MESSAGE
