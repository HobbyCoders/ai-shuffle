"""OAuth sign-in for the outside services a plugin declares.

A plugin declares a service under ``extensions["ai.shuffle"].services`` and,
optionally, how the owner signs in to it (``auth``: OAuth 2.0 authorization
code with PKCE, public endpoints and scope names only; validated by
``fluid.context.plugin_authoring.check_service_auth``). This module is the
provider-neutral core behind that declaration:

* :func:`config_from_manifest` reads and re-validates the declaration of one
  installed plugin's service (marketplace manifests are never trusted as
  written).
* :func:`start_sign_in` builds the authorize URL with a fresh ``state`` and
  PKCE verifier; :func:`exchange_code`, :func:`refresh_tokens` and
  :func:`revoke_token` talk to the provider's token and revocation endpoints.
  HTTP goes through an injectable ``httpx.AsyncClient`` so tests fake it.
* Encrypted storage of the owner's OAuth client (:func:`save_client`) and of
  the tokens (:func:`save_tokens`), keyed by plugin id and service name, in
  ``system_settings`` under ``plugin_oauth:<worker>:``. Like
  ``app.core.chat_vault``: only ``enc:`` envelopes are written, each bound to
  its own key, the namespace is worker-local and never replicated
  (``app.network.policy``), and nothing here decrypts for a model.
  :func:`status` reports a sign-in without any secret.

Errors are :class:`PluginOAuthError` with a fixed reason and fixed wording.
Provider response bodies, codes, tokens and client secrets never reach a log,
an exception message or a value meant for the model.

Entry points for the sign-in routes and ``ChatHttpRequest`` (slice 2):
:func:`installed_config`, :func:`config_from_manifest`, :func:`start_sign_in`,
:func:`complete_sign_in`, :func:`access_token_for_use`, :func:`sign_out`,
:func:`status`, and the client functions :func:`save_client`,
:func:`delete_client`.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from app.core import encryption
from app.core.config import settings
from app.db import database

SETTING_PREFIX = "plugin_oauth:"
# Refresh an access token this many seconds before it expires.
REFRESH_MARGIN = 60
MAX_RESPONSE_BYTES = 64 * 1024
MAX_TOKEN_CHARS = 16_384
HTTP_TIMEOUT = 30

_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}@[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_CLIENT_ID_RE = re.compile(r"^[\x21-\x7e]{1,512}$")

_MESSAGES = {
    "not_installed": "That plugin is not installed on this worker.",
    "plugin_off": "Turn this plugin on in Settings > Plugins before signing in to its services.",
    "not_declared": "This plugin does not declare a sign-in for that service.",
    "invalid_declaration": "This plugin's sign-in declaration is not valid, so it cannot be used.",
    "invalid_client": "The OAuth client ID or secret is not in a usable form.",
    "invalid_redirect": "The sign-in return address must be an https:// address (or http on this computer).",
    "not_configured": "Enter the OAuth client for this service before signing in.",
    "needs_sign_in": "Sign in to this service again.",
    "refused": "The service refused the sign-in request.",
    "unavailable": "The service could not be reached. Try again later.",
    "bad_response": "The service sent a sign-in response this app cannot use.",
    "storage_locked": "Secure credential storage is locked on this worker.",
    "storage": "The saved sign-in could not be read on this worker.",
}


class PluginOAuthError(Exception):
    """A refused OAuth step. ``reason`` is one of a fixed set; the message is
    fixed wording for that reason and never carries provider text or a secret."""

    def __init__(self, reason: str):
        if reason not in _MESSAGES:
            reason = "bad_response"
        self.reason = reason
        super().__init__(_MESSAGES[reason])


# ─── declaration ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthConfig:
    """One plugin service's validated sign-in declaration."""

    plugin_id: str
    service: str
    service_url: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    revoke_url: str = ""
    access: str = "read"  # declared: "read" (GET/HEAD) or "write"


def _check_ids(plugin_id: Any, service: Any) -> None:
    if not isinstance(plugin_id, str) or not _PLUGIN_ID_RE.match(plugin_id):
        raise PluginOAuthError("not_declared")
    if not isinstance(service, str) or not _SERVICE_RE.match(service):
        raise PluginOAuthError("not_declared")


def config_from_manifest(plugin_id: str, manifest: Mapping[str, Any], service: str) -> OAuthConfig:
    """The sign-in declared for ``service`` in an installed plugin's manifest.

    The block is validated again with the same rules as a new draft, because a
    marketplace manifest reaches disk unchecked. Raises
    :class:`PluginOAuthError` (``not_declared`` or ``invalid_declaration``).
    """
    from fluid.context.plugin_authoring import PluginDraftError, check_service_auth

    _check_ids(plugin_id, service)
    extensions = manifest.get("extensions") if isinstance(manifest, Mapping) else None
    shuffle = extensions.get("ai.shuffle") if isinstance(extensions, Mapping) else None
    services = shuffle.get("services") if isinstance(shuffle, Mapping) else None
    entries = [
        s for s in (services if isinstance(services, list) else [])
        if isinstance(s, dict) and s.get("name") == service
    ]
    if not entries or "auth" not in entries[0]:
        raise PluginOAuthError("not_declared")
    if len(entries) > 1:
        raise PluginOAuthError("invalid_declaration")
    url = entries[0].get("url")
    try:
        auth = check_service_auth(service, url if isinstance(url, str) else "", entries[0]["auth"])
    except PluginDraftError:
        raise PluginOAuthError("invalid_declaration") from None
    return OAuthConfig(
        plugin_id=plugin_id, service=service, service_url=str(url),
        authorize_url=auth["authorize_url"], token_url=auth["token_url"],
        scopes=tuple(auth["scopes"]), revoke_url=auth.get("revoke_url", ""),
        access="write" if entries[0].get("access") == "write" else "read",
    )


def _installed_manifest(plugin_id: str, *, require_on: bool) -> Mapping[str, Any]:
    from pathlib import Path

    from app.core.plugin_service import get_plugin_service, plugin_record

    if not isinstance(plugin_id, str) or not _PLUGIN_ID_RE.match(plugin_id):
        raise PluginOAuthError("not_installed")
    try:
        details = get_plugin_service().get_plugin_details(plugin_id)
    except Exception:  # noqa: BLE001 - an unreadable install reads as missing
        details = None
    install_path = getattr(details, "install_path", "") if details is not None else ""
    if not install_path:
        raise PluginOAuthError("not_installed")
    # Enabled AND approved: an agent's install or an authored draft waiting
    # for review (approval "pending") never reaches a sign-in.
    if require_on and (getattr(details, "enabled", False) is not True
                       or getattr(details, "approval", None) == "pending"):
        raise PluginOAuthError("plugin_off")
    name, _, source = plugin_id.rpartition("@")
    record = plugin_record(Path(install_path), name, source, getattr(details, "version", None))
    manifest = getattr(record, "manifest", None)
    if not isinstance(manifest, Mapping):
        raise PluginOAuthError("not_declared")
    return manifest


def installed_config(plugin_id: str, service: str, *, require_on: bool = True) -> OAuthConfig:
    """The sign-in an installed (user-scope) plugin declares for ``service``,
    read from the plugin as it is on disk now.

    With ``require_on`` (the default) the plugin must be switched on and
    approved. Refuses ``not_installed``, ``plugin_off``, ``not_declared`` or
    ``invalid_declaration``. Blocking: call through ``run_sync``.
    """
    return config_from_manifest(plugin_id, _installed_manifest(plugin_id, require_on=require_on), service)


def plugin_ready(plugin_id: str) -> bool:
    """Whether an installed plugin is switched on and approved, so its
    sign-ins can start and its grants can be used."""
    try:
        _installed_manifest(plugin_id, require_on=True)
    except PluginOAuthError as exc:
        return exc.reason != "plugin_off"
    return True


def installed_services(plugin_id: str) -> list[str]:
    """Names of the services an installed plugin declares a sign-in for, in
    manifest order, whether or not the declaration is valid."""
    manifest = _installed_manifest(plugin_id, require_on=False)
    extensions = manifest.get("extensions")
    shuffle = extensions.get("ai.shuffle") if isinstance(extensions, Mapping) else None
    services = shuffle.get("services") if isinstance(shuffle, Mapping) else None
    names = [
        s["name"] for s in (services if isinstance(services, list) else [])
        if isinstance(s, dict) and "auth" in s and isinstance(s.get("name"), str) and _SERVICE_RE.match(s["name"])
    ]
    return list(dict.fromkeys(names))[:20]


# ─── client and tokens ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthClient:
    """The owner's OAuth client for one plugin service. Internal only."""

    client_id: str
    client_secret: str = field(default="", repr=False)


@dataclass(frozen=True)
class TokenSet:
    """Tokens from the provider. Internal only; never returned to a model."""

    access_token: str = field(repr=False)
    refresh_token: str = field(default="", repr=False)
    expires_at: float | None = None  # epoch seconds
    scopes: tuple[str, ...] = ()  # granted


@dataclass(frozen=True)
class StoredTokens:
    """A saved sign-in, with what it was requested for. Internal only."""

    tokens: TokenSet
    requested_scopes: tuple[str, ...]
    token_url: str
    client_id: str
    obtained_at: float


@dataclass(frozen=True)
class AuthorizationRequest:
    """A started sign-in. ``url`` opens in the owner's browser; ``state`` and
    ``code_verifier`` stay on the worker until the callback (slice 2 binds
    them to the browser that started it)."""

    url: str
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)
    redirect_uri: str
    scopes: tuple[str, ...]


def _check_client(client_id: Any, client_secret: Any) -> None:
    if not isinstance(client_id, str) or not _CLIENT_ID_RE.match(client_id):
        raise PluginOAuthError("invalid_client")
    if not isinstance(client_secret, str) or len(client_secret) > 1024 or (
        client_secret and not re.fullmatch(r"[\x21-\x7e]+", client_secret)
    ):
        raise PluginOAuthError("invalid_client")


def _check_redirect(uri: Any) -> None:
    if not isinstance(uri, str) or len(uri) > 2048:
        raise PluginOAuthError("invalid_redirect")
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        raise PluginOAuthError("invalid_redirect") from None
    loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not parts.hostname or parts.fragment or parts.username or parts.password
            or not (parts.scheme == "https" or (parts.scheme == "http" and loopback))):
        raise PluginOAuthError("invalid_redirect")


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256 code challenge for ``verifier``."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")


def start_sign_in(config: OAuthConfig, client: OAuthClient, redirect_uri: str) -> AuthorizationRequest:
    """Build the provider's authorize URL with a fresh ``state`` and a PKCE
    (S256) verifier. Pure: nothing is stored and no request is made.

    Refuses (:class:`PluginOAuthError`) a malformed client id
    (``invalid_client``) or a redirect that is not https or loopback http
    (``invalid_redirect``).
    """
    _check_client(client.client_id, client.client_secret)
    _check_redirect(redirect_uri)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)  # 64 unreserved characters
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    if config.scopes:
        params["scope"] = " ".join(config.scopes)
    return AuthorizationRequest(
        url=config.authorize_url + "?" + urlencode(params), state=state,
        code_verifier=verifier, redirect_uri=redirect_uri, scopes=config.scopes,
    )


def _parse_tokens(response: httpx.Response, requested: tuple[str, ...], *, now: float,
                  previous: TokenSet | None = None) -> TokenSet:
    if response.status_code != 200:
        code = response.status_code
        raise PluginOAuthError("refused" if 400 <= code < 500 else "unavailable" if code >= 500 else "bad_response")
    body = response.content
    if len(body) > MAX_RESPONSE_BYTES:
        raise PluginOAuthError("bad_response")
    ctype = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        if ctype in ("application/x-www-form-urlencoded", "text/plain"):
            data: Any = dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
        else:
            data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PluginOAuthError("bad_response") from None
    if not isinstance(data, dict):
        raise PluginOAuthError("bad_response")
    if "error" in data:
        raise PluginOAuthError("refused")
    access = data.get("access_token")
    if not isinstance(access, str) or not access or len(access) > MAX_TOKEN_CHARS:
        raise PluginOAuthError("bad_response")
    token_type = data.get("token_type", "bearer")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise PluginOAuthError("bad_response")
    refresh = data.get("refresh_token") or (previous.refresh_token if previous else "")
    if not isinstance(refresh, str) or len(refresh) > MAX_TOKEN_CHARS:
        raise PluginOAuthError("bad_response")
    expires_at = None
    expires_in = data.get("expires_in")
    if expires_in not in (None, ""):
        try:
            seconds = int(expires_in)
        except (TypeError, ValueError):
            raise PluginOAuthError("bad_response") from None
        if not 0 <= seconds <= 10 * 365 * 86400:
            raise PluginOAuthError("bad_response")
        expires_at = now + seconds
    scope = data.get("scope")
    if isinstance(scope, str) and scope.strip():
        # GitHub separates with commas; RFC 6749 with spaces.
        granted = tuple(dict.fromkeys(s for s in re.split(r"[\s,]+", scope) if s))[:100]
    elif isinstance(scope, list) and all(isinstance(s, str) for s in scope):
        granted = tuple(dict.fromkeys(scope))[:100]
    else:
        # RFC 6749 5.1: an omitted scope means what was requested (or, on a
        # refresh, what the previous grant had).
        granted = previous.scopes if previous else requested
    return TokenSet(access_token=access, refresh_token=refresh, expires_at=expires_at, scopes=granted)


async def _post(url: str, data: dict[str, str], http: httpx.AsyncClient | None) -> httpx.Response:
    headers = {"Accept": "application/json"}
    try:
        if http is not None:
            return await http.post(url, data=data, headers=headers, follow_redirects=False)
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as own:
            return await own.post(url, data=data, headers=headers)
    except httpx.HTTPError:
        # Never chain: an httpx error can carry the request (and its form body).
        raise PluginOAuthError("unavailable") from None


def _client_fields(client: OAuthClient) -> dict[str, str]:
    # client_secret_post: the form most providers accept.
    fields = {"client_id": client.client_id}
    if client.client_secret:
        fields["client_secret"] = client.client_secret
    return fields


async def exchange_code(
    config: OAuthConfig, client: OAuthClient, *, code: str, code_verifier: str, redirect_uri: str,
    http: httpx.AsyncClient | None = None, now: float | None = None,
) -> TokenSet:
    """Exchange an authorization code for tokens at ``config.token_url``.

    Refuses with ``refused`` (4xx or an OAuth ``error``), ``unavailable``
    (network or 5xx) or ``bad_response`` (no usable access token).
    """
    _check_client(client.client_id, client.client_secret)
    if not isinstance(code, str) or not code or len(code) > 4096:
        raise PluginOAuthError("refused")
    response = await _post(config.token_url, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "code_verifier": code_verifier, **_client_fields(client),
    }, http)
    return _parse_tokens(response, config.scopes, now=time.time() if now is None else now)


async def refresh_tokens(
    config: OAuthConfig, client: OAuthClient, previous: TokenSet, *,
    http: httpx.AsyncClient | None = None, now: float | None = None,
) -> TokenSet:
    """Use ``previous.refresh_token`` for a new access token. A response
    without a new refresh token keeps the old one. Refuses like
    :func:`exchange_code`; ``needs_sign_in`` when there is no refresh token."""
    if not previous.refresh_token:
        raise PluginOAuthError("needs_sign_in")
    response = await _post(config.token_url, {
        "grant_type": "refresh_token", "refresh_token": previous.refresh_token,
        **_client_fields(client),
    }, http)
    return _parse_tokens(response, config.scopes, now=time.time() if now is None else now, previous=previous)


async def revoke_token(
    config: OAuthConfig, client: OAuthClient, token: str, *, token_type_hint: str = "refresh_token",
    http: httpx.AsyncClient | None = None,
) -> bool:
    """RFC 7009 revocation at ``config.revoke_url``. ``False`` when the plugin
    declares no revocation endpoint or the provider did not accept it; never
    raises for a provider answer."""
    if not config.revoke_url or not token:
        return False
    try:
        response = await _post(config.revoke_url, {
            "token": token, "token_type_hint": token_type_hint, **_client_fields(client),
        }, http)
    except PluginOAuthError:
        return False
    return response.status_code == 200


# ─── encrypted storage ───────────────────────────────────────────────────────


def _key(plugin_id: str, service: str, kind: str) -> str:
    _check_ids(plugin_id, service)
    digest = hashlib.sha256(json.dumps([plugin_id, service]).encode()).hexdigest()
    return f"{SETTING_PREFIX}{settings.effective_instance_id}:{digest}:{kind}"


def _seal(key: str, payload: dict[str, Any]) -> None:
    if not encryption.is_encryption_ready():
        raise PluginOAuthError("storage_locked")
    try:
        ciphertext = encryption.encrypt_value(json.dumps({"binding": key, **payload}))
    except RuntimeError:
        raise PluginOAuthError("storage_locked") from None
    if not encryption.is_encrypted(ciphertext):
        raise PluginOAuthError("storage_locked")
    database.set_system_setting(key, ciphertext)


def _open(key: str) -> dict[str, Any] | None:
    raw = database.get_system_setting(key)
    if not raw:
        return None
    if not encryption.is_encryption_ready():
        raise PluginOAuthError("storage_locked")
    if not encryption.is_encrypted(raw):
        raise PluginOAuthError("storage")
    try:
        data = json.loads(encryption.decrypt_value(raw))
    except (ValueError, RuntimeError, TypeError):
        # Wrong key, corrupt envelope or not JSON (binascii and Unicode
        # errors are ValueErrors). Never chained: no ciphertext in tracebacks.
        raise PluginOAuthError("storage") from None
    if not isinstance(data, dict) or data.get("binding") != key:
        raise PluginOAuthError("storage")
    return data


def save_client(plugin_id: str, service: str, client_id: str, client_secret: str = "") -> None:
    """Save the owner's OAuth client for a plugin service, encrypted.

    A different client id removes the saved tokens, which were issued to the
    old client. Refuses ``invalid_client`` or ``storage_locked``.
    """
    _check_client(client_id, client_secret)
    key = _key(plugin_id, service, "client")
    try:
        previous = _open(key)
    except PluginOAuthError as exc:
        if exc.reason == "storage_locked":
            raise
        previous = None
    _seal(key, {"client_id": client_id, "client_secret": client_secret})
    if not previous or previous.get("client_id") != client_id:
        database.delete_system_setting(_key(plugin_id, service, "tokens"))


def load_client(plugin_id: str, service: str) -> OAuthClient | None:
    """The saved client, or ``None``. Internal only: holds the secret."""
    data = _open(_key(plugin_id, service, "client"))
    if data is None:
        return None
    client_id, client_secret = data.get("client_id"), data.get("client_secret", "")
    try:
        _check_client(client_id, client_secret)
    except PluginOAuthError:
        raise PluginOAuthError("storage") from None
    return OAuthClient(client_id=str(client_id), client_secret=str(client_secret))


def delete_client(plugin_id: str, service: str) -> bool:
    """Remove the client and the tokens issued to it. ``True`` when a client was saved."""
    database.delete_system_setting(_key(plugin_id, service, "tokens"))
    return database.delete_system_setting(_key(plugin_id, service, "client"))


def save_tokens(config: OAuthConfig, client: OAuthClient, tokens: TokenSet,
                *, now: float | None = None) -> None:
    """Save a sign-in, encrypted, with the scopes it was requested for and the
    token endpoint and client that issued it."""
    _seal(_key(config.plugin_id, config.service, "tokens"), {
        "access_token": tokens.access_token, "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at, "scopes": list(tokens.scopes),
        "requested_scopes": list(config.scopes), "token_url": config.token_url,
        "client_id": client.client_id, "obtained_at": time.time() if now is None else now,
    })


def load_tokens(plugin_id: str, service: str) -> StoredTokens | None:
    """The saved sign-in, or ``None``. Internal only: holds the tokens."""
    data = _open(_key(plugin_id, service, "tokens"))
    if data is None:
        return None
    try:
        expires = data.get("expires_at")
        return StoredTokens(
            tokens=TokenSet(
                access_token=str(data["access_token"]), refresh_token=str(data.get("refresh_token") or ""),
                expires_at=float(expires) if expires is not None else None,
                scopes=tuple(str(s) for s in data.get("scopes") or ()),
            ),
            requested_scopes=tuple(str(s) for s in data.get("requested_scopes") or ()),
            token_url=str(data["token_url"]), client_id=str(data["client_id"]),
            obtained_at=float(data.get("obtained_at") or 0),
        )
    except (KeyError, TypeError, ValueError):
        raise PluginOAuthError("storage") from None


def delete_tokens(plugin_id: str, service: str) -> bool:
    """Remove the saved sign-in. ``True`` when one was saved."""
    return database.delete_system_setting(_key(plugin_id, service, "tokens"))


def _stale_reason(config: OAuthConfig, stored: StoredTokens, client: OAuthClient | None) -> str:
    """Why a saved sign-in no longer covers the current declaration, or ``""``."""
    if set(config.scopes) - set(stored.requested_scopes):
        return "scopes_widened"
    if stored.token_url != config.token_url:
        return "endpoint_changed"
    if client is None or client.client_id != stored.client_id:
        return "client_changed"
    return ""


def status(config: OAuthConfig, *, now: float | None = None) -> dict[str, Any]:
    """A sign-in's state for Settings or a tool, with no secret in it.

    ``state`` is one of ``not_configured`` (no client), ``signed_out``,
    ``signed_in`` or ``needs_sign_in``; ``reason`` says why a sign-in is
    needed again (``scopes_widened``, ``endpoint_changed``,
    ``client_changed``, ``expired``, ``unreadable``). ``scopes`` are the
    granted scopes, ``missing_scopes`` the declared ones the sign-in lacks.
    Raises only ``storage_locked``.
    """
    now = time.time() if now is None else now
    out: dict[str, Any] = {
        "plugin_id": config.plugin_id, "service": config.service, "state": "not_configured",
        "reason": "", "client_configured": False, "requested_scopes": list(config.scopes),
        "scopes": [], "missing_scopes": [], "expires_at": None, "can_refresh": False,
    }
    try:
        client = load_client(config.plugin_id, config.service)
    except PluginOAuthError as exc:
        if exc.reason == "storage_locked":
            raise
        out.update(state="needs_sign_in", reason="unreadable")
        return out
    out["client_configured"] = client is not None
    try:
        stored = load_tokens(config.plugin_id, config.service)
    except PluginOAuthError as exc:
        if exc.reason == "storage_locked":
            raise
        out.update(state="needs_sign_in", reason="unreadable")
        return out
    if stored is None:
        out["state"] = "signed_out" if client is not None else "not_configured"
        return out
    tokens = stored.tokens
    out.update(
        scopes=list(tokens.scopes),
        missing_scopes=[s for s in config.scopes if s not in tokens.scopes],
        expires_at=tokens.expires_at, can_refresh=bool(tokens.refresh_token),
    )
    reason = _stale_reason(config, stored, client)
    if not reason and tokens.expires_at is not None and tokens.expires_at <= now and not tokens.refresh_token:
        reason = "expired"
    out.update(state="needs_sign_in" if reason else "signed_in", reason=reason)
    return out


# ─── higher-level steps for the routes and ChatHttpRequest (slice 2) ─────────

_locks: dict[str, asyncio.Lock] = {}


def _lock(config: OAuthConfig) -> asyncio.Lock:
    key = _key(config.plugin_id, config.service, "tokens")
    lock = _locks.get(key)
    if lock is None:
        if len(_locks) >= 512:
            for stale in [k for k, v in _locks.items() if not v.locked()]:
                del _locks[stale]
        lock = _locks[key] = asyncio.Lock()
    return lock


async def _client_or_refuse(config: OAuthConfig) -> OAuthClient:
    from app.db.async_database import run_sync

    client = await run_sync(load_client, config.plugin_id, config.service)
    if client is None:
        raise PluginOAuthError("not_configured")
    return client


async def complete_sign_in(
    config: OAuthConfig, request: AuthorizationRequest, code: str, *,
    http: httpx.AsyncClient | None = None, now: float | None = None,
) -> dict[str, Any]:
    """Exchange the callback's ``code`` for the sign-in ``request`` started,
    save the tokens encrypted and return :func:`status` (no secrets).

    The caller has already matched ``state`` and the browser binding. Refuses
    ``not_configured`` when the client was removed meanwhile, and whatever
    :func:`exchange_code` refuses.
    """
    from app.db.async_database import run_sync

    client = await _client_or_refuse(config)
    tokens = await exchange_code(
        config, client, code=code, code_verifier=request.code_verifier,
        redirect_uri=request.redirect_uri, http=http, now=now,
    )
    async with _lock(config):
        await run_sync(save_tokens, config, client, tokens, now=now)
    return await run_sync(status, config, now=now)


async def access_token_for_use(
    config: OAuthConfig, *, http: httpx.AsyncClient | None = None, now: float | None = None,
    force_refresh: bool = False,
) -> str:
    """The access token to attach to a request for this service. Internal
    only: the caller puts it in an ``Authorization`` header and never shows it.

    Refreshes under a per-connection lock when the token expires within
    :data:`REFRESH_MARGIN` seconds, or when ``force_refresh`` (after a 401).
    Refuses ``needs_sign_in`` when nothing is saved, the declaration widened
    its scopes or moved its token endpoint, the client changed, or the
    provider refused the refresh (the saved tokens are then removed);
    ``not_configured`` without a client; ``unavailable`` or ``bad_response``
    when the provider could not be reached or answered oddly (tokens kept).
    """
    from app.db.async_database import run_sync

    async with _lock(config):
        client = await _client_or_refuse(config)
        stored = await run_sync(load_tokens, config.plugin_id, config.service)
        if stored is None or _stale_reason(config, stored, client):
            raise PluginOAuthError("needs_sign_in")
        tokens = stored.tokens
        current = time.time() if now is None else now
        due = tokens.expires_at is not None and tokens.expires_at - REFRESH_MARGIN <= current
        if not (due or force_refresh):
            return tokens.access_token
        try:
            fresh = await refresh_tokens(config, client, tokens, http=http, now=current)
        except PluginOAuthError as exc:
            if exc.reason in ("refused", "needs_sign_in"):
                await run_sync(delete_tokens, config.plugin_id, config.service)
                raise PluginOAuthError("needs_sign_in") from None
            raise
        await run_sync(save_tokens, config, client, fresh, now=current)
        return fresh.access_token


async def sign_out(config: OAuthConfig, *, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Remove the saved sign-in, then revoke it at the provider when the plugin
    declares a revocation endpoint. The local tokens are gone even if the
    provider cannot be reached. Returns ``{"signed_out": bool, "revoked": bool}``.
    """
    from app.db.async_database import run_sync

    async with _lock(config):
        client = await run_sync(load_client, config.plugin_id, config.service)
        try:
            stored = await run_sync(load_tokens, config.plugin_id, config.service)
        except PluginOAuthError as exc:
            if exc.reason == "storage_locked":
                raise
            stored = None
        removed = await run_sync(delete_tokens, config.plugin_id, config.service)
    revoked = False
    if stored is not None and client is not None:
        token, hint = (
            (stored.tokens.refresh_token, "refresh_token") if stored.tokens.refresh_token
            else (stored.tokens.access_token, "access_token")
        )
        revoked = await revoke_token(config, client, token, token_type_hint=hint, http=http)
    return {"signed_out": removed, "revoked": revoked}
