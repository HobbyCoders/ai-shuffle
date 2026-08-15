"""Tests for the model registry.

The presets are Agent SDK shorthand aliases (``haiku``, ``sonnet``, ``opus``,
``fable``). The 1M context window is a capability toggled on a base model by
appending ``[1m]`` to the stored value. These tests pin the registry contents,
the flipped legacy map (older full model IDs → shorthand), and the helpers that
round-trip the suffix.
"""

import json

from app.api import settings_crypto
from app.core import model_registry as m
from app.core.config import settings
from app.db import database as db


def test_registry_contents_and_default():
    # Presets are the four SDK shorthand aliases, in this order.
    assert [opt.value for opt in m.MODELS] == ["haiku", "sonnet", "opus", "fable"]
    # No registry entry carries the [1m] suffix — it's derived on demand.
    assert all(not opt.value.endswith(m.CONTEXT_1M_SUFFIX) for opt in m.MODELS)
    by_value = {opt.value: opt for opt in m.MODELS}
    assert by_value["haiku"].supports_1m is False
    assert by_value["sonnet"].supports_1m is True
    assert by_value["opus"].supports_1m is True
    assert by_value["fable"].supports_1m is True
    assert by_value["fable"].label == "Fable"
    assert m.DEFAULT_MODEL == "sonnet"


def test_legacy_map_flips_full_ids_to_shorthand():
    # Older stored full model IDs normalize to the shorthand alias.
    assert m.normalize_model("claude-haiku-4-5") == "haiku"
    assert m.normalize_model("claude-sonnet-4-6") == "sonnet"
    assert m.normalize_model("claude-sonnet-4-6[1m]") == "sonnet[1m]"
    assert m.normalize_model("claude-opus-4-6") == "opus"
    assert m.normalize_model("claude-opus-4-7") == "opus"
    assert m.normalize_model("claude-opus-4-8") == "opus"
    assert m.normalize_model("claude-opus-4-8[1m]") == "opus[1m]"


def test_normalize_passes_through_custom_and_shorthand():
    # Unknown custom model strings pass through unchanged.
    assert m.normalize_model("claude-sonnet-4-5") == "claude-sonnet-4-5"
    assert m.normalize_model("some-future-model") == "some-future-model"
    # Shorthand presets are already canonical — returned as-is.
    assert m.normalize_model("sonnet") == "sonnet"
    assert m.normalize_model("fable[1m]") == "fable[1m]"
    # Inherit / empty.
    assert m.normalize_model("") == ""
    assert m.normalize_model(None) == ""


def test_has_1m_context():
    assert m.has_1m_context("opus[1m]") is True
    assert m.has_1m_context("opus") is False
    assert m.has_1m_context("") is False
    assert m.has_1m_context(None) is False
    # Legacy full ID normalizes to a 1M shorthand value.
    assert m.has_1m_context("claude-opus-4-8[1m]") is True


def test_strip_context_suffix():
    assert m.strip_context_suffix("opus[1m]") == "opus"
    assert m.strip_context_suffix("opus") == "opus"
    assert m.strip_context_suffix("claude-opus-4-8[1m]") == "opus"
    assert m.strip_context_suffix("") == ""


def test_with_context_suffix_toggles():
    assert m.with_context_suffix("opus", True) == "opus[1m]"
    assert m.with_context_suffix("opus[1m]", False) == "opus"
    # Idempotent — enabling an already-1M model does not double the suffix.
    assert m.with_context_suffix("opus[1m]", True) == "opus[1m]"
    # Inherit / empty has nothing to toggle.
    assert m.with_context_suffix("", True) == ""
    # Works on custom models too.
    assert m.with_context_suffix("my-model", True) == "my-model[1m]"


def test_preset_detection_includes_1m_variants():
    assert m.is_preset_model("opus") is True
    assert m.is_preset_model("opus[1m]") is True
    assert m.is_preset_model("fable") is True
    # Legacy full ID resolves through the map to a preset.
    assert m.is_preset_model("claude-opus-4-8[1m]") is True
    assert m.is_preset_model("totally-custom") is False
    assert m.is_preset_model("") is False


def test_label_and_family_resolve_through_suffix():
    assert m.get_model_label("sonnet") == "Sonnet"
    assert m.get_model_label("sonnet[1m]") == "Sonnet 1M"
    assert m.get_model_family("fable") == "fable"
    assert m.get_model_family("opus[1m]") == "opus"


def test_context_window_for_1m_variant():
    assert m.get_context_window("sonnet") == 200_000
    assert m.get_context_window("sonnet[1m]") == m.CONTEXT_1M_WINDOW
    # Custom model with the suffix still reports the 1M window.
    assert m.get_context_window("my-model[1m]") == m.CONTEXT_1M_WINDOW


def test_model_values_includes_1m_for_supported_models():
    values = m.model_values()
    assert "sonnet" in values
    assert "sonnet[1m]" in values
    assert "fable" in values
    assert "fable[1m]" in values
    assert "opus" in values
    assert "opus[1m]" in values
    # Haiku has no 1M variant.
    assert "haiku" in values
    assert "haiku[1m]" not in values


def test_registry_payload_shape():
    payload = m.registry_payload()
    assert payload["context_1m_suffix"] == m.CONTEXT_1M_SUFFIX
    assert payload["default"] == "sonnet"
    models = payload["models"]
    assert isinstance(models, list)
    assert "supports_1m" in models[0]
    # Flipped legacy map is serialized for the frontend.
    assert payload["legacy_map"]["claude-opus-4-8"] == "opus"


class TestRegistryPayloadProviders:
    """``providers`` is a lazily-sourced projection of enabled
    ``model_providers`` rows — never carries the encrypted auth token, and
    degrades to an empty list rather than exploding the whole registry
    payload when the db isn't ready."""

    def test_empty_table_returns_empty_list(self, mock_db):
        payload = m.registry_payload()
        assert payload["providers"] == []

    def test_providers_sourced_from_enabled_rows_only(self, mock_db):
        db.create_model_provider(
            provider_id="prov-1",
            name="OpenRouter",
            provider_type="openrouter",
            base_url="https://openrouter.ai/api",
            auth_token="enc:tok-blob",
            models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
            default_model="openai/gpt-5.6",
            enabled=True,
        )
        db.create_model_provider(
            provider_id="prov-2",
            name="Disabled Gateway",
            provider_type="custom",
            base_url="https://gw.example/api",
            auth_token="enc:other-blob",
            enabled=False,
        )
        payload = m.registry_payload()
        providers = payload["providers"]
        assert len(providers) == 1
        entry = providers[0]
        assert entry == {
            "id": "prov-1",
            "name": "OpenRouter",
            "provider_type": "openrouter",
            # Display metadata: an API-key row, so no plan string.
            "auth_kind": "api",
            "plan_type": None,
            "models": [{
                "id": "openai/gpt-5.6",
                "label": "GPT-5.6",
                # Additive metadata (Chunk D.4) — null when the row omits them.
                "context_window": None,
                "max_output_tokens": None,
            }],
            "default_model": "openai/gpt-5.6",
        }

    def test_model_entries_carry_provider_context_and_output_metadata(self, mock_db):
        """Chunk D.4 additive keys: each model entry echoes the provider row's
        ``context_window`` / ``max_output_tokens`` (one value shared by all of
        that provider's models — no per-model store)."""
        db.create_model_provider(
            provider_id="prov-meta",
            name="Gateway",
            provider_type="custom",
            base_url="https://gw.example/api",
            models=[
                {"id": "model-a", "label": "Model A"},
                {"id": "model-b", "label": "Model B"},
            ],
            context_window=200000,
            max_output_tokens=8192,
            enabled=True,
        )
        payload = m.registry_payload()
        entry = payload["providers"][0]
        assert entry["models"] == [
            {"id": "model-a", "label": "Model A",
             "context_window": 200000, "max_output_tokens": 8192},
            {"id": "model-b", "label": "Model B",
             "context_window": 200000, "max_output_tokens": 8192},
        ]

    def test_chatgpt_row_is_subscription_and_carries_its_plan_type(self, mock_db):
        """A ChatGPT row reports ``auth_kind='subscription'`` and the plan string
        from its latest ``provider_usage_snapshots`` row — the subtext the UI
        renders under the OpenAI brand."""
        db.create_model_provider(
            provider_id="prov-chatgpt",
            name="ChatGPT",
            provider_type="chatgpt",
            base_url="https://chatgpt.example/api",
            models=[{"id": "gpt-5.6", "label": "GPT-5.6"}],
            enabled=True,
        )
        db.upsert_provider_usage_snapshot(
            provider_id="prov-chatgpt",
            provider_type="chatgpt",
            plan_type="prolite",
            windows=[],
            credits=None,
            raw=None,
            fetched_at=1_700_000_000_000,
        )
        entry = m.registry_payload()["providers"][0]
        assert entry["auth_kind"] == "subscription"
        assert entry["plan_type"] == "prolite"

    def test_api_key_rows_report_api_auth_and_no_plan(self, mock_db):
        """API-key rows never carry a plan string, even if a usage snapshot
        happens to hold one (only subscription rows have a plan)."""
        db.create_model_provider(
            provider_id="prov-openai",
            name="OpenAI",
            provider_type="openai",
            base_url="https://api.openai.com/v1",
            enabled=True,
        )
        db.upsert_provider_usage_snapshot(
            provider_id="prov-openai",
            provider_type="openai",
            plan_type="ignored",
            windows=[],
            credits=None,
            raw=None,
            fetched_at=1_700_000_000_000,
        )
        entry = m.registry_payload()["providers"][0]
        assert entry["auth_kind"] == "api"
        assert entry["plan_type"] is None

    def test_subscription_row_without_a_snapshot_has_a_null_plan(self, mock_db):
        db.create_model_provider(
            provider_id="prov-chatgpt",
            name="ChatGPT",
            provider_type="chatgpt",
            base_url="https://chatgpt.example/api",
            enabled=True,
        )
        entry = m.registry_payload()["providers"][0]
        assert entry["auth_kind"] == "subscription"
        assert entry["plan_type"] is None

    def test_plan_type_lookup_degrades_when_the_snapshot_read_fails(self, mock_db, monkeypatch):
        """A snapshots-table problem must not break the registry payload — the
        row still reports its auth kind, just without a plan string."""
        db.create_model_provider(
            provider_id="prov-chatgpt",
            name="ChatGPT",
            provider_type="chatgpt",
            base_url="https://chatgpt.example/api",
            enabled=True,
        )

        def _raise(*_args, **_kwargs):
            raise Exception('relation "provider_usage_snapshots" does not exist')

        monkeypatch.setattr(db, "get_all_provider_usage_snapshots", _raise)
        entry = m.registry_payload()["providers"][0]
        assert entry["auth_kind"] == "subscription"
        assert entry["plan_type"] is None

    def test_subscription_detection_mirrors_the_gateway_mode_resolution(self):
        """``provider_type == 'chatgpt'`` OR ``auth_mode == 'chatgpt'`` — a
        ``custom`` row can carry the Codex auth mode (mirrors
        ``omni_credentials._is_codex_subscription``)."""
        assert m._is_subscription_row({"provider_type": "chatgpt"}) is True
        assert m._is_subscription_row({"provider_type": "custom", "auth_mode": "chatgpt"}) is True
        assert m._is_subscription_row({"provider_type": "openai"}) is False
        assert m._is_subscription_row({"provider_type": "custom", "auth_mode": None}) is False

    def test_no_auth_token_anywhere_in_payload(self, mock_db):
        db.create_model_provider(
            provider_id="prov-1",
            name="OpenRouter",
            provider_type="openrouter",
            base_url="https://openrouter.ai/api",
            auth_token="enc:super-secret-blob",
            models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
            enabled=True,
        )
        payload = m.registry_payload()
        serialized = json.dumps(payload)
        assert "auth_token" not in serialized
        assert "enc:super-secret-blob" not in serialized

    def test_db_not_initialized_degrades_to_empty_list(self, monkeypatch):
        """Early-boot: the pg pool isn't initialized yet. ``get_db()`` raises
        ``RuntimeError`` in that state — the payload must still build."""

        def _raise_not_initialized(*_args, **_kwargs):
            raise RuntimeError("PostgreSQL connection pool not initialized. Call init_pool() first.")

        monkeypatch.setattr(db, "get_enabled_model_providers", _raise_not_initialized)
        payload = m.registry_payload()
        assert payload["providers"] == []

    def test_db_generic_exception_degrades_to_empty_list(self, monkeypatch):
        """FIX 6: a rolling-deploy missing table (psycopg2 ProgrammingError) or
        pool exhaustion (PoolError) is a plain Exception, not RuntimeError. The
        registry payload must still build (never 500 GET /api/models app-wide)."""

        def _raise_generic(*_args, **_kwargs):
            raise Exception('relation "model_providers" does not exist')

        monkeypatch.setattr(db, "get_enabled_model_providers", _raise_generic)
        payload = m.registry_payload()
        assert payload["providers"] == []
        # And the rest of the payload is intact.
        assert payload["default"] == "sonnet"
        assert isinstance(payload["models"], list)


class TestRegistryPayloadClaude:
    """``claude`` mirrors the providers' ``auth_kind``/``plan_type`` pair for
    native Anthropic, which has no ``model_providers`` row to carry them. It is
    what lets the UI render "Claude · max" instead of a bare "Claude"."""

    def test_payload_always_carries_the_claude_block(self, mock_db):
        payload = m.registry_payload()
        assert "claude" in payload
        assert set(payload["claude"]) == {"auth_kind", "plan_type"}

    def test_api_key_install_reports_api_auth_and_no_plan(self, mock_db, monkeypatch):
        """A stored Anthropic key overrides any connected account (see
        ``environment.inject_claude_code_auth``) → API auth, no plan."""
        iid = settings.effective_instance_id
        db.create_claude_account(
            account_id="acct-1", name="Primary", credentials="enc:x",
            credential_fingerprint=None, expires_at=None, has_refresh_token=True,
            subscription_type="max",
        )
        db.set_system_setting(f"claude_active_account:{iid}", "acct-1")
        monkeypatch.setattr(
            settings_crypto, "get_decrypted_api_key",
            lambda name: "sk-ant-key" if name == "anthropic_api_key" else None,
        )

        claude = m.registry_payload()["claude"]
        # The account's plan is deliberately NOT surfaced — the key is what
        # authenticates, so the subtext must read "API".
        assert claude == {"auth_kind": "api", "plan_type": None}

    def test_account_install_reports_subscription_and_its_plan(self, mock_db, monkeypatch):
        iid = settings.effective_instance_id
        db.create_claude_account(
            account_id="acct-1", name="Primary", credentials="enc:x",
            credential_fingerprint=None, expires_at=None, has_refresh_token=True,
            subscription_type="max",
        )
        db.set_system_setting(f"claude_active_account:{iid}", "acct-1")
        monkeypatch.setattr(settings_crypto, "get_decrypted_api_key", lambda _name: None)

        claude = m.registry_payload()["claude"]
        assert claude == {"auth_kind": "subscription", "plan_type": "max"}

    def test_account_without_a_known_plan_reports_null(self, mock_db, monkeypatch):
        """A row created before the plan lookup existed carries NULL until the
        usage poller heals it — subscription auth, unknown tier."""
        iid = settings.effective_instance_id
        db.create_claude_account(
            account_id="acct-1", name="Primary", credentials="enc:x",
            credential_fingerprint=None, expires_at=None, has_refresh_token=True,
        )
        db.set_system_setting(f"claude_active_account:{iid}", "acct-1")
        monkeypatch.setattr(settings_crypto, "get_decrypted_api_key", lambda _name: None)

        claude = m.registry_payload()["claude"]
        assert claude == {"auth_kind": "subscription", "plan_type": None}

    def test_no_active_account_reports_subscription_with_no_plan(self, mock_db, monkeypatch):
        monkeypatch.setattr(settings_crypto, "get_decrypted_api_key", lambda _name: None)
        claude = m.registry_payload()["claude"]
        assert claude == {"auth_kind": "subscription", "plan_type": None}

    def test_db_failure_degrades_to_nulls(self, monkeypatch):
        """Same contract as ``_providers_payload``: never 500 GET /api/models."""

        def _raise(*_args, **_kwargs):
            raise Exception('relation "claude_accounts" does not exist')

        monkeypatch.setattr(settings_crypto, "get_decrypted_api_key", _raise)
        payload = m.registry_payload()
        assert payload["claude"] == {"auth_kind": None, "plan_type": None}
        # And the rest of the payload is intact.
        assert payload["default"] == "sonnet"
        assert isinstance(payload["models"], list)
