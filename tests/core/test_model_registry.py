"""Tests for the model registry.

The presets are Agent SDK shorthand aliases (``haiku``, ``sonnet``, ``opus``,
``fable``) and each carries its NATIVE context window — the current Claude slugs
run 1M by default and the old ``[1m]`` opt-in suffix is a retired mechanism that
survives only as legacy-data normalization. These tests pin the registry
contents, the flipped legacy map (older full model IDs → shorthand), that a
stored ``[1m]`` id normalizes onto its base slug, and the per-MODEL provider
context-window resolution shared with the spawn env overlay.
"""

import json

from app.api import settings_crypto
from app.core import model_registry as m
from app.core.config import settings
from app.db import database as db


def test_registry_contents_and_default():
    # Presets are the four SDK shorthand aliases, in this order.
    assert [opt.value for opt in m.MODELS] == ["haiku", "sonnet", "opus", "fable"]
    # No registry entry carries the retired [1m] suffix.
    assert all(not opt.value.endswith("[1m]") for opt in m.MODELS)
    by_value = {opt.value: opt for opt in m.MODELS}
    # Current Claude slugs are natively 1M; haiku stays on the standard window.
    assert by_value["haiku"].context_window == 200_000
    assert by_value["sonnet"].context_window == 1_000_000
    assert by_value["opus"].context_window == 1_000_000
    assert by_value["fable"].context_window == 1_000_000
    assert by_value["fable"].label == "Fable"
    assert m.DEFAULT_MODEL == "sonnet"
    # The retired capability flag is gone from the dataclass entirely.
    assert not hasattr(by_value["sonnet"], "supports_1m")


def test_legacy_map_flips_full_ids_to_shorthand():
    # Older stored full model IDs normalize to the shorthand alias.
    assert m.normalize_model("claude-haiku-4-5") == "haiku"
    assert m.normalize_model("claude-sonnet-4-6") == "sonnet"
    assert m.normalize_model("claude-opus-4-6") == "opus"
    assert m.normalize_model("claude-opus-4-7") == "opus"
    assert m.normalize_model("claude-opus-4-8") == "opus"


def test_legacy_1m_ids_normalize_to_the_base_slug():
    """Persisted profiles still hold ``"opus[1m]"``-style values — they must
    resolve onto the base slug now that the suffix selects nothing."""
    assert m.normalize_model("opus[1m]") == "opus"
    assert m.normalize_model("sonnet[1m]") == "sonnet"
    assert m.normalize_model("fable[1m]") == "fable"
    # Legacy FULL id + suffix goes through the map to the same place.
    assert m.normalize_model("claude-opus-4-8[1m]") == "opus"
    assert m.normalize_model("claude-sonnet-4-6[1m]") == "sonnet"
    assert m.normalize_model("claude-fable-5[1m]") == "fable"
    # A custom id keeps its identity, minus the retired suffix.
    assert m.normalize_model("my-model[1m]") == "my-model"


def test_normalize_passes_through_custom_and_shorthand():
    # Unknown custom model strings pass through unchanged.
    assert m.normalize_model("claude-sonnet-4-5") == "claude-sonnet-4-5"
    assert m.normalize_model("some-future-model") == "some-future-model"
    # Shorthand presets are already canonical — returned as-is.
    assert m.normalize_model("sonnet") == "sonnet"
    # Inherit / empty.
    assert m.normalize_model("") == ""
    assert m.normalize_model(None) == ""


def test_the_suffix_helper_machinery_is_gone():
    """The public opt-in helpers were deleted with the mechanism — reintroducing
    one would resurrect a toggle the product no longer has."""
    for name in (
        "has_1m_context",
        "strip_context_suffix",
        "with_context_suffix",
        "CONTEXT_1M_SUFFIX",
        "CONTEXT_1M_WINDOW",
    ):
        assert not hasattr(m, name), name


def test_preset_detection():
    assert m.is_preset_model("opus") is True
    assert m.is_preset_model("fable") is True
    # Legacy ids (with and without the retired suffix) resolve to a preset.
    assert m.is_preset_model("claude-opus-4-8") is True
    assert m.is_preset_model("opus[1m]") is True
    assert m.is_preset_model("totally-custom") is False
    assert m.is_preset_model("") is False


def test_openrouter_display_label_strips_author_prefix():
    assert m.display_provider_model_label(
        "Anthropic: Claude Fable 5.1", "openrouter"
    ) == "Claude Fable 5.1"
    assert m.display_provider_model_label("Z.ai: GLM 5.3", "openrouter") == "GLM 5.3"
    assert m.display_provider_model_label("Grok 4.6", "openrouter") == "Grok 4.6"
    # Non-OpenRouter catalogs keep the prefix; empty rest keeps the original.
    assert m.display_provider_model_label(
        "Anthropic: Claude Fable 5.1", "custom"
    ) == "Anthropic: Claude Fable 5.1"
    assert m.display_provider_model_label("Author: ", "openrouter") == "Author: "
    assert m.display_provider_model_label(None, "openrouter") is None


def test_label_and_family_resolve_through_legacy_ids():
    assert m.get_model_label("sonnet") == "Sonnet"
    # No more " 1M" decoration — the window is not a variant any more.
    assert m.get_model_label("sonnet[1m]") == "Sonnet"
    assert m.get_model_family("fable") == "fable"
    assert m.get_model_family("opus[1m]") == "opus"


def test_context_window_lookup():
    assert m.get_context_window("haiku") == 200_000
    assert m.get_context_window("sonnet") == 1_000_000
    assert m.get_context_window("opus[1m]") == 1_000_000
    # Unknown/custom models fall back to the standard window.
    assert m.get_context_window("my-model") == 200_000


def test_model_values_has_no_suffix_variants():
    assert m.model_values() == ["haiku", "sonnet", "opus", "fable"]


def test_resolve_api_model_id_maps_each_preset_alias():
    # The registry stores short aliases; the native Anthropic leg needs the
    # full API model id.
    assert m.resolve_api_model_id("haiku") == "claude-haiku-4-5"
    assert m.resolve_api_model_id("sonnet") == "claude-sonnet-5"
    assert m.resolve_api_model_id("opus") == "claude-opus-5"
    assert m.resolve_api_model_id("fable") == "claude-fable-5"


def test_resolve_api_model_id_resolves_legacy_1m_values():
    # The retired [1m] suffix was never a distinct API model — persisted values
    # still carrying it normalize to the base slug, then to the API id.
    assert m.resolve_api_model_id("opus[1m]") == "claude-opus-5"


def test_resolve_api_model_id_resolves_legacy_full_ids():
    # Legacy stored ids normalize to their alias first, then to the API id.
    assert m.resolve_api_model_id("claude-opus-4-6") == "claude-opus-5"
    assert m.resolve_api_model_id("claude-sonnet-4-6[1m]") == "claude-sonnet-5"


def test_resolve_api_model_id_passes_through_non_presets():
    # Other providers' ids and custom models are never rewritten.
    assert m.resolve_api_model_id("gpt-5-codex") == "gpt-5-codex"
    assert m.resolve_api_model_id("meta-llama/llama-3.1-70b") == "meta-llama/llama-3.1-70b"
    # A legacy-looking id that is NOT in LEGACY_MODEL_MAP stays as-is.
    assert m.resolve_api_model_id("claude-sonnet-4-5") == "claude-sonnet-4-5"
    # Inherit / empty round-trip untouched.
    assert m.resolve_api_model_id(None) is None
    assert m.resolve_api_model_id("") == ""


def test_registry_payload_shape():
    payload = m.registry_payload()
    assert "context_1m_suffix" not in payload
    assert payload["default"] == "sonnet"
    models = payload["models"]
    assert isinstance(models, list)
    assert "supports_1m" not in models[0]
    assert {"value", "label", "family", "context_window", "effort_levels", "default_effort"} == set(models[0])
    # Claude windows travel to the frontend so the context meter is right.
    by_value = {entry["value"]: entry for entry in models}
    assert by_value["sonnet"]["context_window"] == 1_000_000
    assert by_value["haiku"]["context_window"] == 200_000
    # Flipped legacy map is serialized for the frontend — [1m] ids land on base.
    assert payload["legacy_map"]["claude-opus-4-8"] == "opus"
    assert payload["legacy_map"]["claude-opus-4-8[1m]"] == "opus"


class TestResolveProviderContextWindow:
    """Per-MODEL window resolution: the model's own value, then the preset
    roster, then the provider row's column. Shared by the registry payload and
    the spawn env overlay so the UI and the CLI can never disagree."""

    def test_model_entry_wins_over_preset_and_row(self):
        row = {
            "provider_type": "chatgpt",
            "context_window": 272000,
            "models_json": [
                {"id": "gpt-5.6-sol", "label": "Sol", "context_window": 400000}
            ],
        }
        assert m.resolve_provider_context_window(row, "gpt-5.6-sol") == 400000

    def test_preset_wins_over_the_row_column(self):
        """The bug this fixes: a 1M model on a row whose column says 272k."""
        row = {
            "provider_type": "chatgpt",
            "context_window": 272000,
            "models_json": [{"id": "gpt-5.6-sol", "label": "Sol"}],
        }
        assert m.resolve_provider_context_window(row, "gpt-5.6-sol") == 1_000_000

    def test_preset_applies_to_an_enrolled_codex_row_by_auth_mode(self):
        row = {
            "provider_type": "custom",
            "auth_mode": "chatgpt",
            "context_window": 272000,
            "models_json": [{"id": "gpt-5.6-sol", "label": "Sol"}],
        }
        assert m.resolve_provider_context_window(row, "gpt-5.6-sol") == 1_000_000

    def test_openai_public_api_roster_also_carries_the_1m_model(self):
        row = {"provider_type": "openai", "context_window": 272000, "models_json": []}
        assert m.resolve_provider_context_window(row, "gpt-5.6-sol") == 1_000_000

    def test_model_without_a_preset_window_falls_back_to_the_row(self):
        row = {
            "provider_type": "chatgpt",
            "context_window": 272000,
            "models_json": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
        }
        assert m.resolve_provider_context_window(row, "gpt-5.5") == 272000

    def test_browsable_row_uses_its_own_column(self):
        row = {
            "provider_type": "openrouter",
            "context_window": 128000,
            "models_json": [{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
        }
        assert m.resolve_provider_context_window(row, "openai/gpt-5.6") == 128000

    def test_nothing_known_returns_none(self):
        row = {"provider_type": "openrouter", "context_window": None, "models_json": []}
        assert m.resolve_provider_context_window(row, "openai/gpt-5.6") is None
        assert m.resolve_provider_context_window(None, "anything") is None

    def test_unusable_values_are_ignored(self):
        row = {
            "provider_type": "openrouter",
            "context_window": 0,
            "models_json": [{"id": "m", "label": "M", "context_window": "not-a-number"}],
        }
        assert m.resolve_provider_context_window(row, "m") is None


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
            models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6", "supported_parameters": ["reasoning_effort"]}],
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
                "effort_levels": ["low", "medium", "high"],
                "default_effort": None,
            }],
            "default_model": "openai/gpt-5.6",
        }

    def test_openrouter_model_labels_drop_author_prefix(self, mock_db):
        """Stored OpenRouter catalog names include the author; the picker does not."""
        db.create_model_provider(
            provider_id="prov-or-prefix",
            name="OpenRouter",
            provider_type="openrouter",
            base_url="https://openrouter.ai/api",
            models=[
                {"id": "anthropic/claude-fable-5.1",
                 "label": "Anthropic: Claude Fable 5.1"},
                {"id": "x-ai/grok-4.6", "label": "Grok 4.6"},
            ],
            enabled=True,
        )
        models = m.registry_payload()["providers"][0]["models"]
        assert models[0]["label"] == "Claude Fable 5.1"
        assert models[1]["label"] == "Grok 4.6"

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
             "context_window": 200000, "max_output_tokens": 8192,
             "effort_levels": [], "default_effort": None},
            {"id": "model-b", "label": "Model B",
             "context_window": 200000, "max_output_tokens": 8192,
             "effort_levels": [], "default_effort": None},
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
        ``fluid_credentials._is_codex_subscription``)."""
        assert m._is_subscription_row({"provider_type": "chatgpt"}) is True
        assert m._is_subscription_row({"provider_type": "custom", "auth_mode": "chatgpt"}) is True
        assert m._is_subscription_row({"provider_type": "grok"}) is True
        assert m._is_subscription_row({"provider_type": "custom", "auth_mode": "grok"}) is True
        assert m._is_subscription_row({"provider_type": "openai"}) is False
        assert m._is_subscription_row({"provider_type": "xai"}) is False
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
