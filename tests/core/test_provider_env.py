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
    assemble_subagent_legs,
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
MAX_CONTEXT_TOKENS_KEY = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
MAX_OUTPUT_KEY = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
TRANSLATED_MAIN_KEY = "AIH_GATEWAY_TRANSLATED_MAIN"
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
        # S3: CLAUDE_CODE_SUBAGENT_MODEL is no longer emitted (subagent model
        # selection moved to per-agent frontmatter stamping).
        assert SUBAGENT_KEY not in env

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

    def test_small_context_window_pinned_exactly(self, mock_db):
        """The auto-compact pin is the provider's EXACT window — no ≥100k floor
        (inflating a small real window would delay compaction past the point
        where requests already overflow upstream)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=50000)
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert env[AUTO_COMPACT_KEY] == "50000"

    def test_large_context_window_pinned_exactly(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=400000)
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert env[AUTO_COMPACT_KEY] == "400000"

    def test_max_context_tokens_pinned_to_true_window(self, mock_db):
        """FIX: CLAUDE_CODE_MAX_CONTEXT_TOKENS is pinned to the provider's TRUE
        context_window so the CLI's believed MAX window matches the provider —
        otherwise it clamps CLAUDE_CODE_AUTO_COMPACT_WINDOW to its own 200k
        default for any non-claude-* model id."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=272000)
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert env[MAX_CONTEXT_TOKENS_KEY] == "272000"

    def test_max_context_tokens_not_clamped_to_100k_floor(self, mock_db):
        """Both pins carry the provider's TRUE window — never clamped up to a
        universal floor."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=64000)
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert env[MAX_CONTEXT_TOKENS_KEY] == "64000"
        assert env[AUTO_COMPACT_KEY] == "64000"

    def test_blocking_limit_override_lifts_local_estimator_ceiling(self, mock_db):
        """FIX: the CLI's local "Prompt is too long" guard estimates ~3
        chars/token (vs o200k's ~4 for prose), so turns that really fit the
        provider window get hard-blocked locally — and the same ceiling blocks
        the reactive-compaction rescue request. CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE
        is pinned to 2x the true window so oversize protection defers to the
        gateway's accurate tiktoken guard."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=272000)
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert env["CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"] == "544000"

    def test_blocking_limit_override_absent_when_context_window_null(self, mock_db):
        """No true window known → leave the CLI's own blocking behavior alone."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=None)
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE" not in env

    def test_blocking_limit_override_preset_wins(self, mock_db):
        """setdefault semantics: extra_env/profile preset survives."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=272000)
        env = apply_provider_env(
            {"CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE": "123456"}, "op", "openai/gpt-5.6"
        )
        assert env["CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"] == "123456"

    def test_max_context_tokens_absent_when_context_window_null(self, mock_db):
        """A NULL context_window row leaves CLAUDE_CODE_MAX_CONTEXT_TOKENS unset —
        the CLI's own 200k default is correct in that case."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=None)
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert MAX_CONTEXT_TOKENS_KEY not in env

    def test_max_context_tokens_preset_wins(self, mock_db):
        """setdefault semantics: a pre-existing value (extra_env/profile preset)
        survives over the provider's context_window."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=272000)
        env = apply_provider_env(
            {MAX_CONTEXT_TOKENS_KEY: "999999"}, "op", "openai/gpt-5.6"
        )
        assert env[MAX_CONTEXT_TOKENS_KEY] == "999999"

    def test_row_context_window_overrides_1m_suffix(self, mock_db):
        """The provider row's context_window is AUTHORITATIVE: a sub-1M row
        clamps even an app [1m]-suffixed model id (the suffix is a
        Claude-native-picker decoration, not provider config)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=272000)
        env = apply_provider_env({}, "op", "claude-sonnet-4-5[1m]")
        assert env[DISABLE_1M_KEY] == "1"
        assert env[AUTO_COMPACT_KEY] == "272000"
        assert env[MAX_CONTEXT_TOKENS_KEY] == "272000"

    def test_row_1m_context_window_enables_wide_window(self, mock_db):
        """A row advertising a ≥1M window gets the wide window WITHOUT any [1m]
        suffix — previously impossible (the gate universally forced 200k on
        provider sessions) — and all pins carry the exact row value."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=1_000_000)
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert env[DISABLE_1M_KEY] == "0"
        assert env[AUTO_COMPACT_KEY] == "1000000"
        assert env[MAX_CONTEXT_TOKENS_KEY] == "1000000"
        assert env["CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"] == "2000000"

    def test_null_context_window_leaves_auto_compact_unset(self, mock_db):
        """A provider row with context_window NULL leaves the auto-compact
        window to the CLI — no hardcoded universal 200000 fallback (forcing one
        compaction threshold across providers with different windows is wrong;
        the CLI's own third-party-base-URL default applies)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=None)
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert AUTO_COMPACT_KEY not in env

    def test_null_context_window_auto_compact_preset_survives(self, mock_db):
        """A pre-set value (profile preset / extra_env) passes through untouched
        on a NULL context_window row."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=None)
        env = apply_provider_env(
            {AUTO_COMPACT_KEY: "654321"}, "op", "openai/gpt-5.6"
        )
        assert env[AUTO_COMPACT_KEY] == "654321"

    def test_disable_1m_set_for_codex_mode_provider(self, mock_db):
        """A translated (codex/chatgpt-mode) provider with a foreign model id gets
        DISABLE_1M="1"; with no context_window on the row, the auto-compact
        window is left to the CLI (no hardcoded universal pin)."""
        from app.core.translator_gateway import routing
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        routing.get_registry().clear()
        db.create_model_provider(
            provider_id="gw", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None,
            models=[{"id": "gpt-5-codex", "label": "GPT-5 Codex"}],
        )
        env = apply_provider_env({}, "gw", "gpt-5-codex", session_id="sess-cg")
        assert env[DISABLE_1M_KEY] == "1"
        assert AUTO_COMPACT_KEY not in env

    def test_1m_model_allows_wide_window_no_auto_compact_pin(self, mock_db):
        """An app [1m]-opted model keeps the wide window (DISABLE_1M="0") and does
        NOT pin auto-compaction — parity with the native path's early return."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, context_window=None)
        env = apply_provider_env({}, "op", "claude-sonnet-4-5[1m]")
        assert env[DISABLE_1M_KEY] == "0"
        assert AUTO_COMPACT_KEY not in env

    def test_r4_fallback_default_model(self, mock_db):
        """No small_fast_model → falls back to default_model."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, small_fast_model=None, default_model="openai/gpt-5.6")
        env = apply_provider_env({}, "op", "session-model")
        assert env[SMALL_FAST_KEY] == "openai/gpt-5.6"
        assert SUBAGENT_KEY not in env

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
        assert SUBAGENT_KEY not in env

    def test_r4_fallback_stable_across_per_turn_model(self, mock_db):
        """FIX 3: the small/fast overlay must be IDENTICAL across two calls that
        differ ONLY in the per-turn model — otherwise a within-provider composer
        switch would churn the key into a subagent-killing respawn."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(
            db, small_fast_model=None, default_model=None,
            models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
        )
        env_a = apply_provider_env({}, "op", "model-a")
        env_b = apply_provider_env({}, "op", "model-b")
        assert env_a[SMALL_FAST_KEY] == env_b[SMALL_FAST_KEY]
        # S3: CLAUDE_CODE_SUBAGENT_MODEL is never emitted.
        assert SUBAGENT_KEY not in env_a
        assert SUBAGENT_KEY not in env_b

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
        over the provider context_window (setdefault semantics)."""
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

    def test_translated_main_marker_set_for_chatgpt(self, mock_db):
        """S8-R1: a chatgpt-type (translated) main sets AIH_GATEWAY_TRANSLATED_MAIN
        so attach_hooks_to_options wires the Agent-arg-strip PreToolUse hook."""
        from app.core.translator_gateway import routing
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        routing.get_registry().clear()
        db.create_model_provider(
            provider_id="gw", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None,
            models=[{"id": "gpt-5-codex", "label": "GPT-5 Codex"}],
        )
        env = apply_provider_env({}, "gw", "gpt-5-codex", session_id="sess-cg")
        assert env[TRANSLATED_MAIN_KEY] == "1"

    def test_translated_main_marker_absent_for_openrouter(self, mock_db):
        """A non-translated (openrouter) main does NOT set the marker."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, provider_type="openrouter", token="gw-secret")
        env = apply_provider_env({}, "op", "openai/gpt-5.6")
        assert TRANSLATED_MAIN_KEY not in env

    def test_translated_main_marker_absent_for_custom(self, mock_db):
        """A custom-type main (Anthropic-compatible endpoint) is not translated →
        no marker."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(
            db, provider_type="custom", token="gw-secret",
            base_url="https://custom.example/api",
        )
        env = apply_provider_env({}, "op", "custom/model")
        assert TRANSLATED_MAIN_KEY not in env

    def test_ProviderConfigError_is_value_error(self):
        """R5 mechanism: ProviderConfigError subclasses ValueError so the query
        engines' existing (WorktreeUnavailableError, ValueError) guards surface
        it as a user-visible error."""
        assert issubclass(ProviderConfigError, ValueError)

    # ── S3: pre-honored subagent leg map (ruling A2/G2-r) ────────────────────
    #
    # apply_provider_env now receives an already-honored ``subagent_legs`` map
    # (the caller's assemble_subagent_legs did the servability/enabled gating and
    # account resolution). It threads the whole map into the routing token, routes
    # through the gateway when the map is non-empty, and NEVER emits
    # CLAUDE_CODE_SUBAGENT_MODEL (subagent model selection is frontmatter now).

    def test_honored_leg_mints_token_carries_legs_no_subagent_key(self, mock_db):
        """A translated (chatgpt) main + a pre-honored leg: the minted routing
        token carries the leg map verbatim; ANTHROPIC_SMALL_FAST_MODEL keeps the
        MAIN provider's chain; CLAUDE_CODE_SUBAGENT_MODEL is NOT emitted."""
        from app.core.translator_gateway import routing
        from app.db import database as db

        encryption.set_encryption_key("pw", db)
        routing.get_registry().clear()
        db.create_model_provider(
            provider_id="gw", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None,
            models=[{"id": "gpt-5-codex", "label": "GPT-5 Codex"}],
        )
        legs = [("sub/model", "sub", "acct-sub")]
        env = apply_provider_env(
            {}, "gw", "gpt-5-codex", session_id="sess-leg",
            subagent_legs=legs,
        )
        binding = routing.resolve(env[AUTH_TOKEN_KEY])
        assert binding is not None
        assert binding.provider_id == "gw"
        assert binding.legs == (("sub/model", "sub", "acct-sub"),)
        # small/fast falls back to the main provider's first curated model; the
        # subagent env var is gone entirely.
        assert env[SMALL_FAST_KEY] == "gpt-5-codex"
        assert SUBAGENT_KEY not in env

    def test_anthropic_main_with_leg_routes_through_gateway(self, mock_db):
        """anthropic-main + a pre-honored (Codex) leg → the session is routed
        through the loopback gateway: base URL is the gateway, a routing token
        carrying the leg map is minted, and the REAL Anthropic API key is NOT
        placed in env (the gateway reads it from the provider row at request
        time). CLAUDE_CODE_SUBAGENT_MODEL is not emitted."""
        from app.core.config import settings
        from app.core.translator_gateway import routing
        from app.db import database as db

        encryption.set_encryption_key("pw", db)
        routing.get_registry().clear()
        db.create_model_provider(
            provider_id="an", name="Claude API", provider_type="anthropic",
            base_url="https://api.anthropic.com",
            auth_token=encryption.encrypt_value("sk-ant-real"),
            models=[{"id": "claude-sonnet-4", "label": "Sonnet"}],
        )
        env = apply_provider_env(
            {}, "an", "claude-sonnet-4", session_id="sess-an",
            subagent_legs=[("gpt-5-codex", "sub", "acct-sub")],
        )
        assert env[BASE_URL_KEY] == f"http://127.0.0.1:{settings.translator_gateway_port}"
        binding = routing.resolve(env[AUTH_TOKEN_KEY])
        assert binding is not None
        assert binding.provider_id == "an"
        assert binding.legs == (("gpt-5-codex", "sub", "acct-sub"),)
        # The real Anthropic API key must NEVER be exposed in env in this mode.
        assert "sk-ant-real" not in env.values()
        assert env[API_KEY] == ""
        assert SUBAGENT_KEY not in env

    def test_anthropic_main_no_leg_keeps_direct_env(self, mock_db):
        """Regression: a bare anthropic-type main (no legs) keeps the DIRECT
        base_url path byte-identical to today — real key in env, no gateway, no
        routing token. An explicit empty leg map must match omitting it."""
        from app.core.translator_gateway import routing
        from app.db import database as db

        encryption.set_encryption_key("pw", db)
        routing.get_registry().clear()
        db.create_model_provider(
            provider_id="an", name="Claude API", provider_type="anthropic",
            base_url="https://api.anthropic.com",
            auth_token=encryption.encrypt_value("sk-ant-real"),
            models=[{"id": "claude-sonnet-4", "label": "Sonnet"}],
        )
        env = apply_provider_env({}, "an", "claude-sonnet-4", session_id="sess-an")
        assert env[BASE_URL_KEY] == "https://api.anthropic.com"
        assert env[AUTH_TOKEN_KEY] == "sk-ant-real"   # direct decrypted key
        assert env[API_KEY] == ""
        # No routing token minted on the direct path.
        assert routing.resolve("sk-ant-real") is None
        # Explicit empty leg map is inert → identical env.
        env_empty = apply_provider_env(
            {}, "an", "claude-sonnet-4", session_id="sess-an",
            subagent_legs=(),
        )
        assert env_empty == env

    def test_empty_legs_leaves_subagent_key_unset_no_token(self, mock_db):
        """A bare provider session with an empty leg map: no CLAUDE_CODE_SUBAGENT_MODEL,
        no routing token, direct base_url path preserved."""
        from app.core.translator_gateway import routing
        from app.db import database as db

        encryption.set_encryption_key("pw", db)
        routing.get_registry().clear()
        _mk_provider(db, provider_id="op", token="gw-secret", small_fast_model="op/small")
        env = apply_provider_env({}, "op", "op/main", subagent_legs=())
        assert env[SMALL_FAST_KEY] == "op/small"
        assert SUBAGENT_KEY not in env
        assert env[BASE_URL_KEY] == "https://openrouter.ai/api"  # direct kept
        assert routing.resolve(env[AUTH_TOKEN_KEY]) is None      # token is the raw key


# ---------------------------------------------------------------------------
# assemble_subagent_legs (S3 leg-map assembler)
# ---------------------------------------------------------------------------

class TestAssembleSubagentLegs:
    def test_union_default_leg_first_then_subagent_composite(self, mock_db):
        """Servable main + honored default leg + one enabled-subagent composite on
        a distinct servable provider → default leg FIRST, then the composite leg;
        accounts resolved for chatgpt providers."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="main", name="Main", provider_type="chatgpt",
            base_url="", auth_token=None, small_fast_model="m/small",
            models=[{"id": "m", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="dl", name="DL", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "dl/model", "label": "DL"}],
        )
        db.set_codex_token_bundle("dl", access_token=None, refresh_token=None, account_id="acct-dl")
        db.create_model_provider(
            provider_id="sub", name="Sub", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "sub/model", "label": "Sub"}],
        )
        db.set_codex_token_bundle("sub", access_token=None, refresh_token=None, account_id="acct-sub")
        main_row = db.get_model_provider("main", include_token=False)
        plan = assemble_subagent_legs(
            main_provider_row=main_row,
            default_leg_provider="dl",
            default_leg_model="dl/model",
            enabled_subagents={"agent-x": {"model": "prov:sub:sub/model"}},
        )
        assert plan.honored_legs == [
            ("dl/model", "dl", "acct-dl"),
            ("sub/model", "sub", "acct-sub"),
        ]
        assert plan.session_default == "dl/model"
        assert plan.main_serves_claude_ids is False
        assert plan.dropped == []

    def test_non_servable_main_drops_whole_map_single_error(self, mock_db, caplog):
        """A non-servable (openrouter) main drops the ENTIRE map (A2): honored_legs
        empty, every candidate in dropped, session_default = the main small/fast,
        and exactly ONE aggregate error logged."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="op", name="OR", provider_type="openrouter",
            base_url="https://openrouter.ai/api", auth_token=None,
            small_fast_model="op/small", models=[{"id": "op/main", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="sub", name="Sub", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "sub/model", "label": "Sub"}],
        )
        main_row = db.get_model_provider("op", include_token=False)
        with caplog.at_level(logging.ERROR):
            plan = assemble_subagent_legs(
                main_provider_row=main_row,
                default_leg_provider="dl",
                default_leg_model="dl/model",
                enabled_subagents={"agent-x": {"model": "prov:sub:sub/model"}},
            )
        assert plan.honored_legs == []
        assert ("<profile-default-leg>", "dl") in plan.dropped
        assert ("agent-x", "sub") in plan.dropped
        assert plan.session_default == "op/small"
        errs = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errs) == 1

    def test_per_entry_drop_single_aggregate_error(self, mock_db, caplog):
        """Servable main; one enabled-subagent composite on a DISABLED provider →
        that entry dropped, the other honored, exactly ONE aggregate error naming
        the subagent id + provider id (no token)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="main", name="Main", provider_type="chatgpt",
            base_url="", auth_token=None, small_fast_model="m/small",
            models=[{"id": "m", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="good", name="Good", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "good/model", "label": "G"}],
        )
        db.create_model_provider(
            provider_id="bad", name="Bad", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "bad/model", "label": "B"}],
            enabled=False,
        )
        main_row = db.get_model_provider("main", include_token=False)
        with caplog.at_level(logging.ERROR):
            plan = assemble_subagent_legs(
                main_provider_row=main_row,
                default_leg_provider=None,
                default_leg_model=None,
                enabled_subagents={
                    "agent-good": {"model": "prov:good:good/model"},
                    "agent-bad": {"model": "prov:bad:bad/model"},
                },
            )
        assert ("good/model", "good", None) in plan.honored_legs
        assert ("agent-bad", "bad") in plan.dropped
        assert all(pid != "bad" for _mid, pid, _a in plan.honored_legs)
        errs = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errs) == 1
        msg = errs[0].getMessage()
        assert "agent-bad" in msg and "bad" in msg

    def test_session_default_small_fast_fallback_chain(self, mock_db):
        """No default leg → session_default falls back through
        small_fast_model → default_model → first curated model id."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="main", name="Main", provider_type="chatgpt",
            base_url="", auth_token=None,
            models=[{"id": "first/curated", "label": "F"}],
        )
        main_row = db.get_model_provider("main", include_token=False)
        plan = assemble_subagent_legs(
            main_provider_row=main_row,
            default_leg_provider=None, default_leg_model=None, enabled_subagents={},
        )
        assert plan.session_default == "first/curated"
        assert plan.honored_legs == []

    def test_main_serves_claude_ids_true_only_for_anthropic(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="an", name="An", provider_type="anthropic",
            base_url="https://api.anthropic.com", auth_token=None,
            models=[{"id": "c", "label": "C"}],
        )
        db.create_model_provider(
            provider_id="cg", name="Cg", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "g", "label": "G"}],
        )
        an = db.get_model_provider("an", include_token=False)
        cg = db.get_model_provider("cg", include_token=False)
        p_an = assemble_subagent_legs(
            main_provider_row=an, default_leg_provider=None,
            default_leg_model=None, enabled_subagents={},
        )
        p_cg = assemble_subagent_legs(
            main_provider_row=cg, default_leg_provider=None,
            default_leg_model=None, enabled_subagents={},
        )
        assert p_an.main_serves_claude_ids is True
        assert p_cg.main_serves_claude_ids is False

    def test_account_resolution_non_chatgpt_leg_is_none(self, mock_db):
        """A servable non-chatgpt (anthropic) leg provider carries account None."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="main", name="Main", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "m", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="ansub", name="AnSub", provider_type="anthropic",
            base_url="https://api.anthropic.com", auth_token=None,
            models=[{"id": "an/model", "label": "A"}],
        )
        main_row = db.get_model_provider("main", include_token=False)
        plan = assemble_subagent_legs(
            main_provider_row=main_row,
            default_leg_provider=None, default_leg_model=None,
            enabled_subagents={"a": {"model": "prov:ansub:an/model"}},
        )
        assert plan.honored_legs == [("an/model", "ansub", None)]

    def test_malformed_and_non_composite_produce_no_leg(self, mock_db):
        """Malformed composites (no second colon), non-composites, and unpinned
        subagent models are skipped (not dropped)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="main", name="Main", provider_type="chatgpt",
            base_url="", auth_token=None, small_fast_model="m/small",
            models=[{"id": "m", "label": "M"}],
        )
        main_row = db.get_model_provider("main", include_token=False)
        plan = assemble_subagent_legs(
            main_provider_row=main_row,
            default_leg_provider=None, default_leg_model=None,
            enabled_subagents={
                "a": {"model": "prov:onlyprovider"},  # malformed
                "b": {"model": "sonnet"},              # non-composite
                "c": {"model": None},                  # unpinned
            },
        )
        assert plan.honored_legs == []
        assert plan.dropped == []

    def test_exact_duplicate_legs_deduped(self, mock_db):
        """Two enabled subagents pinned to the SAME (mid, pid, acct) → a single leg."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="main", name="Main", provider_type="chatgpt",
            base_url="", auth_token=None, small_fast_model="m/small",
            models=[{"id": "m", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="sub", name="Sub", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "sub/model", "label": "S"}],
        )
        main_row = db.get_model_provider("main", include_token=False)
        plan = assemble_subagent_legs(
            main_provider_row=main_row,
            default_leg_provider=None, default_leg_model=None,
            enabled_subagents={
                "a": {"model": "prov:sub:sub/model"},
                "b": {"model": "prov:sub:sub/model"},
            },
        )
        assert plan.honored_legs == [("sub/model", "sub", None)]

    def test_default_leg_wins_same_mid_collision(self, mock_db, caplog):
        """Profile-default leg and a per-subagent entry collide on the SAME mid
        (different pids) → the default-leg entry survives, the subagent entry is
        gone, and one aggregate warning names the losing pid."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="main", name="Main", provider_type="chatgpt",
            base_url="", auth_token=None, small_fast_model="m/small",
            models=[{"id": "m", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="dl", name="DL", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "shared/mid", "label": "DL"}],
        )
        db.create_model_provider(
            provider_id="sub", name="Sub", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "shared/mid", "label": "Sub"}],
        )
        main_row = db.get_model_provider("main", include_token=False)
        with caplog.at_level(logging.WARNING):
            plan = assemble_subagent_legs(
                main_provider_row=main_row,
                default_leg_provider="dl",
                default_leg_model="shared/mid",
                enabled_subagents={"agent-x": {"model": "prov:sub:shared/mid"}},
            )
        assert plan.honored_legs == [("shared/mid", "dl", None)]
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
        assert "sub" in warns[0].getMessage()

    def test_lexicographic_pid_wins_same_mid_collision(self, mock_db, caplog):
        """Two per-subagent entries collide on the SAME mid (different pids, no
        default-leg collision) → the lexicographically-smallest pid survives; the
        aggregate warning names the loser."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="main", name="Main", provider_type="chatgpt",
            base_url="", auth_token=None, small_fast_model="m/small",
            models=[{"id": "m", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="aaa", name="Aaa", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "shared/mid", "label": "A"}],
        )
        db.create_model_provider(
            provider_id="zzz", name="Zzz", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "shared/mid", "label": "Z"}],
        )
        main_row = db.get_model_provider("main", include_token=False)
        with caplog.at_level(logging.WARNING):
            plan = assemble_subagent_legs(
                main_provider_row=main_row,
                default_leg_provider=None, default_leg_model=None,
                enabled_subagents={
                    "agent-z": {"model": "prov:zzz:shared/mid"},
                    "agent-a": {"model": "prov:aaa:shared/mid"},
                },
            )
        assert plan.honored_legs == [("shared/mid", "aaa", None)]
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
        assert "zzz" in warns[0].getMessage()

    def test_exact_duplicate_collision_no_warning(self, mock_db, caplog):
        """Exact-duplicate tuples (same mid+pid+acct from two subagents) → a single
        survivor and NO warning (an exact dup is not a genuine collision loser)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="main", name="Main", provider_type="chatgpt",
            base_url="", auth_token=None, small_fast_model="m/small",
            models=[{"id": "m", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="sub", name="Sub", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "sub/model", "label": "S"}],
        )
        main_row = db.get_model_provider("main", include_token=False)
        with caplog.at_level(logging.WARNING):
            plan = assemble_subagent_legs(
                main_provider_row=main_row,
                default_leg_provider=None, default_leg_model=None,
                enabled_subagents={
                    "a": {"model": "prov:sub:sub/model"},
                    "b": {"model": "prov:sub:sub/model"},
                },
            )
        assert plan.honored_legs == [("sub/model", "sub", None)]
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warns == []


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
        small/fast chain already routes subagents there) → collapsed to inherit,
        so no leg is threaded into apply_provider_env."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="op", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "op/main", "label": "M"}],
        )
        profile = {
            "id": "t",
            "config": {
                "model": "op/main", "provider": "op", "cwd": str(tmp_path),
                "subagent_provider": "op", "subagent_model": "op/model",
            },
        }
        with patch.object(qo, "apply_provider_env") as m:
            _build_options(profile)
        assert tuple(m.call_args.kwargs["subagent_legs"]) == ()

    def test_cross_provider_leg_threaded_into_overlay(self, mock_db, tmp_path):
        """A legal cross-provider default leg (servable provider main + a different
        servable subagent provider) is assembled and threaded into
        apply_provider_env as the leg map."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="op", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "op/main", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="sub", name="Sub", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "sub/model", "label": "Sub"}],
        )
        profile = {
            "id": "t",
            "config": {
                "model": "op/main", "provider": "op", "cwd": str(tmp_path),
                "subagent_provider": "sub", "subagent_model": "sub/model",
            },
        }
        with patch.object(qo, "apply_provider_env") as m:
            _build_options(profile)
        assert list(m.call_args.kwargs["subagent_legs"]) == [("sub/model", "sub", None)]

    def test_no_leg_threads_empty_map(self, mock_db, tmp_path):
        """Regression: a provider profile with no subagent leg threads an empty
        leg map."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="op", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "op/main", "label": "M"}],
        )
        profile = {
            "id": "t",
            "config": {"model": "op/main", "provider": "op", "cwd": str(tmp_path)},
        }
        with patch.object(qo, "apply_provider_env") as m:
            _build_options(profile)
        assert tuple(m.call_args.kwargs["subagent_legs"]) == ()


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
        # Context-window safety pins now apply on the provider path too: a
        # non-[1m] model forces the 200k guard so the CLI can't self-derive an
        # auto-compact window off a foreign model id.
        assert env[DISABLE_1M_KEY] == "1"

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
        assert env[DISABLE_1M_KEY] == "1"  # provider path now pins the 200k guard


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
        assert env[DISABLE_1M_KEY] == "1"  # provider path now pins the 200k guard

    def test_native_config_unchanged(self, mock_db):
        """Regression: native generation still pins the 200k window and has no
        gateway overlay."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        opts = build_generation_options({"model": "opus"})
        env = opts.env
        assert env[DISABLE_1M_KEY] == "1"
        assert BASE_URL_KEY not in env


# ---------------------------------------------------------------------------
# S3: per-agent frontmatter stamping via build_options_from_profile
# ---------------------------------------------------------------------------

def _mk_subagent(db, agent_id, model):
    return db.create_subagent(
        subagent_id=agent_id, name=agent_id,
        description="d", prompt="p", model=model,
    )


class TestSubagentFrontmatterStamping:
    def test_gateway_composite_pin_and_unpinned_stamped(self, mock_db, tmp_path):
        """Gateway (chatgpt) main + one composite-pinned enabled subagent (honored)
        + one unpinned: the pinned agent's model becomes the bare upstream id, the
        unpinned agent's model becomes the session default, and apply_provider_env
        is threaded the honored leg."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="op", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None, small_fast_model="op/small",
            models=[{"id": "op/main", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="sub", name="Sub", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "sub/model", "label": "S"}],
        )
        _mk_subagent(db, "pinned", "prov:sub:sub/model")
        _mk_subagent(db, "unpinned", None)
        profile = {
            "id": "t",
            "config": {
                "model": "op/main", "provider": "op", "cwd": str(tmp_path),
                "enabled_agents": ["pinned", "unpinned"],
            },
        }
        with patch.object(qo, "apply_provider_env") as m:
            opts = _build_options(profile)
        assert opts.agents["pinned"].model == "sub/model"
        assert opts.agents["unpinned"].model == "op/small"   # session default
        assert list(m.call_args.kwargs["subagent_legs"]) == [("sub/model", "sub", None)]

    def test_claude_alias_pin_kept_on_anthropic_main(self, mock_db, tmp_path):
        """anthropic main serves Claude ids → a Claude-alias-pinned subagent keeps
        its alias (behavior delta: anthropic-API main + `sonnet` → kept)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="an", name="Claude API", provider_type="anthropic",
            base_url="https://api.anthropic.com",
            auth_token=encryption.encrypt_value("sk-ant"),
            small_fast_model="claude-haiku",
            models=[{"id": "claude-sonnet-4", "label": "S"}],
        )
        _mk_subagent(db, "cl", "sonnet")
        profile = {
            "id": "t",
            "config": {
                "model": "claude-sonnet-4", "provider": "an", "cwd": str(tmp_path),
                "enabled_agents": ["cl"],
            },
        }
        with patch.object(qo, "apply_provider_env"):
            opts = _build_options(profile)
        assert opts.agents["cl"].model == "sonnet"

    def test_claude_alias_pin_replaced_on_chatgpt_main(self, mock_db, tmp_path):
        """chatgpt main does NOT serve Claude ids → a Claude-alias-pinned subagent
        is replaced by the session default."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="op", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None, small_fast_model="op/small",
            models=[{"id": "op/main", "label": "M"}],
        )
        _mk_subagent(db, "cl", "sonnet")
        profile = {
            "id": "t",
            "config": {
                "model": "op/main", "provider": "op", "cwd": str(tmp_path),
                "enabled_agents": ["cl"],
            },
        }
        with patch.object(qo, "apply_provider_env"):
            opts = _build_options(profile)
        assert opts.agents["cl"].model == "op/small"

    def test_composite_non_servable_provider_stamped_session_default(self, mock_db, tmp_path):
        """A composite whose provider is non-servable (openrouter) → not honored →
        the agent is stamped the session default and the leg is absent."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="op", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None, small_fast_model="op/small",
            models=[{"id": "op/main", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="orr", name="OpenRouter", provider_type="openrouter",
            base_url="https://openrouter.ai/api",
            auth_token=encryption.encrypt_value("or-secret"),
            models=[{"id": "orr/model", "label": "OR"}],
        )
        _mk_subagent(db, "foreign", "prov:orr:orr/model")
        profile = {
            "id": "t",
            "config": {
                "model": "op/main", "provider": "op", "cwd": str(tmp_path),
                "enabled_agents": ["foreign"],
            },
        }
        with patch.object(qo, "apply_provider_env") as m:
            opts = _build_options(profile)
        assert opts.agents["foreign"].model == "op/small"
        assert tuple(m.call_args.kwargs["subagent_legs"]) == ()

    def test_token_minted_with_expected_legs(self, mock_db, tmp_path):
        """End-to-end (real apply_provider_env): a chatgpt main + composite-pinned
        subagent mints a routing token whose binding carries the honored leg."""
        from app.core.translator_gateway import routing
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        routing.get_registry().clear()
        db.create_model_provider(
            provider_id="op", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None, small_fast_model="op/small",
            models=[{"id": "op/main", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="sub", name="Sub", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "sub/model", "label": "S"}],
        )
        _mk_subagent(db, "pinned", "prov:sub:sub/model")
        profile = {
            "id": "t",
            "config": {
                "model": "op/main", "provider": "op", "cwd": str(tmp_path),
                "enabled_agents": ["pinned"],
            },
        }
        env = _build_options(profile).env
        binding = routing.resolve(env[AUTH_TOKEN_KEY])
        assert binding is not None
        assert binding.legs == (("sub/model", "sub", None),)

    def test_native_session_leaves_subagent_models_untouched(self, mock_db, tmp_path):
        """Native (no-provider) session: enabled-subagent models are left exactly
        as stored (resolver/helper not invoked); apply_provider_env not called."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_subagent(db, "cl", "sonnet")
        _mk_subagent(db, "unp", None)
        profile = {
            "id": "t",
            "config": {
                "model": "opus", "cwd": str(tmp_path),
                "enabled_agents": ["cl", "unp"],
            },
        }
        with patch.object(qo, "apply_provider_env") as m:
            opts = _build_options(profile)
        m.assert_not_called()
        assert opts.agents["cl"].model == "sonnet"
        assert opts.agents["unp"].model is None

    def test_native_composite_pin_omitted(self, mock_db, tmp_path):
        """S8-R2: a NATIVE (no-provider) session with a composite-pinned subagent
        omits the model (→ None → inherit main); the CLI cannot serve a
        ``prov:...`` composite, so it must never be stamped verbatim."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_subagent(db, "comp", "prov:sub:gpt-5.6-luna")
        profile = {
            "id": "t",
            "config": {
                "model": "opus", "cwd": str(tmp_path),
                "enabled_agents": ["comp"],
            },
        }
        with patch.object(qo, "apply_provider_env") as m:
            opts = _build_options(profile)
        m.assert_not_called()
        assert opts.agents["comp"].model is None

    def test_native_claude_alias_pin_kept(self, mock_db, tmp_path):
        """S8-R2: a NATIVE session keeps a Claude-alias-pinned subagent's model."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_subagent(db, "cl", "sonnet")
        profile = {
            "id": "t",
            "config": {
                "model": "opus", "cwd": str(tmp_path),
                "enabled_agents": ["cl"],
            },
        }
        with patch.object(qo, "apply_provider_env") as m:
            opts = _build_options(profile)
        m.assert_not_called()
        assert opts.agents["cl"].model == "sonnet"

    def test_native_unpinned_stays_none(self, mock_db, tmp_path):
        """S8-R2: a NATIVE session leaves an unpinned subagent's model None."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_subagent(db, "unp", None)
        profile = {
            "id": "t",
            "config": {
                "model": "opus", "cwd": str(tmp_path),
                "enabled_agents": ["unp"],
            },
        }
        with patch.object(qo, "apply_provider_env") as m:
            opts = _build_options(profile)
        m.assert_not_called()
        assert opts.agents["unp"].model is None

    def test_behavior_delta_openrouter_main_sonnet_sub_to_small_fast(self, mock_db, tmp_path):
        """Behavior delta: on an openrouter (non-Claude-serving) provider main, a
        `sonnet`-pinned subagent is stamped the provider small/fast (not sonnet)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, provider_id="op", token="gw-secret", small_fast_model="op/small")
        _mk_subagent(db, "cl", "sonnet")
        profile = {
            "id": "t",
            "config": {
                "model": "op/main", "provider": "op", "cwd": str(tmp_path),
                "enabled_agents": ["cl"],
            },
        }
        with patch.object(qo, "apply_provider_env"):
            opts = _build_options(profile)
        assert opts.agents["cl"].model == "op/small"
