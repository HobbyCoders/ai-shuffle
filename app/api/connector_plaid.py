"""Admin-owned Plaid Link connections; no bank credentials enter AI Shuffle."""
import hashlib
import json
import secrets
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.auth import require_admin
from app.core import encryption
from app.core.connectors.plaid import plaid_request
from app.db import database
from app.db.async_database import run_sync

router = APIRouter(prefix="/oauth/plaid", tags=["Connectors"])
SETTING = "connector_plaid_app"
TTL = 1800
_pending = {}


class PlaidApp(BaseModel):
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(min_length=1, max_length=1024)
    environment: Literal["sandbox", "production"] = "sandbox"
    country_codes: list[Literal["US", "CA", "GB", "FR", "ES", "NL", "IE", "DE", "IT", "PL", "DK", "NO", "SE", "EE", "LT", "LV", "PT", "BE", "AT", "FI"]] = Field(default=["US"], min_length=1)
    redirect_uri: str = Field(max_length=2048)


class Exchange(BaseModel):
    flow_id: str = Field(min_length=1, max_length=128)
    public_token: str = Field(min_length=1, max_length=2048)


def app_config():
    raw = database.get_system_setting(SETTING)
    config = json.loads(raw) if raw else {}
    if config.get("client_secret"):
        config["client_secret"] = encryption.decrypt_value(config["client_secret"])
    return config


def validate_redirect(uri, origin):
    parts = urlsplit(uri)
    if (f"{parts.scheme}://{parts.netloc}" != origin or parts.path != "/plaid-link"
            or parts.query or parts.fragment or parts.username or parts.password
            or parts.scheme != "https"):
        raise ValueError("Use this installation's HTTPS origin followed by /plaid-link, and allowlist it in the Plaid dashboard")


@router.get("/status")
async def status(request: Request, token: str = Depends(require_admin)):
    config = await run_sync(app_config)
    return {"configured": bool(config), **{k: v for k, v in config.items() if k != "client_secret"},
            "suggested_redirect_uri": str(request.base_url).rstrip("/") + "/plaid-link"}


@router.put("/app")
async def configure(data: PlaidApp, request: Request, token: str = Depends(require_admin)):
    try:
        validate_redirect(data.redirect_uri, str(request.base_url).rstrip("/"))
        def save():
            config = data.model_dump()
            config["client_secret"] = encryption.encrypt_value(config["client_secret"])
            database.set_system_setting(SETTING, json.dumps(config))
        await run_sync(save)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError:
        raise HTTPException(409, "Unlock credential storage before saving Plaid app settings")
    return {"configured": True}


@router.post("/start")
async def start(request: Request, token: str = Depends(require_admin)):
    from app.core.connector_packages import require_installed
    try:
        config = await run_sync(app_config)
        if not config:
            raise ValueError("An administrator must configure Plaid first")
        validate_redirect(config["redirect_uri"], str(request.base_url).rstrip("/"))
        await run_sync(require_installed, "plaid")
        now = time.monotonic()
        for key in list(_pending):
            if _pending[key]["expires"] <= now:
                del _pending[key]
        if len(_pending) >= 128:
            raise HTTPException(429, "Too many pending bank connections")
        flow_id, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        # Reserve before awaiting Plaid so concurrent requests respect the cap.
        _pending[flow_id] = {"expires": now + TTL, "browser": hashlib.sha256(browser.encode()).hexdigest(), "config": config}
        try:
            result = await plaid_request(config, "/link/token/create", user={"client_user_id": flow_id},
                client_name="AI Shuffle", products=["transactions"], country_codes=config["country_codes"],
                language="en", redirect_uri=config["redirect_uri"])
        except Exception:
            _pending.pop(flow_id, None)
            raise
        link_token = result.get("link_token") if isinstance(result, dict) else None
        if not link_token or not isinstance(link_token, str):
            # An upstream reply without a token is Plaid's failure, not ours:
            # free the reserved slot and report a bad gateway.
            _pending.pop(flow_id, None)
            raise HTTPException(502, "Plaid did not return a link token. Check the Plaid app settings and try again.")
        response = JSONResponse({"flow_id": flow_id, "link_token": link_token})
        response.set_cookie("plaid_link_" + flow_id[:16], browser, httponly=True, secure=True,
                            samesite="lax", max_age=TTL, path="/api/v1/connectors/oauth/plaid")
        response.headers["Cache-Control"] = "no-store"
        return response
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/exchange")
async def exchange(data: Exchange, request: Request, token: str = Depends(require_admin)):
    pending = _pending.get(data.flow_id)
    browser = request.cookies.get("plaid_link_" + data.flow_id[:16], "")
    if (not pending or pending["expires"] <= time.monotonic() or not browser
            or not secrets.compare_digest(hashlib.sha256(browser.encode()).hexdigest(), pending["browser"])):
        raise HTTPException(400, "This bank sign-in expired or belongs to another browser. Start again.")
    del _pending[data.flow_id]
    config = pending["config"]
    try:
        from app.core.connector_packages import require_installed
        from app.core.connector_service import create_connector
        await run_sync(require_installed, "plaid")
        result = await plaid_request(config, "/item/public_token/exchange", public_token=data.public_token)
        access_token = result["access_token"]
        try:
            # Validate the actual producing path, not browser-supplied institution metadata.
            accounts = await plaid_request(config, "/accounts/get", access_token=access_token)
            name = (accounts.get("accounts") or [{}])[0].get("name") or "Bank accounts"
            connection = await create_connector(name=f"{name} (Plaid {config['environment']})", connector_type="plaid",
                config={"client_id": config["client_id"], "client_secret": config["client_secret"],
                        "environment": config["environment"], "access_token": access_token, "item_id": result["item_id"]}, events=[])
        except Exception:
            # Avoid leaving billable, orphaned Items after a failed save.
            await plaid_request(config, "/item/remove", access_token=access_token)
            raise
        response = JSONResponse({"connector_id": connection["id"]})
        response.delete_cookie("plaid_link_" + data.flow_id[:16], path="/api/v1/connectors/oauth/plaid")
        response.headers["Cache-Control"] = "no-store"
        return response
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        raise HTTPException(400, "Bank sign-in could not be saved. Start again from Plugins.")
