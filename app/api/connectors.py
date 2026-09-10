"""
Connectors management API routes.

Provides CRUD for connectors (Telegram, Discord, WhatsApp, Webhook)
plus actions: test, activate, deactivate, restart.
Also includes the WhatsApp webhook verification endpoint.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Depends, Request, status
from fastapi.responses import PlainTextResponse

from app.core.models import (
    Connector, ConnectorCreate, ConnectorUpdate,
    ConnectorTestResponse, ConnectorEventLog, VALID_CONNECTOR_EVENTS,
    DiscordDiscoverRequest,
)
from app.api.auth import require_admin
from app.api.common import api_error_handler
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/connectors", tags=["Connectors"])
from app.api.connector_oauth import router as oauth_router
router.include_router(oauth_router)


# ---- Meta endpoints ----

@router.get("/types")
async def list_connector_types(token: str = Depends(require_admin)):
    """List available connector types with config schemas."""
    from app.core.connector_service import get_connector_type_info
    return get_connector_type_info()


@router.get("/events", response_model=List[str])
async def list_event_types(token: str = Depends(require_admin)):
    """List valid connector event types."""
    return VALID_CONNECTOR_EVENTS


# ---- CRUD ----

@router.get("", response_model=List[Connector])
async def list_connectors(token: str = Depends(require_admin)):
    """List all connectors."""
    from app.core.connector_service import mask_config

    def _list():
        # The read and the masking pass over its rows are one unit of work —
        # masking decrypts, so it stays off the loop with the query.
        connectors = database.get_all_connectors()
        for c in connectors:
            c["config"] = mask_config(c.get("config", {}), c.get("type", ""))
        return connectors

    return await run_sync(_list)


@router.post("", response_model=Connector, status_code=status.HTTP_201_CREATED)
async def create_connector_endpoint(
    data: ConnectorCreate,
    token: str = Depends(require_admin),
):
    """Create a new connector with a dedicated API user."""
    from app.core.connector_service import create_connector
    try:
        return await create_connector(
            name=data.name,
            connector_type=data.type.value,
            config=data.config,
            events=data.events,
            api_user_id=data.api_user_id,
            project_id=data.project_id,
            profile_ids=data.profile_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.exception("Failed to create connector")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create connector")


# ---- Discord guided setup ----
#
# NOTE: this literal path MUST be declared BEFORE the parametrized
# ``/{connector_id}`` routes below — FastAPI matches routes in declaration
# order, and a ``/{connector_id}`` route would otherwise swallow
# ``/discord/discover``.

@router.post("/discord/discover")
async def discover_discord_resources(
    data: DiscordDiscoverRequest,
    token: str = Depends(require_admin),
):
    """Log into Discord with a bot token and return the bot identity plus the
    guilds/channels it can see, for the guided setup pickers.

    Provide exactly one of ``bot_token`` or ``connector_id``. When
    ``connector_id`` is given, the connector's stored (encrypted) token is
    decrypted and used — this is how the edit view reuses a masked token.
    """
    from app.core.connectors.discord_connector import discover_resources
    from app.core.connector_service import decrypt_config

    bot_token = data.bot_token
    connector_id = data.connector_id

    # Exactly one of bot_token / connector_id must be provided.
    if bool(bot_token) == bool(connector_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide exactly one of bot_token or connector_id",
        )

    if connector_id:
        def _stored_bot_token():
            # Row read + decrypt in one hop; the Discord round-trip below is a
            # real await and stays on the loop.
            connector = database.get_connector(connector_id)
            if not connector or connector.get("type") != "discord":
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Discord connector not found: {connector_id}",
                )
            decrypted = decrypt_config(connector.get("config", {}) or {}, "discord")
            token_value = decrypted.get("bot_token")
            if not token_value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Connector has no stored bot token",
                )
            return token_value

        bot_token = await run_sync(_stored_bot_token)
    elif bot_token.startswith("****"):
        # A masked token was submitted — the real value lives on the connector.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token is masked — pass connector_id to reuse the stored token",
        )

    try:
        return await discover_resources(bot_token)
    except ValueError:
        # Invalid bot token. Do not echo the token.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid bot token",
        )
    except Exception:
        # TimeoutError, discord.HTTPException, or any other upstream failure.
        # Never log or echo the token.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Discord discovery failed",
        )


@router.get("/{connector_id}/dependencies")
async def get_connection_dependencies(connector_id: str, token: str = Depends(require_admin)):
    """Automations that must be considered before removing a connection."""
    from app.core.connection_dependencies import connection_dependencies
    if not await run_sync(database.get_connector, connector_id):
        raise HTTPException(status_code=404, detail="Connection not found")
    return await run_sync(connection_dependencies, connector_id)


@router.get("/{connector_id}", response_model=Connector)
async def get_connector(connector_id: str, token: str = Depends(require_admin)):
    """Get a connector by ID."""
    from app.core.connector_service import mask_config

    def _get():
        connector = database.get_connector(connector_id)
        if not connector:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")
        connector["config"] = mask_config(connector.get("config", {}), connector.get("type", ""))
        return connector

    return await run_sync(_get)


@router.patch("/{connector_id}", response_model=Connector)
async def update_connector_endpoint(
    connector_id: str,
    data: ConnectorUpdate,
    token: str = Depends(require_admin),
):
    """Update a connector."""
    from app.core.connector_service import update_connector
    kwargs = data.model_dump(exclude_unset=True)
    try:
        result = await update_connector(connector_id, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")
    return result


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector_endpoint(connector_id: str, token: str = Depends(require_admin)):
    """Delete a connector and its dedicated API user."""
    from app.core.connector_service import delete_connector
    try:
        deleted = await delete_connector(connector_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")


# ---- Actions ----

def _owns_local_runner(connector: Dict[str, Any]) -> bool:
    """Whether THIS install may operate *connector*'s live client instance.

    Multi-install affinity: two installs can share one Postgres, so only the
    owning install may start/stop the live connector. Returns True when the
    connector is already pinned to us, or when it is unowned (runner_id NULL)
    and we win the atomic claim. Returns False when it is pinned to a different
    install — the caller must then persist the is_active change but NOT touch a
    local instance.
    """
    from app.core.config import settings
    me = settings.effective_instance_id
    runner_id = connector.get("runner_id")
    if runner_id == me:
        return True
    if runner_id is None:
        return database.claim_connector_runner(connector["id"], me)
    return False


@router.post("/{connector_id}/test", response_model=ConnectorTestResponse)
async def test_connector_endpoint(connector_id: str, token: str = Depends(require_admin)):
    """Test a connector's connection."""
    from app.core.connector_service import test_connector
    connector = await run_sync(database.get_connector, connector_id)
    if not connector:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")
    return await test_connector(connector_id)


@router.post("/{connector_id}/activate", response_model=Connector)
async def activate_connector(connector_id: str, token: str = Depends(require_admin)):
    """Activate (enable + start) a connector."""
    from app.core.connector_service import mask_config

    def _read_masked():
        updated = database.get_connector(connector_id)
        updated["config"] = mask_config(updated.get("config", {}), updated.get("type", ""))
        return updated

    def _enable_and_claim():
        """Everything before the (awaited) client start, in ONE hop.

        The existence check, the is_active write and the runner claim are a
        read-decide-write sequence: ``_owns_local_runner`` may issue an atomic
        claim, and splitting it from the check would let a concurrent activate
        on another install slip between them. When we do NOT own the runner
        there is no await at all, so the response read joins this hop too.
        """
        connector = database.get_connector(connector_id)
        if not connector:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")

        # Persist the enabled flag regardless of which install runs the live client.
        database.update_connector(connector_id, is_active=True)

        # Only start the live client here if this install owns the connector (or
        # claims an unowned one). If it is pinned to a DIFFERENT install, the flag is
        # still persisted above; the owning install starts it on its next restart
        # (KNOWN LIMITATION: no cross-install watcher yet).
        if not _owns_local_runner(connector):
            updated = _read_masked()
            updated["applies_on"] = updated.get("runner_id")
            return False, updated
        return True, None

    owns_locally, updated = await run_sync(_enable_and_claim)
    if not owns_locally:
        return updated

    from app.core.connectors.manager import connector_manager
    success = await connector_manager.start_connector(connector_id)

    # Both outcomes re-read the row after the start attempt (it carries
    # last_error / status stamped by the client), so the two branches share
    # this single second hop.
    updated = await run_sync(_read_masked)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start connector: {updated.get('last_error', 'unknown error')}",
        )

    return updated


@router.post("/{connector_id}/deactivate", response_model=Connector)
async def deactivate_connector(connector_id: str, token: str = Depends(require_admin)):
    """Deactivate (stop + disable) a connector."""
    from app.core.connector_service import mask_config

    def _check_and_claim():
        """Existence check + runner claim in ONE hop — the claim is a
        read-decide-write that must not be split from the row it decides on."""
        connector = database.get_connector(connector_id)
        if not connector:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")

        from app.core.connection_dependencies import require_unreferenced_connection
        try:
            require_unreferenced_connection(connector_id, enabled_only=True)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))

        # Only stop the live client here if this install owns the connector. If it is
        # pinned to a DIFFERENT install we still persist is_active=False below; the
        # owning install stops it on its next restart (KNOWN LIMITATION: no
        # cross-install watcher yet).
        return _owns_local_runner(connector)

    owns_locally = await run_sync(_check_and_claim)

    # The client stop is a real await sitting between the claim and the write,
    # so this handler cannot be a single hop.
    if owns_locally:
        from app.core.connectors.manager import connector_manager
        await connector_manager.stop_connector(connector_id)

    def _disable():
        database.update_connector(connector_id, is_active=False)
        updated = database.get_connector(connector_id)
        updated["config"] = mask_config(updated.get("config", {}), updated.get("type", ""))
        return updated

    updated = await run_sync(_disable)
    if not owns_locally:
        updated["applies_on"] = updated.get("runner_id")
    return updated


@router.post("/{connector_id}/restart", response_model=Connector)
async def restart_connector(connector_id: str, token: str = Depends(require_admin)):
    """Restart a running connector."""
    from app.core.connector_service import mask_config

    def _check_and_claim():
        """Existence check + runner claim in ONE hop — see deactivate."""
        connector = database.get_connector(connector_id)
        if not connector:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")

        # Only restart the live client here if this install owns the connector. If it
        # is pinned to a DIFFERENT install, do nothing locally (KNOWN LIMITATION: no
        # cross-install watcher yet — the owning install controls its own client).
        return _owns_local_runner(connector)

    owns_locally = await run_sync(_check_and_claim)

    # The restart is a real await between the claim and the response read.
    if owns_locally:
        from app.core.connectors.manager import connector_manager
        await connector_manager.restart_connector(connector_id)

    def _read_masked():
        updated = database.get_connector(connector_id)
        updated["config"] = mask_config(updated.get("config", {}), updated.get("type", ""))
        return updated

    updated = await run_sync(_read_masked)
    if not owns_locally:
        updated["applies_on"] = updated.get("runner_id")
    return updated


# ---- Status & Logs ----

@router.get("/{connector_id}/status")
async def get_connector_status(connector_id: str, token: str = Depends(require_admin)):
    """Get runtime status of a connector."""
    connector = await run_sync(database.get_connector, connector_id)
    if not connector:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")

    from app.core.connectors.manager import connector_manager
    runtime = connector_manager.get_connector_status(connector_id)
    return {
        "id": connector_id,
        "status": connector.get("status", "disconnected"),
        "status_message": connector.get("status_message"),
        "last_connected_at": connector.get("last_connected_at"),
        "last_event_at": connector.get("last_event_at"),
        "failure_count": connector.get("failure_count", 0),
        "is_running": runtime is not None,
    }


@router.get("/{connector_id}/logs", response_model=List[ConnectorEventLog])
async def get_connector_logs(connector_id: str, token: str = Depends(require_admin)):
    """Get recent event logs for a connector."""

    def _logs():
        connector = database.get_connector(connector_id)
        if not connector:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")
        return database.get_connector_event_log(connector_id)

    return await run_sync(_logs)


# ---- Triggers, Actions & Tools ----

@router.get("/{connector_id}/triggers")
async def get_connector_triggers(connector_id: str, token: str = Depends(require_admin)):
    """Get available triggers for a connector."""
    from dataclasses import asdict
    from app.core.connectors.registry import get_connector_class

    connector = await run_sync(database.get_connector, connector_id)
    if not connector:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")

    try:
        cls = get_connector_class(connector["type"])
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown connector type: {connector['type']}",
        )

    return [asdict(t) for t in cls.TRIGGERS]


@router.get("/{connector_id}/actions")
async def get_connector_actions(connector_id: str, token: str = Depends(require_admin)):
    """Get available actions for a connector."""
    from dataclasses import asdict
    from app.core.connectors.registry import get_connector_class

    connector = await run_sync(database.get_connector, connector_id)
    if not connector:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")

    try:
        cls = get_connector_class(connector["type"])
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown connector type: {connector['type']}",
        )

    return [asdict(a) for a in cls.ACTIONS]


@router.get("/{connector_id}/tools")
async def get_connector_tools(connector_id: str, token: str = Depends(require_admin)):
    """Get available tools for a connector."""
    from dataclasses import asdict
    from app.core.connectors.registry import get_connector_class

    connector = await run_sync(database.get_connector, connector_id)
    if not connector:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")

    try:
        cls = get_connector_class(connector["type"])
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown connector type: {connector['type']}",
        )

    return [asdict(t) for t in cls.TOOLS]


@router.post("/{connector_id}/actions/{action_id}/execute")
async def execute_connector_action(
    connector_id: str,
    action_id: str,
    request: Request,
    token: str = Depends(require_admin),
):
    """Execute a connector action directly."""
    from app.core.connectors.manager import connector_manager
    from app.core.connectors.registry import get_connector_class

    connector = await run_sync(database.get_connector, connector_id)
    if not connector:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Connector not found: {connector_id}")

    # Validate that this action_id exists on the connector class
    try:
        cls = get_connector_class(connector["type"])
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown connector type: {connector['type']}",
        )

    valid_action_ids = [a.id for a in cls.ACTIONS]
    if action_id not in valid_action_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown action '{action_id}' for connector type '{connector['type']}'. "
                   f"Valid actions: {valid_action_ids}",
        )

    # Get the running connector instance
    instance = await connector_manager.get_running_connector(connector_id)
    if not instance:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Connector '{connector_id}' is not running. Activate it first.",
        )

    # Parse request body as JSON params
    try:
        params = await request.json()
    except Exception:
        params = {}

    # Execute the action
    async with api_error_handler(f"execute action '{action_id}' on connector '{connector_id}'"):
        try:
            result = await instance.execute_action(action_id, params)
        except NotImplementedError as e:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=str(e),
            )

    return {"status": "ok", "action_id": action_id, "result": result}


# ---- WhatsApp Webhook ----

@router.get("/whatsapp/webhook")
async def whatsapp_webhook_verify(request: Request):
    """WhatsApp webhook verification endpoint (Meta challenge)."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode != "subscribe" or not token or not challenge:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification request")

    # Find a WhatsApp connector with matching verify_token. The listing and the
    # per-row decrypt scan are one hop: a decrypt-per-row loop on the loop
    # thread is the whole stall, and it short-circuits on the first match
    # exactly as before.
    def _verify_token_matches():
        connectors = database.get_all_connectors()
        for c in connectors:
            if c.get("type") != "whatsapp":
                continue
            config = c.get("config", {})
            # Decrypt verify_token if needed
            from app.core.connector_service import decrypt_config
            decrypted = decrypt_config(config, "whatsapp")
            if decrypted.get("verify_token") == token:
                return True
        return False

    if await run_sync(_verify_token_matches):
        return PlainTextResponse(content=challenge)

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid verify token")


@router.post("/whatsapp/webhook")
async def whatsapp_webhook_receive(request: Request):
    """Receive incoming WhatsApp messages via webhook."""
    import hashlib as _hashlib
    import hmac as _hmac

    body = await request.body()
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    # Verify Meta webhook signature (X-Hub-Signature-256)
    signature = request.headers.get("x-hub-signature-256")

    from app.core.connectors.manager import connector_manager
    from app.core.connector_service import decrypt_config

    any_handled = False
    any_error = False

    # Extract phone_number_id from payload to match the correct connector
    try:
        phone_number_id = (
            payload.get("entry", [{}])[0]
            .get("changes", [{}])[0]
            .get("value", {})
            .get("metadata", {})
            .get("phone_number_id")
        )
    except (IndexError, AttributeError):
        phone_number_id = None

    def _select_recipients():
        """Row read + decrypt + signature/phone matching for every running
        WhatsApp connector, in ONE hop.

        The per-connector row read used to sit inside the dispatch loop, so a
        webhook with N connectors did N blocking reads interleaved with the
        handler awaits. The selection is a pure function of the row config and
        this request, and the config cannot change while we dispatch, so
        hoisting the whole filter ahead of the awaits is behaviour-preserving —
        and it keeps the credential decrypt off the loop as well. Only the
        matched connectors are kept, so this is no larger than the running set.
        """
        recipients = []
        for connector_id, connector in connector_manager.get_running_connectors("whatsapp"):
            conn_row = database.get_connector(connector_id)
            config = conn_row.get("config", {}) if conn_row else {}
            decrypted = decrypt_config(config, "whatsapp")

            # Verify signature using the connector's app_secret if configured
            app_secret = decrypted.get("app_secret")
            if app_secret and signature:
                expected = "sha256=" + _hmac.new(
                    app_secret.encode(), body, _hashlib.sha256
                ).hexdigest()
                if not _hmac.compare_digest(expected, signature):
                    logger.warning(f"WhatsApp webhook signature mismatch for connector {connector_id}")
                    continue
            elif app_secret and not signature:
                logger.warning(f"WhatsApp webhook missing signature for connector {connector_id}")
                continue

            # Match by phone_number_id if available
            configured_phone = decrypted.get("phone_number_id")
            if phone_number_id and configured_phone and phone_number_id != configured_phone:
                continue

            recipients.append((connector_id, connector))
        return recipients

    for connector_id, connector in await run_sync(_select_recipients):
        any_handled = True
        try:
            await connector.handle_webhook_payload(payload)
        except Exception as e:
            logger.error(f"WhatsApp webhook handler error for connector {connector_id}: {e}")
            any_error = True

    if any_error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing failed",
        )

    return {"status": "ok"}


# ---- Generic Inbound Webhook ----

@router.post("/hooks/{webhook_slug}")
async def inbound_webhook(webhook_slug: str, request: Request):
    """
    Generic inbound webhook endpoint.

    External services POST JSON here to trigger AI processing.
    The webhook_slug must match a webhook connector's inbound_path config.
    Auth via Bearer token (Authorization header or X-Webhook-Token) or
    HMAC signature (X-Webhook-Signature header).
    """
    # Parse body
    try:
        body = await request.body()
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body"
        )

    # Find matching webhook connector
    from app.core.connectors.manager import connector_manager
    from app.core.connector_service import decrypt_config

    matched = False
    for connector_id, connector in connector_manager.get_running_connectors("webhook"):
        config = connector.config
        if config.get("inbound_path") != webhook_slug:
            continue
        mode = config.get("mode", "outbound")
        if mode not in ("inbound", "bidirectional"):
            continue

        # Verify auth
        token = None
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            token = request.headers.get("x-webhook-token")

        signature = request.headers.get("x-webhook-signature")

        if not connector.verify_inbound_request(
            body.decode("utf-8", errors="replace"), token=token, signature=signature
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid authentication",
            )

        # Dispatch to AI
        async with api_error_handler("process inbound webhook"):
            await connector.handle_webhook_payload(payload)

        matched = True
        break

    if not matched:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No inbound webhook connector found for slug: {webhook_slug}",
        )

    return {"status": "ok", "message": "Webhook received and processing"}
