"""
Anthropic direct HTTP path for one-shot generation calls.

Replaces the Claude Agent SDK subprocess path for commit messages, PR
descriptions, branch name generation, and profile/subagent generation. Those
calls need no tools, no agent loop, no MCP — a single `messages.create` HTTP
request gets the same result in 0.8-1.5s instead of 3-6s.

Auth model
----------
The user-provided `anthropic_api_key` is stored encrypted in `system_settings`
via `settings_crypto.set_encrypted_api_key`. This module reads it through
`get_decrypted_api_key` on each call (cheap — DB hit + Fernet decrypt).

Fallback behavior
-----------------
Callers (`app.api.generation.generate_with_claude`) are expected to:
- Call `generate_via_http()` first when `is_http_generation_available()` is True.
- Catch `AnthropicAuthError` and `AnthropicNetworkError` and fall back to the
  SDK subprocess path — these usually indicate misconfiguration or transient
  issues where the SDK's bundled auth or retry behavior is still useful.
- Let `AnthropicRateLimitError` propagate — retrying via SDK would hit the
  same rate limit on the same account and only mask the real issue.

Streaming
---------
Not used. One-shot generation produces short outputs (commit message, branch
name, PR body) where streaming adds complexity with no UX benefit. The HTTP
call is awaited to completion and the full text returned.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from app.api.settings_crypto import get_decrypted_api_key

logger = logging.getLogger(__name__)


# API endpoint + version. `2023-06-01` is the stable version string required
# by all current Claude models (haiku-4-5, sonnet-4-5, opus-4.x).
_ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# DB key name for the stored API key. Encryption lists in
# `app.core.encryption` must include this name so re-keying on admin-password
# change doesn't leave the value stranded.
_SETTING_NAME = "anthropic_api_key"

# Cache the httpx client across calls so HTTP/2 + keepalive cuts ~200-500ms
# off the second-and-later calls. Timeout is per-call, not per-client.
_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    """Return a shared AsyncClient with sensible timeouts.

    The client is cached at module level. We don't close it explicitly on
    shutdown — Python's GC handles it when the event loop exits. Sharing
    amortizes TLS handshake and connection-pool setup across generation
    calls.
    """
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _http_client


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


def is_http_generation_available() -> bool:
    """Return True if the user has configured an Anthropic API key.

    Cheap check — one DB read. Call this before attempting HTTP so we can
    gracefully fall through to the SDK subprocess path when the user hasn't
    set a key.
    """
    return bool(get_decrypted_api_key(_SETTING_NAME))


async def validate_api_key(api_key: str) -> None:
    """Make a minimal messages.create call to confirm the key works.

    Used by the Settings endpoint on save. We send a 1-token request — the
    cheapest possible call — so validation is fast and uses negligible quota.
    Raises `AnthropicAuthError` on 401/403, `AnthropicRateLimitError` on 429,
    `AnthropicNetworkError` on connect/timeout, `AnthropicApiError` on 5xx.
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


async def generate_via_http(
    prompt: str,
    model: str,
    system_prompt: Optional[str] = None,
    max_tokens: int = 2048,
) -> str:
    """Run a one-shot completion against Anthropic's REST API.

    Args:
        prompt: User-role message text.
        model: Full model ID (e.g. `claude-haiku-4-5`).
        system_prompt: Optional system message to prepend.
        max_tokens: Upper bound for the response. 2048 covers generated
            commit messages, PR bodies, and JSON profile configs with
            margin.

    Returns:
        Concatenated text across all returned content blocks. Non-text
        blocks (tool_use, thinking) are ignored — we never ask for them
        in one-shot gen.

    Raises:
        AnthropicAuthError: 401/403. Caller should fall back to SDK.
        AnthropicRateLimitError: 429. Caller should surface — retry via SDK
            hits the same quota.
        AnthropicNetworkError: Connect / DNS / timeout. Caller should fall
            back to SDK.
        AnthropicApiError: 5xx or malformed response.
    """
    api_key = get_decrypted_api_key(_SETTING_NAME)
    if not api_key:
        # Shouldn't happen if caller checked is_http_generation_available()
        # first, but guard anyway so we surface a clean error instead of an
        # HTTP 401.
        raise AnthropicAuthError("No Anthropic API key configured")

    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        body["system"] = system_prompt

    t0 = time.perf_counter()
    client = _get_http_client()
    try:
        resp = await client.post(
            _ANTHROPIC_MESSAGES_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=body,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
        logger.warning(f"[anthropic-http] network error after {(time.perf_counter()-t0)*1000:.0f}ms: {e}")
        raise AnthropicNetworkError(f"Network error: {e}") from e

    elapsed_ms = (time.perf_counter() - t0) * 1000

    if resp.status_code in (401, 403):
        logger.warning(f"[anthropic-http] auth failed in {elapsed_ms:.0f}ms")
        raise AnthropicAuthError("Anthropic rejected the API key")
    if resp.status_code == 429:
        logger.warning(f"[anthropic-http] rate-limited in {elapsed_ms:.0f}ms")
        raise AnthropicRateLimitError("Anthropic rate-limit hit")
    if resp.status_code >= 500:
        logger.warning(f"[anthropic-http] server error {resp.status_code} in {elapsed_ms:.0f}ms")
        raise AnthropicApiError(f"Anthropic server error {resp.status_code}")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        logger.warning(f"[anthropic-http] {resp.status_code} in {elapsed_ms:.0f}ms: {detail}")
        raise AnthropicApiError(f"Anthropic API error: {detail}")

    try:
        payload = resp.json()
        blocks = payload.get("content") or []
        text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        text = "".join(text_parts)
    except Exception as e:
        raise AnthropicApiError(f"Malformed Anthropic response: {e}") from e

    logger.info(
        f"[anthropic-http] model={model} took {elapsed_ms:.0f}ms "
        f"output_chars={len(text)} "
        f"usage={payload.get('usage')}"
    )
    return text
