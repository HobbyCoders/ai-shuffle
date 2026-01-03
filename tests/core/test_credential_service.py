"""
Unit tests for credential validation service.

Tests cover:
- OpenAI API key validation
- Gemini API key validation
- GitHub PAT validation
- Meshy API key validation
- Generic credential validation dispatch
- All HTTP response codes and error paths
- Timeout and network error handling
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.core.credential_service import (
    ValidationResult,
    CredentialValidationResponse,
    GitHubUserInfo,
    validate_openai_api_key,
    validate_gemini_api_key,
    validate_github_pat,
    validate_meshy_api_key,
    validate_credential,
)


class TestValidationResult:
    """Test ValidationResult enum."""

    def test_enum_values(self):
        """Should have correct enum values."""
        assert ValidationResult.VALID.value == "valid"
        assert ValidationResult.INVALID.value == "invalid"
        assert ValidationResult.TIMEOUT.value == "timeout"
        assert ValidationResult.ERROR.value == "error"

    def test_all_values_present(self):
        """Should have exactly 4 values."""
        assert len(ValidationResult) == 4


class TestCredentialValidationResponse:
    """Test CredentialValidationResponse dataclass."""

    def test_basic_response(self):
        """Should create response with required fields."""
        response = CredentialValidationResponse(
            result=ValidationResult.VALID,
            message="Test message"
        )

        assert response.result == ValidationResult.VALID
        assert response.message == "Test message"
        assert response.metadata is None

    def test_response_with_metadata(self):
        """Should create response with optional metadata."""
        metadata = {"key": "value", "username": "testuser"}
        response = CredentialValidationResponse(
            result=ValidationResult.VALID,
            message="Test message",
            metadata=metadata
        )

        assert response.metadata == metadata
        assert response.metadata["username"] == "testuser"


class TestGitHubUserInfo:
    """Test GitHubUserInfo dataclass."""

    def test_basic_user_info(self):
        """Should create user info with required fields."""
        user_info = GitHubUserInfo(username="testuser")

        assert user_info.username == "testuser"
        assert user_info.avatar_url is None

    def test_user_info_with_avatar(self):
        """Should create user info with optional avatar_url."""
        user_info = GitHubUserInfo(
            username="testuser",
            avatar_url="https://example.com/avatar.png"
        )

        assert user_info.username == "testuser"
        assert user_info.avatar_url == "https://example.com/avatar.png"


class TestValidateOpenAIApiKey:
    """Test OpenAI API key validation."""

    @pytest.mark.asyncio
    async def test_invalid_format_not_sk_prefix(self):
        """Should reject keys without 'sk-' prefix."""
        result = await validate_openai_api_key("invalid-key-format")

        assert result.result == ValidationResult.INVALID
        assert "must start with 'sk-'" in result.message

    @pytest.mark.asyncio
    async def test_invalid_format_empty_string(self):
        """Should reject empty string."""
        result = await validate_openai_api_key("")

        assert result.result == ValidationResult.INVALID
        assert "must start with 'sk-'" in result.message

    @pytest.mark.asyncio
    async def test_valid_key_200_response(self):
        """Should return valid for 200 response."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_openai_api_key("sk-test-valid-key")

            assert result.result == ValidationResult.VALID
            assert "is valid" in result.message
            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            assert "api.openai.com" in call_args[0][0]
            assert "Bearer sk-test-valid-key" in str(call_args[1]["headers"])

    @pytest.mark.asyncio
    async def test_invalid_key_401_response(self):
        """Should return invalid for 401 response."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_openai_api_key("sk-invalid-key")

            assert result.result == ValidationResult.INVALID
            assert "Invalid OpenAI API key" in result.message

    @pytest.mark.asyncio
    async def test_rate_limited_429_response(self):
        """Should return valid with rate limit message for 429."""
        mock_response = MagicMock()
        mock_response.status_code = 429

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_openai_api_key("sk-rate-limited-key")

            assert result.result == ValidationResult.VALID
            assert "rate limited" in result.message

    @pytest.mark.asyncio
    async def test_unexpected_status_code(self):
        """Should return error for unexpected status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.text = "Service unavailable"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_openai_api_key("sk-some-key")

            assert result.result == ValidationResult.ERROR
            assert "503" in result.message

    @pytest.mark.asyncio
    async def test_timeout_exception(self):
        """Should return timeout for TimeoutException."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Timed out"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_openai_api_key("sk-timeout-key")

            assert result.result == ValidationResult.TIMEOUT
            assert "timed out" in result.message.lower()

    @pytest.mark.asyncio
    async def test_request_error(self):
        """Should return error for RequestError."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.RequestError("Connection failed")
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_openai_api_key("sk-error-key")

            assert result.result == ValidationResult.ERROR
            assert "Could not connect" in result.message


class TestValidateGeminiApiKey:
    """Test Google Gemini API key validation."""

    @pytest.mark.asyncio
    async def test_valid_key_200_response(self):
        """Should return valid for 200 response."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_gemini_api_key("test-gemini-key")

            assert result.result == ValidationResult.VALID
            assert "Google AI API key is valid" in result.message
            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            assert "generativelanguage.googleapis.com" in call_args[0][0]
            assert "key=test-gemini-key" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_invalid_key_400_api_key_invalid(self):
        """Should return invalid for 400 with API_KEY_INVALID."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "API_KEY_INVALID: The key is not valid"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_gemini_api_key("invalid-key")

            assert result.result == ValidationResult.INVALID
            assert "Invalid Google AI API key" in result.message

    @pytest.mark.asyncio
    async def test_other_400_error(self):
        """Should return error for 400 without API_KEY_INVALID."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad request: missing required parameter"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_gemini_api_key("some-key")

            assert result.result == ValidationResult.ERROR
            assert "API request error" in result.message

    @pytest.mark.asyncio
    async def test_invalid_key_403_response(self):
        """Should return invalid for 403 response."""
        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_gemini_api_key("forbidden-key")

            assert result.result == ValidationResult.INVALID
            assert "lacks permissions" in result.message

    @pytest.mark.asyncio
    async def test_rate_limited_429_response(self):
        """Should return valid with rate limit message for 429."""
        mock_response = MagicMock()
        mock_response.status_code = 429

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_gemini_api_key("rate-limited-key")

            assert result.result == ValidationResult.VALID
            assert "rate limited" in result.message

    @pytest.mark.asyncio
    async def test_unexpected_status_code(self):
        """Should return error for unexpected status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal server error"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_gemini_api_key("some-key")

            assert result.result == ValidationResult.ERROR
            assert "500" in result.message

    @pytest.mark.asyncio
    async def test_timeout_exception(self):
        """Should return timeout for TimeoutException."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Timed out"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_gemini_api_key("timeout-key")

            assert result.result == ValidationResult.TIMEOUT
            assert "timed out" in result.message.lower()

    @pytest.mark.asyncio
    async def test_request_error(self):
        """Should return error for RequestError."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.RequestError("Connection failed")
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_gemini_api_key("error-key")

            assert result.result == ValidationResult.ERROR
            assert "Could not connect to Google AI API" in result.message


class TestValidateGitHubPat:
    """Test GitHub Personal Access Token validation."""

    @pytest.mark.asyncio
    async def test_valid_pat_200_response(self):
        """Should return valid with user info for 200 response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "login": "testuser",
            "avatar_url": "https://avatars.github.com/u/12345"
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_github_pat("ghp_validtoken")

            assert result.result == ValidationResult.VALID
            assert "is valid" in result.message
            assert result.metadata is not None
            assert result.metadata["github_username"] == "testuser"
            assert result.metadata["github_avatar_url"] == "https://avatars.github.com/u/12345"

            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            assert "api.github.com/user" in call_args[0][0]
            assert "Bearer ghp_validtoken" in str(call_args[1]["headers"])

    @pytest.mark.asyncio
    async def test_valid_pat_partial_user_data(self):
        """Should handle partial user data in response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "login": "minimaluser"
            # No avatar_url
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_github_pat("ghp_validtoken")

            assert result.result == ValidationResult.VALID
            assert result.metadata["github_username"] == "minimaluser"
            assert result.metadata["github_avatar_url"] is None

    @pytest.mark.asyncio
    async def test_invalid_pat_401_response(self):
        """Should return invalid for 401 response."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_github_pat("ghp_invalidtoken")

            assert result.result == ValidationResult.INVALID
            assert "Invalid GitHub Personal Access Token" in result.message

    @pytest.mark.asyncio
    async def test_rate_limited_403_response(self):
        """Should return valid for 403 with rate limit message."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "API rate limit exceeded. Please try again later."

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_github_pat("ghp_ratelimited")

            assert result.result == ValidationResult.VALID
            assert "rate limited" in result.message

    @pytest.mark.asyncio
    async def test_forbidden_403_no_permissions(self):
        """Should return invalid for 403 without rate limit."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Token lacks required permissions"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_github_pat("ghp_nopermissions")

            assert result.result == ValidationResult.INVALID
            assert "lacks required permissions" in result.message

    @pytest.mark.asyncio
    async def test_unexpected_status_code(self):
        """Should return error for unexpected status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.text = "Bad gateway"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_github_pat("ghp_sometoken")

            assert result.result == ValidationResult.ERROR
            assert "502" in result.message

    @pytest.mark.asyncio
    async def test_timeout_exception(self):
        """Should return timeout for TimeoutException."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Timed out"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_github_pat("ghp_timeout")

            assert result.result == ValidationResult.TIMEOUT
            assert "timed out" in result.message.lower()

    @pytest.mark.asyncio
    async def test_request_error(self):
        """Should return error for RequestError."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.RequestError("Connection refused")
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_github_pat("ghp_error")

            assert result.result == ValidationResult.ERROR
            assert "Could not connect to GitHub API" in result.message


class TestValidateMeshyApiKey:
    """Test Meshy API key validation."""

    @pytest.mark.asyncio
    async def test_valid_key_200_response(self):
        """Should return valid for 200 response."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_meshy_api_key("meshy-valid-key")

            assert result.result == ValidationResult.VALID
            assert "Meshy API key is valid" in result.message
            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            assert "api.meshy.ai" in call_args[0][0]
            assert "Bearer meshy-valid-key" in str(call_args[1]["headers"])

    @pytest.mark.asyncio
    async def test_invalid_key_401_response(self):
        """Should return invalid for 401 response."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_meshy_api_key("invalid-key")

            assert result.result == ValidationResult.INVALID
            assert "Invalid Meshy API key" in result.message

    @pytest.mark.asyncio
    async def test_invalid_key_403_response(self):
        """Should return invalid for 403 response."""
        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_meshy_api_key("forbidden-key")

            assert result.result == ValidationResult.INVALID
            assert "Invalid Meshy API key" in result.message

    @pytest.mark.asyncio
    async def test_no_credits_402_response(self):
        """Should return valid with no credits message for 402."""
        mock_response = MagicMock()
        mock_response.status_code = 402

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_meshy_api_key("no-credits-key")

            assert result.result == ValidationResult.VALID
            assert "no credits remaining" in result.message

    @pytest.mark.asyncio
    async def test_rate_limited_429_response(self):
        """Should return valid with rate limit message for 429."""
        mock_response = MagicMock()
        mock_response.status_code = 429

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_meshy_api_key("rate-limited-key")

            assert result.result == ValidationResult.VALID
            assert "rate limited" in result.message

    @pytest.mark.asyncio
    async def test_unexpected_status_code(self):
        """Should return error for unexpected status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 504
        mock_response.text = "Gateway timeout"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_meshy_api_key("some-key")

            assert result.result == ValidationResult.ERROR
            assert "504" in result.message

    @pytest.mark.asyncio
    async def test_timeout_exception(self):
        """Should return timeout for TimeoutException."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Timed out"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_meshy_api_key("timeout-key")

            assert result.result == ValidationResult.TIMEOUT
            assert "timed out" in result.message.lower()

    @pytest.mark.asyncio
    async def test_request_error(self):
        """Should return error for RequestError."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.RequestError("DNS lookup failed")
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_meshy_api_key("error-key")

            assert result.result == ValidationResult.ERROR
            assert "Could not connect to Meshy API" in result.message


class TestValidateCredential:
    """Test generic credential validation dispatcher."""

    @pytest.mark.asyncio
    async def test_dispatch_openai_api_key(self):
        """Should dispatch to OpenAI validator for openai_api_key type."""
        with patch(
            "app.core.credential_service.validate_openai_api_key",
            new_callable=AsyncMock
        ) as mock_validate:
            mock_validate.return_value = CredentialValidationResponse(
                result=ValidationResult.VALID,
                message="Valid"
            )

            result = await validate_credential("openai_api_key", "sk-test-key")

            mock_validate.assert_called_once_with("sk-test-key")
            assert result.result == ValidationResult.VALID

    @pytest.mark.asyncio
    async def test_dispatch_gemini_api_key(self):
        """Should dispatch to Gemini validator for gemini_api_key type."""
        with patch(
            "app.core.credential_service.validate_gemini_api_key",
            new_callable=AsyncMock
        ) as mock_validate:
            mock_validate.return_value = CredentialValidationResponse(
                result=ValidationResult.VALID,
                message="Valid"
            )

            result = await validate_credential("gemini_api_key", "test-gemini-key")

            mock_validate.assert_called_once_with("test-gemini-key")
            assert result.result == ValidationResult.VALID

    @pytest.mark.asyncio
    async def test_dispatch_meshy_api_key(self):
        """Should dispatch to Meshy validator for meshy_api_key type."""
        with patch(
            "app.core.credential_service.validate_meshy_api_key",
            new_callable=AsyncMock
        ) as mock_validate:
            mock_validate.return_value = CredentialValidationResponse(
                result=ValidationResult.VALID,
                message="Valid"
            )

            result = await validate_credential("meshy_api_key", "meshy-test-key")

            mock_validate.assert_called_once_with("meshy-test-key")
            assert result.result == ValidationResult.VALID

    @pytest.mark.asyncio
    async def test_dispatch_github_pat(self):
        """Should dispatch to GitHub validator for github_pat type."""
        with patch(
            "app.core.credential_service.validate_github_pat",
            new_callable=AsyncMock
        ) as mock_validate:
            mock_validate.return_value = CredentialValidationResponse(
                result=ValidationResult.VALID,
                message="Valid",
                metadata={"github_username": "testuser"}
            )

            result = await validate_credential("github_pat", "ghp_testtoken")

            mock_validate.assert_called_once_with("ghp_testtoken")
            assert result.result == ValidationResult.VALID

    @pytest.mark.asyncio
    async def test_unknown_credential_type_raises_valueerror(self):
        """Should raise ValueError for unknown credential types."""
        with pytest.raises(ValueError) as exc_info:
            await validate_credential("unknown_type", "some-value")

        assert "Unknown credential type: unknown_type" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_empty_credential_type_raises_valueerror(self):
        """Should raise ValueError for empty credential type."""
        with pytest.raises(ValueError) as exc_info:
            await validate_credential("", "some-value")

        assert "Unknown credential type" in str(exc_info.value)


class TestLogging:
    """Test that logging is performed correctly for various scenarios."""

    @pytest.mark.asyncio
    async def test_openai_unexpected_status_logs_warning(self):
        """Should log warning for unexpected OpenAI status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal server error"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with patch("app.core.credential_service.logger") as mock_logger:
                await validate_openai_api_key("sk-test")
                mock_logger.warning.assert_called_once()
                assert "500" in str(mock_logger.warning.call_args)

    @pytest.mark.asyncio
    async def test_openai_timeout_logs_warning(self):
        """Should log warning for OpenAI timeout."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with patch("app.core.credential_service.logger") as mock_logger:
                await validate_openai_api_key("sk-test")
                mock_logger.warning.assert_called_once()
                assert "Timeout" in str(mock_logger.warning.call_args)

    @pytest.mark.asyncio
    async def test_openai_request_error_logs_error(self):
        """Should log error for OpenAI request errors."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.RequestError("Connection failed"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with patch("app.core.credential_service.logger") as mock_logger:
                await validate_openai_api_key("sk-test")
                mock_logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_gemini_unexpected_status_logs_warning(self):
        """Should log warning for unexpected Gemini status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.text = "Bad gateway"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with patch("app.core.credential_service.logger") as mock_logger:
                await validate_gemini_api_key("test-key")
                mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_github_unexpected_status_logs_warning(self):
        """Should log warning for unexpected GitHub status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.text = "Service unavailable"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with patch("app.core.credential_service.logger") as mock_logger:
                await validate_github_pat("ghp_test")
                mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_meshy_unexpected_status_logs_warning(self):
        """Should log warning for unexpected Meshy status codes."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Server error"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with patch("app.core.credential_service.logger") as mock_logger:
                await validate_meshy_api_key("test-key")
                mock_logger.warning.assert_called_once()


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_openai_key_with_only_sk_prefix(self):
        """Should handle key that is just 'sk-' prefix."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # "sk-" technically passes the prefix check
            result = await validate_openai_api_key("sk-")

            # Should make the API call and get 401 back
            assert result.result == ValidationResult.INVALID

    @pytest.mark.asyncio
    async def test_gemini_400_response_truncates_long_error(self):
        """Should truncate long error messages in 400 response."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "A" * 200  # Long error message, no API_KEY_INVALID

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_gemini_api_key("test-key")

            assert result.result == ValidationResult.ERROR
            # Message should be truncated to first 100 chars
            assert len(result.message) < 200

    @pytest.mark.asyncio
    async def test_github_403_rate_limit_case_insensitive(self):
        """Should detect rate limit in GitHub 403 case insensitively."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "API RATE LIMIT EXCEEDED"  # Uppercase

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_github_pat("ghp_test")

            # The code uses .lower() so this should work
            assert result.result == ValidationResult.VALID
            assert "rate limited" in result.message

    @pytest.mark.asyncio
    async def test_special_characters_in_credentials(self):
        """Should handle credentials with special characters."""
        special_key = "sk-test+key/with=special&chars"
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_openai_api_key(special_key)

            # Should still make the request
            mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_unicode_in_error_response(self):
        """Should handle unicode in error responses."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Error: \u00e9\u00e0\u00fc special chars"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            result = await validate_openai_api_key("sk-test")

            assert result.result == ValidationResult.ERROR
