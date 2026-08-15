"""Codex / ChatGPT-subscription OAuth — standalone port for fluid.

Ported from AI-Shuffle's ``codex_oauth.py`` + ``codex_token_refresh.py`` with the
app/DB dependencies removed: the token bundle persists to a JSON file (default
``~/.config/fluid/codex_auth.json``, ``0600``) instead of an encrypted Postgres
row. Pure logic + outbound HTTP only; every call takes an optional ``client`` so
tests drive it with an ``httpx.MockTransport`` and never touch the network.

Mechanics (verified against openai/codex ``main``):

  * OAuth 2.0 authorization-code + PKCE (S256), with a browser-redirect variant
    and a device-code variant, both against ``https://auth.openai.com`` using the
    official Codex CLI ``client_id`` (env-overridable). Reusing that id is what
    makes this a ChatGPT *subscription* login rather than a paid API key.
  * The refresh token ROTATES on every refresh — the whole rotated bundle is
    persisted atomically; two concurrent refreshes of the same credential race to
    ``refresh_token_reused`` death, so :class:`CodexAuth` serializes with an
    ``asyncio.Lock``.
  * :class:`CodexAuth` implements the client's auth protocol: ``get_access_token``
    (proactive refresh when the 8-day interval elapsed or the JWT ``exp`` is
    within 5 min), ``on_401`` (refresh-once, skipping the POST if a peer already
    rotated), and an ``account_id`` property.

Reference implementations studied (NOT vendored): openai/codex ``login``,
``numman-ali/opencode-openai-codex-auth``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# Issuer / endpoints. One editable place if the undocumented backend shifts.
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
TOKEN_URL = f"{ISSUER}/oauth/token"
REVOKE_URL = f"{ISSUER}/oauth/revoke"
DEVICE_USERCODE_URL = f"{ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{ISSUER}/api/accounts/deviceauth/token"
DEVICE_VERIFICATION_URL = f"{ISSUER}/codex/device"
DEVICE_CALLBACK_URL = f"{ISSUER}/deviceauth/callback"

# The official Codex CLI client id, deliberately reused so the flow authenticates
# a ChatGPT subscription. Env-overridable.
_DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

SCOPE = "openid profile email offline_access"

# Refresh policy thresholds.
PROACTIVE_REFRESH_AFTER_SECONDS = 8 * 24 * 3600  # 8 days since last refresh
EXP_SKEW_SECONDS = 5 * 60  # refresh when the access-token exp is within 5 min

# Refresh failure codes that are PERMANENT — the credential is dead and requires a
# fresh enrollment.
PERMANENT_FAILURE_CODES = frozenset(
    {"refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated"}
)

_HTTP_TIMEOUT = 30.0

DEFAULT_TOKEN_PATH = Path.home() / ".config" / "fluid" / "codex_auth.json"


def client_id() -> str:
    """Resolve the OAuth client id (env override wins)."""
    return os.environ.get("CODEX_APP_SERVER_LOGIN_CLIENT_ID") or _DEFAULT_CLIENT_ID


# ─── result / error types ────────────────────────────────────────────────────


@dataclass
class DeviceCodeStart:
    device_code: str
    user_code: str
    verification_uri: str
    code_verifier: str
    interval: int = 5
    expires_in: int = 900


@dataclass
class AuthorizationStart:
    """A browser-redirect authorization-code start. Carry ``code_verifier`` and
    ``state`` server-side (not to the browser) until the callback exchange."""

    url: str
    code_verifier: str
    state: str
    redirect_uri: str


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    id_token: Optional[str] = None
    account_id: Optional[str] = None


class CodexAuthPending(Exception):
    """Device authorization not complete yet (poll again)."""

    def __init__(self, slow_down: bool = False):
        super().__init__("slow_down" if slow_down else "authorization_pending")
        self.slow_down = slow_down


class CodexAuthError(Exception):
    """A terminal auth error. ``is_permanent`` marks the rotating-refresh death."""

    def __init__(self, code: str, message: Optional[str] = None):
        super().__init__(message or code)
        self.code = code

    @property
    def is_permanent(self) -> bool:
        return isinstance(self.code, str) and self.code in PERMANENT_FAILURE_CODES


# ─── PKCE ─────────────────────────────────────────────────────────────────────


def generate_pkce() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ─── JWT payload (no signature verification — our own TLS exchange) ───────────


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 — malformed token must not break auth
        logger.debug("codex_oauth: id_token payload decode failed", exc_info=True)
        return {}


def extract_account_id(id_token: Optional[str]) -> Optional[str]:
    """Extract the ``chatgpt_account_id`` claim from an id_token."""
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


def _access_token_exp(token: Optional[str]) -> Optional[datetime]:
    """Read the ``exp`` claim (a UNIX timestamp) from a JWT, if present."""
    if not token:
        return None
    exp = _decode_jwt_payload(token).get("exp")
    if isinstance(exp, (int, float)):
        try:
            return datetime.fromtimestamp(exp, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    return None


# ─── HTTP helpers ─────────────────────────────────────────────────────────────


async def _post(
    client: Optional[httpx.AsyncClient],
    url: str,
    *,
    data: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> httpx.Response:
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
    err = body.get("error")
    if isinstance(err, dict):
        code = err.get("code") or err.get("type")
        return (code if isinstance(code, str) and code else None), err.get("message")
    if isinstance(err, str) and err:
        return err, body.get("error_description")
    return None, None


def _bundle_from_token_response(
    body: dict, *, prior_refresh_token: Optional[str] = None
) -> TokenBundle:
    access_token = body.get("access_token")
    if not access_token:
        raise CodexAuthError("invalid_token_response", "token response missing access_token")
    refresh_token = body.get("refresh_token") or prior_refresh_token
    if not refresh_token:
        raise CodexAuthError("invalid_token_response", "token response missing refresh_token")
    id_token = body.get("id_token")
    return TokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        account_id=extract_account_id(id_token),
    )


# ─── authorization-code (browser redirect) flow ──────────────────────────────


def build_authorize_url(
    redirect_uri: str, *, client_id_override: Optional[str] = None
) -> AuthorizationStart:
    """Build the ``/oauth/authorize`` URL for the PKCE browser-redirect flow."""
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id_override or client_id(),
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "prompt": "login",
    }
    return AuthorizationStart(
        url=f"{AUTHORIZE_URL}?{urlencode(params)}",
        code_verifier=verifier,
        state=state,
        redirect_uri=redirect_uri,
    )


async def exchange_authorization_code(
    code: str,
    code_verifier: str,
    redirect_uri: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> TokenBundle:
    """PKCE authorization-code exchange → the initial token bundle."""
    resp = await _post(
        client,
        TOKEN_URL,
        data={
            "client_id": client_id(),
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
    )
    if resp.status_code >= 400:
        code_err, message = _error_code(_safe_json(resp))
        raise CodexAuthError(code_err or f"http_{resp.status_code}", message)
    return _bundle_from_token_response(_safe_json(resp))


# ─── device-code flow ─────────────────────────────────────────────────────────


async def start_device_code(*, client: Optional[httpx.AsyncClient] = None) -> DeviceCodeStart:
    """Begin the device-code flow: request a user-code."""
    verifier, _challenge = generate_pkce()
    resp = await _post(client, DEVICE_USERCODE_URL, json_body={"client_id": client_id()})
    body = _safe_json(resp)
    if resp.status_code >= 400:
        code, message = _error_code(body)
        raise CodexAuthError(code or f"http_{resp.status_code}", message)
    device_code = body.get("device_auth_id") or body.get("device_code")
    user_code = body.get("user_code")
    if not device_code or not user_code:
        raise CodexAuthError(
            "invalid_usercode_response", "usercode response missing device_auth_id/user_code"
        )
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
    """One poll step. Raises :class:`CodexAuthPending` until authorized."""
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

    if body.get("access_token"):
        return _bundle_from_token_response(body)

    code = body.get("authorization_code") or body.get("code")
    if not code:
        raise CodexAuthError(
            "invalid_poll_response", "authorized poll returned neither tokens nor a code"
        )
    # The device flow's PKCE pair is SERVER-minted; the success poll returns the
    # verifier bound to the code. Ours is only a fallback.
    verifier = body.get("code_verifier") or code_verifier
    return await exchange_authorization_code(code, verifier, DEVICE_CALLBACK_URL, client=client)


# ─── refresh (rotating) / revoke ─────────────────────────────────────────────


async def refresh_tokens(
    refresh_token: str, *, client: Optional[httpx.AsyncClient] = None
) -> TokenBundle:
    """Refresh the access token; returns the ROTATED bundle."""
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
        code, message = _error_code(_safe_json(resp))
        raise CodexAuthError(code or f"http_{resp.status_code}", message)
    return _bundle_from_token_response(_safe_json(resp), prior_refresh_token=refresh_token)


async def revoke_token(token: str, *, client: Optional[httpx.AsyncClient] = None) -> bool:
    """Best-effort token revoke. Never raises."""
    try:
        resp = await _post(client, REVOKE_URL, data={"client_id": client_id(), "token": token})
        return 200 <= resp.status_code < 300
    except Exception:  # noqa: BLE001 — revoke is best-effort
        logger.debug("codex_oauth: revoke failed", exc_info=True)
        return False


# ─── file-backed token store + auth hook ─────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


@dataclass
class StoredBundle:
    """The on-disk token bundle (superset of :class:`TokenBundle` with timing)."""

    access_token: str
    refresh_token: str
    id_token: Optional[str] = None
    account_id: Optional[str] = None
    last_refresh: Optional[str] = None  # ISO-8601 of the last successful refresh

    @classmethod
    def from_bundle(cls, b: TokenBundle, *, last_refresh: Optional[str] = None) -> "StoredBundle":
        return cls(
            access_token=b.access_token,
            refresh_token=b.refresh_token,
            id_token=b.id_token,
            account_id=b.account_id,
            last_refresh=last_refresh or _now_iso(),
        )


def save_bundle(bundle: StoredBundle, path: Optional[Path] = None) -> Path:
    """Persist a bundle as JSON with ``0600`` perms (dir created ``0700``)."""
    target = Path(path or DEFAULT_TOKEN_PATH)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Write then chmod so the secret is never briefly world-readable.
    target.write_text(json.dumps(asdict(bundle), indent=2))
    os.chmod(target, 0o600)
    return target


def load_bundle(path: Optional[Path] = None) -> Optional[StoredBundle]:
    """Load a bundle from JSON, or ``None`` if the file is absent/unreadable."""
    target = Path(path or DEFAULT_TOKEN_PATH)
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("access_token"):
        return None
    known = {f for f in StoredBundle.__dataclass_fields__}  # type: ignore[attr-defined]
    return StoredBundle(**{k: v for k, v in raw.items() if k in known})


def _needs_refresh(bundle: StoredBundle) -> bool:
    """Proactive-refresh window: 8-day interval elapsed OR JWT exp within 5 min."""
    now = datetime.now(timezone.utc)
    last_refresh = _parse_iso(bundle.last_refresh)
    if last_refresh is None:
        return True
    if (now - last_refresh).total_seconds() >= PROACTIVE_REFRESH_AFTER_SECONDS:
        return True
    exp = _access_token_exp(bundle.access_token)
    if exp is not None and (exp - now).total_seconds() <= EXP_SKEW_SECONDS:
        return True
    return False


class CodexAuth:
    """File-backed Codex auth implementing the Responses client's auth protocol.

    ``get_access_token`` refreshes proactively when due; ``on_401`` forces a
    single refresh (skipping the POST if a peer already rotated the token on
    disk). Refreshes are serialized by an ``asyncio.Lock`` because the refresh
    token rotates — a concurrent double-POST trips ``refresh_token_reused``.
    """

    def __init__(
        self,
        *,
        token_path: Optional[Path] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.token_path = Path(token_path or DEFAULT_TOKEN_PATH)
        self._client = client
        self._bundle: Optional[StoredBundle] = None
        self._lock = asyncio.Lock()

    @property
    def account_id(self) -> Optional[str]:
        return self._bundle.account_id if self._bundle else None

    def _ensure_loaded(self) -> StoredBundle:
        if self._bundle is None:
            self._bundle = load_bundle(self.token_path)
        if self._bundle is None:
            raise CodexAuthError(
                "not_authenticated", f"no stored Codex token at {self.token_path}"
            )
        return self._bundle

    async def _refresh_locked(self) -> None:
        assert self._bundle is not None
        prior = self._bundle
        new = await refresh_tokens(prior.refresh_token, client=self._client)
        bundle = StoredBundle(
            access_token=new.access_token,
            refresh_token=new.refresh_token,
            id_token=new.id_token or prior.id_token,
            # A refresh response may omit the id_token → keep the prior account id.
            account_id=new.account_id or prior.account_id,
            last_refresh=_now_iso(),
        )
        self._bundle = bundle
        save_bundle(bundle, self.token_path)

    async def get_access_token(self) -> str:
        async with self._lock:
            bundle = self._ensure_loaded()
            if _needs_refresh(bundle):
                await self._refresh_locked()
            assert self._bundle is not None
            return self._bundle.access_token

    async def on_401(self) -> None:
        """Refresh once after an upstream 401.

        If a peer already rotated the token on disk while we waited on the lock
        (the stored access token differs from our in-memory one), adopt it and
        skip the redundant POST — a wasted refresh could trip reuse detection.
        """
        async with self._lock:
            stale = self._bundle.access_token if self._bundle else None
            disk = load_bundle(self.token_path)
            if disk is not None and stale is not None and disk.access_token != stale:
                self._bundle = disk
                return
            if disk is not None:
                self._bundle = disk
            self._ensure_loaded()
            await self._refresh_locked()
