"""
ConnectorManager — lifecycle management, event dispatch, and message routing.
"""

import asyncio
import logging
import secrets
import uuid
from typing import Any, Dict, List, Optional

from app.core.connectors.base import (
    BaseConnector,
    ConnectorStatus,
    IncomingMessage,
    OutgoingMessage,
    PermissionPrompt,
)
from app.core.connectors.registry import get_connector_class
from app.db import database

logger = logging.getLogger(__name__)


class ConnectorManager:
    """Central hub for connector lifecycle, event dispatch, and message routing."""

    def __init__(self):
        self._connectors: Dict[str, BaseConnector] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_all(self) -> None:
        """Start the active connectors this install owns.

        Two installs can share one Postgres, so we must not both connect the
        same connector (that would duplicate every inbound reply). Each active
        connector row carries a ``runner_id``:

        * owned by us            → start it here.
        * unowned (runner NULL)  → attempt an atomic claim; start only if we win
                                   the race (self-heals pre-affinity NULL rows).
        * owned by another install → skip; the owner starts it on its own boot.
        """
        from app.core.config import settings

        me = settings.effective_instance_id
        connectors = database.get_active_connectors()
        logger.info(
            f"Evaluating {len(connectors)} active connector(s) for runner {me}"
        )

        for conn_row in connectors:
            cid = conn_row["id"]
            runner_id = conn_row.get("runner_id")
            try:
                if runner_id == me:
                    await self.start_connector(cid)
                elif runner_id is None:
                    # Race-safe claim: only the install whose UPDATE flips the
                    # NULL owner starts it; losers skip.
                    if database.claim_connector_runner(cid, me):
                        logger.info(f"Claimed unowned connector {cid} for runner {me}")
                        await self.start_connector(cid)
                    else:
                        logger.info(
                            f"Connector {cid} claimed by another runner — skipping start"
                        )
                else:
                    logger.info(
                        f"Connector {cid} is pinned to runner {runner_id} "
                        f"(this install is {me}) — skipping start"
                    )
            except Exception as e:
                logger.error(f"Failed to start connector {cid}: {e}")

    async def shutdown_all(self) -> None:
        """Stop every running connector."""
        async with self._lock:
            connector_ids = list(self._connectors.keys())

        for cid in connector_ids:
            try:
                await self.stop_connector(cid)
            except Exception as e:
                logger.error(f"Error stopping connector {cid}: {e}")

        logger.info("All connectors shut down")

    async def start_connector(self, connector_id: str) -> bool:
        """Instantiate, connect, and register a single connector."""
        connector_row = database.get_connector(connector_id)
        if not connector_row:
            logger.error(f"Connector not found in DB: {connector_id}")
            return False

        connector_type = connector_row["type"]
        config = connector_row.get("config", {}) or {}

        # Decrypt sensitive configuration fields
        config = self._decrypt_config(config, connector_type)

        # Look up the implementation class from the registry
        try:
            cls = get_connector_class(connector_type)
        except ValueError as e:
            logger.error(f"Cannot start connector {connector_id}: {e}")
            database.update_connector_status(connector_id, "error", error=str(e))
            return False

        # Create the connector instance
        instance = cls(
            connector_id=connector_id,
            connector_type=connector_type,
            config=config,
            on_incoming_message=self.handle_incoming_message,
            on_permission_response=self.handle_permission_response,
            on_trigger=self._handle_trigger,
        )

        # Attempt to connect
        try:
            database.update_connector_status(connector_id, "connecting")
            success = await instance.connect()
        except Exception as e:
            logger.error(f"Connector {connector_id} connect() raised: {e}")
            database.update_connector_status(connector_id, "error", error=str(e))
            return False

        if success:
            async with self._lock:
                self._connectors[connector_id] = instance
            # Keep status as "connecting" — the connector's on_ready callback
            # will update to "connected" once the gateway handshake completes.
            # If login fails, _run_client will update to "error".
            logger.info(f"Connector started: {connector_id} ({connector_type})")
            return True
        else:
            database.update_connector_status(
                connector_id, "error", error="connect() returned False"
            )
            return False

    async def stop_connector(self, connector_id: str) -> None:
        """Disconnect and unregister a running connector."""
        async with self._lock:
            instance = self._connectors.pop(connector_id, None)

        if instance:
            try:
                await instance.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting connector {connector_id}: {e}")

        database.update_connector_status(connector_id, "disconnected")
        logger.info(f"Connector stopped: {connector_id}")

    async def restart_connector(self, connector_id: str) -> bool:
        """Stop then start a connector."""
        await self.stop_connector(connector_id)
        return await self.start_connector(connector_id)

    # ------------------------------------------------------------------
    # Trigger dispatch (connector variant triggers → automations)
    # ------------------------------------------------------------------

    async def _handle_trigger(
        self, connector_id: str, trigger_id: str, payload: Dict[str, Any]
    ) -> None:
        """Callback invoked by connectors when they emit a trigger event.

        Routes the trigger to the automation manager which will fire any
        automations configured with ``trigger_type="connector_trigger"``
        matching this connector and trigger ID.
        """
        logger.debug(
            "Connector %s emitted trigger '%s'", connector_id, trigger_id
        )
        try:
            from app.core.automation_manager import automation_manager
            await automation_manager.handle_connector_trigger(
                connector_id, trigger_id, payload
            )
        except Exception as e:
            logger.error(
                "Failed to dispatch connector trigger %s/%s to automations: %s",
                connector_id, trigger_id, e,
            )

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def dispatch_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """
        Fan-out an event to every connector subscribed to *event_type*.

        If *data* contains a ``session_id``, only connector-sessions linked to
        that AI session are notified.  Otherwise every active session for each
        connector is sent the notification.
        """
        subscribed = database.get_connectors_for_event(event_type)
        if not subscribed:
            # Still dispatch to legacy webhooks
            try:
                from app.core.webhook_service import dispatch_webhook
                await dispatch_webhook(event_type, data)
            except Exception as e:
                logger.error(f"Legacy webhook dispatch failed: {e}")
            return

        session_id = data.get("session_id")

        for conn_row in subscribed:
            cid = conn_row["id"]
            log_id = secrets.token_hex(8)

            async with self._lock:
                instance = self._connectors.get(cid)

            if not instance:
                logger.debug(f"Connector {cid} not running — skipping event {event_type}")
                continue

            try:
                # Determine which platform chats to notify
                if session_id:
                    sessions = database.get_connector_sessions_for_ai_session(session_id)
                    # Filter to only sessions belonging to this connector
                    sessions = [s for s in sessions if s.get("connector_id") == cid]
                else:
                    # Broadcast to all active sessions for this connector
                    # (no dedicated DB helper — query directly)
                    from app.db.base import get_db, rows_to_list
                    with get_db() as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            "SELECT * FROM connector_sessions WHERE connector_id = %s AND is_active = TRUE",
                            (cid,),
                        )
                        sessions = rows_to_list(cursor.fetchall())

                if not sessions:
                    logger.debug(
                        f"No connector sessions linked for connector {cid} "
                        f"event={event_type} session_id={session_id} — skipping"
                    )
                    continue

                any_sent = False
                for sess in sessions:
                    chat_id = sess.get("platform_chat_id")
                    if chat_id:
                        ok = await instance.send_notification(chat_id, event_type, data)
                        if ok:
                            any_sent = True
                        else:
                            logger.warning(
                                f"send_notification failed for connector {cid} "
                                f"chat_id={chat_id} event={event_type}"
                            )

                status = "delivered" if any_sent else "failed"
                database.create_connector_event_log(
                    log_id=log_id,
                    connector_id=cid,
                    event_type=event_type,
                    direction="outbound",
                    payload=data,
                    status=status,
                    error=None if any_sent else "No messages were delivered to platform",
                )
                database.update_connector_event_dispatched(cid, success=any_sent)

            except Exception as e:
                logger.error(f"Event dispatch failed for connector {cid}: {e}")
                database.create_connector_event_log(
                    log_id=log_id,
                    connector_id=cid,
                    event_type=event_type,
                    direction="outbound",
                    payload=data,
                    status="failed",
                    error=str(e),
                )
                database.update_connector_event_dispatched(cid, success=False)

        # Also dispatch to legacy webhooks
        try:
            from app.core.webhook_service import dispatch_webhook
            await dispatch_webhook(event_type, data)
        except Exception as e:
            logger.error(f"Legacy webhook dispatch failed: {e}")

        # Dispatch to automations triggered by connector events
        try:
            from app.core.automation_manager import automation_manager
            await automation_manager.handle_connector_event(event_type, data)
        except Exception as e:
            logger.error(f"Automation event dispatch failed: {e}")

    # ------------------------------------------------------------------
    # Connector context for system prompt
    # ------------------------------------------------------------------

    @staticmethod
    def _build_connector_context(
        connector_row: Dict[str, Any],
        instance: BaseConnector,
        platform_chat_id: str,
    ) -> str:
        """Build system prompt context describing the platform and memory instructions."""
        from dataclasses import fields as dc_fields

        parts: List[str] = []

        connector_type = connector_row.get("type", "unknown")
        connector_name = connector_row.get("name", connector_type)

        # Platform identity
        parts.append(
            f"You are communicating via {connector_name} ({connector_type}). "
            f"Messages are from platform chat {platform_chat_id}."
        )

        # Memory instructions
        parts.append(
            "MEMORY: Important context about this user, ongoing projects, preferences, "
            "and key decisions should be persisted to CLAUDE.md in the working directory. "
            "Read CLAUDE.md at the start of each conversation to recall prior context. "
            "Update it when you learn important new information worth remembering."
        )

        # Platform capabilities
        caps = instance.CAPABILITIES
        cap_names = [
            f.name.replace("_", " ")
            for f in dc_fields(caps)
            if f.name != "is_messaging" and getattr(caps, f.name) is True
        ]
        if cap_names:
            parts.append(f"Platform capabilities: {', '.join(cap_names)}.")
        if instance.MAX_MESSAGE_LENGTH:
            parts.append(
                f"Max message length: {instance.MAX_MESSAGE_LENGTH} characters. "
                "Split long responses if needed."
            )

        # Connector-provided usage instructions (e.g. the Discord action
        # gateway blurb). Each connector owns its own guidance.
        try:
            extra = instance.get_context_instructions(platform_chat_id)
            if extra:
                parts.append(extra)
        except Exception:
            logger.debug("get_context_instructions failed", exc_info=True)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Incoming message handling
    # ------------------------------------------------------------------

    async def handle_incoming_message(self, message: IncomingMessage) -> None:
        """
        Process a message arriving from a platform connector.

        Resolves (or creates) a connector session, executes the query via the
        query engine, and sends the response back through the connector.
        """
        try:
            # Get the connector instance early for ack/typing/read
            async with self._lock:
                instance = self._connectors.get(message.connector_id)

            # Acknowledge receipt immediately (emoji reaction + read receipt)
            if instance and message.platform_message_id:
                try:
                    await instance.send_ack_reaction(
                        message.platform_chat_id, message.platform_message_id
                    )
                except Exception:
                    logger.debug("Ack reaction failed", exc_info=True)
                try:
                    await instance.mark_as_read(
                        message.platform_chat_id, message.platform_message_id
                    )
                except Exception:
                    logger.debug("Mark-as-read failed", exc_info=True)

            # Look up or create connector session
            session = database.get_connector_session(
                message.connector_id, message.platform_chat_id
            )

            if not session:
                session_id = secrets.token_hex(8)
                session = database.create_connector_session(
                    session_id=session_id,
                    connector_id=message.connector_id,
                    platform_chat_id=message.platform_chat_id,
                    platform_user_id=message.platform_user_id,
                    platform_user_name=message.platform_user_name,
                )

            # Determine the API user for this connector
            connector_row = database.get_connector(message.connector_id)
            api_user_id = connector_row.get("api_user_id") if connector_row else None

            # Resolve profile / project from the API user
            api_user = database.get_api_user(api_user_id) if api_user_id else None
            profile_id = None
            project_id = None
            if api_user:
                profile_ids = api_user.get("profile_ids", [])
                profile_id = profile_ids[0] if profile_ids else None
                project_id = api_user.get("project_id")

            # Fall back to the first available profile when none is configured
            if not profile_id:
                profiles = database.get_all_profiles()
                if profiles:
                    profile_id = profiles[0]["id"]
                else:
                    logger.error("No profiles available for connector query")
                    return

            # Pre-mint + link the AI session BEFORE the query runs.
            #
            # Per-session builtin tools (notably the Discord-native tools) are
            # registered at query build time by resolving session_id -> linked
            # connector session. Historically the linkage was written only AFTER
            # the first turn, so those tools were missing on the very first
            # message of a channel. execute_query resolves an existing session by
            # id (it RAISES on an unknown id — it does not create one from a fresh
            # id), so we create the AI-session row here via the same helper
            # execute_query uses and link it first, then hand the query that id to
            # resume. On subsequent turns the connector session already carries
            # the link, so this is a no-op.
            ai_session_id = session.get("session_id") if session else None
            if not ai_session_id and session and session.get("id"):
                minted_id = str(uuid.uuid4())
                title = (message.text or "")[:50].strip()
                if len(message.text or "") > 50:
                    title += "..."
                try:
                    database.create_session(
                        session_id=minted_id,
                        profile_id=profile_id,
                        project_id=project_id,
                        title=title or "Discord conversation",
                        api_user_id=api_user_id,
                        is_active=True,
                    )
                    database.update_connector_session_ai_session(
                        session.get("id"), minted_id
                    )
                    ai_session_id = minted_id
                    # Mirror execute_query's sidebar broadcast for new sessions so
                    # connector-started conversations appear without a refresh.
                    try:
                        from app.core.sync_engine import sync_engine
                        await sync_engine.broadcast_user_session_created(
                            user_id="admin",
                            session_id=minted_id,
                            source_device_id=None,
                        )
                    except Exception:
                        logger.debug("session-created broadcast failed", exc_info=True)
                except Exception:
                    # Fall back to letting execute_query create the session (tools
                    # then register from the 2nd message, as before the fix).
                    logger.exception(
                        "Failed to pre-create AI session for connector message"
                    )
                    ai_session_id = None

            # Send typing indicator and keep it alive while the query runs
            typing_task: Optional[asyncio.Task] = None
            if instance:
                # Fire one immediate typing indicator so the user sees it
                # before the query starts (the loop task may not yield in time)
                try:
                    await instance.send_typing_indicator(message.platform_chat_id)
                except Exception:
                    logger.debug("Initial typing indicator failed", exc_info=True)

                async def _keep_typing() -> None:
                    try:
                        while True:
                            await instance.send_typing_indicator(
                                message.platform_chat_id
                            )
                            await asyncio.sleep(4)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.debug("Typing indicator refresh failed", exc_info=True)

                typing_task = asyncio.create_task(_keep_typing())

            # Execute the query — import lazily to avoid circular imports
            from app.core import account_failover

            # Build connector context for the system prompt (memory + capabilities)
            overrides: Dict[str, Any] = {}
            if instance and connector_row:
                context_append = self._build_connector_context(
                    connector_row, instance, message.platform_chat_id
                )
                overrides["system_prompt_append"] = context_append

            try:
                result = await account_failover.execute_query_with_failover(
                    prompt=message.text,
                    profile_id=profile_id,
                    project_id=project_id,
                    session_id=ai_session_id,
                    api_user_id=api_user_id,
                    overrides=overrides or None,
                )
            finally:
                if typing_task:
                    typing_task.cancel()

            response_text = result.get("response", "I couldn't generate a response.")

            # Link the AI session to the connector session for conversation memory.
            # execute_query creates an AI session and returns its ID — store it
            # so the next message in this channel resumes the same conversation.
            ai_session_id = result.get("session_id")
            if ai_session_id and session:
                connector_session_id = session.get("id")
                if connector_session_id and session.get("session_id") != ai_session_id:
                    database.update_connector_session_ai_session(
                        connector_session_id, ai_session_id
                    )

            # Send response back through the connector
            sent = False
            if instance:
                sent = await instance.send_message(
                    OutgoingMessage(
                        platform_chat_id=message.platform_chat_id,
                        text=response_text,
                        reply_to_message_id=message.platform_message_id,
                    )
                )
                if not sent:
                    logger.error(
                        f"Failed to send response to platform for connector "
                        f"{message.connector_id} chat_id={message.platform_chat_id}"
                    )

            # Log the inbound message cycle and update last_event_at
            log_id = secrets.token_hex(8)
            database.create_connector_event_log(
                log_id=log_id,
                connector_id=message.connector_id,
                event_type="message.received",
                direction="inbound",
                payload={"text": message.text[:100], "platform_chat_id": message.platform_chat_id},
                status="delivered" if sent else "failed",
                error=None if sent else "Failed to send response",
            )
            database.update_connector_event_dispatched(message.connector_id, success=sent)

        except Exception as e:
            logger.error(
                f"Error handling incoming message from connector "
                f"{message.connector_id}: {e}",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Permission response handling
    # ------------------------------------------------------------------

    async def handle_permission_response(
        self, request_id: str, session_id: str, approved: bool
    ) -> None:
        """Forward a permission approval/denial from a messaging platform.

        Connectors (Telegram, Discord, Slack, etc.) send interactive
        Approve/Deny prompts and invoke this callback when the user presses a
        button. We translate the boolean into the permission_handler decision
        and resolve the pending request so the waiting tool call unblocks.

        broadcast_func is None because a connector response has no associated
        WebSocket UI session; permission_handler tolerates a None broadcaster
        (all queue-update broadcasts are guarded).
        """
        logger.info(
            f"Permission response: request={request_id} session={session_id} approved={approved}"
        )

        if not request_id or not session_id:
            logger.warning(
                "Ignoring connector permission response with missing "
                f"request_id/session_id (request={request_id!r} session={session_id!r})"
            )
            return

        from app.core.permission_handler import permission_handler

        result = await permission_handler.respond(
            request_id=request_id,
            session_id=session_id,
            decision="allow" if approved else "deny",
            broadcast_func=None,
        )
        logger.debug(f"Connector permission response resolved: {result}")

    # ------------------------------------------------------------------
    # Public accessors (avoids external code reaching into _connectors)
    # ------------------------------------------------------------------

    async def get_running_connector(self, connector_id: str) -> Optional[BaseConnector]:
        """Return a running connector instance by ID, or None."""
        async with self._lock:
            return self._connectors.get(connector_id)

    def get_running_instance(self, connector_id: str) -> Optional[BaseConnector]:
        """Return a running connector instance by ID without acquiring the async
        lock, for synchronous / tool-call resolution paths.

        Reads a single dict key (atomic in CPython); returns None when the
        connector is not running on this install. Lets external code (e.g. the
        Discord-native MCP tools) resolve the live instance without reaching
        into ``_connectors`` directly."""
        return self._connectors.get(connector_id)

    def get_running_connectors(self, connector_type: Optional[str] = None):
        """Yield (connector_id, instance) pairs, optionally filtered by type.

        Note: iterates over a snapshot of the dict to avoid holding the lock.
        """
        snapshot = list(self._connectors.items())
        for cid, instance in snapshot:
            if connector_type is None or instance.connector_type == connector_type:
                yield cid, instance

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def get_connector_status(self, connector_id: str) -> Optional[Dict]:
        """Return runtime status for a running connector, or None."""
        instance = self._connectors.get(connector_id)
        if not instance:
            return None
        return {
            "connector_id": connector_id,
            "status": instance.status.value,
            "status_message": instance.status_message,
        }

    # ------------------------------------------------------------------
    # Encryption helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_sensitive_fields(connector_type: str) -> List[str]:
        """Return the list of config keys that contain secrets for each type."""
        from app.core.connector_service import get_sensitive_fields
        return get_sensitive_fields(connector_type)

    @staticmethod
    def _decrypt_config(config: Dict, connector_type: str) -> Dict:
        """Decrypt sensitive fields in a connector config dict."""
        from app.core import encryption

        sensitive = ConnectorManager._get_sensitive_fields(connector_type)
        if not sensitive:
            return config

        decrypted = dict(config)
        for key in sensitive:
            value = decrypted.get(key)
            if value and isinstance(value, str):
                if encryption.is_encrypted(value):
                    if not encryption.is_encryption_ready():
                        logger.warning(
                            f"Cannot decrypt connector config field '{key}' — "
                            "encryption key not loaded"
                        )
                        decrypted[key] = None
                        continue
                    try:
                        decrypted[key] = encryption.decrypt_value(value)
                    except Exception as e:
                        logger.error(f"Failed to decrypt connector field '{key}': {e}")
                        decrypted[key] = None
        return decrypted

    @staticmethod
    def _encrypt_config(config: Dict, connector_type: str) -> Dict:
        """Encrypt sensitive fields in a connector config dict."""
        from app.core import encryption

        sensitive = ConnectorManager._get_sensitive_fields(connector_type)
        if not sensitive:
            return config

        encrypted = dict(config)
        for key in sensitive:
            value = encrypted.get(key)
            if value and isinstance(value, str) and not encryption.is_encrypted(value):
                if encryption.is_encryption_ready():
                    try:
                        encrypted[key] = encryption.encrypt_value(value)
                    except Exception as e:
                        logger.error(f"Failed to encrypt connector field '{key}': {e}")
                else:
                    raise ValueError(
                        f"Cannot store connector config field '{key}': "
                        "encryption is not available. Set the ENCRYPTION_KEY "
                        "environment variable before saving sensitive configuration."
                    )
        return encrypted


# Module-level singleton
connector_manager = ConnectorManager()
