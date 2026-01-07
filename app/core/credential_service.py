"""
Credential validation and management service.

This module handles business logic for validating API credentials with external
providers (OpenAI, Google Gemini, GitHub).
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class ValidationResult(Enum):
    """Result of credential validation"""
    VALID = "valid"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class CredentialValidationResponse:
    """Response from credential validation"""
    result: ValidationResult
    message: str
    metadata: Optional[dict] = None


async def validate_openai_api_key(api_key: str) -> CredentialValidationResponse:
    """
    Validate an OpenAI API key by calling the models endpoint.

    Args:
        api_key: The OpenAI API key to validate

    Returns:
        CredentialValidationResponse with validation result
    """
    # Basic format check
    if not api_key.startswith("sk-"):
        return CredentialValidationResponse(
            result=ValidationResult.INVALID,
            message="Invalid OpenAI API key format - must start with 'sk-'"
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"}
            )

            if response.status_code == 200:
                return CredentialValidationResponse(
                    result=ValidationResult.VALID,
                    message="OpenAI API key is valid"
                )
            elif response.status_code == 401:
                return CredentialValidationResponse(
                    result=ValidationResult.INVALID,
                    message="Invalid OpenAI API key"
                )
            elif response.status_code == 429:
                # Rate limited but key is valid
                return CredentialValidationResponse(
                    result=ValidationResult.VALID,
                    message="OpenAI API key is valid (rate limited)"
                )
            else:
                # Other HTTP errors - treat as temporary issues, not invalid key
                logger.warning(
                    f"OpenAI API returned unexpected status {response.status_code}: {response.text}"
                )
                return CredentialValidationResponse(
                    result=ValidationResult.ERROR,
                    message=f"Could not validate key: API returned status {response.status_code}"
                )

    except httpx.TimeoutException:
        logger.warning("Timeout while validating OpenAI API key")
        return CredentialValidationResponse(
            result=ValidationResult.TIMEOUT,
            message="Validation timed out - key may still be valid"
        )
    except httpx.RequestError as e:
        logger.error(f"Request error validating OpenAI API key: {e}")
        return CredentialValidationResponse(
            result=ValidationResult.ERROR,
            message=f"Could not connect to OpenAI API: {str(e)}"
        )


async def validate_gemini_api_key(api_key: str) -> CredentialValidationResponse:
    """
    Validate a Google Gemini API key by calling the models endpoint.

    Args:
        api_key: The Gemini API key to validate

    Returns:
        CredentialValidationResponse with validation result
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            )

            if response.status_code == 200:
                return CredentialValidationResponse(
                    result=ValidationResult.VALID,
                    message="Google AI API key is valid"
                )
            elif response.status_code == 400:
                if "API_KEY_INVALID" in response.text:
                    return CredentialValidationResponse(
                        result=ValidationResult.INVALID,
                        message="Invalid Google AI API key"
                    )
                else:
                    # Other 400 errors
                    return CredentialValidationResponse(
                        result=ValidationResult.ERROR,
                        message=f"API request error: {response.text[:100]}"
                    )
            elif response.status_code == 403:
                return CredentialValidationResponse(
                    result=ValidationResult.INVALID,
                    message="Google AI API key is invalid or lacks permissions"
                )
            elif response.status_code == 429:
                # Rate limited but key is valid
                return CredentialValidationResponse(
                    result=ValidationResult.VALID,
                    message="Google AI API key is valid (rate limited)"
                )
            else:
                logger.warning(
                    f"Gemini API returned unexpected status {response.status_code}: {response.text}"
                )
                return CredentialValidationResponse(
                    result=ValidationResult.ERROR,
                    message=f"Could not validate key: API returned status {response.status_code}"
                )

    except httpx.TimeoutException:
        logger.warning("Timeout while validating Gemini API key")
        return CredentialValidationResponse(
            result=ValidationResult.TIMEOUT,
            message="Validation timed out - key may still be valid"
        )
    except httpx.RequestError as e:
        logger.error(f"Request error validating Gemini API key: {e}")
        return CredentialValidationResponse(
            result=ValidationResult.ERROR,
            message=f"Could not connect to Google AI API: {str(e)}"
        )


@dataclass
class GitHubUserInfo:
    """GitHub user information from PAT validation"""
    username: str
    avatar_url: Optional[str] = None


async def validate_github_pat(pat: str) -> CredentialValidationResponse:
    """
    Validate a GitHub Personal Access Token by calling the user endpoint.

    Args:
        pat: The GitHub Personal Access Token to validate

    Returns:
        CredentialValidationResponse with validation result and user info in metadata
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/vnd.github+json"
                }
            )

            if response.status_code == 200:
                user_data = response.json()
                return CredentialValidationResponse(
                    result=ValidationResult.VALID,
                    message="GitHub Personal Access Token is valid",
                    metadata={
                        "github_username": user_data.get("login"),
                        "github_avatar_url": user_data.get("avatar_url")
                    }
                )
            elif response.status_code == 401:
                return CredentialValidationResponse(
                    result=ValidationResult.INVALID,
                    message="Invalid GitHub Personal Access Token"
                )
            elif response.status_code == 403:
                # Could be rate limited or token has insufficient permissions
                if "rate limit" in response.text.lower():
                    return CredentialValidationResponse(
                        result=ValidationResult.VALID,
                        message="GitHub token is valid (rate limited)"
                    )
                return CredentialValidationResponse(
                    result=ValidationResult.INVALID,
                    message="GitHub token lacks required permissions"
                )
            else:
                logger.warning(
                    f"GitHub API returned unexpected status {response.status_code}: {response.text}"
                )
                return CredentialValidationResponse(
                    result=ValidationResult.ERROR,
                    message=f"Could not validate token: API returned status {response.status_code}"
                )

    except httpx.TimeoutException:
        logger.warning("Timeout while validating GitHub PAT")
        return CredentialValidationResponse(
            result=ValidationResult.TIMEOUT,
            message="Validation timed out - token may still be valid"
        )
    except httpx.RequestError as e:
        logger.error(f"Request error validating GitHub PAT: {e}")
        return CredentialValidationResponse(
            result=ValidationResult.ERROR,
            message=f"Could not connect to GitHub API: {str(e)}"
        )


async def validate_credential(credential_type: str, value: str) -> CredentialValidationResponse:
    """
    Validate a credential based on its type.

    Args:
        credential_type: Type of credential ('openai_api_key', 'gemini_api_key', 'github_pat')
        value: The credential value to validate

    Returns:
        CredentialValidationResponse with validation result

    Raises:
        ValueError: If credential_type is not recognized
    """
    if credential_type == "openai_api_key":
        return await validate_openai_api_key(value)
    elif credential_type == "gemini_api_key":
        return await validate_gemini_api_key(value)
    elif credential_type == "github_pat":
        return await validate_github_pat(value)
    else:
        raise ValueError(f"Unknown credential type: {credential_type}")
