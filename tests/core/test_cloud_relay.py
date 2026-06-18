"""Unit tests for the cloud relay client (config + session linkage helpers)."""

import pytest

from app.core import cloud_relay


@pytest.fixture
def fake_settings(monkeypatch):
    """Provide controllable cloud config without touching a real DB/crypto."""
    store = {"url": "", "key": ""}

    monkeypatch.setattr(
        cloud_relay.database,
        "get_system_setting",
        lambda k: store["url"] if k == cloud_relay.CLOUD_URL_SETTING else None,
    )

    import app.api.settings_crypto as crypto
    monkeypatch.setattr(
        crypto,
        "get_decrypted_api_key",
        lambda k: store["key"] if k == cloud_relay.CLOUD_API_KEY_SETTING else None,
    )
    return store


def test_not_configured_when_empty(fake_settings):
    assert cloud_relay.is_configured() is False
    with pytest.raises(cloud_relay.CloudNotConfigured):
        cloud_relay.get_cloud_config()


def test_not_configured_with_url_but_no_key(fake_settings):
    fake_settings["url"] = "https://worker.example.com"
    assert cloud_relay.is_configured() is False


def test_configured_returns_normalized_url(fake_settings):
    # Trailing slash should be stripped so path joins stay clean.
    fake_settings["url"] = "https://worker.example.com/"
    fake_settings["key"] = "aih_secret"
    assert cloud_relay.is_configured() is True
    base_url, api_key = cloud_relay.get_cloud_config()
    assert base_url == "https://worker.example.com"
    assert api_key == "aih_secret"


def test_headers_use_bearer_scheme():
    assert cloud_relay._headers("abc123") == {"Authorization": "Bearer abc123"}


def test_session_remote_extracts_url_and_id():
    session = {
        "cloud_remote_url": "https://worker.example.com/",
        "cloud_remote_session_id": "remote-123",
    }
    base_url, remote_id = cloud_relay._session_remote(session)
    assert base_url == "https://worker.example.com"
    assert remote_id == "remote-123"


def test_session_remote_rejects_non_cloud_session():
    with pytest.raises(cloud_relay.CloudRelayError):
        cloud_relay._session_remote({"cloud_remote_url": None, "cloud_remote_session_id": None})
