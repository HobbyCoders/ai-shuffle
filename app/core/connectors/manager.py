"""
ConnectorManager — lifecycle management, event dispatch, and message routing.
"""

import asyncio
import logging
import secrets
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.core.connectors.base import (
    BaseConnector,
    IncomingMessage,
    OutgoingMessage,
)
from app.core.connectors.registry import get_connector_class
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)


class ConnectorManager:
    """Central hub for connector lifecycle, event dispatch, and message routing."""

    def __init__(self):
        self._connectors: Dict[str, BaseConnector] = {}
        self._lock = asyncio.Lock()
        self._event_relay = None

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
        # One isolated read; each connector is then started with an await, so
        # nothing after this can be folded into the same hop.
        connectors = await run_sync(database.get_active_connectors)
        logger.info(
            f"Evaluating {len(connectors)} active connector(s) for runner {me}"
        )

        from app.network import ownership

        for conn_row in connectors:
            cid = conn_row["id"]
            runner_id = conn_row.get("runner_id")
            try:
                # Worker network: the owner device runs the connector; an
                # unowned definition is claimed by the first device to see it,
                # and a definition owned elsewhere stays inactive here.
                network_rule = ownership.decide_connector(conn_row)
                if network_rule == "start":
                    await self.start_connector(cid)
                    continue
                if network_rule == "claim":
                    if await run_sync(ownership.claim, "connectors", cid):
                        logger.info(f"Claimed unowned connector {cid} for device {ownership.my_device_id()}")
                        await self.start_connector(cid)
                    else:
                        logger.info(f"Connector {cid} claimed by another device — skipping start")
                    continue
                if network_rule == "skip":
                    logger.info(f"Connector {cid} is owned by device {conn_row.get('owner_device_id')} — skipping start")
                    continue
                if runner_id == me:
                    await self.start_connector(cid)
                elif runner_id is None:
                    # Race-safe claim: only the install whose UPDATE flips the
                    # NULL owner starts it; losers skip.
                    # Its own hop: the claim decides whether the ``await``
                    # below runs, so it cannot be batched with anything.
                    if await run_sync(database.claim_connector_runner, cid, me):
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

    def start_event_relay(self) -> None:
        """Listen for lifecycle notifications addressed to this connector owner."""
        from app.core.config import settings
        from app.core.connectors.lifecycle_relay import LifecycleRelay

        if self._event_relay is None:
            self._event_relay = LifecycleRelay(
                settings.effective_instance_id, self._deliver_relayed_event
            )
        self._event_relay.start()

    async def shutdown_all(self) -> None:
        """Stop every running connector and its lifecycle relay."""
        if self._event_relay is not None:
            await self._event_relay.stop()
            self._event_relay = None
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
        def _prepare_blocking() -> Optional[Dict[str, Any]]:
            """Row read, config decrypt and impl-class lookup in ONE hop.

            Contiguous — no awaits between them — and the unknown-type branch
            STAMPS the error status, so the decide-and-write stays inside the
            same block rather than becoming a second hop. ``None`` means the
            caller returns ``False``; the logging is unchanged.
            """
            connector_row = database.get_connector(connector_id)
            if not connector_row:
                logger.error(f"Connector not found in DB: {connector_id}")
                return None

            from app.network import ownership
            try:
                decision = ownership.decide_connector(connector_row)
                if decision == "skip":
                    return None
                if decision == "claim" and not ownership.claim("connectors", connector_id):
                    return None
            except Exception:
                logger.exception("Network ownership unavailable; refusing connector start")
                return None

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
                return None
            return {"row": connector_row, "type": connector_type,
                    "config": config, "cls": cls}

        prepared = await run_sync(_prepare_blocking)
        if prepared is None:
            return False
        connector_type = prepared["type"]
        config = prepared["config"]
        cls = prepared["cls"]

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
            # The status flip has to land BEFORE connect() is awaited, so it is
            # its own hop; connect() is network work that stays on the loop.
            await run_sync(database.update_connector_status, connector_id, "connecting")
            success = await instance.connect()
        except Exception as e:
            logger.error(f"Connector {connector_id} connect() raised: {e}")
            await run_sync(
                database.update_connector_status, connector_id, "error", error=str(e)
            )
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
            await run_sync(
                database.update_connector_status,
                connector_id, "error", error="connect() returned False",
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

        # Single write after the disconnect await — one hop, nothing to batch.
        await run_sync(database.update_connector_status, connector_id, "disconnected")
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
        from app.core.config import settings
        from app.core.connectors.lifecycle_relay import publish

        subscribed = await run_sync(database.get_connectors_for_event, event_type)
        from app.network import ownership
        for conn_row in subscribed:
            if ownership.enrolled() and not ownership.owns(conn_row):
                # Another device holds this connector's session; it sees its
                # own sessions' events, so there is nothing to relay.
                continue
            owner = conn_row.get("runner_id")
            if owner and owner != settings.effective_instance_id:
                try:
                    await run_sync(publish, conn_row["id"], owner, event_type, data)
                except Exception:
                    logger.warning("Connector owner relay failed for %s", conn_row["id"], exc_info=True)
            else:
                await self._deliver_connector_event(conn_row, event_type, data)

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

    async def _deliver_relayed_event(self, connector_id: str, event_type: str, data: dict) -> None:
        """Recheck ownership/subscription; never re-dispatch webhooks or automations."""
        from app.core.config import settings

        row = await run_sync(database.get_connector, connector_id)
        from app.network import ownership
        if ownership.enrolled() and row and not ownership.owns(row):
            return
        if (not row or not row.get("is_active")
                or (not ownership.enrolled() and row.get("runner_id") != settings.effective_instance_id)
                or event_type not in (row.get("events") or [])):
            return
        await self._deliver_connector_event(row, event_type, data)

    async def _deliver_connector_event(self, conn_row: dict, event_type: str, data: dict) -> None:
        """Deliver to local platform chats linked to the event's session."""
        session_id = data.get("session_id")
        cid = conn_row["id"]
        log_id = secrets.token_hex(8)

        async with self._lock:
            instance = self._connectors.get(cid)

        if not instance:
            logger.debug(f"Connector {cid} not running — skipping event {event_type}")
            return

        try:
            # Determine which platform chats to notify. Either branch is a
            # blocking query (the broadcast branch opens its own cursor), so
            # the whole decision runs off-loop as one hop; exceptions still
            # land in this ``try``'s handler below.
            def _load_sessions() -> List[Dict[str, Any]]:
                if session_id:
                    rows = database.get_connector_sessions_for_ai_session(session_id)
                    # Filter to only sessions belonging to this connector
                    return [s for s in rows if s.get("connector_id") == cid]
                # Broadcast to all active sessions for this connector
                # (no dedicated DB helper — query directly)
                from app.db.base import get_db, rows_to_list
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT * FROM connector_sessions WHERE connector_id = %s AND is_active = TRUE",
                        (cid,),
                    )
                    return rows_to_list(cursor.fetchall())

            sessions = await run_sync(_load_sessions)

            if not sessions:
                logger.debug(
                    f"No connector sessions linked for connector {cid} "
                    f"event={event_type} session_id={session_id} — skipping"
                )
                return

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

            # Both writes are contiguous — one hop for the pair.
            def _log_delivery() -> None:
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

            await run_sync(_log_delivery)

        except Exception as e:
            logger.error(f"Event dispatch failed for connector {cid}: {e}")

            def _log_failure(err: str = str(e)) -> None:
                database.create_connector_event_log(
                    log_id=log_id,
                    connector_id=cid,
                    event_type=event_type,
                    direction="outbound",
                    payload=data,
                    status="failed",
                    error=err,
                )
                database.update_connector_event_dispatched(cid, success=False)

            await run_sync(_log_failure)

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
            "and key decisions should be persisted to AGENTS.md in the working directory. "
            "Read AGENTS.md at the start of each conversation to recall prior context. "
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

        Resolves (or creates) a connector session and its linked AI session,
        enrols the message as a durable ``source=connector`` turn on the v2 turn
        queue, waits for the session to drain, and sends the assistant's reply
        back through the connector.

        Going through the queue is what keeps a chatty channel safe: two
        messages seconds apart used to start two concurrent agent loops against
        the same session (a forked conversation and spend nobody accounted for).
        Now the second one queues behind the live turn and runs when it settles.
        """
        try:
            from app.network import ownership

            def _can_receive():
                row = database.get_connector(message.connector_id)
                return not ownership.enrolled() or bool(row and ownership.owns(row))

            # An existing connection can still deliver callbacks after revocation.
            # Fail closed before the very first external acknowledgment.
            if not await run_sync(_can_receive):
                return

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

            def _resolve_routing_blocking() -> Optional[Dict[str, Any]]:
                """Session lookup/creation + connector row + API user + profile
                fallback: FIVE contiguous round trips, ONE thread hop.

                Nothing between them awaits, and the "no session row yet →
                create one" step is a read-decide-write that must not be split
                across hops (two inbound messages racing on a fresh chat would
                otherwise both decide to create the session). ``None`` means
                "no profiles available" — the caller returns, exactly as the
                inline code did after logging the same error here.
                """
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

                # Resolve profile / project / run config from the API user
                api_user = database.get_api_user(api_user_id) if api_user_id else None
                profile_id = None
                project_id = None
                api_user_model = None
                api_user_effort = None
                if api_user:
                    profile_ids = api_user.get("profile_ids", [])
                    profile_id = profile_ids[0] if profile_ids else None
                    project_id = api_user.get("project_id")
                    api_user_model = api_user.get("model")
                    api_user_effort = api_user.get("effort")

                # Fall back to the first available profile when none is configured
                if not profile_id:
                    profiles = database.get_all_profiles()
                    if profiles:
                        profile_id = profiles[0]["id"]
                    else:
                        logger.error("No profiles available for connector query")
                        return None
                return {
                    "session": session,
                    "connector_row": connector_row,
                    "api_user_id": api_user_id,
                    "profile_id": profile_id,
                    "project_id": project_id,
                    "api_user_model": api_user_model,
                    "api_user_effort": api_user_effort,
                }

            routing = await run_sync(_resolve_routing_blocking)
            if routing is None:
                return
            session = routing["session"]
            connector_row = routing["connector_row"]
            api_user_id = routing["api_user_id"]
            profile_id = routing["profile_id"]
            project_id = routing["project_id"]
            api_user_model = routing["api_user_model"]
            api_user_effort = routing["api_user_effort"]

            # Pre-mint + link the AI session BEFORE the turn is submitted.
            #
            # Two reasons this has to happen first. Per-session builtin tools
            # (notably the Discord-native ones) are registered at query build
            # time by resolving session_id -> linked connector session, so a
            # link written only after the first turn left those tools missing on
            # the very first message of a channel. And ``turn_manager.submit``
            # enqueues against an EXISTING session row — it never creates one —
            # so without the row there is nothing to enrol the turn on. On
            # subsequent turns the connector session already carries the link,
            # so this is a no-op.
            ai_session_id = session.get("session_id") if session else None
            link_written = False
            if not ai_session_id and session and session.get("id"):
                minted_id = str(uuid.uuid4())
                title = (message.text or "")[:50].strip()
                if len(message.text or "") > 50:
                    title += "..."

                def _mint_blocking() -> None:
                    """The session INSERT and its connector-session link, as
                    ONE hop and ONE unit: a link written without its session
                    row (or vice versa) is exactly the split this must not
                    allow. The sidebar broadcast below stays on the loop — it
                    is a coroutine and would be dropped from a worker thread."""
                    database.create_session(
                        session_id=minted_id,
                        profile_id=profile_id,
                        project_id=project_id,
                        title=title or "Discord conversation",
                        api_user_id=api_user_id,
                        is_active=True,
                        # Run binding from the API user's preconfigured
                        # selection. Permission mode is pinned to Ask so a
                        # profile's bypass/accept-edits setting can never leak
                        # into a headless connector conversation — prompts ride
                        # the connector funnel instead.
                        model=api_user_model,
                        effort=api_user_effort,
                        permission_mode="default",
                    )
                    database.update_connector_session_ai_session(
                        session.get("id"), minted_id
                    )

                try:
                    await run_sync(_mint_blocking)
                    ai_session_id = minted_id
                    link_written = True
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
                    logger.exception(
                        "Failed to pre-create AI session for connector message"
                    )
                    ai_session_id = None

            # No session row means there is nothing to enrol a turn on, so bail
            # rather than falling back to a session-less query (that fallback is
            # exactly the concurrent-loop path this method no longer takes).
            # Nothing to clean up: the typing indicator starts below.
            if not ai_session_id:
                logger.error(
                    f"No AI session for connector {message.connector_id} "
                    f"chat_id={message.platform_chat_id} — dropping the message"
                )
                return

            # Send typing indicator and keep it alive while the turn runs
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

            # Run the message as a queued turn — import lazily to avoid circular
            # imports.
            from app.core import turn_manager, turn_outcome

            # Build connector context for the system prompt (memory + capabilities)
            overrides: Dict[str, Any] = {}
            if instance and connector_row:
                context_append = self._build_connector_context(
                    connector_row, instance, message.platform_chat_id
                )
                overrides["system_prompt_append"] = context_append

            # Re-assert the API user's run binding every turn (``connector`` is
            # a _BINDING_SOURCES member, so these persist to the session row —
            # editing the API user re-binds existing conversations). The
            # permission mode is ALWAYS Ask: the agent's configured tools list
            # governs, and every non-whitelisted tool raises a permission
            # request that funnels out through the connector.
            if api_user_model:
                overrides["model"] = api_user_model
            if api_user_effort:
                overrides["effort"] = api_user_effort
            overrides["permission_mode"] = "default"

            try:
                # Enqueue, then block until the session drains. If a turn is
                # already live this one waits its place in the queue instead of
                # starting a second loop against the same session.
                await turn_manager.submit_async(
                    ai_session_id,
                    "connector",
                    prompt=message.text,
                    profile_id=profile_id,
                    project_id=project_id,
                    overrides=overrides or None,
                )
                idle = await turn_manager.wait_session_idle(
                    ai_session_id, timeout=3600
                )
            finally:
                if typing_task:
                    typing_task.cancel()

            if not idle:
                # The turn is durable and keeps running; only our wait gave up,
                # so never interrupt here — just tell the channel.
                logger.error(
                    f"Connector turn did not settle within the wait window for "
                    f"session {ai_session_id}"
                )
                response_text = "I couldn't generate a response."
            else:
                # Both helpers load the session's whole event log — two
                # blocking reads back to back, so they share one hop.
                def _read_outcome() -> Tuple[Dict[str, Any], str]:
                    return (
                        turn_outcome.final_turn_outcome(ai_session_id),
                        turn_outcome.extract_final_assistant_text(ai_session_id),
                    )

                outcome, final_text = await run_sync(_read_outcome)
                response_text = final_text or "I couldn't generate a response."
                if outcome["error"]:
                    # Still send whatever text the turn produced before it died.
                    logger.error(
                        f"Connector turn failed for session {ai_session_id}: "
                        f"{outcome['error']}"
                    )

            # The AI session id is known up front now, so the link is already
            # written for sessions minted above. This only repairs legacy rows
            # whose link points somewhere else (or nowhere).
            if (
                not link_written
                and session
                and session.get("id")
                and session.get("session_id") != ai_session_id
            ):
                # Isolated repair write between two awaits — one hop.
                await run_sync(
                    database.update_connector_session_ai_session,
                    session.get("id"), ai_session_id,
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

            # Log the inbound message cycle and update last_event_at — two
            # contiguous writes, one hop.
            log_id = secrets.token_hex(8)

            def _log_inbound() -> None:
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

            await run_sync(_log_inbound)

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
                try:
                    encrypted[key] = encryption.encrypt_value(value)
                except Exception:
                    raise ValueError("Cannot store connector secret: credential storage requires local recovery.") from None
        return encrypted


# Module-level singleton
connector_manager = ConnectorManager()
