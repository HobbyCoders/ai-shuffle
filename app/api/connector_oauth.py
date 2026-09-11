"""Gmail account authorization: admin-initiated OAuth with PKCE and browser binding."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.api.auth import require_admin
from app.core import encryption
from app.db import database
from app.db.async_database import run_sync

router = APIRouter(prefix="/oauth/google", tags=["Connectors"])
CALLBACK = "/api/v1/connectors/oauth/google/callback"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"
SETTING = "connector_google_oauth"
TTL = 600
_pending: dict[str, dict] = {}


class GoogleApp(BaseModel):
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(min_length=1, max_length=1024)
    redirect_uri: str = Field(max_length=2048)


class GoogleOptions(BaseModel):
    labels: str = "INBOX"
    watch_inbox: bool = False
    enabled_action_categories: str | list[str] = ""
    poll_interval_seconds: int = Field(default=60, ge=15, le=86400)
    max_results: int = Field(default=20, ge=1, le=100)


class StartAccount(BaseModel):
    options: GoogleOptions = Field(default_factory=GoogleOptions)
    name: str = Field(default="", max_length=200)
    connector_id: str | None = None


def app_config() -> dict:
    raw = database.get_system_setting(SETTING)
    config = json.loads(raw) if raw else {}
    if config.get("client_secret"):
        config["client_secret"] = encryption.decrypt_value(config["client_secret"])
    return {
        "client_id": os.getenv("SHUFFLE_GOOGLE_CLIENT_ID") or config.get("client_id", ""),
        "client_secret": os.getenv("SHUFFLE_GOOGLE_CLIENT_SECRET") or config.get("client_secret", ""),
        "redirect_uri": os.getenv("SHUFFLE_GOOGLE_REDIRECT_URI") or config.get("redirect_uri", ""),
    }


def _origin(uri: str) -> str:
    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}"


def _validate_redirect(uri: str, origin: str) -> None:
    parsed = urlsplit(uri)
    if (_origin(uri) != origin or parsed.path != CALLBACK or parsed.query or parsed.fragment
            or parsed.username or parsed.password
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("Use this app's HTTPS origin (or localhost) and the displayed callback path for the Google redirect URI")


@router.get("/status")
async def status(request: Request, token: str = Depends(require_admin)):
    config = await run_sync(app_config)
    return {"configured": all(config.values()), "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"], "suggested_redirect_uri": str(request.base_url).rstrip("/") + CALLBACK,
            "environment_managed": any(os.getenv(k) for k in ("SHUFFLE_GOOGLE_CLIENT_ID", "SHUFFLE_GOOGLE_CLIENT_SECRET", "SHUFFLE_GOOGLE_REDIRECT_URI"))}


@router.put("/app")
async def configure(data: GoogleApp, request: Request, token: str = Depends(require_admin)):
    try:
        _validate_redirect(data.redirect_uri, str(request.base_url).rstrip("/"))
        if any(os.getenv(k) for k in ("SHUFFLE_GOOGLE_CLIENT_ID", "SHUFFLE_GOOGLE_CLIENT_SECRET", "SHUFFLE_GOOGLE_REDIRECT_URI")):
            raise ValueError("Google app settings are managed by this host's environment")
        def persist():
            config = data.model_dump()
            config["client_secret"] = encryption.encrypt_value(config["client_secret"])
            database.set_system_setting(SETTING, json.dumps(config))
        await run_sync(persist)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError:
        raise HTTPException(409, "Credential storage is locked. Complete local credential recovery before saving Google app settings.")
    return {"configured": True}


@router.post("/start")
async def start(data: StartAccount, request: Request, token: str = Depends(require_admin)):
    from app.core.connector_packages import require_installed
    try:
        config = await run_sync(app_config)
        if not all(config.values()):
            raise ValueError("An administrator must configure a Google OAuth app for this installation first")
        _validate_redirect(config["redirect_uri"], str(request.base_url).rstrip("/"))
        await run_sync(require_installed, "gmail")
        if data.connector_id:
            row = await run_sync(database.get_connector, data.connector_id)
            if not row or row["type"] != "gmail":
                raise ValueError("Gmail connection not found")
            from app.core.config import settings
            if row.get("runner_id") not in (None, settings.effective_instance_id):
                raise ValueError("Reconnect this account on the host that runs it")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    now = time.monotonic()
    for key in list(_pending):
        if _pending[key]["expires"] <= now:
            del _pending[key]
    if len(_pending) >= 128:
        raise HTTPException(429, "Too many pending sign-ins. Try again in a few minutes.")
    state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    _pending[state] = {"expires": now + TTL, "browser": hashlib.sha256(browser.encode()).hexdigest(),
                       "verifier": verifier, "app": config, "name": data.name, "connector_id": data.connector_id, "options": data.options.model_dump()}
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": config["client_id"], "redirect_uri": config["redirect_uri"], "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    response = JSONResponse({"authorization_url": url, "flow_id": state})
    response.set_cookie("gmail_oauth_" + state[:16], browser, httponly=True,
                        secure=config["redirect_uri"].startswith("https:"), samesite="lax", max_age=TTL, path=CALLBACK)
    response.headers["Cache-Control"] = "no-store"
    return response


async def _credentials(pending: dict, code: str) -> dict:
    app = pending["app"]
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": app["client_id"], "client_secret": app["client_secret"], "code": code,
            "code_verifier": pending["verifier"], "redirect_uri": app["redirect_uri"], "grant_type": "authorization_code"})
        response.raise_for_status()
        tokens = response.json()
        if SCOPE not in tokens.get("scope", "").split():
            raise ValueError("Google did not grant the required Gmail permission")
        if not tokens.get("refresh_token"):
            raise ValueError("Google did not return offline access. Remove this app's access in your Google account and connect again.")
        profile = await client.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                                   headers={"Authorization": "Bearer " + tokens["access_token"]})
        profile.raise_for_status()
        email = profile.json()["emailAddress"]
    return {"client_id": app["client_id"], "client_secret": app["client_secret"],
            "refresh_token": tokens["refresh_token"], "email_address": email, "oauth_managed": True}


def _callback_page(state: str, origin: str, *, connector_id: str | None = None, error: str | None = None):
    # No code, access token, refresh token, or client secret enters this page.
    message = {"type": "connector-oauth", "flow_id": state, "connector_id": connector_id, "error": error}
    payload = json.dumps(message).replace("<", "\u003c")
    target = json.dumps(origin).replace("<", "\u003c")
    title = "Google account connected" if connector_id else "Google sign-in did not complete"
    response = HTMLResponse(f'<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>{title}</title><h1>{title}</h1><p>You can close this window and return to Plugins.</p><a href="/">Return to AI Shuffle</a><script>if(window.opener){{window.opener.postMessage({payload},{target});window.close();}}</script>')
    response.delete_cookie("gmail_oauth_" + state[:16], path=CALLBACK)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/callback")
async def callback(request: Request, state: str = "", code: str = "", error: str = ""):
    # This is a public OAuth callback, not a local-owner authorization bypass.
    # Both a single-use state and its HttpOnly browser binding are required.
    request.scope["query_string"] = b""
    pending = _pending.get(state)
    browser = request.cookies.get("gmail_oauth_" + state[:16], "")
    if (not pending or pending["expires"] <= time.monotonic() or not browser
            or not secrets.compare_digest(hashlib.sha256(browser.encode()).hexdigest(), pending["browser"])
            or _origin(str(request.url)) != _origin(pending["app"]["redirect_uri"])):
        raise HTTPException(400, "This Google sign-in has expired or belongs to another browser. Start again from Plugins.")
    # Consume before any network or database await. Replays cannot create accounts.
    del _pending[state]
    origin = _origin(pending["app"]["redirect_uri"])
    if error or not code:
        return _callback_page(state, origin, error="Google sign-in was cancelled. No connection was changed.")
    try:
        credentials = await _credentials(pending, code)
        from app.core.connector_service import create_connector, update_connector, decrypt_config
        from app.core.connector_packages import require_installed
        await run_sync(require_installed, "gmail")
        if pending["connector_id"]:
            row = await run_sync(database.get_connector, pending["connector_id"])
            if not row or row["type"] != "gmail":
                raise ValueError("The connection was removed while signing in")
            # Refuse a different account on reconnect: silent replacement could
            # cause existing automations to read or send another user's mail.
            saved = decrypt_config(row.get("config") or {}, "gmail")
            if saved.get("email_address") and saved["email_address"].lower() != credentials["email_address"].lower():
                raise ValueError("Choose the same Google account to reconnect, or add a separate connection")
            result = await update_connector(row["id"], config={**saved, **credentials})
            if not result:
                raise ValueError("The connection was removed while signing in")
        else:
            result = await create_connector(name=pending["name"].strip() or credentials["email_address"],
                                            connector_type="gmail", config={**pending["options"], **credentials}, events=[])
        return _callback_page(state, origin, connector_id=result["id"])
    except ValueError as exc:
        return _callback_page(state, origin, error=str(exc))
    except Exception:
        # Do not log token endpoint bodies or OAuth codes through exception reprs.
        return _callback_page(state, origin, error="Google sign-in could not be saved. Check the app configuration and try again.")
