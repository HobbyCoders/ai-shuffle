"""Owner sign-in to the outside services a plugin declares (OAuth 2.0 with PKCE).

The same shape as the Gmail connector flow (``app.api.connector_oauth``):
owner-only start, a single-use ``state`` that expires, an HttpOnly cookie that
binds the flow to the browser that started it, a callback that clears its
query from the request scope, and ``no-store`` pages that carry no code or
token. In addition, a pending sign-in is bound to the owner credential that
started it, and the callback re-reads the plugin: it must still be switched
on, approved and declare the same sign-in.

The return address is derived from the address the owner's browser used to
reach this worker (``request.base_url``) plus :data:`CALLBACK`, exactly as the
Gmail connector derives and checks its redirect URI. It must be ``https://``
or ``http://localhost``. With an owner-entered client the owner registers that
address with the provider; a plugin's shipped client works only from the
addresses its author registered; a client registered automatically
(``registration_url``) is registered for each address on its first sign-in.

The owner's OAuth client, registered clients and the tokens are stored by
:mod:`app.core.plugin_oauth`, encrypted and worker-local. No route returns a
client secret, a code or a token.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.api.auth import require_admin
from app.core import plugin_oauth as core
from app.core.plugin_oauth import AuthorizationRequest, OAuthConfig, PluginOAuthError
from app.db.async_database import run_sync

router = APIRouter(prefix="/api/v1/plugins/oauth", tags=["Plugins"])
logger = logging.getLogger(__name__)

CALLBACK = "/api/v1/plugins/oauth/callback"
COOKIE_PREFIX = "plugin_oauth_"
TTL = 600
MAX_PENDING = 64

_STATUS = {
    "not_installed": 404, "not_declared": 404, "plugin_off": 409, "invalid_declaration": 409,
    "not_configured": 409, "registration_failed": 409, "invalid_client": 400, "invalid_redirect": 400,
    "storage_locked": 409, "storage": 409,
}

INSECURE_TRANSPORT = ("Enter an OAuth client only from the AI Shuffle desktop app window, this worker's "
                      "HTTPS address, or a browser on this computer.")


@dataclass(repr=False)
class _Pending:
    expires: float  # time.monotonic()
    browser: str  # sha256 of the HttpOnly cookie value
    owner: str  # sha256 of the owner credential that started the sign-in
    flow_id: str
    config: OAuthConfig
    request: AuthorizationRequest


_pending: dict[str, _Pending] = {}


class ServiceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin: StrictStr = Field(min_length=1, max_length=260)
    service: StrictStr = Field(min_length=1, max_length=63)


class ClientInput(ServiceRef):
    client_id: StrictStr = Field(min_length=1, max_length=512)
    client_secret: StrictStr = Field(default="", max_length=1024)


def _refuse(exc: PluginOAuthError) -> HTTPException:
    return HTTPException(_STATUS.get(exc.reason, 502), str(exc))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _origin(uri: str) -> str:
    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}"


def redirect_uri(request: Request) -> str:
    """This worker's return address as the owner's browser reaches it, the same
    derivation as the Gmail connector's suggested redirect URI."""
    return str(request.base_url).rstrip("/") + CALLBACK


def _private_transport(request: Request) -> None:
    """A client secret travels only where the worker can vouch for the
    connection: the same rule as private access inputs (app.api.chat_access)."""
    from app.core.owner_auth import loopback_request
    if (request.url.scheme != "https"
            and request.scope.get("shuffle.forwarded_https") is not True
            and not loopback_request(request)):
        raise HTTPException(403, INSECURE_TRANSPORT)
    if getattr(request.state, "preview_grant", None) is not None:
        raise HTTPException(403, "Preview access cannot manage sign-ins.")


def optional_owner(request: Request) -> str | None:
    """The owner credential on this request, or ``None``. The callback is a
    public address; it only proceeds for the owner who started the sign-in."""
    try:
        return require_admin(request)
    except HTTPException:
        return None


def _service_status(plugin_id: str, service: str) -> dict:
    try:
        config = core.installed_config(plugin_id, service, require_on=False)
    except PluginOAuthError as exc:
        if exc.reason != "invalid_declaration":
            raise
        return {"plugin_id": plugin_id, "service": service, "state": "invalid", "reason": exc.reason,
                "client_configured": False, "client_source": "none", "client_id": "",
                "plugin_client_id": "", "registration_url": ""}
    result = core.status(config)
    try:
        client = core.load_client(plugin_id, service)
    except PluginOAuthError:
        client = None  # status() already reported an unreadable client
    result["client_id"] = client.client_id if client else ""
    result["access"] = config.access
    result["authorize_host"] = urlsplit(config.authorize_url).hostname or ""
    # What the plugin declares, as the owner approved it: its shipped public
    # client and its registration endpoint ("" when not declared).
    result["plugin_client_id"] = config.client_id
    result["registration_url"] = config.registration_url
    return result


def _plugin_status(plugin_id: str, service: str | None) -> dict:
    names = [service] if service else core.installed_services(plugin_id)
    return {"plugin_id": plugin_id, "enabled": core.plugin_ready(plugin_id),
            "services": [_service_status(plugin_id, name) for name in names]}


@router.get("/status")
async def sign_in_status(request: Request, plugin: str, service: str = "",
                         token: str = Depends(require_admin)):
    """Sign-in state of one service, or of every service the plugin declares a
    sign-in for. Never a secret."""
    try:
        result = await run_sync(_plugin_status, plugin, service or None)
    except PluginOAuthError as exc:
        raise _refuse(exc) from None
    result["redirect_uri"] = redirect_uri(request)
    return result


@router.put("/client")
async def save_client(request: Request, body: dict, token: str = Depends(require_admin)):
    """Save the owner's OAuth client for a plugin service, encrypted on this worker."""
    _private_transport(request)
    # Validate manually so a validation error never echoes the client secret.
    try:
        data = ClientInput.model_validate(body)
    except ValueError:
        raise HTTPException(422, "Send plugin, service, client_id and optionally client_secret.") from None

    def persist() -> dict:
        core.installed_config(data.plugin, data.service, require_on=False)
        core.save_client(data.plugin, data.service, data.client_id, data.client_secret)
        return _service_status(data.plugin, data.service)

    try:
        return await run_sync(persist)
    except PluginOAuthError as exc:
        raise _refuse(exc) from None


@router.delete("/client")
async def delete_client(plugin: str, service: str, token: str = Depends(require_admin)):
    """Remove the OAuth client and the sign-in issued to it. Works after the
    plugin is gone, so nothing is left behind."""
    try:
        deleted = await run_sync(core.delete_client, plugin, service)
    except PluginOAuthError as exc:
        raise _refuse(exc) from None
    return {"deleted": deleted}


@router.post("/start")
async def start(data: ServiceRef, request: Request, token: str = Depends(require_admin)):
    """Begin a sign-in. Returns the provider's ``authorization_url`` for the
    owner's browser; the flow expires after :data:`TTL` seconds."""
    address = redirect_uri(request)

    try:
        config = await run_sync(core.installed_config, data.plugin, data.service, require_on=True)
        # The owner's client, else the plugin's shipped public client, else one
        # registered for this return address (registered now when needed).
        client, _source = await core.client_for_sign_in(config, address)
        auth = core.start_sign_in(config, client, address)
    except PluginOAuthError as exc:
        raise _refuse(exc) from None
    now = time.monotonic()
    for key in [k for k, v in _pending.items() if v.expires <= now]:
        del _pending[key]
    if len(_pending) >= MAX_PENDING:
        raise HTTPException(429, "Too many sign-ins are waiting. Try again in a few minutes.")
    browser = secrets.token_urlsafe(32)
    flow_id = secrets.token_urlsafe(16)
    _pending[auth.state] = _Pending(now + TTL, _digest(browser), _digest(token), flow_id, config, auth)
    response = JSONResponse({"authorization_url": auth.url, "flow_id": flow_id,
                             "redirect_uri": address, "expires_in": TTL})
    response.set_cookie(COOKIE_PREFIX + auth.state[:16], browser, httponly=True,
                        secure=address.startswith("https:"), samesite="lax", max_age=TTL, path=CALLBACK)
    response.headers["Cache-Control"] = "no-store"
    return response


def _callback_page(state: str, origin: str, pending: _Pending, *, result: dict | None = None,
                   error: str | None = None) -> HTMLResponse:
    # No code, token or client secret enters this page.
    config = pending.config
    message = {"type": "plugin-oauth", "flow_id": pending.flow_id, "plugin_id": config.plugin_id,
               "service": config.service, "state": (result or {}).get("state"), "error": error}
    payload = json.dumps(message).replace("<", "\\u003c")
    target = json.dumps(origin).replace("<", "\\u003c")
    service = html.escape(config.service)
    title = f"Signed in to {service}" if error is None else f"Sign-in to {service} did not complete"
    detail = "" if error is None else f"<p>{html.escape(error)}</p>"
    response = HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>{title}</title>'
        f'<h1>{title}</h1>{detail}<p>You can close this window and return to AI Shuffle.</p>'
        f'<a href="/">Return to AI Shuffle</a>'
        f'<script>if(window.opener){{window.opener.postMessage({payload},{target});window.close();}}</script>')
    response.delete_cookie(COOKIE_PREFIX + state[:16], path=CALLBACK)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/callback")
async def callback(request: Request, state: str = "", code: str = "", error: str = "",
                   owner: str | None = Depends(optional_owner)):
    # A public OAuth return address, not an authorization bypass: the
    # single-use state, its HttpOnly browser binding and the owner credential
    # that started the sign-in are all required.
    request.scope["query_string"] = b""
    pending = _pending.get(state) if state else None
    if pending is not None and pending.expires <= time.monotonic():
        del _pending[state]
        pending = None
    browser = request.cookies.get(COOKIE_PREFIX + state[:16], "")
    if (pending is None or not browser or owner is None
            or not secrets.compare_digest(_digest(browser), pending.browser)
            or not secrets.compare_digest(_digest(owner), pending.owner)
            or _origin(str(request.url)) != _origin(pending.request.redirect_uri)):
        raise HTTPException(400, "This sign-in has expired, was already used, or belongs to another "
                                 "browser. Start it again from AI Shuffle.")
    # Consume before any await: a replayed callback finds nothing.
    del _pending[state]
    origin = _origin(pending.request.redirect_uri)
    if error or not code:
        return _callback_page(state, origin, pending,
                              error="The sign-in was cancelled at the service. Nothing was changed.")
    try:
        current = await run_sync(core.installed_config, pending.config.plugin_id, pending.config.service)
        if current != pending.config:
            raise PluginOAuthError("invalid_declaration")
        result = await core.complete_sign_in(current, pending.request, code)
    except PluginOAuthError as exc:
        return _callback_page(state, origin, pending, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - never log codes or provider bodies
        logger.warning("Plugin sign-in could not be completed: %s", type(exc).__name__)
        return _callback_page(state, origin, pending, error="The sign-in could not be saved. Try again.")
    return _callback_page(state, origin, pending, result=result)


@router.post("/sign-out")
async def sign_out(data: ServiceRef, token: str = Depends(require_admin)):
    """Remove the saved sign-in and revoke it at the provider when the plugin
    declares a revocation endpoint. A plugin that is gone or no longer declares
    the sign-in still has its local tokens removed."""
    try:
        config = await run_sync(core.installed_config, data.plugin, data.service, require_on=False)
    except PluginOAuthError as exc:
        if exc.reason not in ("not_installed", "not_declared", "invalid_declaration"):
            raise _refuse(exc) from None
        try:
            removed = await run_sync(core.delete_tokens, data.plugin, data.service)
        except PluginOAuthError as inner:
            raise _refuse(inner) from None
        return {"signed_out": removed, "revoked": False}
    try:
        return await core.sign_out(config)
    except PluginOAuthError as exc:
        raise _refuse(exc) from None
