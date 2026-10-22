"""Owner-wide commit attribution API and strict boolean round-trip."""
import pytest

from app.core import commit_attribution
from app.db import database

ENDPOINT = "/api/v1/settings/commit-attribution"


@pytest.mark.parametrize("method", ["get", "put"])
def test_requires_auth(client, method):
    response = getattr(client, method)(
        ENDPOINT, **({"json": {"enabled": False}} if method == "put" else {})
    )
    assert response.status_code in (401, 403)


def test_default_and_roundtrip(authenticated_client):
    assert authenticated_client.get(ENDPOINT).json() == {"enabled": True}
    for enabled in (False, True):
        response = authenticated_client.put(ENDPOINT, json={"enabled": enabled})
        assert response.status_code == 200, response.text
        assert response.json() == {"enabled": enabled}
        assert authenticated_client.get(ENDPOINT).json() == response.json()
        assert database.get_system_setting(commit_attribution.SETTING_KEY) == (
            "true" if enabled else "false"
        )


@pytest.mark.parametrize("body", [{}, {"enabled": None}, {"enabled": "false"},
                                 {"enabled": 0}, {"enabled": []}])
def test_invalid_input_does_not_change_policy(authenticated_client, body):
    response = authenticated_client.put(ENDPOINT, json=body)
    assert response.status_code == 422
    assert authenticated_client.get(ENDPOINT).json() == {"enabled": True}


def test_api_user_cannot_change_owner_policy(client):
    from app.api.auth import hash_api_key

    key = "test-attribution-api-user"
    database.create_api_user("attribution-reader", "Reader", hash_api_key(key))
    response = client.put(ENDPOINT, json={"enabled": False},
                          headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 403
    assert commit_attribution.read() is True
