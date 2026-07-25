"""Upstream HTTP/SSE client for the translator gateway (Chunk A, stage 2).

This is the ONLY layer in the gateway that touches the network and decrypts
credentials. Three upstream modes:

  * ``codex``                — ChatGPT-subscription Codex backend
    (``POST https://chatgpt.com/backend-api/codex/responses``, Codex OAuth access
    token + ``chatgpt-account-id``). On a 401 it triggers a Chunk B refresh ONCE
    and retries once, then surfaces the error.
  * ``openai``               — OpenAI platform Responses API
    (``POST https://api.openai.com/v1/responses``, the provider's API key).
  * ``anthropic_passthrough``— forwards an Anthropic Messages request VERBATIM to
    ``https://api.anthropic.com`` using a REAL Anthropic API key from the provider
    row. HARD RULE (F1/G9): this NEVER reads a Claude *subscription* OAuth token
    (``claude_accounts``); it only ever uses a model-provider API key and REFUSES
    to run without one. See :func:`require_passthrough_key`.

Credential handling (S4): tokens are decrypted at request time, are NEVER logged
or printed (not even truncated), and request headers (``Authorization`` /
``chatgpt-account-id``) are never included in any error or log output. Upstream
error *bodies* are the upstream's own JSON and are surfaced trimmed and
header-free via :func:`_summarize_error_body`.

Rate limits (2026-07-22 storm): every >=400 branch below routes an upstream HTTP
**429** — and only a 429 — into
:mod:`app.core.translator_gateway.circuit_breaker`, which then refuses further
gateway requests to that provider install-wide until a cooldown elapses. The
Codex 401 token-rotation retry above is deliberately untouched by that hook.

The Responses SSE is parsed into ``(event_name, payload)`` tuples and handed to
the pure stage-1 translator; the translation itself lives in ``router.py`` so
this module stays a thin, testable network boundary.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from app.core import encryption
from app.core.http_client import get_http_client
from app.core.translator_gateway import circuit_breaker

# ─── Upstream endpoints & identity ───────────────────────────────────────────
# Codex backend (ChatGPT subscription) — the working request template is the
# binding contract in docs/CODEX_SPIKE_FINDINGS.md.
CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
# OpenAI platform Responses API.
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
# Anthropic passthrough target (verbatim Messages forwarding).
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Codex CLI identity headers (docs/CODEX_SPIKE_FINDINGS.md working template).
CODEX_USER_AGENT = "codex_cli_rs/1.0.0"
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_OPENAI_BETA = "responses=experimental"

# Streaming reads can be long; the shared 30s client timeout would cut a slow
# reasoning turn off mid-stream. Disable the read timeout for the streaming POST
# (connect/write still bounded by the client default).
import httpx  # noqa: E402  (kept next to the constant it configures)

_STREAM_TIMEOUT = httpx.Timeout(connect=15.0, write=30.0, read=None, pool=15.0)
_BUFFERED_TIMEOUT = httpx.Timeout(connect=15.0, write=30.0, read=600.0, pool=15.0)


# ─── Errors ──────────────────────────────────────────────────────────────────

class UpstreamError(Exception):
    """An upstream request failed. ``message`` is already credential-free."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = int(status_code)
        self.message = message
        super().__init__(message)


class UpstreamAuthError(UpstreamError):
    """Missing / unusable upstream credentials (surfaced as 401)."""

    def __init__(self, message: str) -> None:
        super().__init__(401, message)


# ─── Mode resolution ─────────────────────────────────────────────────────────

def provider_mode(provider: Optional[Dict[str, Any]]) -> str:
    """Map a provider row to an upstream mode.

    ``chatgpt`` / ``auth_mode==chatgpt`` → ``codex``; ``openai`` → ``openai``;
    ``anthropic`` → ``anthropic_passthrough``. Anything else does not route
    through this gateway.
    """
    provider = provider or {}
    ptype = provider.get("provider_type")
    auth_mode = provider.get("auth_mode")
    if ptype == "chatgpt" or auth_mode == "chatgpt":
        return "codex"
    if ptype == "openai":
        return "openai"
    if ptype == "anthropic":
        return "anthropic_passthrough"
    raise UpstreamError(
        500, f"provider_type '{ptype}' does not route through the translator gateway"
    )


# ─── Credential resolution (decrypted at request time; NEVER logged) ─────────

def _decrypt_provider_auth_token(provider: Optional[Dict[str, Any]]) -> Optional[str]:
    """Decrypt a provider row's ``auth_token`` blob (or pass plaintext through)."""
    raw = (provider or {}).get("auth_token")
    if not raw:
        return None
    if encryption.is_encrypted(raw):
        return encryption.decrypt_value(raw)
    return raw  # plaintext (migration compatibility)


async def _codex_auth(provider_id: str) -> Tuple[str, Optional[str]]:
    """Return ``(access_token, account_id)`` for a Codex provider, or raise."""
    from app.db import database as db
    from app.db.async_database import run_sync

    stored = await run_sync(db.get_codex_tokens, provider_id)
    if not stored:
        raise UpstreamAuthError(
            "No ChatGPT/Codex credentials are configured for this provider."
        )
    if stored.get("needs_reauth"):
        raise UpstreamAuthError(
            "The ChatGPT account needs to be reconnected before it can be used."
        )
    enc_access = stored.get("codex_access_token")
    access = encryption.decrypt_value(enc_access) if enc_access else None
    if not access:
        raise UpstreamAuthError(
            "The ChatGPT access token is missing; reconnect the account."
        )
    return access, stored.get("codex_account_id")


def _codex_headers(access_token: str, account_id: Optional[str]) -> Dict[str, str]:
    """Build the Codex request headers (fresh per-request ``session_id`` uuid)."""
    return {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id or "",
        "OpenAI-Beta": CODEX_OPENAI_BETA,
        "originator": CODEX_ORIGINATOR,
        "session_id": str(uuid.uuid4()),
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": CODEX_USER_AGENT,
    }


async def _openai_key(provider_id: str) -> str:
    """Return the decrypted OpenAI API key for an ``openai`` provider, or raise."""
    from app.db import database as db
    from app.db.async_database import run_sync

    provider = await run_sync(db.get_model_provider, provider_id, True)
    if not provider:
        raise UpstreamError(500, "OpenAI provider not found.")
    key = _decrypt_provider_auth_token(provider)
    if not key:
        raise UpstreamAuthError("No OpenAI API key is configured for this provider.")
    return key


def _openai_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": CODEX_OPENAI_BETA,
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }


def require_passthrough_key(provider: Optional[Dict[str, Any]]) -> str:
    """Return the REAL Anthropic API key for passthrough, or raise (F1/G9 guard).

    HARD RULE: Anthropic passthrough forwards traffic ONLY with an Anthropic API
    key stored on the model-provider row. The gateway MUST NOT read, and this
    function never reads, any Claude *subscription* OAuth token
    (``claude_accounts``) — no subscription credential may ever flow through the
    gateway. A provider row with no API key means passthrough REFUSES to run.

    Kept as a pure, DB-free function so the guard is unit-testable in isolation.
    """
    key = _decrypt_provider_auth_token(provider)
    if not key:
        raise UpstreamAuthError(
            "Anthropic passthrough requires a real Anthropic API key on the "
            "provider row; refusing to run without one (a Claude subscription "
            "login is never used by the gateway)."
        )
    return key


def _anthropic_headers(api_key: str) -> Dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }


async def _anthropic_passthrough_key(provider_id: str) -> str:
    from app.db import database as db
    from app.db.async_database import run_sync

    provider = await run_sync(db.get_model_provider, provider_id, True)
    if not provider:
        raise UpstreamError(500, "Anthropic passthrough provider not found.")
    return require_passthrough_key(provider)


# ─── SSE parsing (pure; testable against any async line source) ──────────────

async def iter_sse_events(
    lines: AsyncIterator[str],
) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
    """Parse an async iterator of SSE text lines into ``(event, payload)`` tuples.

    Event name comes from the ``event:`` field, falling back to the payload's
    ``type``. ``data: [DONE]`` sentinels and unparseable frames are skipped.
    """
    event_name: Optional[str] = None
    data_lines: list[str] = []

    def _flush() -> Optional[Tuple[str, Dict[str, Any]]]:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = None
            return None
        data_str = "\n".join(data_lines)
        name = event_name
        event_name = None
        data_lines = []
        if data_str.strip() in ("", "[DONE]"):
            return None
        try:
            payload = json.loads(data_str)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        resolved = name or payload.get("type")
        if not resolved:
            return None
        return str(resolved), payload

    async for raw_line in lines:
        line = raw_line.rstrip("\r\n") if isinstance(raw_line, str) else str(raw_line)
        if line == "":
            emitted = _flush()
            if emitted is not None:
                yield emitted
            continue
        if line.startswith(":"):
            continue  # SSE comment / heartbeat
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip(" "))
    # Trailing event with no terminating blank line.
    emitted = _flush()
    if emitted is not None:
        yield emitted


def _summarize_error_body(body: Any) -> str:
    """Trim an upstream error body to a short, credential-free string.

    NEVER includes request headers — ``Authorization`` and ``chatgpt-account-id``
    are never logged (S4). The body itself is the upstream's own error payload.
    """
    try:
        if isinstance(body, (bytes, bytearray)):
            text = bytes(body).decode("utf-8", "replace")
        else:
            text = str(body)
    except Exception:  # noqa: BLE001
        text = "<unreadable upstream error body>"
    return text.strip().replace("\r", " ").replace("\n", " ")[:500]


def _note_rate_limit(provider_id: str, status_code: int, headers: Any) -> None:
    """Trip the per-provider circuit breaker when the upstream returns HTTP 429.

    ONLY 429 trips it. A 401 stays on the Codex token-rotation retry path
    (untouched), and 5xx is a transient upstream fault, not a quota signal.
    ``Retry-After`` (delta-seconds or HTTP-date) is passed through and honoured
    by the breaker, capped by ``provider_circuit_breaker_max_cooldown_seconds``.

    Never raises: recording is a side channel and must not replace the
    ``UpstreamError`` the caller is about to raise.
    """
    if status_code != 429:
        return
    try:
        retry_after = headers.get("retry-after") if headers is not None else None
        circuit_breaker.record_rate_limited(provider_id, retry_after)
    except Exception:  # noqa: BLE001 — breaker bookkeeping must never mask the error
        pass


# ─── Streaming: Codex / OpenAI Responses SSE ─────────────────────────────────

async def stream_responses_events(
    *,
    provider_id: str,
    mode: str,
    responses_body: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
    """POST a Responses request and yield parsed ``(event, payload)`` SSE tuples.

    ``mode`` is ``codex`` or ``openai``. On a Codex 401 this refreshes ONCE (Chunk
    B) and retries once; any other >=400 raises :class:`UpstreamError`.
    """
    if mode not in ("codex", "openai"):
        raise UpstreamError(500, f"stream_responses_events: bad mode {mode!r}")
    http = client or get_http_client()
    url = OPENAI_RESPONSES_URL if mode == "openai" else CODEX_RESPONSES_URL
    attempted_refresh = False

    while True:
        if mode == "codex":
            access, account_id = await _codex_auth(provider_id)
            headers = _codex_headers(access, account_id)
            stale_access: Optional[str] = access
        else:
            headers = _openai_headers(await _openai_key(provider_id))
            stale_access = None

        async with http.stream(
            "POST", url, json=responses_body, headers=headers, timeout=_STREAM_TIMEOUT
        ) as resp:
            if (
                resp.status_code == 401
                and mode == "codex"
                and not attempted_refresh
            ):
                attempted_refresh = True
                await resp.aread()  # drain so the connection can be reused
                from app.core import codex_token_refresh

                refreshed = await codex_token_refresh.refresh_on_401_once(
                    provider_id, stale_access_token=stale_access, client=http
                )
                if refreshed:
                    continue  # retry once with the rotated token
                raise UpstreamAuthError(
                    "The ChatGPT account was rejected (401) and the token could "
                    "not be refreshed; reconnect the account."
                )
            if resp.status_code >= 400:
                body = await resp.aread()
                _note_rate_limit(provider_id, resp.status_code, resp.headers)
                raise UpstreamError(resp.status_code, _summarize_error_body(body))
            if mode == "codex":
                try:
                    from app.core import provider_usage

                    provider_usage.capture_codex_headers(provider_id, resp.headers)
                except Exception:  # noqa: BLE001 — capture must never break a turn
                    pass
            async for event in iter_sse_events(resp.aiter_lines()):
                yield event
            return


# ─── Anthropic passthrough (verbatim forwarding) ─────────────────────────────

async def anthropic_passthrough_stream(
    *,
    provider_id: str,
    anthropic_body: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> AsyncIterator[bytes]:
    """Forward a streaming Anthropic Messages request verbatim, yielding raw bytes."""
    api_key = await _anthropic_passthrough_key(provider_id)
    http = client or get_http_client()
    async with http.stream(
        "POST",
        ANTHROPIC_MESSAGES_URL,
        json=anthropic_body,
        headers=_anthropic_headers(api_key),
        timeout=_STREAM_TIMEOUT,
    ) as resp:
        if resp.status_code >= 400:
            body = await resp.aread()
            _note_rate_limit(provider_id, resp.status_code, resp.headers)
            raise UpstreamError(resp.status_code, _summarize_error_body(body))
        async for chunk in resp.aiter_raw():
            yield chunk


async def anthropic_passthrough_json(
    *,
    provider_id: str,
    anthropic_body: Dict[str, Any],
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Forward a non-streaming Anthropic Messages request verbatim; return JSON."""
    api_key = await _anthropic_passthrough_key(provider_id)
    http = client or get_http_client()
    resp = await http.post(
        ANTHROPIC_MESSAGES_URL,
        json=anthropic_body,
        headers=_anthropic_headers(api_key),
        timeout=_BUFFERED_TIMEOUT,
    )
    if resp.status_code >= 400:
        _note_rate_limit(provider_id, resp.status_code, resp.headers)
        raise UpstreamError(resp.status_code, _summarize_error_body(resp.content))
    return resp.json()
