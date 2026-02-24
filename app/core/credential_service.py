"""
Credential validation and management service.

This module handles business logic for validating API credentials with external
providers (fal.ai, GitHub).
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


async def validate_fal_api_key(api_key: str) -> CredentialValidationResponse:
    """
    Validate a fal.ai API key by making a lightweight API call.

    Uses the queue status endpoint which returns 404 for valid keys (no such request)
    but 401/403 for invalid keys.

    Args:
        api_key: The fal.ai API key to validate

    Returns:
        CredentialValidationResponse with validation result
    """
    if not api_key or not api_key.strip():
        return CredentialValidationResponse(
            result=ValidationResult.INVALID,
            message="API key cannot be empty"
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Use the fal.ai REST API to check key validity
            # A GET to any model's queue status with an invalid key returns 401
            response = await client.get(
                "https://queue.fal.run/fal-ai/flux/schnell/requests/test-validation/status",
                headers={"Authorization": f"Key {api_key.strip()}"}
            )

            if response.status_code in (401, 403):
                return CredentialValidationResponse(
                    result=ValidationResult.INVALID,
                    message="Invalid fal.ai API key"
                )
            elif response.status_code in (404, 422):
                # Key is valid but request doesn't exist — expected
                return CredentialValidationResponse(
                    result=ValidationResult.VALID,
                    message="fal.ai API key is valid"
                )
            elif response.status_code == 429:
                # Rate limited but key is valid
                return CredentialValidationResponse(
                    result=ValidationResult.VALID,
                    message="fal.ai API key is valid (rate limited)"
                )
            elif response.status_code == 200:
                return CredentialValidationResponse(
                    result=ValidationResult.VALID,
                    message="fal.ai API key is valid"
                )
            else:
                logger.warning(
                    f"fal.ai API returned unexpected status {response.status_code}: {response.text}"
                )
                return CredentialValidationResponse(
                    result=ValidationResult.ERROR,
                    message=f"Could not validate key: API returned status {response.status_code}"
                )

    except httpx.TimeoutException:
        logger.warning("Timeout while validating fal.ai API key")
        return CredentialValidationResponse(
            result=ValidationResult.TIMEOUT,
            message="Validation timed out - key may still be valid"
        )
    except httpx.RequestError as e:
        logger.error(f"Request error validating fal.ai API key: {e}")
        return CredentialValidationResponse(
            result=ValidationResult.ERROR,
            message=f"Could not connect to fal.ai API: {str(e)}"
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
        credential_type: Type of credential ('fal_api_key', 'github_pat')
        value: The credential value to validate

    Returns:
        CredentialValidationResponse with validation result

    Raises:
        ValueError: If credential_type is not recognized
    """
    if credential_type == "fal_api_key":
        return await validate_fal_api_key(value)
    elif credential_type == "github_pat":
        return await validate_github_pat(value)
    else:
        raise ValueError(f"Unknown credential type: {credential_type}")
