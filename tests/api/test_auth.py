"""
Tests for authentication API endpoints.

These are placeholder tests that verify the basic structure works.
Full integration tests will require proper FastAPI testing setup.
"""

from datetime import datetime, timedelta, timezone
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


async def _auth_status(claude_authenticated: bool = False, session_token: str | None = None):
    """Call the status endpoint with the Claude/OAuth side stubbed out."""
    from app.api import auth as auth_api

    request = MagicMock()
    request.cookies = {"session": session_token} if session_token else {}

    oauth = MagicMock()
    oauth.ensure_fresh_credentials = AsyncMock(return_value=None)

    # The gh signal is behind a process-wide TTL cache; start from a clean
    # slate so this call's own stub is what answers.
    auth_api.reset_github_auth_cache()

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

    @pytest.mark.asyncio
    async def test_paired_controller_is_authenticated_but_not_admin(self, mock_db):
        from app.core.pairing import hash_controller_session
        from app.db import database

        database.create_pairing_grant(
            token_hash="grant-hash-auth-test",
            purpose="controller",
            scopes=["controller"],
            target_install_id="runner",
            endpoint_url=None,
            device_label=None,
            created_by="test",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        raw_session = "shc_paired-auth-test"
        database.exchange_grant_for_client(
            token_hash="grant-hash-auth-test",
            device_id="paired-auth-device",
            device_name="Paired Tablet",
            platform="web",
            client_type="browser",
            session_token_hash=hash_controller_session(raw_session),
            session_expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )

        status = await _auth_status(session_token=raw_session)
        assert status["authenticated"] is True
        assert status["is_admin"] is False
        assert status["paired_client"] is True
        assert status["username"] == "Paired Tablet"

    @pytest.mark.asyncio
    async def test_paired_logout_invalidates_controller_session(self, mock_db):
        from fastapi import Response
        from app.api import auth as auth_api
        from app.core.pairing import hash_controller_session
        from app.db import database

        database.create_pairing_grant(
            token_hash="grant-hash-logout-test",
            purpose="controller",
            scopes=["controller"],
            target_install_id="runner",
            endpoint_url=None,
            device_label=None,
            created_by="test",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        raw_session = "shc_paired-logout-test"
        database.exchange_grant_for_client(
            token_hash="grant-hash-logout-test",
            device_id="paired-logout-device",
            device_name="Paired Phone",
            platform="web",
            client_type="browser",
            session_token_hash=hash_controller_session(raw_session),
            session_expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        request = MagicMock()
        request.cookies = {"session": raw_session}

        await auth_api.logout(request, Response())

        assert database.get_paired_client_by_session_hash(
            hash_controller_session(raw_session)
        ) is None


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


# =============================================================================
# GitHub auth status: caching + invalidation (finding G1)
# =============================================================================

class TestGithubAuthStatusCaching:
    """``gh auth status`` is a 10s-timeout subprocess and ``/auth/status`` is
    polled. It must run off the event loop, once per TTL window — and an
    explicit login/logout must invalidate the cache rather than wait it out."""

    @pytest.fixture(autouse=True)
    def _clean_caches(self):
        from app.core.auth import reset_github_auth_cache
        from app.core.github_service import github_service

        reset_github_auth_cache()
        github_service.invalidate_auth_cache()
        yield
        reset_github_auth_cache()
        github_service.invalidate_auth_cache()

    @pytest.mark.asyncio
    async def test_status_probes_once_across_repeated_polls(self, mock_db):
        calls = []

        async def _poll():
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
                mock_auth.is_claude_authenticated.return_value = False
                mock_auth.get_claude_token_status.return_value = {"token_valid": False}
                mock_auth.is_onboarding_complete.return_value = True
                mock_auth.is_github_authenticated.side_effect = lambda: (
                    calls.append(1) or True
                )
                return await auth_api.get_auth_status(request)

        from app.core.auth import reset_github_auth_cache

        reset_github_auth_cache()
        first = await _poll()
        second = await _poll()

        assert first["github_authenticated"] is True
        assert second["github_authenticated"] is True
        assert len(calls) == 1, "gh auth status re-run on a cached poll"

    @pytest.mark.asyncio
    async def test_status_runs_the_probe_off_the_event_loop(self, mock_db):
        import threading

        from app.api import auth as auth_api
        from app.core.auth import reset_github_auth_cache

        loop_thread = threading.current_thread().ident
        seen = {}

        def _probe():
            seen["thread"] = threading.current_thread().ident
            return True

        request = MagicMock()
        request.cookies = {}
        oauth = MagicMock()
        oauth.ensure_fresh_credentials = AsyncMock(return_value=None)

        reset_github_auth_cache()
        with patch.object(auth_api, "auth_service") as mock_auth, \
             patch.object(auth_api, "direct_oauth", oauth), \
             patch.object(auth_api, "_sync_back_active_account_safe", MagicMock()):
            mock_auth.validate_session.return_value = False
            mock_auth.is_setup_required.return_value = False
            mock_auth.is_claude_authenticated.return_value = False
            mock_auth.get_claude_token_status.return_value = {"token_valid": False}
            mock_auth.is_onboarding_complete.return_value = True
            mock_auth.is_github_authenticated.side_effect = _probe

            await auth_api.get_auth_status(request)

        assert seen["thread"] != loop_thread

    @pytest.mark.asyncio
    async def test_login_invalidates_both_caches(self):
        from app.api import auth as auth_api
        from app.core import auth as core_auth
        from app.core.github_service import github_service

        # Pretend both caches believe GitHub is signed out.
        core_auth.set_github_auth_cached(False)
        github_service.invalidate_auth_cache(False)

        request = MagicMock()
        request.json = AsyncMock(return_value={"token": "ghp_x"})

        with patch.object(auth_api, "auth_service") as mock_auth:
            mock_auth.github_login_with_token.return_value = {"success": True}
            result = await auth_api.github_login(request, token="admin")

        assert result["success"] is True
        # Both caches now reflect the login without another subprocess.
        assert core_auth._github_auth_cache[0] is True
        assert github_service._auth_cache.get(github_service._AUTH_CACHE_KEY) is True

    @pytest.mark.asyncio
    async def test_logout_invalidates_both_caches(self):
        from app.api import auth as auth_api
        from app.core import auth as core_auth
        from app.core.github_service import github_service

        core_auth.set_github_auth_cached(True)
        github_service.invalidate_auth_cache(True)

        with patch.object(auth_api, "auth_service") as mock_auth:
            mock_auth.github_logout.return_value = {"success": True}
            result = await auth_api.github_logout(token="admin")

        assert result["success"] is True
        assert core_auth._github_auth_cache[0] is False
        assert github_service._auth_cache.get(github_service._AUTH_CACHE_KEY) is False

    @pytest.mark.asyncio
    async def test_github_status_endpoint_seeds_the_caches(self):
        """It is the device-flow completion poll, so its answer is the freshest
        one available — and it must not block the loop while it asks."""
        import threading

        from app.api import auth as auth_api
        from app.core import auth as core_auth
        from app.core.github_service import github_service

        core_auth.set_github_auth_cached(False)
        github_service.invalidate_auth_cache(False)

        loop_thread = threading.current_thread().ident
        seen = {}

        def _info():
            seen["thread"] = threading.current_thread().ident
            return {"authenticated": True, "user": "octocat"}

        with patch.object(auth_api, "auth_service") as mock_auth:
            mock_auth.get_github_auth_info.side_effect = _info
            result = await auth_api.github_auth_status()

        assert result["user"] == "octocat"
        assert seen["thread"] != loop_thread
        assert core_auth._github_auth_cache[0] is True
        assert github_service._auth_cache.get(github_service._AUTH_CACHE_KEY) is True

    @pytest.mark.asyncio
    async def test_web_login_drops_the_cached_answer(self):
        """The device flow finishes out-of-band: nothing is known yet, so the
        cache is cleared rather than seeded."""
        from app.api import auth as auth_api
        from app.core import auth as core_auth
        from app.core.github_service import github_service

        core_auth.set_github_auth_cached(False)
        github_service.invalidate_auth_cache(False)

        request = MagicMock()
        with patch.object(auth_api, "auth_service") as mock_auth:
            mock_auth.github_login_web = AsyncMock(return_value={"code": "ABCD-1234"})
            result = await auth_api.github_login_web(request, token="admin")

        assert result["code"] == "ABCD-1234"
        assert core_auth._github_auth_cache is None
        assert (
            github_service._auth_cache.get_or_miss(github_service._AUTH_CACHE_KEY)
            is not False
        )

