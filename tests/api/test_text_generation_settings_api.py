"""``GET/PUT /api/v1/settings/text-generation``.

The model + effort behind every internal one-shot call (session titles, commit
messages, PR descriptions, branch names, agent generation). It replaced the
built-in "Default AI" agent's model, and lives under Settings > Default models.
"""

import pytest

from app.core import encryption
from app.db import database as db


ENDPOINT = "/api/v1/settings/text-generation"


def _make_provider(provider_id="prov-or", *, models=None, enabled=True):
    encryption.set_encryption_key("test-password", db)
    db.create_model_provider(
        provider_id=provider_id,
        name="OpenRouter",
        provider_type="openrouter",
        base_url="https://openrouter.ai/api",
        auth_token=encryption.encrypt_value("gw-secret"),
        models=models if models is not None else [{"id": "openai/gpt-5.6"}],
        enabled=enabled,
    )
    return provider_id


class TestTextGenerationSettings:
    @pytest.mark.parametrize("method", ["get", "put"])
    def test_requires_auth(self, client, method):
        resp = getattr(client, method)(ENDPOINT, **({"json": {}} if method == "put" else {}))
        assert resp.status_code in (401, 403)

    def test_defaults_to_unset(self, authenticated_client, mock_db):
        body = authenticated_client.get(ENDPOINT).json()
        assert body == {"text_generation_model": "", "text_generation_effort": ""}

    def test_put_roundtrip(self, authenticated_client, mock_db):
        resp = authenticated_client.put(
            ENDPOINT,
            json={"text_generation_model": "haiku", "text_generation_effort": "low"},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "text_generation_model": "haiku",
            "text_generation_effort": "low",
        }
        # The GET agrees, and generation resolves the same binding.
        assert authenticated_client.get(ENDPOINT).json() == resp.json()

        from app.core.generation_options import text_generation_config

        assert text_generation_config() == {
            "model": "haiku",
            "provider": None,
            "effort": "low",
        }

    def test_partial_update_leaves_the_other_key_alone(self, authenticated_client, mock_db):
        authenticated_client.put(
            ENDPOINT,
            json={"text_generation_model": "haiku", "text_generation_effort": "high"},
        )
        body = authenticated_client.put(ENDPOINT, json={"text_generation_model": "opus"}).json()
        assert body == {"text_generation_model": "opus", "text_generation_effort": "high"}

    def test_empty_values_clear_back_to_the_default(self, authenticated_client, mock_db):
        authenticated_client.put(
            ENDPOINT,
            json={"text_generation_model": "opus", "text_generation_effort": "max"},
        )
        body = authenticated_client.put(
            ENDPOINT,
            json={"text_generation_model": "", "text_generation_effort": ""},
        ).json()
        assert body == {"text_generation_model": "", "text_generation_effort": ""}

    def test_accepts_a_curated_provider_model(self, authenticated_client, mock_db):
        provider_id = _make_provider()
        resp = authenticated_client.put(
            ENDPOINT,
            json={"text_generation_model": f"prov:{provider_id}:openai/gpt-5.6"},
        )
        assert resp.status_code == 200

        from app.core.generation_options import text_generation_config

        assert text_generation_config() == {
            "model": "openai/gpt-5.6",
            "provider": provider_id,
            "effort": None,
        }

    def test_refuses_a_model_the_provider_does_not_serve(self, authenticated_client, mock_db):
        """Caught here, not on every commit-message generation afterwards."""
        provider_id = _make_provider()
        resp = authenticated_client.put(
            ENDPOINT,
            json={"text_generation_model": f"prov:{provider_id}:not-a-real-model"},
        )
        assert resp.status_code == 400
        assert authenticated_client.get(ENDPOINT).json()["text_generation_model"] == ""

    def test_ultra_roundtrip(self, authenticated_client, mock_db):
        from app.core.generation_options import text_generation_config
        response = authenticated_client.put(ENDPOINT, json={"text_generation_effort": "ultra"})
        assert response.status_code == 200
        assert response.json()["text_generation_effort"] == "ultra"
        assert authenticated_client.get(ENDPOINT).json()["text_generation_effort"] == "ultra"
        assert text_generation_config()["effort"] == "ultra"

    def test_refuses_an_unknown_effort(self, authenticated_client, mock_db):
        resp = authenticated_client.put(ENDPOINT, json={"text_generation_effort": "turbo"})
        assert resp.status_code == 400
