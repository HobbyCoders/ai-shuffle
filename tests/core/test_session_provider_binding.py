"""Per-session provider binding (multi-provider phase-2 Chunk E, ruling G2-r).

A session's provider is bound at creation (``sessions.provider_id``, stamped
from the profile's configured provider) and is authoritative on every subsequent
turn/spawn: editing the profile later never reroutes a live session, and a NULL
binding stays native even if the profile now carries a provider.

Covers:
- binding is stamped at session creation from the profile's ``config.provider``
  (and stays NULL for a native profile);
- a turn spawn (``build_options_from_profile`` with a ``session_id``) resolves
  the gateway overlay from the SESSION binding, so editing the profile does NOT
  reroute the session;
- a NULL binding = native (no gateway overlay), even when the profile config now
  carries a provider — the pre-migration / native path;
- the generation path threads an explicit per-session binding through
  ``build_generation_options`` (binding wins over config.provider; explicit None
  keeps it native);
- (PA13 / W-R4) the composer's model override can REBIND the turn's provider: a
  ``prov:<pid>:<mid>`` composite beats the session binding, a native Claude pick
  CLEARS it, and an invalid composite fails loudly.
"""

from unittest.mock import patch

import pytest

import app.core.query_options as qo
from app.core import encryption
from app.db import database as db
from app.core.generation_options import build_generation_options

BASE_URL_KEY = "ANTHROPIC_BASE_URL"
AUTH_TOKEN_KEY = "ANTHROPIC_AUTH_TOKEN"
API_KEY = "ANTHROPIC_API_KEY"
DISABLE_1M_KEY = "CLAUDE_CODE_DISABLE_1M_CONTEXT"
AUTO_COMPACT_KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"


def _mk_provider(provider_id="op", *, token="gw-secret"):
    """Create an enabled provider row with an encrypted token (mirrors
    tests/core/test_provider_env.py::_mk_provider)."""
    return db.create_model_provider(
        provider_id=provider_id,
        name="OpenRouter",
        provider_type="openrouter",
        base_url="https://openrouter.ai/api",
        auth_token=encryption.encrypt_value(token),
        models=[{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
        enabled=True,
    )


def _mk_profile(profile_id, config):
    return db.create_profile(
        profile_id=profile_id,
        name=profile_id,
        description=None,
        config=config,
    )


def _build_options(profile, *, session_id=None, overrides=None):
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(
            profile=profile, session_id=session_id, overrides=overrides
        )
    return opts


# ---------------------------------------------------------------------------
# Binding is stamped at session creation
# ---------------------------------------------------------------------------

class TestBindingStampedAtCreation:
    def test_provider_profile_stamps_provider_id(self, mock_db):
        _mk_profile("prov", {"model": "openai/gpt-5.6", "provider": "op"})
        row = db.create_session("s-prov", profile_id="prov")
        assert row["provider_id"] == "op"
        # And the persisted read-back agrees.
        assert db.get_session("s-prov")["provider_id"] == "op"

    def test_native_profile_leaves_binding_null(self, mock_db):
        _mk_profile("nat", {"model": "opus"})
        row = db.create_session("s-nat", profile_id="nat")
        assert row["provider_id"] is None
        assert db.get_session("s-nat")["provider_id"] is None


# ---------------------------------------------------------------------------
# Turn spawn resolves the overlay from the SESSION binding
# ---------------------------------------------------------------------------

class TestSpawnResolvesFromBinding:
    def test_binding_drives_overlay_and_profile_edit_does_not_reroute(
        self, mock_db, tmp_path
    ):
        """Session bound to 'op' at creation. Even after the profile is edited to
        drop its provider, the spawn env still routes through 'op' — the binding,
        not the live profile, is authoritative."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        _mk_profile("prov", {"model": "openai/gpt-5.6", "provider": "op"})
        db.create_session("s-bound", profile_id="prov")  # stamps provider_id='op'

        # Simulate an EDITED profile: provider removed. Passing overrides.cwd
        # keeps the build off the worktree-materialization path; the binding is
        # fetched from the session row by id.
        edited_profile = {
            "id": "prov",
            "config": {"model": "opus", "cwd": str(tmp_path)},
        }
        env = _build_options(
            edited_profile,
            session_id="s-bound",
            overrides={"cwd": str(tmp_path)},
        ).env

        assert env[BASE_URL_KEY] == "https://openrouter.ai/api"  # binding won
        assert env[AUTH_TOKEN_KEY] == "gw-secret"
        assert env[API_KEY] == ""
        # Provider path pins the 1M gate (non-[1m] model → 200k guard on) but —
        # the provider row carries no context_window — leaves the auto-compact
        # window to the CLI (no hardcoded universal pin).
        assert env[DISABLE_1M_KEY] == "1"
        assert AUTO_COMPACT_KEY not in env

    def test_null_binding_is_native_even_if_profile_has_provider(
        self, mock_db, tmp_path
    ):
        """A session created native (NULL binding) — as every pre-migration
        session is — stays native even when the profile config now carries a
        provider. The binding's NULL wins over config.provider."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        _mk_profile("nat", {"model": "opus"})
        row = db.create_session("s-native", profile_id="nat")
        assert row["provider_id"] is None

        # Profile now (post-edit) carries a provider, but the session binding is
        # NULL → native path, no gateway overlay.
        profile_with_provider = {
            "id": "nat",
            "config": {
                "model": "openai/gpt-5.6",
                "provider": "op",
                "cwd": str(tmp_path),
            },
        }
        env = _build_options(
            profile_with_provider,
            session_id="s-native",
            overrides={"cwd": str(tmp_path)},
        ).env

        assert BASE_URL_KEY not in env
        assert AUTH_TOKEN_KEY not in env
        assert env[DISABLE_1M_KEY] == "1"  # native 1M guard applied

    def test_sessionless_build_still_uses_config_provider(self, mock_db, tmp_path):
        """Regression: with no session_id (e.g. tests / project-only builds) the
        profile's config.provider drives the overlay exactly as before."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        profile = {
            "id": "t",
            "config": {
                "model": "openai/gpt-5.6",
                "provider": "op",
                "cwd": str(tmp_path),
            },
        }
        env = _build_options(profile).env  # no session_id
        assert env[BASE_URL_KEY] == "https://openrouter.ai/api"
        assert env[API_KEY] == ""


# ---------------------------------------------------------------------------
# Generation path threads an explicit binding
# ---------------------------------------------------------------------------

class TestGenerationBinding:
    def test_bound_provider_wins_over_native_config(self, mock_db):
        """A native default profile, but the caller supplies a session binding to
        'op' → the overlay routes through 'op'."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        opts = build_generation_options({"model": "opus"}, session_provider_id="op")
        env = opts.env
        assert env[BASE_URL_KEY] == "https://openrouter.ai/api"
        assert env[AUTH_TOKEN_KEY] == "gw-secret"
        assert env[API_KEY] == ""
        # Provider path pins the 1M gate (non-[1m] model → 200k guard on); no
        # context_window on the row → auto-compact window left to the CLI.
        assert env[DISABLE_1M_KEY] == "1"
        assert AUTO_COMPACT_KEY not in env

    def test_explicit_none_binding_keeps_native(self, mock_db):
        """An explicit None binding keeps the call native even though the profile
        config carries a provider (the NULL-binding = native rule)."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        opts = build_generation_options(
            {"model": "openai/gpt-5.6", "provider": "op"}, session_provider_id=None
        )
        env = opts.env
        assert BASE_URL_KEY not in env
        assert env[DISABLE_1M_KEY] == "1"

    def test_default_uses_config_provider(self, mock_db):
        """Regression: omitting session_provider_id keeps today's behavior —
        config.provider drives the overlay."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        opts = build_generation_options({"model": "openai/gpt-5.6", "provider": "op"})
        env = opts.env
        assert env[BASE_URL_KEY] == "https://openrouter.ai/api"
        assert env[API_KEY] == ""


# ---------------------------------------------------------------------------
# PA13 / W-R4 — the composer's model override rebinds the provider
# ---------------------------------------------------------------------------


def _mk_anthropic_provider(provider_id="ap", *, token="ak-secret", models=None):
    """An Anthropic-API-key provider row (servable), curating claude-* ids."""
    return db.create_model_provider(
        provider_id=provider_id,
        name="Anthropic API",
        provider_type="anthropic",
        base_url="https://api.anthropic.com",
        auth_token=encryption.encrypt_value(token),
        models=models or [{"id": "claude-sonnet-4-5", "label": "Sonnet 4.5"}],
        enabled=True,
    )


class TestProviderModelCodec:
    """``decode_provider_model`` — the backend half of the shared codec."""

    def test_round_trips_a_plain_id(self):
        assert qo.decode_provider_model("prov:op:gpt-5.6") == ("op", "gpt-5.6")

    def test_model_id_may_contain_slashes_and_colons(self):
        """Split on the FIRST colon after the prefix only — OpenRouter slugs
        carry both (`meta-llama/llama-3.1-8b-instruct:free`)."""
        assert qo.decode_provider_model(
            "prov:op:meta-llama/llama-3.1-8b-instruct:free"
        ) == ("op", "meta-llama/llama-3.1-8b-instruct:free")

    def test_non_composites_decode_to_none(self):
        for value in (
            None,
            "",
            "opus",
            "claude-sonnet-4-5",
            "openai/gpt-5.6",
            "prov:",           # nothing after the prefix
            "prov:op",         # no separator
            "prov::gpt-5",     # empty provider id
            "prov:op:",        # empty model id
            123,
        ):
            assert qo.decode_provider_model(value) is None, value

    def test_is_native_claude_model(self):
        assert qo.is_native_claude_model("opus") is True
        assert qo.is_native_claude_model("sonnet[1m]") is True
        assert qo.is_native_claude_model("claude-sonnet-4-5") is True
        assert qo.is_native_claude_model("openai/gpt-5.6") is False
        assert qo.is_native_claude_model("gpt-5") is False
        assert qo.is_native_claude_model(None) is False


class TestCompositeOverrideRebindsProvider:
    def test_composite_beats_the_session_binding(self, mock_db, tmp_path):
        """Session bound to 'op' (OpenRouter). A `prov:ap:...` composite override
        rebinds the turn to 'ap' and uses the DECODED model id."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        _mk_anthropic_provider("ap", token="ak-secret")
        _mk_profile("prov", {"model": "openai/gpt-5.6", "provider": "op"})
        db.create_session("s-x", profile_id="prov")

        opts = _build_options(
            {"id": "prov", "config": {"model": "openai/gpt-5.6", "cwd": str(tmp_path)}},
            session_id="s-x",
            overrides={"cwd": str(tmp_path), "model": "prov:ap:claude-sonnet-4-5"},
        )

        assert opts.model == "claude-sonnet-4-5"
        assert opts.aih_provider_id == "ap"
        assert opts.aih_provider_type == "anthropic"
        assert opts.env[BASE_URL_KEY] == "https://api.anthropic.com"
        assert opts.env[AUTH_TOKEN_KEY] == "ak-secret"

    def test_composite_binds_a_native_session(self, mock_db, tmp_path):
        """No binding at all → a composite still sets the provider for the turn."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        _mk_profile("nat", {"model": "opus"})
        db.create_session("s-nat2", profile_id="nat")

        opts = _build_options(
            {"id": "nat", "config": {"model": "opus", "cwd": str(tmp_path)}},
            session_id="s-nat2",
            overrides={"cwd": str(tmp_path), "model": "prov:op:openai/gpt-5.6"},
        )
        assert opts.model == "openai/gpt-5.6"
        assert opts.aih_provider_id == "op"
        assert opts.env[BASE_URL_KEY] == "https://openrouter.ai/api"

    def test_openrouter_composite_is_accepted(self, mock_db, tmp_path):
        """OpenRouter rows fail ``is_provider_servable`` (that predicate gates
        cross-provider WORKER models), but a MAIN session may bind to any enabled
        provider — exactly as the profile editor has always allowed."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        _mk_profile("nat", {"model": "opus"})
        db.create_session("s-or", profile_id="nat")

        opts = _build_options(
            {"id": "nat", "config": {"model": "opus", "cwd": str(tmp_path)}},
            session_id="s-or",
            overrides={"cwd": str(tmp_path), "model": "prov:op:openai/gpt-5.6"},
        )
        assert opts.aih_provider_id == "op"


class TestNativePickClearsTheBinding:
    def test_claude_override_clears_a_provider_binding(self, mock_db, tmp_path):
        """The pre-PA13 `or`-chain could only SET a provider. A native Claude pick
        on a provider-bound session now resolves the turn back to native."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        _mk_profile("prov", {"model": "openai/gpt-5.6", "provider": "op"})
        db.create_session("s-clear", profile_id="prov")

        opts = _build_options(
            {"id": "prov", "config": {"model": "openai/gpt-5.6", "cwd": str(tmp_path)}},
            session_id="s-clear",
            overrides={"cwd": str(tmp_path), "model": "opus"},
        )
        assert opts.model == "opus"
        assert opts.aih_provider_id is None
        assert opts.aih_provider_type is None
        assert BASE_URL_KEY not in opts.env

    def test_plain_model_of_the_bound_provider_keeps_the_binding(
        self, mock_db, tmp_path
    ):
        """A bare provider model id (today's same-provider hot-swap) is untouched."""
        encryption.set_encryption_key("pw", db)
        _mk_provider(
            "op",
            token="gw-secret",
        )
        _mk_profile("prov", {"model": "openai/gpt-5.6", "provider": "op"})
        db.create_session("s-keep", profile_id="prov")

        opts = _build_options(
            {"id": "prov", "config": {"model": "openai/gpt-5.6", "cwd": str(tmp_path)}},
            session_id="s-keep",
            overrides={"cwd": str(tmp_path), "model": "openai/gpt-5.6"},
        )
        assert opts.aih_provider_id == "op"
        assert opts.env[BASE_URL_KEY] == "https://openrouter.ai/api"

    def test_claude_id_curated_by_the_bound_provider_keeps_the_binding(
        self, mock_db, tmp_path
    ):
        """An Anthropic-API provider row serves real ``claude-*`` ids. Picking one
        of ITS curated ids must NOT be read as "go native" — the curated-id
        exemption keeps the session on its own provider."""
        encryption.set_encryption_key("pw", db)
        _mk_anthropic_provider("ap", token="ak-secret")
        _mk_profile("apx", {"model": "claude-sonnet-4-5", "provider": "ap"})
        db.create_session("s-ap", profile_id="apx")

        opts = _build_options(
            {"id": "apx", "config": {"model": "claude-sonnet-4-5", "cwd": str(tmp_path)}},
            session_id="s-ap",
            overrides={"cwd": str(tmp_path), "model": "claude-sonnet-4-5"},
        )
        assert opts.aih_provider_id == "ap"

    def test_explicit_provider_override_is_never_cleared(self, mock_db, tmp_path):
        """Internal callers (ProviderAgent workers, generation) pin the provider
        explicitly — a model pick must not unbind them."""
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        _mk_profile("nat", {"model": "opus"})
        db.create_session("s-pin", profile_id="nat")

        opts = _build_options(
            {"id": "nat", "config": {"model": "opus", "cwd": str(tmp_path)}},
            session_id="s-pin",
            overrides={"cwd": str(tmp_path), "model": "opus", "provider": "op"},
        )
        assert opts.aih_provider_id == "op"


class TestInvalidCompositeIsRejected:
    """A bad composite raises ``ValueError`` — the channel both query engines
    already turn into a user-visible ``{"type": "error"}`` frame, instead of
    shipping a garbage model id upstream and 4xx-ing mid-turn."""

    def _build_with(self, tmp_path, model):
        return _build_options(
            {"id": "nat", "config": {"model": "opus", "cwd": str(tmp_path)}},
            overrides={"cwd": str(tmp_path), "model": model},
        )

    def test_unknown_provider(self, mock_db, tmp_path):
        encryption.set_encryption_key("pw", db)
        with pytest.raises(ValueError, match="does not exist"):
            self._build_with(tmp_path, "prov:nope:some-model")

    def test_uncurated_model(self, mock_db, tmp_path):
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        with pytest.raises(ValueError, match="curated"):
            self._build_with(tmp_path, "prov:op:not-a-curated-model")

    def test_disabled_provider(self, mock_db, tmp_path):
        encryption.set_encryption_key("pw", db)
        _mk_provider("op", token="gw-secret")
        db.update_model_provider("op", enabled=False)
        with pytest.raises(ValueError, match="disabled"):
            self._build_with(tmp_path, "prov:op:openai/gpt-5.6")
