"""Tests for the model-provider registry DB helpers (app/db/model_providers.py).

Covers CRUD round-trip, ``include_token`` gating (public reads never carry the
``auth_token`` key — matching the claude_accounts ``include_credentials``
precedent), the TEXT-encoded JSON columns (``models_json`` / ``extra_env_json``)
round-tripping through the pg_shim, the enabled filter, and that a partial
update leaves the stored token intact. Runs on the in-memory SQLite shim via the
autouse DB-routing fixture (documented by depending on ``mock_db``).
"""

from app.db import database as db


def _create(provider_id="prov-1", **overrides):
    kwargs = dict(
        provider_id=provider_id,
        name="OpenRouter",
        provider_type="openrouter",
        base_url="https://openrouter.ai/api",
        auth_token="enc:tok-blob",
        models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
        default_model="openai/gpt-5.6",
        small_fast_model="openai/gpt-5.6-mini",
        extra_env={"FOO": "bar"},
        context_window=200000,
        max_output_tokens=8192,
        enabled=True,
    )
    kwargs.update(overrides)
    return db.create_model_provider(**kwargs)


class TestCreateAndGet:
    def test_create_returns_public_row_without_token(self, mock_db):
        row = _create()
        assert row["id"] == "prov-1"
        assert row["name"] == "OpenRouter"
        assert row["provider_type"] == "openrouter"
        assert row["base_url"] == "https://openrouter.ai/api"
        # Token is NEVER exposed on a public read.
        assert "auth_token" not in row
        # Scalars round-trip.
        assert row["default_model"] == "openai/gpt-5.6"
        assert row["small_fast_model"] == "openai/gpt-5.6-mini"
        assert row["context_window"] == 200000
        assert row["max_output_tokens"] == 8192
        assert row["enabled"]

    def test_get_public_read_has_no_token_key(self, mock_db):
        _create()
        row = db.get_model_provider("prov-1")
        assert row is not None
        assert "auth_token" not in row

    def test_get_include_token_returns_encrypted_blob(self, mock_db):
        _create()
        row = db.get_model_provider("prov-1", include_token=True)
        assert row["auth_token"] == "enc:tok-blob"

    def test_get_missing_returns_none(self, mock_db):
        assert db.get_model_provider("nope") is None

    def test_json_columns_round_trip(self, mock_db):
        _create()
        row = db.get_model_provider("prov-1")
        # Parsed back to real Python structures, not JSON strings.
        assert isinstance(row["models_json"], list)
        assert row["models_json"] == [{"id": "openai/gpt-5.6", "label": "GPT-5.6"}]
        assert isinstance(row["extra_env_json"], dict)
        assert row["extra_env_json"] == {"FOO": "bar"}

    def test_defaults_for_optional_json_and_nullable_columns(self, mock_db):
        db.create_model_provider(
            provider_id="prov-min",
            name="Custom",
            provider_type="custom",
            base_url="https://gw.example/api",
        )
        row = db.get_model_provider("prov-min", include_token=True)
        assert row["models_json"] == []
        assert row["extra_env_json"] == {}
        assert row["auth_token"] is None
        assert row["default_model"] is None
        assert row["small_fast_model"] is None
        assert row["context_window"] is None
        assert row["max_output_tokens"] is None
        assert row["enabled"]


class TestListAndEnabledFilter:
    def test_get_all_lists_every_provider_without_token(self, mock_db):
        _create(provider_id="p1")
        _create(provider_id="p2", enabled=False)
        rows = db.get_all_model_providers()
        ids = {r["id"] for r in rows}
        assert ids == {"p1", "p2"}
        assert all("auth_token" not in r for r in rows)

    def test_get_all_include_token(self, mock_db):
        _create(provider_id="p1")
        rows = db.get_all_model_providers(include_token=True)
        assert rows[0]["auth_token"] == "enc:tok-blob"

    def test_get_enabled_filters_disabled(self, mock_db):
        _create(provider_id="on")
        _create(provider_id="off", enabled=False)
        rows = db.get_enabled_model_providers()
        ids = {r["id"] for r in rows}
        assert ids == {"on"}
        assert all("auth_token" not in r for r in rows)

    def test_get_enabled_include_token(self, mock_db):
        _create(provider_id="on")
        rows = db.get_enabled_model_providers(include_token=True)
        assert rows[0]["auth_token"] == "enc:tok-blob"


class TestUpdate:
    def test_partial_update_leaves_token_intact(self, mock_db):
        _create()
        # Metadata-only update: auth_token NOT passed.
        updated = db.update_model_provider("prov-1", name="Renamed", base_url="https://new/api")
        assert updated["name"] == "Renamed"
        assert updated["base_url"] == "https://new/api"
        # The stored token must be untouched.
        row = db.get_model_provider("prov-1", include_token=True)
        assert row["auth_token"] == "enc:tok-blob"

    def test_update_json_columns_accept_python_structures(self, mock_db):
        _create()
        db.update_model_provider(
            "prov-1",
            models_json=[{"id": "x/y", "label": "XY"}],
            extra_env_json={"K": "V"},
        )
        row = db.get_model_provider("prov-1")
        assert row["models_json"] == [{"id": "x/y", "label": "XY"}]
        assert row["extra_env_json"] == {"K": "V"}

    def test_update_can_replace_token_when_explicitly_passed(self, mock_db):
        _create()
        db.update_model_provider("prov-1", auth_token="enc:new-blob")
        row = db.get_model_provider("prov-1", include_token=True)
        assert row["auth_token"] == "enc:new-blob"

    def test_update_no_fields_is_noop_returns_row(self, mock_db):
        _create()
        row = db.update_model_provider("prov-1")
        assert row["id"] == "prov-1"
        assert db.get_model_provider("prov-1", include_token=True)["auth_token"] == "enc:tok-blob"

    def test_update_missing_returns_none(self, mock_db):
        assert db.update_model_provider("nope", name="x") is None


class TestSetEnabledAndDelete:
    def test_set_enabled_toggles_flag(self, mock_db):
        _create()
        row = db.set_model_provider_enabled("prov-1", False)
        assert not row["enabled"]
        assert db.get_enabled_model_providers() == []
        row = db.set_model_provider_enabled("prov-1", True)
        assert row["enabled"]
        assert {r["id"] for r in db.get_enabled_model_providers()} == {"prov-1"}

    def test_set_enabled_missing_returns_none(self, mock_db):
        assert db.set_model_provider_enabled("nope", True) is None

    def test_delete_removes_row(self, mock_db):
        _create()
        assert db.delete_model_provider("prov-1") is True
        assert db.get_model_provider("prov-1") is None
        assert db.get_all_model_providers() == []

    def test_delete_missing_returns_false(self, mock_db):
        assert db.delete_model_provider("nope") is False
