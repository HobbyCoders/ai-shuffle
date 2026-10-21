"""Local tuning is persisted and validated through the provider API."""
import pytest

ROOT = "/api/v1/model-providers"


def create(client, config, **overrides):
    return client.post(ROOT, json={
        "name": "Local", "provider_type": "custom", "api_dialect": "openai",
        "base_url": "http://fixture.invalid", "models": [
            {"id": "local", "label": "Local", "local_settings": config}], **overrides})


def test_create_patch_read_and_clear(authenticated_client, mock_db):
    config = {"server": "llama_cpp", "preset": "qwen3-thinking", "sampling": {"top_k": 10}}
    response = create(authenticated_client, config)
    assert response.status_code == 200, response.text
    provider = response.json()
    assert provider["models"][0]["local_settings"] == config
    url = ROOT + "/" + provider["id"]
    config = {"server": "vllm", "reasoning": False, "sampling": {"min_p": 0.2}}
    response = authenticated_client.patch(url, json={"models": [
        {"id": "local", "label": "Local", "local_settings": config}]})
    assert response.status_code == 200, response.text
    saved = next(p for p in authenticated_client.get(ROOT).json()["providers"]
                 if p["id"] == provider["id"])
    assert saved["models"][0]["local_settings"] == config
    # A dialect switch must not silently strand incompatible model settings.
    assert authenticated_client.patch(url, json={"api_dialect": "anthropic"}).status_code == 400
    response = authenticated_client.patch(url, json={"models": [
        {"id": "local", "label": "Local", "local_settings": None}]})
    assert response.status_code == 200
    saved = next(p for p in authenticated_client.get(ROOT).json()["providers"]
                 if p["id"] == provider["id"])
    assert saved["models"][0]["local_settings"] is None


@pytest.mark.parametrize("config", [
    {"server": "unknown"}, {"server": "vllm", "preset": []},
    {"server": "vllm", "sampling": {"top_p": 2}},
    {"server": "vllm", "sampling": {"top_k": True}},
    {"server": "vllm", "reasoning": "off"},
])
def test_invalid_settings_return_400(authenticated_client, mock_db, config):
    assert create(authenticated_client, config).status_code == 400


def test_local_settings_are_not_sent_to_cloud_routes(authenticated_client, mock_db):
    assert create(authenticated_client, {"server": "vllm"}, provider_type="openrouter").status_code == 400
