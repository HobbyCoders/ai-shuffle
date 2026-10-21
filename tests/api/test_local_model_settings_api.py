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


def test_rename_ignores_legacy_non_dict_model_entries(authenticated_client, mock_db):
    from app.db import model_providers

    response = create(authenticated_client, None)
    assert response.status_code == 200, response.text
    provider_id = response.json()["id"]
    models = ["legacy-model", None, {"id": "local", "label": "Local"}]
    model_providers.update_model_provider(provider_id, models_json=models)
    response = authenticated_client.patch(ROOT + "/" + provider_id, json={"name": "Renamed"})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"
    expected = [{"id": "legacy-model", "label": "legacy-model"}, {"id": "local", "label": "Local"}]
    assert response.json()["models"] == expected
    saved = next(p for p in authenticated_client.get(ROOT).json()["providers"]
                 if p["id"] == provider_id)
    assert saved["models"] == expected
    # A rename must not rewrite synced/legacy model data.
    assert model_providers.get_model_provider(provider_id)["models_json"] == models


@pytest.fixture(params=[
    {"server": "sglang"},
    {"server": "vllm", "sampling": {"future_sampling_key": 0.5}},
])
def synced_unknown_settings(authenticated_client, mock_db, request):
    from app.db import model_providers

    response = create(authenticated_client, None, models=[
        {"id": "local", "label": "Local"},
        {"id": "other", "label": "Other", "local_settings": {"server": "vllm"}},
    ])
    assert response.status_code == 200, response.text
    provider_id = response.json()["id"]
    models = model_providers.get_model_provider(provider_id)["models_json"]
    models[0]["local_settings"] = request.param
    model_providers.update_model_provider(provider_id, models_json=models)
    return provider_id, models


@pytest.mark.parametrize("change", ["rename", "toggle", "round_trip", "other_model"])
def test_patch_preserves_unchanged_unknown_local_settings(
    authenticated_client, synced_unknown_settings, change
):
    from app.db import model_providers

    provider_id, models = synced_unknown_settings
    if change == "rename":
        body = {"name": "Renamed"}
    elif change == "toggle":
        body = {"enabled": False}
    elif change == "round_trip":
        models[0]["label"] = "Relabelled"
        body = {"models": models, "provider_type": "custom", "api_dialect": "openai"}
    else:
        models[1]["local_settings"] = {"server": "vllm", "sampling": {"temperature": 0.5}}
        body = {"models": models}
    response = authenticated_client.patch(ROOT + "/" + provider_id, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["models"][0]["local_settings"] == models[0]["local_settings"]
    stored = model_providers.get_model_provider(provider_id)
    assert stored["models_json"][0]["local_settings"] == models[0]["local_settings"]
    if change == "rename":
        assert stored["name"] == "Renamed"
    elif change == "toggle":
        assert response.json()["enabled"] is False
        assert stored["enabled"] == 0
    elif change == "round_trip":
        assert stored["models_json"][0]["label"] == "Relabelled"
    else:
        assert stored["models_json"][1]["local_settings"] == models[1]["local_settings"]


@pytest.mark.parametrize("new_model", [False, True])
def test_patch_rejects_changed_or_new_unknown_local_settings(
    authenticated_client, synced_unknown_settings, new_model
):
    from app.db import model_providers

    provider_id, models = synced_unknown_settings
    before = model_providers.get_model_provider(provider_id)["models_json"]
    if new_model:
        models.append({"id": "new", "label": "New", "local_settings": models[0]["local_settings"]})
    else:
        models[0]["local_settings"] = {"server": "another-unsupported-server"}
    response = authenticated_client.patch(ROOT + "/" + provider_id, json={"models": models})
    assert response.status_code == 400, response.text
    assert model_providers.get_model_provider(provider_id)["models_json"] == before


def test_patch_can_clear_unknown_local_settings(authenticated_client, synced_unknown_settings):
    from app.db import model_providers

    provider_id, models = synced_unknown_settings
    models[0]["local_settings"] = None
    response = authenticated_client.patch(ROOT + "/" + provider_id, json={"models": models})
    assert response.status_code == 200, response.text
    assert model_providers.get_model_provider(provider_id)["models_json"][0]["local_settings"] is None


@pytest.mark.parametrize("original,changed", [
    ({"server": "vllm", "sampling": {"top_k": 1}},
     {"server": "vllm", "sampling": {"top_k": True}}),
    ({"server": "vllm", "reasoning": True}, {"server": "vllm", "reasoning": 1}),
])
def test_patch_does_not_confuse_booleans_with_numbers(authenticated_client, mock_db, original, changed):
    from app.db import model_providers

    response = create(authenticated_client, original)
    assert response.status_code == 200, response.text
    provider_id = response.json()["id"]
    response = authenticated_client.patch(ROOT + "/" + provider_id, json={"models": [
        {"id": "local", "label": "Local", "local_settings": changed}]})
    assert response.status_code == 400, response.text
    assert model_providers.get_model_provider(provider_id)["models_json"][0]["local_settings"] == original


def test_local_settings_are_not_sent_to_cloud_routes(authenticated_client, mock_db):
    assert create(authenticated_client, {"server": "vllm"}, provider_type="openrouter").status_code == 400
