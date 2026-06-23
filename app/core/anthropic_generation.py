"""
Anthropic API-key validation for the Claude Code authentication credential.

The user-provided `anthropic_api_key` is stored encrypted in `system_settings`
via `settings_crypto.set_encrypted_api_key`. It is an **authentication
credential for Claude Code** — when set, it is injected as `ANTHROPIC_API_KEY`
into every Claude Code subprocess (see
`app.core.environment.inject_claude_code_auth`), overriding the connected
Claude account / OAuth credentials. When unset, the OAuth account is used.

This module only validates the key when the user sets it (the Settings
endpoint calls `validate_api_key`). There is no longer a direct-HTTP
generation path: AI generation (commit messages, PR descriptions, branch
names, profile / subagent generation) always runs through the Claude Agent
SDK `query()` subprocess, which authenticates the same way as the rest of
Claude Code.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


# API endpoint + version. `2023-06-01` is the stable version string required
# by all current Claude models (haiku-4-5, sonnet-4-5, opus-4.x).
_ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


# ---------------------------------------------------------------------------
# Typed errors for caller dispatch.
# ---------------------------------------------------------------------------


class AnthropicHttpError(Exception):
    """Base for Anthropic HTTP path errors."""


class AnthropicAuthError(AnthropicHttpError):
    """401/403 from Anthropic. Key missing, invalid, or revoked."""


class AnthropicRateLimitError(AnthropicHttpError):
    """429 from Anthropic. Account/org is rate-limited or over quota."""


class AnthropicNetworkError(AnthropicHttpError):
    """Connection / timeout / DNS failure."""


class AnthropicApiError(AnthropicHttpError):
    """5xx or unexpected response shape from Anthropic."""


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


async def validate_api_key(api_key: str) -> None:
    """Make a minimal messages.create call to confirm the key works.

    Used by the Settings endpoint on save (the key is an auth credential for
    Claude Code). We send a 1-token request — the cheapest possible call — so
    validation is fast and uses negligible quota. Raises `AnthropicAuthError`
    on 401/403, `AnthropicRateLimitError` on 429, `AnthropicNetworkError` on
    connect/timeout, `AnthropicApiError` on 5xx.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _ANTHROPIC_MESSAGES_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
        raise AnthropicNetworkError(f"Network error contacting Anthropic: {e}") from e

    if resp.status_code in (401, 403):
        raise AnthropicAuthError("Anthropic rejected the API key")
    if resp.status_code == 429:
        raise AnthropicRateLimitError("Anthropic rate-limit during validation")
    if resp.status_code >= 500:
        raise AnthropicApiError(f"Anthropic server error {resp.status_code}")
    if resp.status_code >= 400:
        # 4xx other than auth/rate-limit — model not allowed, quota exhausted, etc.
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        raise AnthropicApiError(f"Anthropic rejected request: {detail}")
