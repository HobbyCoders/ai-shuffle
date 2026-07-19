"""Codex / ChatGPT-subscription OAuth device-code flow (pure logic).

Phase-2 Chunk B.2/B.3 — the enrollment + refresh *flow* that sits on top of the
already-landed storage layer (``app/db/model_providers.set_codex_token_bundle`` /
``get_codex_tokens``). This module is deliberately storage-agnostic and
side-effect-free apart from the outbound HTTP it makes: every function takes an
optional ``client`` so tests drive it with an ``httpx.MockTransport`` and NEVER
touch the real network.

Mechanics are the verified F3 finding of ``docs/MULTI_PROVIDER_PHASE2_PLAN.md``
(read from openai/codex ``main``):

* OAuth 2.0 Authorization-Code + PKCE (S256), DEVICE-CODE variant.
* Issuer ``https://auth.openai.com``.
* Device start (user-code)  → ``/api/accounts/deviceauth/usercode``
* Device poll               → ``/deviceauth/token``
* Verification URL the admin opens → ``/codex/device``
* Token exchange / refresh   → ``/oauth/token``
* Revoke                     → ``/oauth/revoke``
* The refresh token ROTATES on every refresh — the caller MUST persist the whole
  rotated bundle atomically (``set_codex_token_bundle`` does this in one UPDATE),
  and two concurrent refreshes of the same credential race to
  ``refresh_token_reused`` permanent death (serialized by the per-provider lock
  in ``app/core/codex_token_refresh.py``).

ToS note (plan F2/F3): the default ``client_id`` below is the OFFICIAL Codex CLI
client id. Reusing it is precisely what makes this ChatGPT *subscription* auth
rather than a paid API key — the deliberate ToS-grey seam, accepted by the
project owner (OpenAI-side grey only; F1 keeps Anthropic subscription OAuth out
entirely). Overridable via ``CODEX_APP_SERVER_LOGIN_CLIENT_ID``.

Reference implementations studied (NOT vendored): openai/codex ``login`` (device
auth), ``numman-ali/opencode-openai-codex-auth`` (Codex header/auth handling).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Issuer / endpoints (F3). Kept as module constants — one editable place if the
# undocumented backend shifts.
ISSUER = "https://auth.openai.com"
DEVICE_USERCODE_URL = f"{ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{ISSUER}/api/accounts/deviceauth/token"
DEVICE_VERIFICATION_URL = f"{ISSUER}/codex/device"
# The device flow's implicit redirect target — /oauth/token REQUIRES it on the
# authorization_code exchange ("same value used during authorization"); value
# mirrors codex-rs device_code_auth.rs.
DEVICE_CALLBACK_URL = f"{ISSUER}/deviceauth/callback"
TOKEN_URL = f"{ISSUER}/oauth/token"
REVOKE_URL = f"{ISSUER}/oauth/revoke"

# ToS-grey seam (F2/F3): the official Codex CLI client id, deliberately reused so
# the flow authenticates a ChatGPT *subscription*. Env-overridable. Accepted by
# the project owner.
_DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

SCOPE = "openid profile email offline_access"

# Refresh failure codes that are PERMANENT — the credential is dead and requires
# a fresh device-code enrollment. The refresh worker maps these to
# ``needs_reauth=TRUE`` and stops refreshing that provider (B.3 / G9).
PERMANENT_FAILURE_CODES = frozenset(
    {"refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated"}
)

# httpx timeout for the outbound auth calls (device poll is a fast single step;
# the frontend drives the poll cadence, not a long-poll here).
_HTTP_TIMEOUT = 30.0


def client_id() -> str:
    """Resolve the OAuth client id (env override wins)."""
    return os.environ.get("CODEX_APP_SERVER_LOGIN_CLIENT_ID") or _DEFAULT_CLIENT_ID


# ---------------------------------------------------------------------------
# Result / error types
# ---------------------------------------------------------------------------

@dataclass
class DeviceCodeStart:
    """The device-code start result the admin acts on.

    ``code_verifier`` is the PKCE verifier minted for THIS enrollment; the caller
    must carry it (server-side, not to the browser) from start → poll so the
    token exchange can prove possession. ``device_code`` is the opaque handle the
    poll step submits.
    """
    device_code: str
    user_code: str
    verification_uri: str
    code_verifier: str
    interval: int = 5
    expires_in: int = 900


@dataclass
class TokenBundle:
    """A full OAuth token set. ``account_id`` is the ``chatgpt_account_id`` claim
    decoded from ``id_token`` (F3) — ``None`` if the id_token omitted it.
    """
    access_token: str
    refresh_token: str
    id_token: Optional[str] = None
    account_id: Optional[str] = None


class CodexAuthPending(Exception):
    """The device authorization is not complete yet (poll again).

    ``slow_down`` mirrors the RFC 8628 signal — the caller should lengthen its
    poll interval.
    """
    def __init__(self, slow_down: bool = False):
        super().__init__("slow_down" if slow_down else "authorization_pending")
        self.slow_down = slow_down


class CodexAuthError(Exception):
    """A terminal auth error. ``code`` is the upstream ``error`` string when one
    was provided. ``is_permanent`` marks the rotating-refresh death codes.
    """
    def __init__(self, code: str, message: Optional[str] = None):
        super().__init__(message or code)
        self.code = code

    @property
    def is_permanent(self) -> bool:
        return self.code in PERMANENT_FAILURE_CODES


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def generate_pkce() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for PKCE S256.

    Verifier: 32 random bytes, base64url without padding (43 chars, within the
    RFC 7636 43–128 range). Challenge: ``base64url(sha256(verifier))`` without
    padding.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# JWT (payload only — NO signature verification; we consume our own exchange)
# ---------------------------------------------------------------------------

def _decode_jwt_payload(token: str) -> dict:
    """Base64url-decode a JWT's payload segment to a dict.

    No signature verification: these tokens come straight back from our own token
    exchange over TLS, so there is nothing to attest against (F3). Returns ``{}``
    on any malformed input rather than raising — a missing account id is handled
    by the caller, not fatal to enrollment.
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
    except Exception:  # noqa: BLE001 — malformed id_token must not break enrollment
        logger.debug("codex_oauth: id_token payload decode failed", exc_info=True)
        return {}


def extract_account_id(id_token: Optional[str]) -> Optional[str]:
    """Extract the ``chatgpt_account_id`` claim from an id_token (F3).

    Codex places the id under the ``https://api.openai.com/auth`` namespaced
    claim; older/simple tokens carry it top-level. Both are checked. Returns
    ``None`` when absent.
    """
    if not id_token:
        return None
    claims = _decode_jwt_payload(id_token)
    top = claims.get("chatgpt_account_id")
    if isinstance(top, str) and top:
        return top
    ns = claims.get("https://api.openai.com/auth")
    if isinstance(ns, dict):
        val = ns.get("chatgpt_account_id")
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
    json_body: Optional[dict] = None,
) -> httpx.Response:
    """POST via the caller-supplied client, or a throwaway one.

    Tests pass an ``httpx.AsyncClient(transport=MockTransport(...))``; production
    passes the shared client (``app.core.http_client.get_http_client``). When no
    client is given a short-lived one is created and closed so this module has no
    hard dependency on app singletons.
    """
    if client is not None:
        return await client.post(url, data=data, json=json_body, timeout=_HTTP_TIMEOUT)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
        return await c.post(url, data=data, json=json_body)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        obj = resp.json()
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _error_code(body: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract ``(code, message)`` from an upstream error body.

    The deviceauth endpoints return a NESTED error object
    (``{"error": {"code": "deviceauth_authorization_pending", "message": ...}}``)
    while ``/oauth/token`` uses flat RFC-6749 strings — both shapes are handled.
    """
    err = body.get("error")
    if isinstance(err, dict):
        code = err.get("code") or err.get("type")
        return (code if isinstance(code, str) and code else None), err.get("message")
    if isinstance(err, str) and err:
        return err, body.get("error_description")
    return None, None


# ---------------------------------------------------------------------------
# Device-code enrollment
# ---------------------------------------------------------------------------

async def start_device_code(
    *, client: Optional[httpx.AsyncClient] = None
) -> DeviceCodeStart:
    """Begin the device-code flow: mint PKCE, request a user-code.

    POSTs the client id + PKCE S256 challenge + scope as JSON (the endpoint
    rejects form encoding) and returns the ``user_code`` (admin types it at the
    verification URL) plus the ``device_auth_id`` handle — stored in
    ``device_code`` — and ``code_verifier`` the caller carries into the poll
    step. The endpoint answers ``device_auth_id`` / string ``interval`` /
    ISO ``expires_at`` rather than the RFC-8628 field names.
    """
    verifier, challenge = generate_pkce()
    resp = await _post(
        client,
        DEVICE_USERCODE_URL,
        json_body={
            "client_id": client_id(),
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    body = _safe_json(resp)
    if resp.status_code >= 400:
        code, message = _error_code(body)
        raise CodexAuthError(code or f"http_{resp.status_code}", message)
    device_code = body.get("device_auth_id") or body.get("device_code")
    user_code = body.get("user_code")
    if not device_code or not user_code:
        raise CodexAuthError("invalid_usercode_response",
                             "usercode response missing device_auth_id/user_code")
    expires_in = 900
    if body.get("expires_in"):
        expires_in = int(body["expires_in"])
    elif body.get("expires_at"):
        try:
            exp = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            expires_in = max(0, int((exp - datetime.now(timezone.utc)).total_seconds()))
        except ValueError:
            pass
    return DeviceCodeStart(
        device_code=device_code,
        user_code=user_code,
        verification_uri=body.get("verification_uri") or DEVICE_VERIFICATION_URL,
        code_verifier=verifier,
        interval=int(body.get("interval") or 5),
        expires_in=expires_in,
    )


async def poll_device_code(
    device_code: str,
    code_verifier: str,
    user_code: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> TokenBundle:
    """One poll step. Raises :class:`CodexAuthPending` until authorized.

    Posts JSON ``{client_id, device_auth_id, user_code}`` to the poll endpoint
    (``device_code`` holds the ``device_auth_id`` from the start step). Errors
    come back as a nested object whose ``code`` is e.g.
    ``deviceauth_authorization_pending`` → pending; anything else → terminal.
    On authorization the poll returns either the tokens directly, or an
    authorization code we exchange at ``/oauth/token`` with the PKCE verifier
    (per F3). Either way a full :class:`TokenBundle` (with ``account_id``
    decoded from the id_token) comes back.
    """
    resp = await _post(
        client,
        DEVICE_TOKEN_URL,
        json_body={
            "client_id": client_id(),
            "device_auth_id": device_code,
            "user_code": user_code,
        },
    )
    body = _safe_json(resp)
    err_code, err_message = _error_code(body)
    if err_code:
        if "pending" in err_code:
            raise CodexAuthPending(slow_down=False)
        if "slow_down" in err_code:
            raise CodexAuthPending(slow_down=True)
        raise CodexAuthError(err_code, err_message)
    if resp.status_code >= 400:
        raise CodexAuthError(f"http_{resp.status_code}")

    # Authorized. Some deployments return tokens straight from the poll; the F3
    # path returns an authorization code we then exchange with the PKCE verifier.
    if body.get("access_token"):
        return _bundle_from_token_response(body)

    code = body.get("authorization_code") or body.get("code")
    if not code:
        raise CodexAuthError("invalid_poll_response",
                             "authorized poll returned neither tokens nor a code")
    return await _exchange_authorization_code(code, code_verifier, client=client)


async def _exchange_authorization_code(
    code: str,
    code_verifier: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> TokenBundle:
    """PKCE authorization-code exchange → the initial token bundle (F3)."""
    resp = await _post(
        client,
        TOKEN_URL,
        data={
            "client_id": client_id(),
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": DEVICE_CALLBACK_URL,
        },
    )
    if resp.status_code >= 400:
        body = _safe_json(resp)
        raise CodexAuthError(body.get("error", f"http_{resp.status_code}"),
                             body.get("error_description"))
    return _bundle_from_token_response(_safe_json(resp))


def _bundle_from_token_response(
    body: dict, *, prior_refresh_token: Optional[str] = None
) -> TokenBundle:
    """Shape a token endpoint JSON body into a :class:`TokenBundle`.

    The refresh endpoint may omit a rotated ``refresh_token`` on some responses;
    ``prior_refresh_token`` is the fallback so we never drop the credential (the
    normal case DOES rotate and this fallback is not hit).
    """
    access_token = body.get("access_token")
    if not access_token:
        raise CodexAuthError("invalid_token_response",
                             "token response missing access_token")
    refresh_token = body.get("refresh_token") or prior_refresh_token
    if not refresh_token:
        raise CodexAuthError("invalid_token_response",
                             "token response missing refresh_token")
    id_token = body.get("id_token")
    return TokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        account_id=extract_account_id(id_token),
    )


# ---------------------------------------------------------------------------
# Refresh (ROTATING refresh token)
# ---------------------------------------------------------------------------

async def refresh_tokens(
    refresh_token: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> TokenBundle:
    """Refresh the access token; returns the ROTATED bundle (F3).

    POST JSON ``{client_id, grant_type:"refresh_token", refresh_token}``. The
    refresh token rotates — the returned bundle carries the NEW refresh token,
    and the caller MUST persist the whole bundle atomically. Permanent failures
    (``refresh_token_expired`` / ``_reused`` / ``_invalidated``) raise a
    :class:`CodexAuthError` whose ``is_permanent`` is ``True``.

    Serialization of concurrent refreshes is the CALLER's responsibility (the
    per-provider lock in ``codex_token_refresh``) — this function is pure I/O.
    """
    resp = await _post(
        client,
        TOKEN_URL,
        json_body={
            "client_id": client_id(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    if resp.status_code >= 400:
        body = _safe_json(resp)
        raise CodexAuthError(body.get("error", f"http_{resp.status_code}"),
                             body.get("error_description"))
    # When the response id_token is omitted the account id cannot be re-derived
    # here; the caller preserves the previously stored account id in that case.
    return _bundle_from_token_response(_safe_json(resp),
                                       prior_refresh_token=refresh_token)


async def revoke_token(
    token: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> bool:
    """Best-effort token revoke (on provider delete). Never raises.

    Returns ``True`` on a 2xx, ``False`` otherwise (including any transport
    error) — deletion must never be blocked by a revoke failure.
    """
    try:
        resp = await _post(
            client,
            REVOKE_URL,
            data={"client_id": client_id(), "token": token},
        )
        return 200 <= resp.status_code < 300
    except Exception:  # noqa: BLE001 — revoke is best-effort
        logger.debug("codex_oauth: revoke failed", exc_info=True)
        return False
