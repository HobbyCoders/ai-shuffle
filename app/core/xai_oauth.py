"""xAI / SuperGrok OAuth device-code flow (pure logic).

Storage-agnostic RFC 8628 device authorization against ``auth.x.ai``. Every
function takes an optional ``client`` so tests drive it with an
``httpx.MockTransport`` and NEVER touch the real network.

Mechanics:

* Standard OAuth 2.0 device-code (RFC 8628), not OpenAI's custom deviceauth.
* Issuer ``https://auth.x.ai``.
* Device start → ``POST /oauth2/device/code`` (form-urlencoded)
* Token / poll / refresh → ``POST /oauth2/token`` (form-urlencoded)
* Poll grant: ``urn:ietf:params:oauth:grant-type:device_code``
* Refresh grant: ``refresh_token``
* Pending errors: ``authorization_pending``, ``slow_down``
* Terminal: ``expired_token``, ``access_denied``, ``invalid_grant``, etc.

The default ``client_id`` is the official Grok CLI public client. Overridable
via ``XAI_OAUTH_CLIENT_ID``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ISSUER = "https://auth.x.ai"
DEVICE_CODE_URL = f"{ISSUER}/oauth2/device/code"
TOKEN_URL = f"{ISSUER}/oauth2/token"

_DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
REFERRER = "ai-shuffle"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# Refresh / token failures that mean the credential is dead and needs a fresh
# device-code enrollment. The refresh worker maps these to ``needs_reauth``.
PERMANENT_FAILURE_CODES = frozenset(
    {
        "invalid_grant",
        "expired_token",
        "access_denied",
        "invalid_token",
        "unauthorized_client",
    }
)

_HTTP_TIMEOUT = 30.0


def client_id() -> str:
    """Resolve the OAuth client id (env override wins)."""
    return os.environ.get("XAI_OAUTH_CLIENT_ID") or _DEFAULT_CLIENT_ID


# ---------------------------------------------------------------------------
# Result / error types
# ---------------------------------------------------------------------------

@dataclass
class DeviceCodeStart:
    """The device-code start result the admin acts on.

    ``verification_uri_complete`` is the prefilled URL when upstream supplies
    one; the API layer prefers it as the "Open sign-in" link.
    """
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: Optional[str] = None
    interval: int = 5
    expires_in: int = 900


@dataclass
class TokenBundle:
    """A full OAuth token set. ``account_id`` is a best-effort claim decoded
    from the JWT (team_id / sub); ``None`` when absent. Not required for
    inference against api.x.ai.
    """
    access_token: str
    refresh_token: str
    id_token: Optional[str] = None
    account_id: Optional[str] = None


class XaiAuthPending(Exception):
    """The device authorization is not complete yet (poll again).

    ``slow_down`` mirrors the RFC 8628 signal — the caller should lengthen its
    poll interval.
    """
    def __init__(self, slow_down: bool = False):
        super().__init__("slow_down" if slow_down else "authorization_pending")
        self.slow_down = slow_down


class XaiAuthError(Exception):
    """A terminal auth error. ``code`` is the upstream ``error`` string when one
    was provided. ``is_permanent`` marks dead-credential refresh codes.
    """
    def __init__(self, code: str, message: Optional[str] = None):
        super().__init__(message or code)
        self.code = code

    @property
    def is_permanent(self) -> bool:
        return isinstance(self.code, str) and self.code in PERMANENT_FAILURE_CODES


# ---------------------------------------------------------------------------
# JWT (payload only — NO signature verification; we consume our own exchange)
# ---------------------------------------------------------------------------

def _decode_jwt_payload(token: str) -> dict:
    """Base64url-decode a JWT's payload segment to a dict.

    No signature verification: these tokens come straight back from our own token
    exchange over TLS. Returns ``{}`` on any malformed input rather than raising.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 — malformed token must not break enrollment
        logger.debug("xai_oauth: jwt payload decode failed", exc_info=True)
        return {}


def extract_account_id(
    id_token: Optional[str] = None, access_token: Optional[str] = None
) -> Optional[str]:
    """Best-effort account id from an id_token or access-token JWT.

    Prefers ``team_id``, then ``sub``. Returns ``None`` when absent. Not used
    as a request header for api.x.ai (unlike Codex's chatgpt-account-id).
    """
    for token in (id_token, access_token):
        if not token:
            continue
        claims = _decode_jwt_payload(token)
        for key in ("team_id", "org_id", "sub"):
            val = claims.get(key)
            if isinstance(val, str) and val:
                return val
    return None


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

async def _post(
    client: Optional[httpx.AsyncClient],
    url: str,
    *,
    data: Optional[dict] = None,
) -> httpx.Response:
    """POST form-urlencoded via the caller-supplied client, or a throwaway one."""
    if client is not None:
        return await client.post(url, data=data, timeout=_HTTP_TIMEOUT)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        return await c.post(url, data=data)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        obj = resp.json()
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _error_code(body: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract ``(code, message)`` from an upstream error body.

    RFC 6749 uses a flat ``error`` string; some issuers nest
    ``{"error": {"code": ..., "message": ...}}``. Both shapes are handled.
    """
    err = body.get("error")
    if isinstance(err, dict):
        code = err.get("code") or err.get("type")
        return (code if isinstance(code, str) and code else None), err.get("message")
    if isinstance(err, str) and err:
        return err, body.get("error_description")
    return None, None


def _raise_if_oauth_error(body: dict, status_code: int) -> None:
    """Raise Pending or Error from an OAuth error body, or return if none."""
    err_code, err_message = _error_code(body)
    if not err_code:
        if status_code >= 400:
            raise XaiAuthError(f"http_{status_code}")
        return
    if err_code == "authorization_pending" or "pending" in err_code:
        raise XaiAuthPending(slow_down=False)
    if err_code == "slow_down" or "slow_down" in err_code:
        raise XaiAuthPending(slow_down=True)
    raise XaiAuthError(err_code, err_message)


# ---------------------------------------------------------------------------
# Device-code enrollment
# ---------------------------------------------------------------------------

async def start_device_code(
    *, client: Optional[httpx.AsyncClient] = None
) -> DeviceCodeStart:
    """Begin the RFC 8628 device-code flow: request a user-code."""
    resp = await _post(
        client,
        DEVICE_CODE_URL,
        data={
            "client_id": client_id(),
            "scope": SCOPE,
            "referrer": REFERRER,
        },
    )
    body = _safe_json(resp)
    if resp.status_code >= 400:
        code, message = _error_code(body)
        raise XaiAuthError(code or f"http_{resp.status_code}", message)
    device_code = body.get("device_code")
    user_code = body.get("user_code")
    verification_uri = body.get("verification_uri") or body.get("verification_url")
    if not device_code or not user_code or not verification_uri:
        raise XaiAuthError(
            "invalid_device_response",
            "device-code response missing device_code/user_code/verification_uri",
        )
    complete = body.get("verification_uri_complete") or body.get(
        "verification_url_complete"
    )
    return DeviceCodeStart(
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=complete if isinstance(complete, str) and complete else None,
        interval=int(body.get("interval") or 5),
        expires_in=int(body.get("expires_in") or 900),
    )


async def poll_device_code(
    device_code: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> TokenBundle:
    """One poll step. Raises :class:`XaiAuthPending` until authorized."""
    resp = await _post(
        client,
        TOKEN_URL,
        data={
            "client_id": client_id(),
            "grant_type": DEVICE_GRANT_TYPE,
            "device_code": device_code,
        },
    )
    body = _safe_json(resp)
    _raise_if_oauth_error(body, resp.status_code)
    return _bundle_from_token_response(body)


def _bundle_from_token_response(
    body: dict, *, prior_refresh_token: Optional[str] = None
) -> TokenBundle:
    """Shape a token endpoint JSON body into a :class:`TokenBundle`."""
    access_token = body.get("access_token")
    if not access_token:
        raise XaiAuthError(
            "invalid_token_response", "token response missing access_token"
        )
    refresh_token = body.get("refresh_token") or prior_refresh_token
    if not refresh_token:
        raise XaiAuthError(
            "invalid_token_response", "token response missing refresh_token"
        )
    id_token = body.get("id_token")
    return TokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        account_id=extract_account_id(id_token, access_token),
    )


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

async def refresh_token(
    refresh_token: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> TokenBundle:
    """Refresh the access token; returns the (possibly rotated) bundle.

    Permanent failures (``invalid_grant`` / ``expired_token`` / …) raise a
    :class:`XaiAuthError` whose ``is_permanent`` is ``True``. Serialization of
    concurrent refreshes is the CALLER's responsibility.
    """
    resp = await _post(
        client,
        TOKEN_URL,
        data={
            "client_id": client_id(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    body = _safe_json(resp)
    if resp.status_code >= 400:
        code, message = _error_code(body)
        raise XaiAuthError(code or f"http_{resp.status_code}", message)
    return _bundle_from_token_response(body, prior_refresh_token=refresh_token)
