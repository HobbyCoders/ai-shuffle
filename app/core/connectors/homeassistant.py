"""HomeAssistantConnector — Home Assistant integration via REST API (httpx)."""

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from app.core.connectors.base import (
    BaseConnector,
    ConnectorCapabilities,
    ConnectorStatus,
    IncomingMessage,
    IncomingMessageCallback,
    OutgoingMessage,
    PermissionPrompt,
    PermissionResponseCallback,
    TriggerCallback,
    TriggerDef,
    chunk_text,
)
from app.core.connectors import homeassistant_actions

logger = logging.getLogger(__name__)

HA_TIMEOUT = 10.0  # seconds


class HomeAssistantConnector(BaseConnector):
    """
    Connector for Home Assistant using the REST API.

    Uses httpx (no extra dependencies) to communicate with a Home
    Assistant instance.  Polls entity states at a configurable interval
    and emits IncomingMessage events when an entity's state changes.

    Config keys:
        url (str): Home Assistant base URL (e.g. "http://homeassistant.local:8123").
        access_token (str): Long-lived access token.
        entity_filter (str, optional): Comma-separated entity-id prefixes
            (e.g. "light.,switch.,climate.").  When set, only entities whose
            id starts with one of the prefixes are tracked.
        poll_interval_seconds (str, optional): Seconds between state polls.
            Defaults to "60".
    """

    MAX_MESSAGE_LENGTH = 4000
    CAPABILITIES = ConnectorCapabilities(is_messaging=False)

    TRIGGERS = [
        TriggerDef(
            id="state_changed",
            name="State Changed",
            description="Fires when an entity's state changes",
            payload_schema={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "old_state": {"type": "string"},
                    "new_state": {"type": "string"},
                    "attributes": {"type": "object"},
                },
            },
        ),
    ]

    # Full declarative action catalog (see homeassistant_actions.py), exposed
    # through the homeassistant_actions/homeassistant_invoke gateway tool pair.
    # Legacy action ids (call_service, toggle_entity, get_states) are preserved.
    ACTIONS = homeassistant_actions.HOMEASSISTANT_ACTIONS

    # No per-tool MCP registration: the action gateway replaces the legacy
    # homeassistant_control tool.
    TOOLS: List[Any] = []

    SUPPORTS_ACTION_GATEWAY = True

    def __init__(
        self,
        connector_id: str,
        connector_type: str,
        config: Dict[str, Any],
        on_incoming_message: IncomingMessageCallback,
        on_permission_response: PermissionResponseCallback,
        on_trigger: Optional[TriggerCallback] = None,
    ):
        super().__init__(
            connector_id, connector_type, config,
            on_incoming_message, on_permission_response,
            on_trigger=on_trigger,
        )
        self._http_client: Optional[httpx.AsyncClient] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._entity_states: Dict[str, str] = {}  # entity_id -> state value

        # Parse entity filter prefixes
        raw_filter = self.config.get("entity_filter", "")
        if raw_filter and raw_filter.strip():
            self._entity_filter: List[str] = [
                p.strip() for p in raw_filter.split(",") if p.strip()
            ]
        else:
            self._entity_filter = []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        """Return authorization headers for the HA REST API."""
        return {
            "Authorization": f"Bearer {self.config['access_token']}",
            "Content-Type": "application/json",
        }

    async def _api_get(self, path: str) -> httpx.Response:
        """Make an authenticated GET request to Home Assistant."""
        if self._http_client is None:
            raise RuntimeError("HTTP client is not initialised")
        return await self._http_client.get(
            f"{self.config['url'].rstrip('/')}{path}",
            headers=self._headers(),
        )

    async def _api_post(
        self, path: str, data: Optional[Dict[str, Any]] = None
    ) -> httpx.Response:
        """Make an authenticated POST request to Home Assistant."""
        if self._http_client is None:
            raise RuntimeError("HTTP client is not initialised")
        return await self._http_client.post(
            f"{self.config['url'].rstrip('/')}{path}",
            headers=self._headers(),
            json=data or {},
        )

    def _matches_filter(self, entity_id: str) -> bool:
        """Return True if the entity matches the configured filter (or no filter is set)."""
        if not self._entity_filter:
            return True
        return any(entity_id.startswith(prefix) for prefix in self._entity_filter)

    def _resolve_entity(self, user_input: str) -> Optional[str]:
        """
        Resolve a user-provided entity reference to an actual entity_id.

        Tries exact match first, then partial match against entity_id and
        friendly_name stored in ``_entity_states``.
        """
        user_input = user_input.strip()

        # Exact match
        if user_input in self._entity_states:
            return user_input

        # Normalise: replace spaces with underscores, lower-case
        normalised = user_input.lower().replace(" ", "_")

        # Check against known entity_ids (partial / suffix match)
        for eid in self._entity_states:
            if normalised == eid:
                return eid
            # e.g. user says "kitchen lights" and entity is "light.kitchen"
            # Try matching the object_id portion
            _, _, object_id = eid.partition(".")
            if normalised == object_id:
                return eid
            # Substring match (entity contains normalised input)
            if normalised in eid or normalised in object_id:
                return eid

        return None

    def _format_entity_line(self, entity: Dict[str, Any]) -> str:
        """Format one entity dict as a summary line."""
        eid = entity.get("entity_id", "?")
        state = entity.get("state", "?")
        attrs = entity.get("attributes", {})
        friendly = attrs.get("friendly_name", "")
        name_part = f" ({friendly})" if friendly else ""
        # Compact attribute summary — include a few useful keys
        extra_keys = ["unit_of_measurement", "temperature", "brightness", "color_temp"]
        extras = []
        for k in extra_keys:
            if k in attrs:
                extras.append(f"{k}={attrs[k]}")
        extra_str = f" [{', '.join(extras)}]" if extras else ""
        return f"{eid}{name_part}: {state}{extra_str}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _do_connect(self) -> None:
        """Connect to Home Assistant, validate API, load initial states."""
        self._http_client = httpx.AsyncClient(timeout=HA_TIMEOUT)

        try:
            # Validate connectivity
            resp = await self._api_get("/api/")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"HA API returned {resp.status_code}: {resp.text[:200]}"
                )
            body = resp.json()
            if body.get("message") != "API running.":
                raise RuntimeError(f"Unexpected API response: {body}")

            # Load initial entity states
            states_resp = await self._api_get("/api/states")
            if states_resp.status_code != 200:
                raise RuntimeError(
                    f"Failed to fetch states: {states_resp.status_code}"
                )

            for entity in states_resp.json():
                eid = entity.get("entity_id", "")
                if self._matches_filter(eid):
                    self._entity_states[eid] = entity.get("state", "")

            logger.info(
                "HomeAssistantConnector %s loaded %d entities",
                self.connector_id, len(self._entity_states),
            )

            # Start polling task
            self._poll_task = asyncio.create_task(self._poll_states())

        except Exception:
            if self._http_client is not None:
                await self._http_client.aclose()
                self._http_client = None
            raise

    async def disconnect(self) -> None:
        """Stop polling and close the HTTP client."""
        try:
            if self._poll_task is not None and not self._poll_task.done():
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass
            if self._http_client is not None:
                await self._http_client.aclose()
                self._http_client = None
        except Exception as exc:
            logger.warning(
                "Error during HomeAssistantConnector disconnect: %s", exc
            )
        finally:
            self._entity_states.clear()
            self._status = ConnectorStatus.DISCONNECTED
            self._status_message = None
            logger.info("HomeAssistantConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_states(self) -> None:
        """Periodically poll entity states and emit change events."""
        interval = int(self.config.get("poll_interval_seconds", "60"))
        consecutive_failures = 0
        max_consecutive_failures = 5
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    resp = await self._api_get("/api/states")
                    if resp.status_code != 200:
                        consecutive_failures += 1
                        logger.warning(
                            "HomeAssistantConnector %s poll returned %s (%d/%d)",
                            self.connector_id, resp.status_code, consecutive_failures, max_consecutive_failures,
                        )
                    else:
                        consecutive_failures = 0
                        await self._poll_recovered()
                        for entity in resp.json():
                            eid = entity.get("entity_id", "")
                            if not self._matches_filter(eid):
                                continue
                            new_state = entity.get("state", "")
                            old_state = self._entity_states.get(eid)

                            if old_state is not None and new_state != old_state:
                                friendly = (
                                    entity.get("attributes", {})
                                    .get("friendly_name", eid)
                                )
                                text = (
                                    f"{friendly} ({eid}) changed from "
                                    f"{old_state} to {new_state}"
                                )
                                msg = IncomingMessage(
                                    connector_id=self.connector_id,
                                    platform_chat_id="homeassistant",
                                    platform_user_id="homeassistant",
                                    platform_user_name="Home Assistant",
                                    text=text,
                                    platform_message_id=f"{eid}-{int(time.time())}",
                                    metadata={
                                        "entity_id": eid,
                                        "old_state": old_state,
                                        "new_state": new_state,
                                    },
                                )
                                await self._on_incoming_message(msg)
                                await self.emit_trigger("state_changed", {
                                    "entity_id": eid,
                                    "old_state": old_state,
                                    "new_state": new_state,
                                    "attributes": entity.get("attributes", {}),
                                })

                            self._entity_states[eid] = new_state

                except Exception as exc:
                    consecutive_failures += 1
                    logger.warning(
                        "HomeAssistantConnector %s poll error (%d/%d): %s",
                        self.connector_id, consecutive_failures, max_consecutive_failures, exc,
                    )

                if consecutive_failures >= max_consecutive_failures:
                    self._status = ConnectorStatus.ERROR
                    self._status_message = f"Repeated poll failures ({consecutive_failures})"
                    logger.error("HomeAssistantConnector %s entering ERROR state after %d consecutive failures", self.connector_id, consecutive_failures)
                    try:
                        from app.db import database
                        from app.db.async_database import run_sync
                        await run_sync(database.update_connector_status, self.connector_id, "error", error=self._status_message)
                    except Exception:
                        logger.debug("Failed to update DB status", exc_info=True)

        except asyncio.CancelledError:
            logger.info(
                "HomeAssistantConnector %s poll task cancelled", self.connector_id
            )

    async def _poll_recovered(self) -> None:
        """Leave ERROR once a poll succeeds again after repeated failures."""
        if self._status != ConnectorStatus.ERROR:
            return
        self._status = ConnectorStatus.CONNECTED
        self._status_message = None
        logger.info("HomeAssistantConnector %s recovered: polling succeeded again", self.connector_id)
        try:
            from app.db import database
            from app.db.async_database import run_sync
            await run_sync(database.update_connector_status, self.connector_id, ConnectorStatus.CONNECTED.value)
        except Exception:
            logger.debug("Failed to update DB status", exc_info=True)

    # ------------------------------------------------------------------
    # Outgoing — command parsing
    # ------------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> bool:
        """
        Parse the outgoing text as a Home Assistant command and execute it.

        Supported commands:
            list entities / entities / status
            turn on {entity}
            turn off {entity}
            toggle {entity}
            set {entity} to {value}
            get {entity} / state {entity}
            call service {domain}.{service} {entity_id} {json_data?}

        Returns True if the command was recognised and executed.
        """
        if self._http_client is None:
            logger.error("Cannot send message: HTTP client is not initialised")
            return False

        text = message.text.strip()
        text_lower = text.lower()
        # Commands match case-insensitively on the original text: entity
        # references resolve case-insensitively anyway, but values must keep
        # their case (input_select options and input_text are case-sensitive).
        flags = re.IGNORECASE

        try:
            # --- list entities / entities / status ---
            if text_lower in ("list entities", "entities", "status"):
                return await self._cmd_list_entities(message)

            # --- turn on {entity} ---
            m = re.match(r"turn\s+on\s+(.+)", text, flags)
            if m:
                return await self._cmd_service(
                    "homeassistant", "turn_on", m.group(1), message
                )

            # --- turn off {entity} ---
            m = re.match(r"turn\s+off\s+(.+)", text, flags)
            if m:
                return await self._cmd_service(
                    "homeassistant", "turn_off", m.group(1), message
                )

            # --- toggle {entity} ---
            m = re.match(r"toggle\s+(.+)", text, flags)
            if m:
                return await self._cmd_service(
                    "homeassistant", "toggle", m.group(1), message
                )

            # --- set {entity} to {value} ---
            m = re.match(r"set\s+(.+?)\s+to\s+(.+)", text, flags)
            if m:
                return await self._cmd_set(m.group(1), m.group(2), message)

            # --- get {entity} / state {entity} ---
            m = re.match(r"(?:get|state)\s+(.+)", text, flags)
            if m:
                return await self._cmd_get_state(m.group(1), message)

            # --- call service {domain}.{service} {entity_id} {json_data?} ---
            m = re.match(
                r"call\s+service\s+([\w]+)\.([\w]+)\s+([\w.]+)(?:\s+(.+))?",
                text,
                flags,
            )
            if m:
                # Domains, services and entity IDs are lower-case in Home
                # Assistant; the JSON data keeps its case.
                return await self._cmd_call_service(
                    m.group(1).lower(), m.group(2).lower(), m.group(3).lower(),
                    m.group(4), message,
                )

            # Unrecognised command
            return False

        except Exception as exc:
            logger.exception(
                "HomeAssistantConnector %s command error: %s",
                self.connector_id, exc,
            )
            return False

    # ---- Command implementations ----

    async def _cmd_list_entities(self, original: OutgoingMessage) -> bool:
        """Fetch and return all matching entities with state summaries."""
        resp = await self._api_get("/api/states")
        if resp.status_code != 200:
            await self._send_reply(
                original, f"Failed to fetch entities (HTTP {resp.status_code})"
            )
            return True

        lines: List[str] = []
        for entity in resp.json():
            eid = entity.get("entity_id", "")
            if self._matches_filter(eid):
                lines.append(self._format_entity_line(entity))

        if not lines:
            await self._send_reply(original, "No matching entities found.")
        else:
            header = f"Entities ({len(lines)}):\n"
            body = "\n".join(lines)
            for chunk in chunk_text(header + body, self.MAX_MESSAGE_LENGTH):
                await self._send_reply(original, chunk)
        return True

    async def _cmd_service(
        self,
        domain: str,
        service: str,
        entity_ref: str,
        original: OutgoingMessage,
    ) -> bool:
        """Call a HA service on a resolved entity."""
        entity_id = self._resolve_entity(entity_ref)
        if entity_id is None:
            await self._send_reply(
                original, f"Entity not found: {entity_ref}"
            )
            return True

        resp = await self._api_post(
            f"/api/services/{domain}/{service}",
            data={"entity_id": entity_id},
        )
        if 200 <= resp.status_code < 300:
            await self._send_reply(
                original, f"OK: {service.replace('_', ' ')} {entity_id}"
            )
        else:
            await self._send_reply(
                original,
                f"Failed ({resp.status_code}): {resp.text[:200]}",
            )
        return True

    async def _cmd_set(
        self,
        entity_ref: str,
        value: str,
        original: OutgoingMessage,
    ) -> bool:
        """Determine the domain from the entity and call the appropriate service."""
        entity_id = self._resolve_entity(entity_ref)
        if entity_id is None:
            await self._send_reply(
                original, f"Entity not found: {entity_ref}"
            )
            return True

        domain = entity_id.split(".")[0]
        wanted = value.strip().lower()

        # Map domains to services + data keys
        service_map: Dict[str, tuple] = {
            "light": ("light", "turn_on", "brightness"),
            "climate": ("climate", "set_temperature", "temperature"),
            "cover": ("cover", "set_cover_position", "position"),
            "fan": ("fan", "set_percentage", "percentage"),
            "input_number": ("input_number", "set_value", "value"),
            "input_text": ("input_text", "set_value", "value"),
            "input_select": ("input_select", "select_option", "option"),
            "number": ("number", "set_value", "value"),
        }

        if wanted in ("on", "off", "toggle"):
            # "set X to off" is a power command whatever the domain; it must
            # never fall through to turn_on.
            service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}[wanted]
            resp = await self._api_post(
                f"/api/services/homeassistant/{service}",
                data={"entity_id": entity_id},
            )
        elif domain in service_map:
            svc_domain, svc_name, data_key = service_map[domain]
            # Try to convert to numeric if possible
            try:
                parsed_value: Any = float(value)
                if parsed_value == int(parsed_value):
                    parsed_value = int(parsed_value)
            except ValueError:
                parsed_value = value

            payload = {"entity_id": entity_id, data_key: parsed_value}
            resp = await self._api_post(
                f"/api/services/{svc_domain}/{svc_name}", data=payload
            )
        else:
            # No value service is known for this domain; guessing a service
            # could do the opposite of what was asked, so say so instead.
            await self._send_reply(
                original,
                f"Don't know how to set {entity_id} to {value}. Use "
                f"'turn on/off {entity_ref}' or "
                f"'call service <domain>.<service> {entity_id} {{json}}'.",
            )
            return True

        if 200 <= resp.status_code < 300:
            await self._send_reply(
                original, f"OK: set {entity_id} to {value}"
            )
        else:
            await self._send_reply(
                original,
                f"Failed ({resp.status_code}): {resp.text[:200]}",
            )
        return True

    async def _cmd_get_state(
        self, entity_ref: str, original: OutgoingMessage
    ) -> bool:
        """Fetch and return detailed state + attributes for an entity."""
        entity_id = self._resolve_entity(entity_ref)
        if entity_id is None:
            await self._send_reply(
                original, f"Entity not found: {entity_ref}"
            )
            return True

        resp = await self._api_get(f"/api/states/{entity_id}")
        if resp.status_code != 200:
            await self._send_reply(
                original,
                f"Failed to get state ({resp.status_code}): {resp.text[:200]}",
            )
            return True

        data = resp.json()
        state = data.get("state", "?")
        attrs = data.get("attributes", {})
        friendly = attrs.get("friendly_name", entity_id)
        last_changed = data.get("last_changed", "?")

        lines = [
            f"{friendly} ({entity_id})",
            f"State: {state}",
            f"Last changed: {last_changed}",
            "",
            "Attributes:",
        ]
        for k, v in attrs.items():
            lines.append(f"  {k}: {v}")

        body = "\n".join(lines)
        for chunk in chunk_text(body, self.MAX_MESSAGE_LENGTH):
            await self._send_reply(original, chunk)
        return True

    async def _cmd_call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        json_data_str: Optional[str],
        original: OutgoingMessage,
    ) -> bool:
        """Call an arbitrary HA service."""
        payload: Dict[str, Any] = {"entity_id": entity_id}
        if json_data_str:
            try:
                extra = json.loads(json_data_str)
                if isinstance(extra, dict):
                    payload.update(extra)
            except json.JSONDecodeError:
                await self._send_reply(
                    original, f"Invalid JSON data: {json_data_str}"
                )
                return True

        resp = await self._api_post(
            f"/api/services/{domain}/{service}", data=payload
        )
        if 200 <= resp.status_code < 300:
            await self._send_reply(
                original, f"OK: called {domain}.{service} on {entity_id}"
            )
        else:
            await self._send_reply(
                original,
                f"Failed ({resp.status_code}): {resp.text[:200]}",
            )
        return True

    # ---- Synthetic reply helper ----

    async def _send_reply(self, original: OutgoingMessage, text: str) -> None:
        """Emit a synthetic IncomingMessage as a reply to a command."""
        msg = IncomingMessage(
            connector_id=self.connector_id,
            platform_chat_id=original.platform_chat_id,
            platform_user_id="homeassistant",
            platform_user_name="Home Assistant",
            text=text,
            platform_message_id=f"ha-reply-{int(time.time() * 1000)}",
            metadata={"synthetic": True},
        )
        await self._on_incoming_message(msg)

    # ------------------------------------------------------------------
    # Permission prompt (not supported)
    # ------------------------------------------------------------------

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Not applicable for Home Assistant."""
        return False

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """
        Send a notification by creating a persistent_notification in Home Assistant.
        """
        if self._http_client is None:
            logger.error("Cannot send notification: HTTP client is not initialised")
            return False

        try:
            indicators = {
                "session.started": "[Started]",
                "session.complete": "[Complete]",
                "session.error": "[Error]",
                "permission.requested": "[Permission]",
            }
            prefix = indicators.get(event_type, f"[{event_type}]")

            if event_type == "session.complete":
                duration = data.get("duration_seconds", 0)
                cost = data.get("total_cost", 0)
                text = (
                    f"{prefix} Session Complete\n\n"
                    f"Duration: {duration:.1f}s\n"
                    f"Cost: ${cost:.4f}"
                )
            elif event_type == "session.error":
                error_msg = data.get("error_message", "Unknown error")
                text = f"{prefix} Session Error\n\n{error_msg}"
            else:
                text = f"{prefix} {event_type}\n\n{data}"

            resp = await self._api_post(
                "/api/services/persistent_notification/create",
                data={
                    "title": f"AI Shuffle: {event_type}",
                    "message": text,
                    "notification_id": f"ai_shuffle_{int(time.time() * 1000)}",
                },
            )
            if 200 <= resp.status_code < 300:
                return True

            logger.warning(
                "HomeAssistantConnector %s notification failed (%s): %s",
                self.connector_id, resp.status_code, resp.text[:200],
            )
            return False

        except Exception as exc:
            logger.exception(
                "Failed to send HomeAssistant notification: %s", exc
            )
            return False

    # ------------------------------------------------------------------
    # Actions & Tools
    # ------------------------------------------------------------------

    async def execute_action(
        self,
        action_id: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a catalog action (see homeassistant_actions.py)."""
        return await homeassistant_actions.execute(self, action_id, params, context)

    def get_context_instructions(self, platform_chat_id: str) -> Optional[str]:
        return (
            "HOME ASSISTANT TOOLS: Call homeassistant_actions to discover what "
            "you can do (read entity states and history, turn devices on/off, "
            "set values, call any HA service, render templates, fire events, "
            "create notifications) with each action's input schema, then call "
            "homeassistant_invoke with {action, params} to act. Use get_states "
            "to find entity IDs before controlling devices, and call_service "
            "with list_services for anything without a dedicated action."
        )

    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a Home Assistant tool call by routing to the appropriate action."""
        if tool_id != "homeassistant_control":
            return {"success": False, "error": f"Unknown tool: {tool_id}"}

        command = params.get("command")
        if not command:
            return {"success": False, "error": "command is required"}

        if command == "call_service":
            return await self.execute_action("call_service", {
                "domain": params.get("domain"),
                "service": params.get("service"),
                "entity_id": params.get("entity_id"),
                "data": params.get("data"),
            })

        if command == "toggle":
            return await self.execute_action("toggle_entity", {
                "entity_id": params.get("entity_id"),
            })

        if command == "get_states":
            return await self.execute_action("get_states", {})

        return {"success": False, "error": f"Unknown command: {command}"}

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    async def test_connection(self) -> Dict[str, Any]:
        """Validate credentials by fetching the HA config endpoint."""
        try:
            async with httpx.AsyncClient(timeout=HA_TIMEOUT) as client:
                resp = await client.get(
                    f"{self.config['url'].rstrip('/')}/api/config",
                    headers=self._headers(),
                )

            if resp.status_code == 200:
                data = resp.json()
                version = data.get("version", "unknown")
                location = data.get("location_name", "unknown")
                return {
                    "success": True,
                    "message": (
                        f"Connected to Home Assistant {version} "
                        f"(location: {location})"
                    ),
                }

            if resp.status_code == 401:
                return {
                    "success": False,
                    "message": "Authentication failed -- check access_token",
                }

            return {
                "success": False,
                "message": f"API returned {resp.status_code}: {resp.text[:200]}",
            }

        except Exception as exc:
            return {"success": False, "message": str(exc)}
