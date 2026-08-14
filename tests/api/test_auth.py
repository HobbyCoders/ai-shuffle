"""
Tests for authentication API endpoints.

These are placeholder tests that verify the basic structure works.
Full integration tests will require proper FastAPI testing setup.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAuthModuleImports:
    """Verify auth module can be imported correctly."""

    def test_auth_module_imports(self):
        """Auth module should import without errors."""
        from app.api import auth
        assert auth is not None

    def test_auth_router_exists(self):
        """Auth router should exist."""
        from app.api.auth import router
        assert router is not None


class TestSessionToken:
    """Test session token generation and validation."""

    def test_generate_session_token_format(self):
        """Session tokens should have correct format."""
        import secrets
        token = secrets.token_urlsafe(32)
        # Token should be a reasonable length
        assert len(token) >= 32

    def test_session_token_uniqueness(self):
        """Multiple tokens should be unique."""
        import secrets
        tokens = [secrets.token_urlsafe(32) for _ in range(100)]
        # All tokens should be unique
        assert len(set(tokens)) == 100


class TestPasswordHashing:
    """Test password hashing utilities."""

    def test_bcrypt_available(self):
        """bcrypt should be available for password hashing."""
        import bcrypt
        assert bcrypt is not None

    def test_password_hash_roundtrip(self):
        """Password hashing should be verifiable."""
        import bcrypt
        password = "test_password_123"
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
        assert bcrypt.checkpw(password.encode(), hashed)

    def test_different_passwords_different_hashes(self):
        """Different passwords should produce different hashes."""
        import bcrypt
        hash1 = bcrypt.hashpw(b"password1", bcrypt.gensalt())
        hash2 = bcrypt.hashpw(b"password2", bcrypt.gensalt())
        assert hash1 != hash2

    def test_same_password_verifies(self):
        """Same password should verify against its hash."""
        import bcrypt
        password = b"secure_password"
        hashed = bcrypt.hashpw(password, bcrypt.gensalt())
        assert bcrypt.checkpw(password, hashed)
        assert not bcrypt.checkpw(b"wrong_password", hashed)


# =============================================================================
# GET /auth/status — has_any_credential (PA10, docs/PROVIDER_AGNOSTIC_PLAN.md)
# =============================================================================

def _make_provider(provider_id: str, *, auth_token=None, enabled: bool = True):
    """Insert a ``model_providers`` row through the real db layer."""
    from app.db import database as db
    return db.create_model_provider(
        provider_id=provider_id,
        name=f"Provider {provider_id}",
        provider_type="openrouter",
        base_url="https://openrouter.ai/api",
        auth_token=auth_token,
        enabled=enabled,
    )


async def _auth_status(claude_authenticated: bool = False):
    """Call the status endpoint with the Claude/OAuth side stubbed out."""
    from app.api import auth as auth_api

    request = MagicMock()
    request.cookies = {}

    oauth = MagicMock()
    oauth.ensure_fresh_credentials = AsyncMock(return_value=None)

    with patch.object(auth_api, "auth_service") as mock_auth, \
         patch.object(auth_api, "direct_oauth", oauth), \
         patch.object(auth_api, "_sync_back_active_account_safe", MagicMock()):
        mock_auth.validate_session.return_value = False
        mock_auth.is_setup_required.return_value = False
        mock_auth.is_claude_authenticated.return_value = claude_authenticated
        mock_auth.get_claude_token_status.return_value = {"token_valid": claude_authenticated}
        mock_auth.is_github_authenticated.return_value = False
        mock_auth.is_onboarding_complete.return_value = True
        return await auth_api.get_auth_status(request)


class TestAuthStatusAnyCredential:
    """``has_any_credential`` = claude subscription OR a configured provider."""

    @pytest.mark.asyncio
    async def test_false_when_nothing_configured(self, mock_db):
        status = await _auth_status(claude_authenticated=False)
        assert status["claude_authenticated"] is False
        assert status["has_any_credential"] is False

    @pytest.mark.asyncio
    async def test_true_via_provider_row(self, mock_db):
        _make_provider("prov-with-token", auth_token="enc:whatever")
        status = await _auth_status(claude_authenticated=False)
        # Claude is still not connected — the provider credential alone unlocks.
        assert status["claude_authenticated"] is False
        assert status["has_any_credential"] is True

    @pytest.mark.asyncio
    async def test_provider_without_credential_does_not_count(self, mock_db):
        _make_provider("prov-no-token", auth_token=None)
        status = await _auth_status(claude_authenticated=False)
        assert status["has_any_credential"] is False

    @pytest.mark.asyncio
    async def test_disabled_provider_does_not_count(self, mock_db):
        _make_provider("prov-disabled", auth_token="enc:whatever", enabled=False)
        status = await _auth_status(claude_authenticated=False)
        assert status["has_any_credential"] is False

    @pytest.mark.asyncio
    async def test_true_via_claude_auth_without_providers(self, mock_db):
        status = await _auth_status(claude_authenticated=True)
        assert status["has_any_credential"] is True

    @pytest.mark.asyncio
    async def test_probe_failure_degrades_to_false(self, mock_db):
        from app.db import database as db
        with patch.object(
            db, "count_configured_model_providers", side_effect=RuntimeError("pool down")
        ):
            status = await _auth_status(claude_authenticated=False)
        assert status["has_any_credential"] is False


class TestCountConfiguredModelProviders:
    """The db-layer helper backing the status field."""

    def test_counts_only_enabled_rows_with_a_credential(self, mock_db):
        from app.db import database as db
        _make_provider("a", auth_token="enc:one")
        _make_provider("b", auth_token=None)
        _make_provider("c", auth_token="enc:three", enabled=False)
        _make_provider("d", auth_token="")
        assert db.count_configured_model_providers() == 1

    def test_codex_access_token_counts(self, mock_db):
        from app.db import database as db
        _make_provider("codex", auth_token=None)
        db.update_model_provider("codex", codex_access_token="enc:codex")
        assert db.count_configured_model_providers() == 1
