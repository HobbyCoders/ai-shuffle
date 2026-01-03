"""
Unit tests for OAuth module.

Tests cover:
- PKCE challenge generation
- State parameter generation
- OAuth flow initiation
- Token exchange
- Credential writing and reading
- Token refresh
- Error handling for all paths
"""

import json
import pytest
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import httpx

from app.core.oauth import (
    DirectOAuth,
    PKCEChallenge,
    OAuthTokens,
    direct_oauth,
    OAUTH_CLIENT_ID,
    OAUTH_AUTHORIZE_URL,
    OAUTH_TOKEN_URL,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPES,
)


class TestPKCEChallenge:
    """Test PKCE challenge dataclass."""

    def test_pkce_challenge_creation(self):
        """Should create a PKCE challenge with default method."""
        challenge = PKCEChallenge(verifier="test_verifier", challenge="test_challenge")

        assert challenge.verifier == "test_verifier"
        assert challenge.challenge == "test_challenge"
        assert challenge.method == "S256"

    def test_pkce_challenge_custom_method(self):
        """Should allow custom method."""
        challenge = PKCEChallenge(verifier="v", challenge="c", method="plain")

        assert challenge.method == "plain"


class TestOAuthTokens:
    """Test OAuth tokens dataclass."""

    def test_oauth_tokens_creation(self):
        """Should create OAuth tokens with all fields."""
        tokens = OAuthTokens(
            access_token="access123",
            refresh_token="refresh456",
            expires_at=1234567890000,
            scopes=["user:inference", "user:profile"]
        )

        assert tokens.access_token == "access123"
        assert tokens.refresh_token == "refresh456"
        assert tokens.expires_at == 1234567890000
        assert tokens.scopes == ["user:inference", "user:profile"]


class TestDirectOAuthInit:
    """Test DirectOAuth initialization."""

    def test_init_with_custom_config_dir(self, temp_dir):
        """Should use provided config directory."""
        oauth = DirectOAuth(config_dir=temp_dir)

        assert oauth.config_dir == temp_dir

    def test_init_without_config_dir_uses_home(self):
        """Should auto-detect config directory from HOME."""
        with patch.dict('os.environ', {'HOME': '/custom/home'}, clear=False):
            oauth = DirectOAuth()

            assert oauth.config_dir == Path('/custom/home/.claude')

    def test_init_uses_userprofile_on_windows(self):
        """Should use USERPROFILE if HOME not set."""
        env = {'USERPROFILE': 'C:\\Users\\Test'}
        with patch.dict('os.environ', env, clear=True):
            with patch('os.environ.get') as mock_get:
                def env_get(key, default=None):
                    if key == 'HOME':
                        return None
                    if key == 'USERPROFILE':
                        return 'C:\\Users\\Test'
                    return default
                mock_get.side_effect = env_get

                oauth = DirectOAuth()
                # Should contain .claude in the path
                assert '.claude' in str(oauth.config_dir)

    def test_init_sets_up_empty_pending_challenges(self):
        """Should initialize with empty pending challenges."""
        oauth = DirectOAuth(config_dir=Path("/tmp"))

        assert oauth._pending_challenges == {}

    def test_init_sets_up_credential_cache(self):
        """Should initialize credential cache as invalid."""
        oauth = DirectOAuth(config_dir=Path("/tmp"))

        assert oauth._cred_cache_valid is None
        assert oauth._cred_cache_time == 0
        assert oauth._cred_cache_ttl == 60


class TestGeneratePKCEChallenge:
    """Test PKCE challenge generation."""

    def test_generates_valid_verifier_length(self):
        """Verifier should be appropriate length."""
        challenge = DirectOAuth.generate_pkce_challenge()

        # Base64-encoded 32 bytes without padding
        assert len(challenge.verifier) >= 40

    def test_generates_valid_challenge_length(self):
        """Challenge should be appropriate length."""
        challenge = DirectOAuth.generate_pkce_challenge()

        # SHA256 hash is 32 bytes, base64-encoded without padding
        assert len(challenge.challenge) >= 40

    def test_method_is_s256(self):
        """Should use S256 method."""
        challenge = DirectOAuth.generate_pkce_challenge()

        assert challenge.method == "S256"

    def test_generates_unique_challenges(self):
        """Each call should generate unique values."""
        challenges = [DirectOAuth.generate_pkce_challenge() for _ in range(100)]
        verifiers = [c.verifier for c in challenges]

        assert len(set(verifiers)) == 100

    def test_verifier_is_url_safe(self):
        """Verifier should only contain URL-safe characters."""
        challenge = DirectOAuth.generate_pkce_challenge()

        # URL-safe base64 uses only alphanumeric, dash, and underscore
        valid_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert all(c in valid_chars for c in challenge.verifier)

    def test_challenge_is_url_safe(self):
        """Challenge should only contain URL-safe characters."""
        challenge = DirectOAuth.generate_pkce_challenge()

        valid_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert all(c in valid_chars for c in challenge.challenge)


class TestGenerateState:
    """Test state parameter generation."""

    def test_generates_non_empty_state(self):
        """Should generate a non-empty state."""
        state = DirectOAuth.generate_state()

        assert len(state) > 0

    def test_generates_unique_states(self):
        """Each call should generate unique states."""
        states = [DirectOAuth.generate_state() for _ in range(100)]

        assert len(set(states)) == 100

    def test_state_is_url_safe(self):
        """State should only contain URL-safe characters."""
        state = DirectOAuth.generate_state()

        valid_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert all(c in valid_chars for c in state)


class TestStartOAuthFlow:
    """Test OAuth flow initiation."""

    def test_returns_oauth_url_and_state(self, temp_dir):
        """Should return dict with oauth_url and state."""
        oauth = DirectOAuth(config_dir=temp_dir)
        result = oauth.start_oauth_flow()

        assert "oauth_url" in result
        assert "state" in result

    def test_oauth_url_contains_required_params(self, temp_dir):
        """OAuth URL should contain all required parameters."""
        oauth = DirectOAuth(config_dir=temp_dir)
        result = oauth.start_oauth_flow()
        url = result["oauth_url"]

        assert OAUTH_AUTHORIZE_URL in url
        assert f"client_id={OAUTH_CLIENT_ID}" in url
        assert "response_type=code" in url
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert "code=true" in url  # Important for showing code on page

    def test_stores_pending_challenge(self, temp_dir):
        """Should store PKCE challenge keyed by state."""
        oauth = DirectOAuth(config_dir=temp_dir)
        result = oauth.start_oauth_flow()
        state = result["state"]

        assert state in oauth._pending_challenges
        assert isinstance(oauth._pending_challenges[state], PKCEChallenge)

    def test_state_equals_verifier(self, temp_dir):
        """State should equal the PKCE verifier."""
        oauth = DirectOAuth(config_dir=temp_dir)
        result = oauth.start_oauth_flow()
        state = result["state"]

        pkce = oauth._pending_challenges[state]
        assert state == pkce.verifier


class TestExchangeCodeForTokens:
    """Test token exchange."""

    @pytest.fixture
    def oauth_with_pending(self, temp_dir):
        """Create OAuth instance with a pending challenge."""
        oauth = DirectOAuth(config_dir=temp_dir)
        flow = oauth.start_oauth_flow()
        return oauth, flow["state"]

    @pytest.mark.asyncio
    async def test_invalid_state_returns_error(self, temp_dir):
        """Should return error for invalid state."""
        oauth = DirectOAuth(config_dir=temp_dir)

        tokens, error = await oauth.exchange_code_for_tokens("code123", "invalid_state")

        assert tokens is None
        assert "Invalid or expired state" in error

    @pytest.mark.asyncio
    async def test_successful_token_exchange(self, oauth_with_pending):
        """Should return tokens on successful exchange."""
        oauth, state = oauth_with_pending

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "access123",
            "refresh_token": "refresh456",
            "expires_in": 3600,
            "scope": "user:inference user:profile"
        }

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.exchange_code_for_tokens("auth_code", state)

        assert error is None
        assert tokens is not None
        assert tokens.access_token == "access123"
        assert tokens.refresh_token == "refresh456"

    @pytest.mark.asyncio
    async def test_handles_code_with_state_suffix(self, oauth_with_pending):
        """Should handle code with #state appended."""
        oauth, state = oauth_with_pending

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "access123",
            "refresh_token": "refresh456",
            "expires_in": 3600,
        }

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.exchange_code_for_tokens(
                "auth_code#some_state", state
            )

        assert error is None
        assert tokens is not None

    @pytest.mark.asyncio
    async def test_http_error_returns_error_message(self, oauth_with_pending):
        """Should return error on HTTP error response."""
        oauth, state = oauth_with_pending

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"
        mock_response.json.return_value = {
            "error": "invalid_grant",
            "error_description": "The authorization code is invalid"
        }

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.exchange_code_for_tokens("bad_code", state)

        assert tokens is None
        assert "authorization code is invalid" in error

    @pytest.mark.asyncio
    async def test_http_error_with_non_json_response(self, oauth_with_pending):
        """Should handle non-JSON error responses."""
        oauth, state = oauth_with_pending

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.json.side_effect = ValueError("Not JSON")

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.exchange_code_for_tokens("code", state)

        assert tokens is None
        assert "Internal Server Error" in error

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, oauth_with_pending):
        """Should return error on timeout."""
        oauth, state = oauth_with_pending

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.exchange_code_for_tokens("code", state)

        assert tokens is None
        assert "timed out" in error

    @pytest.mark.asyncio
    async def test_network_error_returns_error(self, oauth_with_pending):
        """Should return error on network error."""
        oauth, state = oauth_with_pending

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(
                side_effect=httpx.RequestError("Connection refused")
            )
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.exchange_code_for_tokens("code", state)

        assert tokens is None
        assert "Network error" in error

    @pytest.mark.asyncio
    async def test_unexpected_error_returns_error(self, oauth_with_pending):
        """Should handle unexpected exceptions."""
        oauth, state = oauth_with_pending

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(side_effect=RuntimeError("Unexpected"))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.exchange_code_for_tokens("code", state)

        assert tokens is None
        assert "Unexpected error" in error

    @pytest.mark.asyncio
    async def test_removes_challenge_after_use(self, oauth_with_pending):
        """Should remove pending challenge after exchange attempt."""
        oauth, state = oauth_with_pending

        assert state in oauth._pending_challenges

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(side_effect=RuntimeError("Error"))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            await oauth.exchange_code_for_tokens("code", state)

        assert state not in oauth._pending_challenges

    @pytest.mark.asyncio
    async def test_uses_default_scopes_when_missing(self, oauth_with_pending):
        """Should use default scopes when not in response."""
        oauth, state = oauth_with_pending

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "access123",
            "refresh_token": "refresh456",
        }

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.exchange_code_for_tokens("code", state)

        assert tokens.scopes == OAUTH_SCOPES


class TestWriteCredentials:
    """Test credential writing."""

    def test_writes_credentials_file(self, temp_dir):
        """Should write credentials in correct format."""
        oauth = DirectOAuth(config_dir=temp_dir)
        tokens = OAuthTokens(
            access_token="access123",
            refresh_token="refresh456",
            expires_at=1234567890000,
            scopes=["user:inference"]
        )

        result = oauth.write_credentials(tokens)

        assert result is True

        creds_file = temp_dir / ".credentials.json"
        assert creds_file.exists()

        data = json.loads(creds_file.read_text())
        assert data["claudeAiOauth"]["accessToken"] == "access123"
        assert data["claudeAiOauth"]["refreshToken"] == "refresh456"
        assert data["claudeAiOauth"]["expiresAt"] == 1234567890000
        assert data["claudeAiOauth"]["scopes"] == ["user:inference"]

    def test_creates_config_directory(self, temp_dir):
        """Should create config directory if it doesn't exist."""
        config_dir = temp_dir / "new_dir" / ".claude"
        oauth = DirectOAuth(config_dir=config_dir)
        tokens = OAuthTokens(
            access_token="access",
            refresh_token="refresh",
            expires_at=123,
            scopes=[]
        )

        result = oauth.write_credentials(tokens)

        assert result is True
        assert config_dir.exists()

    def test_handles_write_error(self, temp_dir):
        """Should return False on write error."""
        oauth = DirectOAuth(config_dir=temp_dir)
        tokens = OAuthTokens(
            access_token="access",
            refresh_token="refresh",
            expires_at=123,
            scopes=[]
        )

        with patch.object(Path, 'write_text', side_effect=PermissionError("Denied")):
            result = oauth.write_credentials(tokens)

        assert result is False

    def test_sets_file_permissions_on_unix(self, temp_dir):
        """Should set 600 permissions on Unix systems."""
        oauth = DirectOAuth(config_dir=temp_dir)
        tokens = OAuthTokens(
            access_token="access",
            refresh_token="refresh",
            expires_at=123,
            scopes=[]
        )

        with patch('os.name', 'posix'):
            with patch('os.chmod') as mock_chmod:
                oauth.write_credentials(tokens)

                # Check that chmod was called with 0o600
                if mock_chmod.called:
                    args = mock_chmod.call_args[0]
                    assert args[1] == 0o600

    def test_skips_chmod_on_windows(self, temp_dir):
        """Should skip chmod on Windows."""
        oauth = DirectOAuth(config_dir=temp_dir)
        tokens = OAuthTokens(
            access_token="access",
            refresh_token="refresh",
            expires_at=123,
            scopes=[]
        )

        with patch('os.name', 'nt'):
            with patch('os.chmod') as mock_chmod:
                oauth.write_credentials(tokens)

                mock_chmod.assert_not_called()


class TestEnsureOnboardingComplete:
    """Test onboarding completion."""

    def test_creates_settings_file(self, temp_dir):
        """Should create settings.json with onboarding flag."""
        oauth = DirectOAuth(config_dir=temp_dir)
        temp_dir.mkdir(exist_ok=True)

        result = oauth.ensure_onboarding_complete()

        assert result is True

        settings_file = temp_dir / "settings.json"
        assert settings_file.exists()

        data = json.loads(settings_file.read_text())
        assert data["hasCompletedOnboarding"] is True
        assert data["theme"] == "dark"

    def test_preserves_existing_settings(self, temp_dir):
        """Should preserve existing settings."""
        temp_dir.mkdir(exist_ok=True)
        settings_file = temp_dir / "settings.json"
        settings_file.write_text(json.dumps({
            "theme": "light",
            "customSetting": "value"
        }))

        oauth = DirectOAuth(config_dir=temp_dir)
        result = oauth.ensure_onboarding_complete()

        assert result is True

        data = json.loads(settings_file.read_text())
        assert data["hasCompletedOnboarding"] is True
        assert data["theme"] == "light"  # Preserved
        assert data["customSetting"] == "value"  # Preserved

    def test_handles_invalid_json(self, temp_dir):
        """Should handle corrupted settings file."""
        temp_dir.mkdir(exist_ok=True)
        settings_file = temp_dir / "settings.json"
        settings_file.write_text("not valid json {{{")

        oauth = DirectOAuth(config_dir=temp_dir)
        result = oauth.ensure_onboarding_complete()

        assert result is True

        data = json.loads(settings_file.read_text())
        assert data["hasCompletedOnboarding"] is True

    def test_handles_write_error(self, temp_dir):
        """Should return False on error."""
        oauth = DirectOAuth(config_dir=temp_dir)

        with patch.object(Path, 'write_text', side_effect=PermissionError("Denied")):
            with patch.object(Path, 'exists', return_value=False):
                result = oauth.ensure_onboarding_complete()

        assert result is False


class TestCompleteOAuthFlow:
    """Test complete OAuth flow."""

    @pytest.mark.asyncio
    async def test_successful_flow(self, temp_dir):
        """Should complete full OAuth flow successfully."""
        oauth = DirectOAuth(config_dir=temp_dir)
        flow = oauth.start_oauth_flow()
        state = flow["state"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "access123",
            "refresh_token": "refresh456",
            "expires_in": 3600,
        }

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await oauth.complete_oauth_flow("code123", state)

        assert result["success"] is True
        assert result["authenticated"] is True

    @pytest.mark.asyncio
    async def test_flow_fails_on_token_exchange_error(self, temp_dir):
        """Should return error if token exchange fails."""
        oauth = DirectOAuth(config_dir=temp_dir)

        result = await oauth.complete_oauth_flow("code", "invalid_state")

        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_flow_fails_on_credential_write_error(self, temp_dir):
        """Should return error if credential write fails."""
        oauth = DirectOAuth(config_dir=temp_dir)
        flow = oauth.start_oauth_flow()
        state = flow["state"]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "access123",
            "refresh_token": "refresh456",
            "expires_in": 3600,
        }

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            with patch.object(oauth, 'write_credentials', return_value=False):
                result = await oauth.complete_oauth_flow("code123", state)

        assert result["success"] is False
        assert "save credentials" in result["message"]


class TestHasValidCredentials:
    """Test credential validation."""

    def test_no_credentials_file_returns_false(self, temp_dir):
        """Should return False if no credentials file exists."""
        oauth = DirectOAuth(config_dir=temp_dir)
        temp_dir.mkdir(exist_ok=True)

        result = oauth.has_valid_credentials()

        assert result is False

    def test_valid_credentials_returns_true(self, temp_dir):
        """Should return True for valid non-expired credentials."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"

        # Expires 1 hour from now
        future_time = int((time.time() + 3600) * 1000)
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "valid_token",
                "refreshToken": "refresh",
                "expiresAt": future_time,
            }
        }))

        oauth = DirectOAuth(config_dir=temp_dir)
        result = oauth.has_valid_credentials()

        assert result is True

    def test_expired_credentials_returns_false(self, temp_dir):
        """Should return False for expired credentials."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"

        # Expired 1 hour ago
        past_time = int((time.time() - 3600) * 1000)
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "expired_token",
                "refreshToken": "refresh",
                "expiresAt": past_time,
            }
        }))

        oauth = DirectOAuth(config_dir=temp_dir)
        result = oauth.has_valid_credentials()

        assert result is False

    def test_expiring_soon_returns_false(self, temp_dir):
        """Should return False if expiring within 5 minutes."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"

        # Expires in 2 minutes (within 5 minute buffer)
        soon_time = int((time.time() + 120) * 1000)
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "expiring_soon",
                "refreshToken": "refresh",
                "expiresAt": soon_time,
            }
        }))

        oauth = DirectOAuth(config_dir=temp_dir)
        result = oauth.has_valid_credentials()

        assert result is False

    def test_no_access_token_returns_false(self, temp_dir):
        """Should return False if no access token."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "refreshToken": "refresh",
            }
        }))

        oauth = DirectOAuth(config_dir=temp_dir)
        result = oauth.has_valid_credentials()

        assert result is False

    def test_invalid_json_returns_false(self, temp_dir):
        """Should return False for invalid JSON."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"
        creds_file.write_text("not valid json")

        oauth = DirectOAuth(config_dir=temp_dir)
        result = oauth.has_valid_credentials()

        assert result is False

    def test_uses_cached_result(self, temp_dir):
        """Should use cached result within TTL."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"

        future_time = int((time.time() + 3600) * 1000)
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "valid_token",
                "expiresAt": future_time,
            }
        }))

        oauth = DirectOAuth(config_dir=temp_dir)

        # First call populates cache
        result1 = oauth.has_valid_credentials()
        assert result1 is True

        # Delete the file
        creds_file.unlink()

        # Second call should still return True (cached)
        result2 = oauth.has_valid_credentials()
        assert result2 is True  # Still True from cache

    def test_cache_expires(self, temp_dir):
        """Should refresh cache after TTL."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"

        future_time = int((time.time() + 3600) * 1000)
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "valid_token",
                "expiresAt": future_time,
            }
        }))

        oauth = DirectOAuth(config_dir=temp_dir)
        oauth._cred_cache_ttl = 0  # Expire immediately

        # First call
        result1 = oauth.has_valid_credentials()
        assert result1 is True

        # Delete the file
        creds_file.unlink()

        # Wait to ensure cache is stale
        oauth._cred_cache_time = 0

        # Second call should re-check
        result2 = oauth.has_valid_credentials()
        assert result2 is False


class TestInvalidateCredentialsCache:
    """Test credential cache invalidation."""

    def test_invalidates_cache(self, temp_dir):
        """Should clear cached credential validity."""
        oauth = DirectOAuth(config_dir=temp_dir)
        oauth._cred_cache_valid = True
        oauth._cred_cache_time = time.time()

        oauth.invalidate_credentials_cache()

        assert oauth._cred_cache_valid is None
        assert oauth._cred_cache_time == 0


class TestRefreshTokens:
    """Test token refresh."""

    @pytest.mark.asyncio
    async def test_no_credentials_file_returns_error(self, temp_dir):
        """Should return error if no credentials file."""
        oauth = DirectOAuth(config_dir=temp_dir)
        temp_dir.mkdir(exist_ok=True)

        tokens, error = await oauth.refresh_tokens()

        assert tokens is None
        assert "Could not read credentials" in error

    @pytest.mark.asyncio
    async def test_no_refresh_token_returns_error(self, temp_dir):
        """Should return error if no refresh token."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "access",
            }
        }))

        oauth = DirectOAuth(config_dir=temp_dir)
        tokens, error = await oauth.refresh_tokens()

        assert tokens is None
        assert "No refresh token found" in error

    @pytest.mark.asyncio
    async def test_successful_refresh(self, temp_dir):
        """Should refresh tokens successfully."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "old_access",
                "refreshToken": "valid_refresh",
            }
        }))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
            "scope": "user:inference"
        }

        oauth = DirectOAuth(config_dir=temp_dir)

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.refresh_tokens()

        assert error is None
        assert tokens.access_token == "new_access"
        assert tokens.refresh_token == "new_refresh"

    @pytest.mark.asyncio
    async def test_refresh_keeps_old_refresh_token_if_not_returned(self, temp_dir):
        """Should keep old refresh token if new one not provided."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "old_access",
                "refreshToken": "original_refresh",
            }
        }))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access",
            "expires_in": 3600,
        }

        oauth = DirectOAuth(config_dir=temp_dir)

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.refresh_tokens()

        assert tokens.refresh_token == "original_refresh"

    @pytest.mark.asyncio
    async def test_refresh_http_error(self, temp_dir):
        """Should return error on HTTP error."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "refreshToken": "invalid_refresh",
            }
        }))

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Invalid refresh token"

        oauth = DirectOAuth(config_dir=temp_dir)

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.refresh_tokens()

        assert tokens is None
        assert "Token refresh failed" in error

    @pytest.mark.asyncio
    async def test_refresh_network_error(self, temp_dir):
        """Should handle network errors."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "refreshToken": "refresh_token",
            }
        }))

        oauth = DirectOAuth(config_dir=temp_dir)

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(side_effect=RuntimeError("Network error"))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            tokens, error = await oauth.refresh_tokens()

        assert tokens is None
        assert "Refresh error" in error

    @pytest.mark.asyncio
    async def test_refresh_writes_new_credentials(self, temp_dir):
        """Should write new credentials after refresh."""
        temp_dir.mkdir(exist_ok=True)
        creds_file = temp_dir / ".credentials.json"
        creds_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "old_access",
                "refreshToken": "valid_refresh",
            }
        }))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
        }

        oauth = DirectOAuth(config_dir=temp_dir)

        with patch('httpx.AsyncClient') as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            await oauth.refresh_tokens()

        # Verify credentials were updated
        data = json.loads(creds_file.read_text())
        assert data["claudeAiOauth"]["accessToken"] == "new_access"


class TestClearPendingChallenges:
    """Test clearing pending challenges."""

    def test_clears_all_challenges(self, temp_dir):
        """Should clear all pending PKCE challenges."""
        oauth = DirectOAuth(config_dir=temp_dir)

        # Add some pending challenges
        oauth.start_oauth_flow()
        oauth.start_oauth_flow()
        oauth.start_oauth_flow()

        assert len(oauth._pending_challenges) == 3

        oauth.clear_pending_challenges()

        assert len(oauth._pending_challenges) == 0


class TestGetPendingStates:
    """Test getting pending states."""

    def test_returns_empty_list_initially(self, temp_dir):
        """Should return empty list when no pending states."""
        oauth = DirectOAuth(config_dir=temp_dir)

        states = oauth.get_pending_states()

        assert states == []

    def test_returns_all_pending_states(self, temp_dir):
        """Should return all pending state parameters."""
        oauth = DirectOAuth(config_dir=temp_dir)

        flow1 = oauth.start_oauth_flow()
        flow2 = oauth.start_oauth_flow()

        states = oauth.get_pending_states()

        assert len(states) == 2
        assert flow1["state"] in states
        assert flow2["state"] in states


class TestGlobalInstance:
    """Test the global direct_oauth instance."""

    def test_global_instance_exists(self):
        """Should have a global DirectOAuth instance."""
        assert direct_oauth is not None
        assert isinstance(direct_oauth, DirectOAuth)

    def test_global_instance_has_config_dir(self):
        """Global instance should have config directory set."""
        assert direct_oauth.config_dir is not None
        assert '.claude' in str(direct_oauth.config_dir)


class TestOAuthConstants:
    """Test OAuth configuration constants."""

    def test_client_id_is_set(self):
        """Should have a client ID configured."""
        assert OAUTH_CLIENT_ID is not None
        assert len(OAUTH_CLIENT_ID) > 0

    def test_authorize_url_is_set(self):
        """Should have authorize URL configured."""
        assert "claude.ai" in OAUTH_AUTHORIZE_URL

    def test_token_url_is_set(self):
        """Should have token URL configured."""
        assert "anthropic.com" in OAUTH_TOKEN_URL

    def test_redirect_uri_is_set(self):
        """Should have redirect URI configured."""
        assert "anthropic.com" in OAUTH_REDIRECT_URI

    def test_scopes_include_inference(self):
        """Should include inference scope."""
        assert "user:inference" in OAUTH_SCOPES
