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
        upstream's own limit check."""
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
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
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
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
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

    # ── translated (Codex/OpenAI) rows after the gateway retirement ─────────
    #
    # These rows used to be pointed at the loopback translator gateway with a
    # per-session routing token. The gateway is gone and omni serves them
    # natively, so the CLI env carries NO upstream credential for them — but the
    # overlay must still apply every guard/window/small-fast key, and must NOT
    # raise for a row that has neither an auth_token nor a base_url (a ChatGPT
    # subscription row legitimately has neither).

    def test_translated_row_gets_blank_base_url_and_token(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="gw", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None,
            models=[{"id": "gpt-5-codex", "label": "GPT-5 Codex"}],
        )
        env = apply_provider_env({}, "gw", "gpt-5-codex", session_id="sess-cg")
        assert env[BASE_URL_KEY] == ""
        assert env[AUTH_TOKEN_KEY] == ""
        assert env[API_KEY] == ""
        for k in MITIGATION_KEYS:
            assert env[k] == "1"
        assert env[SMALL_FAST_KEY] == "gpt-5-codex"   # first curated id
        assert SUBAGENT_KEY not in env

    def test_translated_row_without_token_or_base_url_does_not_raise(self, mock_db):
        """Regression guard for the retirement: the direct path's "neither token
        nor base URL" error must never fire for a translated row, or every Codex
        session becomes unspawnable."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="gw", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None,
            models=[{"id": "gpt-5-codex", "label": "GPT-5 Codex"}],
        )
        env = apply_provider_env({}, "gw", "gpt-5-codex")
        assert env[BASE_URL_KEY] == ""

    def test_anthropic_row_keeps_the_direct_key_path(self, mock_db):
        """An anthropic-type row is NOT translated: it keeps its real decrypted
        key and its own base_url (this used to be diverted onto the gateway as
        soon as a subagent leg was configured; legs are gone)."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="an", name="Claude API", provider_type="anthropic",
            base_url="https://api.anthropic.com",
            auth_token=encryption.encrypt_value("sk-ant-real"),
            models=[{"id": "claude-sonnet-4", "label": "Sonnet"}],
        )
        env = apply_provider_env({}, "an", "claude-sonnet-4", session_id="sess-an")
        assert env[BASE_URL_KEY] == "https://api.anthropic.com"
        assert env[AUTH_TOKEN_KEY] == "sk-ant-real"
        assert env[API_KEY] == ""
        assert SUBAGENT_KEY not in env


# ---------------------------------------------------------------------------
# D4: cross-provider subagent legs are REMOVED
# ---------------------------------------------------------------------------

class TestSubagentLegsRemoved:
    """The gateway-served cross-provider subagent leg is gone as a feature.

    ``apply_provider_env`` no longer accepts a leg map, and ``query_options``
    no longer assembles one — a profile that still carries
    ``subagent_provider``/``subagent_model`` is inert config, not a route.
    ProviderAgent workers are the replacement.
    """

    def test_apply_provider_env_rejects_a_leg_map(self, mock_db):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, token="gw-secret")
        with pytest.raises(TypeError):
            apply_provider_env(
                {}, "op", "openai/gpt-5.6",
                subagent_legs=[("sub/model", "sub", None)],  # type: ignore[call-arg]
            )

    def test_query_options_threads_no_leg_map(self, mock_db, tmp_path):
        """Even with a fully-configured cross-provider leg on the profile, the
        overlay call carries no leg argument."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="op", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "op/main", "label": "M"}],
        )
        db.create_model_provider(
            provider_id="sub", name="Sub", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "sub/model", "label": "S"}],
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
        assert "subagent_legs" not in m.call_args.kwargs

    def test_native_session_with_a_configured_leg_stays_native(self, mock_db, tmp_path):
        """A native (no-provider) profile that still names a subagent provider is
        simply ignored — no overlay, native window guard intact."""
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        profile = {
            "id": "t",
            "config": {
                "model": "opus", "cwd": str(tmp_path),
                "subagent_provider": "sub", "subagent_model": "sub/model",
            },
        }
        with patch.object(qo, "apply_provider_env") as m:
            env = _build_options(profile).env
        m.assert_not_called()
        assert env[DISABLE_1M_KEY] == "1"


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


class TestSubagentDefinitionsRetired:
    """(P2c, rulings A6/R2) SDK AgentDefinitions are gone: enabled_agents is
    now the Task tool's optional invoke allowlist, consumed at dispatch time
    by app.core.task_dispatch — build_options_from_profile no longer builds
    agents_dict no matter what the profile enables."""

    def test_enabled_agents_produce_no_agent_definitions(self, mock_db):
        from unittest.mock import patch as _patch
        import app.core.query_options as qo
        from app.db import database as db

        db.create_profile("helper", "Helper", None, {"model": "sonnet"})
        with _patch.object(qo, "get_security_instructions", return_value=""):
            opts, agents_dict = qo.build_options_from_profile(
                profile={"id": "t", "config": {
                    "model": "opus", "tools_preset": "claude_code",
                    "enabled_agents": ["helper"],
                }},
            )
        assert agents_dict is None
        assert opts.agents is None


class TestClaudeApiSkillGuardSurvivesLegRemoval:
    """The guard used to key off ``SubagentLegPlan.main_serves_claude_ids``.

    That field was only ever ``provider_type == "anthropic"`` on the session's
    bound provider row, so removing the leg assembler must not change ANY of
    these outcomes.
    """

    def _opts(self, tmp_path, provider_id=None):
        config = {"model": "m", "cwd": str(tmp_path)}
        if provider_id:
            config["provider"] = provider_id
        with patch.object(qo, "apply_provider_env"):
            return _build_options({"id": "t", "config": config})

    def test_blocked_on_a_chatgpt_provider_session(self, mock_db, tmp_path):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="op", name="Codex", provider_type="chatgpt",
            base_url="", auth_token=None, models=[{"id": "m", "label": "M"}],
        )
        opts = self._opts(tmp_path, "op")
        assert qo.NON_CLAUDE_DISALLOWED_SKILL in opts.disallowed_tools

    def test_blocked_on_an_openrouter_provider_session(self, mock_db, tmp_path):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        _mk_provider(db, provider_id="op", token="gw-secret")
        opts = self._opts(tmp_path, "op")
        assert qo.NON_CLAUDE_DISALLOWED_SKILL in opts.disallowed_tools

    def test_allowed_on_an_anthropic_provider_session(self, mock_db, tmp_path):
        from app.db import database as db
        encryption.set_encryption_key("pw", db)
        db.create_model_provider(
            provider_id="an", name="Claude API", provider_type="anthropic",
            base_url="https://api.anthropic.com",
            auth_token=encryption.encrypt_value("sk-ant"),
            models=[{"id": "m", "label": "M"}],
        )
        opts = self._opts(tmp_path, "an")
        assert qo.NON_CLAUDE_DISALLOWED_SKILL not in opts.disallowed_tools

    def test_allowed_on_a_native_session(self, mock_db, tmp_path):
        opts = self._opts(tmp_path)
        assert qo.NON_CLAUDE_DISALLOWED_SKILL not in opts.disallowed_tools
