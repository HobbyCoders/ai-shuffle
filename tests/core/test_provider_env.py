"""Chunk 3 (Core env overlay) tests — multi-provider model support.

Covers:
- ``environment.apply_provider_env``: full overlay contents, extra_env-can't-
  clobber-the-guard ordering, context-window clamp, R4 small/fast-model fallback
  chain, and the R5 error cases (missing / disabled provider, encryption not
  ready, and the no-token-but-base_url allowance).
- ``environment.inject_claude_code_auth``: provider gating (no Anthropic key when
  a provider is set; injected on the native path).
- ``query_options.build_options_from_profile`` and
  ``generation_options.build_generation_options``: the provider overlay is
  applied (with ``CLAUDE_CODE_DISABLE_1M_CONTEXT`` absent), the native path is
  unchanged (regression), and a query-level provider override wins over the
  profile config.
"""

import logging
from unittest.mock import patch

import pytest

import app.core.query_options as qo
from app.core import encryption
from app.core.environment import (
    ProviderConfigError,
    apply_provider_env,
    inject_claude_code_auth,
)
from app.core.generation_options import build_generation_options

BASE_URL_KEY = "ANTHROPIC_BASE_URL"
AUTH_TOKEN_KEY = "ANTHROPIC_AUTH_TOKEN"
API_KEY = "ANTHROPIC_API_KEY"
SMALL_FAST_KEY = "ANTHROPIC_SMALL_FAST_MODEL"
SUBAGENT_KEY = "CLAUDE_CODE_SUBAGENT_MODEL"
DISABLE_1M_KEY = "CLAUDE_CODE_DISABLE_1M_CONTEXT"
AUTO_COMPACT_KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
MAX_OUTPUT_KEY = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
MITIGATION_KEYS = (
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK",
)


def _mk_provider(db, provider_id="op", *, token="gw-secret", encrypt=True, **kwargs):
    """Create a provider row. ``token`` is stored encrypted (default) so the
    overlay must decrypt it; pass ``encrypt=False`` for a plaintext blob or
    ``token=None`` for a token-less provider."""
    stored = None
    if token is not None:
        stored = encryption.encrypt_value(token) if encrypt else token
    return db.create_model_provider(
        provider_id=provider_id,
        name=kwargs.pop("name", "OpenRouter"),
        provider_type=kwargs.pop("provider_type", "openrouter"),
        base_url=kwargs.pop("base_url", "https://openrouter.ai/api"),
        auth_token=stored,
        models=kwargs.pop("models", [{"id": "openai/gpt-5.6", "label": "GPT-5.6"}]),
        default_model=kwargs.pop("default_model", None),
        small_fast_model=kwargs.pop("small_fast_model", None),
        extra_env=kwargs.pop("extra_env", None),
        context_window=kwargs.pop("context_window", None),
        max_output_tokens=kwargs.pop("max_output_tokens", None),
        enabled=kwargs.pop("enabled", True),
    )


# ---------------------------------------------------------------------------
# apply_provider_env
# ---------------------------------------------------------------------------

class TestApplyProviderEnv:
    def test_full_overlay_contents(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(
            db, token="gw-secret",
            max_output_tokens=8192, context_window=256000,
            small_fast_model="openai/gpt-5.6-mini",
        )
        env = apply_provider_env({}, "op", "openai/gpt-5.6")

        assert env[BASE_URL_KEY] == "https://openrouter.ai/api"
        assert env[AUTH_TOKEN_KEY] == "gw-secret"
        assert env[API_KEY] == ""  # explicit empty → blocks Anthropic fallback
        for k in MITIGATION_KEYS:
            assert env[k] == "1"
        assert env[MAX_OUTPUT_KEY] == "8192"
        assert env[AUTO_COMPACT_KEY] == "256000"
        assert env[SMALL_FAST_KEY] == "openai/gpt-5.6-mini"
        assert env[SUBAGENT_KEY] == "openai/gpt-5.6-mini"

    def test_extra_env_cannot_clobber_api_key_guard(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        # extra_env tries to set ANTHROPIC_API_KEY (and base URL) — the forced
        # values applied LAST must win.
        _mk_provider(
            db, token="gw-secret",
            extra_env={API_KEY: "leaked-key", "FOO": "bar", BASE_URL_KEY: "evil"},
        )
        env = apply_provider_env({}, "op", "openai/gpt-5.6")

        assert env[API_KEY] == ""              # guard survived
        assert env[BASE_URL_KEY] == "https://openrouter.ai/api"  # forced wins
        assert env["FOO"] == "bar"             # non-conflicting extra_env kept

    def test_context_window_clamped_to_100k(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=50000)
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert env[AUTO_COMPACT_KEY] == "100000"  # 50k clamped up to 100k

    def test_context_window_above_floor_preserved(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=400000)
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert env[AUTO_COMPACT_KEY] == "400000"

    def test_r4_fallback_default_model(self, mock_db):
        """No small_fast_model → falls back to default_model."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, small_fast_model=None, default_model="openai/gpt-5.6")
        env = apply_provider_env({}, "op", "session-model")
        assert env[SMALL_FAST_KEY] == "openai/gpt-5.6"
        assert env[SUBAGENT_KEY] == "openai/gpt-5.6"

    def test_r4_fallback_first_curated_model(self, mock_db):
        """FIX 3: no small_fast_model and no default_model → FIRST CURATED model
        id (a stable provider-scoped value), NEVER the per-turn session model."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(
            db, small_fast_model=None, default_model=None,
            models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
        )
        env = apply_provider_env({}, "op", "session-model")
        assert env[SMALL_FAST_KEY] == "openai/gpt-5.6"
        assert env[SUBAGENT_KEY] == "openai/gpt-5.6"

    def test_r4_fallback_stable_across_per_turn_model(self, mock_db):
        """FIX 3: the small/fast + subagent overlay must be IDENTICAL across two
        calls that differ ONLY in the per-turn model — otherwise a within-provider
        composer switch would churn these keys into a subagent-killing respawn."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(
            db, small_fast_model=None, default_model=None,
            models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
        )
        env_a = apply_provider_env({}, "op", "model-a")
        env_b = apply_provider_env({}, "op", "model-b")
        assert env_a[SMALL_FAST_KEY] == env_b[SMALL_FAST_KEY]
        assert env_a[SUBAGENT_KEY] == env_b[SUBAGENT_KEY]

    def test_r4_no_curated_models_leaves_keys_unset(self, mock_db):
        """FIX 3: no small_fast_model, no default_model, and no curated models →
        both overlay keys left unset."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, small_fast_model=None, default_model=None, models=[])
        env = apply_provider_env({}, "op", "session-model")
        assert SMALL_FAST_KEY not in env
        assert SUBAGENT_KEY not in env

    def test_auto_compact_window_preset_wins(self, mock_db):
        """FIX 4: a pre-set CLAUDE_CODE_AUTO_COMPACT_WINDOW (profile preset) wins
        over the provider context_window — parity with the native path's
        setdefault."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=400000)
        env = apply_provider_env({AUTO_COMPACT_KEY: "123456"}, "op", "openai/gpt-5.6")
        assert env[AUTO_COMPACT_KEY] == "123456"  # preset survived

    def test_missing_provider_raises(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        with pytest.raises(ProviderConfigError):
            apply_provider_env({}, "does-not-exist", "m")

    def test_disabled_provider_raises(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, enabled=False)
        with pytest.raises(ProviderConfigError):
            apply_provider_env({}, "op", "m")

    def test_encryption_not_ready_raises(self, mock_db):
        from app.db import database as db
        # Store an encrypted token WHILE the key is loaded...
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, token="gw-secret")
        # ...then clear the key so decrypt is impossible at spawn time.
        encryption.clear_encryption_key()
        with pytest.raises(ProviderConfigError):
            apply_provider_env({}, "op", "m")

    def test_no_token_with_base_url_allowed(self, mock_db):
        """A token-less provider is allowed when base_url is set (unauthenticated
        self-hosted gateway); the token becomes the empty string."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, token=None, base_url="http://localhost:4000")
        env = apply_provider_env({}, "op", "m")
        assert env[BASE_URL_KEY] == "http://localhost:4000"
        assert env[AUTH_TOKEN_KEY] == ""
        assert env[API_KEY] == ""

    def test_ProviderConfigError_is_value_error(self):
        """R5 mechanism: ProviderConfigError subclasses ValueError so the query
        engines' existing (WorktreeUnavailableError, ValueError) guards surface
        it as a user-visible error."""
        assert issubclass(ProviderConfigError, ValueError)

    # ── Chunk F: cross-provider subagent leg (ruling A2/G2-r) ────────────────

    def test_subagent_leg_overrides_only_subagent_model_key(self, mock_db):
        """A leg overrides CLAUDE_CODE_SUBAGENT_MODEL to the leg model, but
        ANTHROPIC_SMALL_FAST_MODEL keeps the MAIN provider's chain (small/fast
        probes stay on the main leg)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, provider_id="op", small_fast_model="op/small")
        env = apply_provider_env(
            {}, "op", "op/main",
            subagent_provider_id="sub", subagent_model="sub/model",
        )
        assert env[SMALL_FAST_KEY] == "op/small"   # main chain unchanged
        assert env[SUBAGENT_KEY] == "sub/model"    # overridden to the leg model

    def test_no_subagent_leg_keeps_subagent_key_on_main_chain(self, mock_db):
        """Regression: without a leg, CLAUDE_CODE_SUBAGENT_MODEL follows the main
        small/fast chain (byte-identical to pre-Chunk-F)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, provider_id="op", small_fast_model="op/small")
        env = apply_provider_env({}, "op", "op/main")
        assert env[SMALL_FAST_KEY] == "op/small"
        assert env[SUBAGENT_KEY] == "op/small"

    def test_leg_absent_env_identical_with_explicit_none(self, mock_db):
        """Passing the leg kwargs as None yields the SAME env as omitting them —
        the leg is inert unless both fields are set."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, provider_id="op", small_fast_model="op/small")
        env_omit = apply_provider_env({}, "op", "op/main")
        env_none = apply_provider_env(
            {}, "op", "op/main",
            subagent_provider_id=None, subagent_model=None,
        )
        assert env_omit == env_none

    def test_partial_leg_is_inert(self, mock_db):
        """Only ONE leg field set (not both) leaves the subagent key on the main
        chain — mirrors the gateway's both-fields-required rule."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, provider_id="op", small_fast_model="op/small")
        env = apply_provider_env(
            {}, "op", "op/main", subagent_provider_id="sub", subagent_model=None
        )
        assert env[SUBAGENT_KEY] == "op/small"  # no override

    def test_chatgpt_main_with_leg_mints_token_and_resolves_account(self, mock_db):
        """A translated (chatgpt) main + chatgpt subagent leg: the minted routing
        token carries the leg, and the leg's codex account id is resolved."""
        from app.core.translator_gateway import routing
        from app.db import database as db

        encryption.set_encryption_key("pw", db)
        routing.get_registry().clear()
        db.create_model_provider(
            provider_id="gw", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None,
            models=[{"id": "gpt-5-codex", "label": "GPT-5 Codex"}],
        )
        db.create_model_provider(
            provider_id="sub", name="Sub Codex", provider_type="chatgpt",
            base_url="", auth_token=None,
            models=[{"id": "sub/model", "label": "Sub"}],
        )
        db.set_codex_token_bundle(
            "sub", access_token=None, refresh_token=None, account_id="acct-sub"
        )
        env = apply_provider_env(
            {}, "gw", "gpt-5-codex", session_id="sess-leg",
            subagent_provider_id="sub", subagent_model="sub/model",
        )
        binding = routing.resolve(env[AUTH_TOKEN_KEY])
        assert binding is not None
        assert binding.provider_id == "gw"
        assert binding.subagent_provider_id == "sub"
        assert binding.subagent_model == "sub/model"
        assert binding.subagent_account_id == "acct-sub"
        # small/fast falls back to the main provider's first curated model; the
        # subagent key is overridden to the leg model.
        assert env[SMALL_FAST_KEY] == "gpt-5-codex"
        assert env[SUBAGENT_KEY] == "sub/model"


# ---------------------------------------------------------------------------
# query_options: subagent leg resolution (config-only) + A2 legality
# ---------------------------------------------------------------------------

class TestQueryOptionsSubagentLeg:
    def test_native_session_drops_leg_and_logs_error(self, mock_db, tmp_path, caplog):
        """A2: a native (no-provider) main cannot host a cross-provider subagent.
        The leg is DROPPED with a logged error (never raised), env stays native."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        profile = {
            "id": "t",
            "config": {
                "model": "opus", "cwd": str(tmp_path),
                "subagent_provider": "sub", "subagent_model": "sub/model",
            },
        }
        with patch.object(qo, "apply_provider_env") as m, \
                caplog.at_level(logging.ERROR):
            env = _build_options(profile).env
        m.assert_not_called()               # native path → no gateway overlay
        assert env[DISABLE_1M_KEY] == "1"   # native window guard still applied
        assert any("subagent" in r.getMessage().lower() for r in caplog.records)

    def test_same_provider_leg_inherits(self, mock_db, tmp_path):
        """A subagent provider equal to the main provider is redundant (the
        small/fast chain already routes subagents there) → treated as inherit."""
        profile = {
            "id": "t",
            "config": {
                "model": "op/main", "provider": "op", "cwd": str(tmp_path),
                "subagent_provider": "op", "subagent_model": "op/model",
            },
        }
        with patch.object(qo, "apply_provider_env") as m:
            _build_options(profile)
        assert m.call_args.kwargs["subagent_provider_id"] is None
        assert m.call_args.kwargs["subagent_model"] is None

    def test_cross_provider_leg_threaded_into_overlay(self, mock_db, tmp_path):
        """A legal cross-provider leg (provider main + different subagent provider)
        is threaded into apply_provider_env."""
        profile = {
            "id": "t",
            "config": {
                "model": "op/main", "provider": "op", "cwd": str(tmp_path),
                "subagent_provider": "sub", "subagent_model": "sub/model",
            },
        }
        with patch.object(qo, "apply_provider_env") as m:
            _build_options(profile)
        assert m.call_args.kwargs["subagent_provider_id"] == "sub"
        assert m.call_args.kwargs["subagent_model"] == "sub/model"

    def test_no_leg_threads_none(self, mock_db, tmp_path):
        """Regression: a provider profile with no subagent leg threads None/None."""
        profile = {
            "id": "t",
            "config": {"model": "op/main", "provider": "op", "cwd": str(tmp_path)},
        }
        with patch.object(qo, "apply_provider_env") as m:
            _build_options(profile)
        assert m.call_args.kwargs["subagent_provider_id"] is None
        assert m.call_args.kwargs["subagent_model"] is None


# ---------------------------------------------------------------------------
# inject_claude_code_auth
# ---------------------------------------------------------------------------

class TestInjectClaudeCodeAuth:
    def test_provider_set_skips_anthropic_key(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.set_system_setting("anthropic_api_key", encryption.encrypt_value("sk-test"))
        env = inject_claude_code_auth({}, provider_id="op")
        assert API_KEY not in env

    def test_native_injects_anthropic_key(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.set_system_setting("anthropic_api_key", encryption.encrypt_value("sk-test"))
        env = inject_claude_code_auth({}, provider_id=None)
        assert env[API_KEY] == "sk-test"

    def test_native_no_key_injects_nothing(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        env = inject_claude_code_auth({})  # default provider_id=None, no stored key
        assert API_KEY not in env


# ---------------------------------------------------------------------------
# build_options_from_profile (chat path)
# ---------------------------------------------------------------------------

def _build_options(profile, overrides=None):
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=profile, overrides=overrides)
    return opts


class TestBuildOptionsFromProfile:
    def test_provider_profile_applies_overlay(self, mock_db, tmp_path):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, token="gw-secret")
        profile = {
            "id": "t",
            "config": {
                "model": "openai/gpt-5.6",
                "provider": "op",
                "cwd": str(tmp_path),
            },
        }
        env = _build_options(profile).env
        assert env[BASE_URL_KEY] == "https://openrouter.ai/api"
        assert env[AUTH_TOKEN_KEY] == "gw-secret"
        assert env[API_KEY] == ""
        # Context-window (1M) logic is Claude-specific → skipped for providers.
        assert DISABLE_1M_KEY not in env

    def test_native_profile_unchanged(self, mock_db, tmp_path):
        """Regression: a native profile still gets the 1M-guard and no gateway
        overlay."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        profile = {
            "id": "t",
            "config": {"model": "opus", "cwd": str(tmp_path)},
        }
        env = _build_options(profile).env
        assert env[DISABLE_1M_KEY] == "1"
        assert BASE_URL_KEY not in env
        assert AUTH_TOKEN_KEY not in env

    def test_override_provider_wins_over_config(self, mock_db, tmp_path):
        """A query-level provider override beats the profile config (mirrors the
        model override precedence)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, provider_id="op", token="gw-secret")
        profile = {  # profile config carries NO provider
            "id": "t",
            "config": {"model": "opus", "cwd": str(tmp_path)},
        }
        env = _build_options(
            profile, overrides={"provider": "op", "model": "openai/gpt-5.6"}
        ).env
        assert env[BASE_URL_KEY] == "https://openrouter.ai/api"
        assert env[API_KEY] == ""
        assert DISABLE_1M_KEY not in env


# ---------------------------------------------------------------------------
# build_generation_options (one-shot path)
# ---------------------------------------------------------------------------

class TestBuildGenerationOptions:
    def test_provider_config_applies_overlay(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, token="gw-secret")
        opts = build_generation_options(
            {"model": "openai/gpt-5.6", "provider": "op"}
        )
        env = opts.env
        assert env[BASE_URL_KEY] == "https://openrouter.ai/api"
        assert env[AUTH_TOKEN_KEY] == "gw-secret"
        assert env[API_KEY] == ""
        assert DISABLE_1M_KEY not in env

    def test_native_config_unchanged(self, mock_db):
        """Regression: native generation still pins the 200k window and has no
        gateway overlay."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        opts = build_generation_options({"model": "opus"})
        env = opts.env
        assert env[DISABLE_1M_KEY] == "1"
        assert BASE_URL_KEY not in env
