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
  keeps it native).
"""

from unittest.mock import patch

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
        # Provider path now unconditionally pins the context window (parity with
        # the native apply_context_window_env): a non-[1m] model forces the 200k
        # guard on, and — the provider row carries no context_window — the
        # auto-compact trigger falls back to 200000.
        assert env[DISABLE_1M_KEY] == "1"
        assert env[AUTO_COMPACT_KEY] == "200000"

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
        # Provider path now pins the context window unconditionally: non-[1m]
        # model → 200k guard on, and no context_window on the row → 200000.
        assert env[DISABLE_1M_KEY] == "1"
        assert env[AUTO_COMPACT_KEY] == "200000"

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
