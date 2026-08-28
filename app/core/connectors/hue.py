"""HueConnector — Philips Hue smart-lighting integration via local Bridge REST API."""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from app.core.connectors import hue_actions
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

logger = logging.getLogger(__name__)

# Color temperature presets (mirek values)
COLOR_TEMP_PRESETS: Dict[str, int] = {
    "warm": 400,
    "neutral": 300,
    "cool": 200,
}


class HueConnector(BaseConnector):
    """
    Connector for Philips Hue smart lights via the local Bridge REST API.

    Communicates directly with a Hue Bridge on the local network using
    httpx.  No extra dependencies are required beyond httpx.

    Config keys:
        bridge_ip (str): IP address of the Hue Bridge on the local network.
        api_key (str): Hue Bridge API key (username) for authentication.
        poll_interval_seconds (str, optional): Seconds between state-change
            polls.  Defaults to "60".
    """

    MAX_MESSAGE_LENGTH = 4000
    CAPABILITIES = ConnectorCapabilities(is_messaging=False)

    TRIGGERS = [
        TriggerDef(
            id="light_state_changed",
            name="Light State Changed",
            description="Fires when a light's state changes",
            payload_schema={
                "type": "object",
                "properties": {
                    "light_id": {"type": "string"},
                    "light_name": {"type": "string"},
                    "on": {"type": "boolean"},
                    "brightness": {"type": "integer"},
                    "hue": {"type": "integer"},
                    "saturation": {"type": "integer"},
                },
            },
        ),
    ]

    ACTIONS = hue_actions.HUE_ACTIONS
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
        self._lights: Dict[str, str] = {}        # light_id -> name
        self._light_states: Dict[str, Dict[str, Any]] = {}  # light_id -> last-known state

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _api_url(self, path: str) -> str:
        """Build the full URL for a Hue Bridge API endpoint."""
        bridge_ip = self.config["bridge_ip"]
        api_key = self.config["api_key"]
        return f"http://{bridge_ip}/api/{api_key}{path}"

    async def _api_get(self, path: str) -> httpx.Response:
        """Make a GET request to the Hue Bridge API."""
        if self._http_client is None:
            raise RuntimeError("HTTP client is not initialised")
        return await self._http_client.get(self._api_url(path))

    async def _api_put(self, path: str, data: Dict[str, Any]) -> httpx.Response:
        """Make a PUT request to the Hue Bridge API."""
        if self._http_client is None:
            raise RuntimeError("HTTP client is not initialised")
        return await self._http_client.put(self._api_url(path), json=data)

    def _find_light(self, name_or_id: str) -> Optional[str]:
        """
        Match a light by name (case-insensitive partial match) or numeric ID.

        Returns the light ID string, or ``None`` if no match is found.
        """
        # Direct ID match
        if name_or_id in self._lights:
            return name_or_id

        # Case-insensitive partial name match
        query = name_or_id.lower()
        for light_id, light_name in self._lights.items():
            if query in light_name.lower():
                return light_id

        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _do_connect(self) -> None:
        """
        Create an HTTP client, validate the bridge connection by fetching
        the lights list, cache it, and optionally start the polling task.
        """
        self._http_client = httpx.AsyncClient(timeout=10.0)

        try:
            # Validate connection by fetching lights
            response = await self._api_get("/lights")

            if response.status_code != 200:
                raise RuntimeError(
                    f"Bridge API returned {response.status_code}: {response.text[:200]}"
                )

            lights_data = response.json()

            # Check for Hue API error responses (list of error dicts)
            if isinstance(lights_data, list):
                errors = [e.get("error", {}).get("description", "") for e in lights_data if "error" in e]
                if errors:
                    raise RuntimeError("; ".join(errors))

            # Cache light ID -> name mapping
            self._lights = {
                lid: info.get("name", f"Light {lid}")
                for lid, info in lights_data.items()
            }

            # Snapshot initial states for change detection
            self._light_states = {
                lid: info.get("state", {})
                for lid, info in lights_data.items()
            }

            # Start optional polling task
            poll_interval = int(self.config.get("poll_interval_seconds", "60"))
            if poll_interval > 0:
                self._poll_task = asyncio.create_task(self._poll_state(poll_interval))

        except Exception:
            if self._http_client is not None:
                await self._http_client.aclose()
                self._http_client = None
            raise

    async def disconnect(self) -> None:
        """Cancel the poll task and close the HTTP client."""
        try:
            if self._poll_task is not None and not self._poll_task.done():
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass
                self._poll_task = None

            if self._http_client is not None:
                await self._http_client.aclose()
                self._http_client = None
        except Exception as exc:
            logger.warning("Error during HueConnector disconnect: %s", exc)
        finally:
            self._status = ConnectorStatus.DISCONNECTED
            self._status_message = None
            logger.info("HueConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _poll_state(self, interval: int) -> None:
        """
        Periodically poll the bridge for light-state changes.

        When a light changes state (on/off, brightness, etc.), an
        IncomingMessage describing the change is emitted.
        """
        consecutive_failures = 0
        max_consecutive_failures = 5
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    response = await self._api_get("/lights")
                    if response.status_code != 200:
                        logger.warning(
                            "HueConnector %s poll got status %s",
                            self.connector_id, response.status_code,
                        )
                        consecutive_failures += 1
                    else:
                        lights_data = response.json()
                        if isinstance(lights_data, list):
                            # Error response from bridge
                            consecutive_failures += 1
                        else:
                            consecutive_failures = 0

                            # Update light name cache
                            self._lights = {
                                lid: info.get("name", f"Light {lid}")
                                for lid, info in lights_data.items()
                            }

                            # Detect state changes
                            changes: List[str] = []
                            for lid, info in lights_data.items():
                                current_state = info.get("state", {})
                                previous_state = self._light_states.get(lid, {})
                                name = info.get("name", f"Light {lid}")

                                diffs = self._describe_state_diff(name, previous_state, current_state)
                                if diffs:
                                    changes.extend(diffs)
                                    await self.emit_trigger("light_state_changed", {
                                        "light_id": lid,
                                        "light_name": name,
                                        "on": current_state.get("on", False),
                                        "brightness": current_state.get("bri", 0),
                                        "hue": current_state.get("hue", 0),
                                        "saturation": current_state.get("sat", 0),
                                    })

                            # Store new states
                            self._light_states = {
                                lid: info.get("state", {})
                                for lid, info in lights_data.items()
                            }

                            if changes:
                                text = "Light state changes detected:\n" + "\n".join(changes)
                                msg = IncomingMessage(
                                    connector_id=self.connector_id,
                                    platform_chat_id="hue-bridge",
                                    platform_user_id="hue-bridge",
                                    platform_user_name="Hue Bridge",
                                    text=text,
                                    metadata={"event": "state_change"},
                                )
                                await self._on_incoming_message(msg)

                except Exception as exc:
                    consecutive_failures += 1
                    logger.warning(
                        "HueConnector %s poll error (%d/%d): %s", self.connector_id, consecutive_failures, max_consecutive_failures, exc,
                    )

                if consecutive_failures >= max_consecutive_failures:
                    self._status = ConnectorStatus.ERROR
                    self._status_message = f"Repeated poll failures ({consecutive_failures})"
                    logger.error("HueConnector %s entering ERROR state after %d consecutive failures", self.connector_id, consecutive_failures)
                    try:
                        from app.db import database
                        from app.db.async_database import run_sync
                        await run_sync(database.update_connector_status, self.connector_id, "error", error=self._status_message)
                    except Exception:
                        logger.debug("Failed to update DB status", exc_info=True)

        except asyncio.CancelledError:
            logger.info("HueConnector %s poll task cancelled", self.connector_id)

    @staticmethod
    def _describe_state_diff(
        name: str,
        old: Dict[str, Any],
        new: Dict[str, Any],
    ) -> List[str]:
        """Return human-readable descriptions of state differences."""
        diffs: List[str] = []

        # on/off
        if old.get("on") != new.get("on"):
            state_str = "on" if new.get("on") else "off"
            diffs.append(f"  - {name} turned {state_str}")

        # brightness
        old_bri = old.get("bri")
        new_bri = new.get("bri")
        if old_bri is not None and new_bri is not None and old_bri != new_bri:
            pct = round(new_bri * 100 / 254)
            diffs.append(f"  - {name} brightness changed to {pct}%")

        # color temperature
        old_ct = old.get("ct")
        new_ct = new.get("ct")
        if old_ct is not None and new_ct is not None and old_ct != new_ct:
            diffs.append(f"  - {name} color temperature changed to {new_ct} mirek")

        # reachable
        if old.get("reachable") != new.get("reachable"):
            reach_str = "reachable" if new.get("reachable") else "unreachable"
            diffs.append(f"  - {name} became {reach_str}")

        return diffs

    # ------------------------------------------------------------------
    # Outgoing — command parsing
    # ------------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> bool:
        """
        Parse the outgoing message text as a Hue command and execute it.

        Supported commands:
            list lights / lights
            list rooms / rooms
            list scenes / scenes
            turn on {light}
            turn off {light}
            set {light} brightness {0-100}
            set {light} to {0-100}%
            set {light} color temp {warm|cool|neutral}
            activate scene {name}
            all on
            all off

        Returns True if the command was recognised and executed, False
        otherwise.
        """
        if self._http_client is None:
            logger.error("Cannot send message: HTTP client is not initialised")
            return False

        text = message.text.strip()
        text_lower = text.lower()

        try:
            # --- list lights ---
            if text_lower in ("list lights", "lights"):
                return await self._cmd_list_lights(message)

            # --- list rooms ---
            if text_lower in ("list rooms", "rooms"):
                return await self._cmd_list_rooms(message)

            # --- list scenes ---
            if text_lower in ("list scenes", "scenes"):
                return await self._cmd_list_scenes(message)

            # --- all on / all off ---
            if text_lower == "all on":
                return await self._cmd_group_action({"on": True}, "All lights turned on")

            if text_lower == "all off":
                return await self._cmd_group_action({"on": False}, "All lights turned off")

            # --- turn on / turn off ---
            match = re.match(r"turn\s+(on|off)\s+(.+)", text, re.IGNORECASE)
            if match:
                action = match.group(1).lower()
                light_query = match.group(2).strip()
                return await self._cmd_turn(light_query, on=(action == "on"))

            # --- set brightness: "set {name} brightness {0-100}" ---
            match = re.match(
                r"set\s+(.+?)\s+brightness\s+(\d{1,3})",
                text,
                re.IGNORECASE,
            )
            if match:
                light_query = match.group(1).strip()
                pct = int(match.group(2))
                return await self._cmd_set_brightness(light_query, pct)

            # --- set brightness: "set {name} to {0-100}%" ---
            match = re.match(
                r"set\s+(.+?)\s+to\s+(\d{1,3})%",
                text,
                re.IGNORECASE,
            )
            if match:
                light_query = match.group(1).strip()
                pct = int(match.group(2))
                return await self._cmd_set_brightness(light_query, pct)

            # --- set color temp ---
            match = re.match(
                r"set\s+(.+?)\s+color\s+temp\s+(warm|cool|neutral)",
                text,
                re.IGNORECASE,
            )
            if match:
                light_query = match.group(1).strip()
                preset = match.group(2).lower()
                return await self._cmd_set_color_temp(light_query, preset)

            # --- activate scene ---
            match = re.match(r"activate\s+scene\s+(.+)", text, re.IGNORECASE)
            if match:
                scene_query = match.group(1).strip()
                return await self._cmd_activate_scene(scene_query)

            # Unrecognised command
            return False

        except Exception as exc:
            logger.exception(
                "HueConnector %s error executing command '%s': %s",
                self.connector_id, text[:100], exc,
            )
            return False

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------

    async def _cmd_list_lights(self, message: OutgoingMessage) -> bool:
        """List all lights with their current state."""
        response = await self._api_get("/lights")
        if response.status_code != 200:
            return False

        lights_data = response.json()
        if isinstance(lights_data, list):
            return False

        lines = ["Lights:"]
        for lid, info in lights_data.items():
            name = info.get("name", f"Light {lid}")
            state = info.get("state", {})
            on_off = "on" if state.get("on") else "off"
            bri = state.get("bri")
            bri_str = f", {round(bri * 100 / 254)}%" if bri is not None else ""
            lines.append(f"  [{lid}] {name} — {on_off}{bri_str}")

        reply_text = "\n".join(lines)
        await self._emit_reply(reply_text)
        return True

    async def _cmd_list_rooms(self, message: OutgoingMessage) -> bool:
        """List all groups/rooms."""
        response = await self._api_get("/groups")
        if response.status_code != 200:
            return False

        groups_data = response.json()
        if isinstance(groups_data, list):
            return False

        lines = ["Rooms / Groups:"]
        for gid, info in groups_data.items():
            name = info.get("name", f"Group {gid}")
            group_type = info.get("type", "")
            light_count = len(info.get("lights", []))
            all_on = info.get("state", {}).get("all_on", False)
            any_on = info.get("state", {}).get("any_on", False)

            if all_on:
                state_str = "all on"
            elif any_on:
                state_str = "some on"
            else:
                state_str = "all off"

            lines.append(
                f"  [{gid}] {name} ({group_type}, {light_count} lights) — {state_str}"
            )

        reply_text = "\n".join(lines)
        await self._emit_reply(reply_text)
        return True

    async def _cmd_list_scenes(self, message: OutgoingMessage) -> bool:
        """List all scenes."""
        response = await self._api_get("/scenes")
        if response.status_code != 200:
            return False

        scenes_data = response.json()
        if isinstance(scenes_data, list):
            return False

        lines = ["Scenes:"]
        for sid, info in scenes_data.items():
            name = info.get("name", f"Scene {sid}")
            group = info.get("group", "?")
            lines.append(f"  [{sid}] {name} (group {group})")

        reply_text = "\n".join(lines)
        await self._emit_reply(reply_text)
        return True

    async def _cmd_turn(self, light_query: str, *, on: bool) -> bool:
        """Turn a light on or off."""
        light_id = self._find_light(light_query)
        if light_id is None:
            await self._emit_reply(f"Light not found: {light_query}")
            return True

        response = await self._api_put(f"/lights/{light_id}/state", {"on": on})
        if response.status_code != 200:
            logger.error(
                "HueConnector %s turn %s failed (%s): %s",
                self.connector_id, "on" if on else "off",
                response.status_code, response.text[:200],
            )
            return False

        name = self._lights.get(light_id, light_query)
        state_str = "on" if on else "off"
        await self._emit_reply(f"{name} turned {state_str}")
        return True

    async def _cmd_set_brightness(self, light_query: str, pct: int) -> bool:
        """Set a light's brightness to a percentage (0-100)."""
        light_id = self._find_light(light_query)
        if light_id is None:
            await self._emit_reply(f"Light not found: {light_query}")
            return True

        pct = max(0, min(100, pct))
        bri = round(pct * 254 / 100)

        response = await self._api_put(f"/lights/{light_id}/state", {"bri": bri})
        if response.status_code != 200:
            logger.error(
                "HueConnector %s set brightness failed (%s): %s",
                self.connector_id, response.status_code, response.text[:200],
            )
            return False

        name = self._lights.get(light_id, light_query)
        await self._emit_reply(f"{name} brightness set to {pct}%")
        return True

    async def _cmd_set_color_temp(self, light_query: str, preset: str) -> bool:
        """Set a light's color temperature to a named preset."""
        light_id = self._find_light(light_query)
        if light_id is None:
            await self._emit_reply(f"Light not found: {light_query}")
            return True

        ct_value = COLOR_TEMP_PRESETS.get(preset)
        if ct_value is None:
            await self._emit_reply(
                f"Unknown color temperature preset: {preset}. "
                f"Use warm, neutral, or cool."
            )
            return True

        response = await self._api_put(f"/lights/{light_id}/state", {"ct": ct_value})
        if response.status_code != 200:
            logger.error(
                "HueConnector %s set color temp failed (%s): %s",
                self.connector_id, response.status_code, response.text[:200],
            )
            return False

        name = self._lights.get(light_id, light_query)
        await self._emit_reply(f"{name} color temperature set to {preset} ({ct_value} mirek)")
        return True

    async def _cmd_activate_scene(self, scene_query: str) -> bool:
        """Activate a scene by name (case-insensitive partial match)."""
        response = await self._api_get("/scenes")
        if response.status_code != 200:
            return False

        scenes_data = response.json()
        if isinstance(scenes_data, list):
            return False

        # Find scene by name
        query_lower = scene_query.lower()
        scene_id: Optional[str] = None
        scene_name: Optional[str] = None

        for sid, info in scenes_data.items():
            name = info.get("name", "")
            if query_lower in name.lower():
                scene_id = sid
                scene_name = name
                break

        if scene_id is None:
            await self._emit_reply(f"Scene not found: {scene_query}")
            return True

        response = await self._api_put("/groups/0/action", {"scene": scene_id})
        if response.status_code != 200:
            logger.error(
                "HueConnector %s activate scene failed (%s): %s",
                self.connector_id, response.status_code, response.text[:200],
            )
            return False

        await self._emit_reply(f"Scene '{scene_name}' activated")
        return True

    async def _cmd_group_action(
        self, action: Dict[str, Any], description: str
    ) -> bool:
        """Apply an action to group 0 (all lights)."""
        response = await self._api_put("/groups/0/action", action)
        if response.status_code != 200:
            logger.error(
                "HueConnector %s group action failed (%s): %s",
                self.connector_id, response.status_code, response.text[:200],
            )
            return False

        await self._emit_reply(description)
        return True

    # ------------------------------------------------------------------
    # Reply helper
    # ------------------------------------------------------------------

    async def _emit_reply(self, text: str) -> None:
        """Send a synthetic IncomingMessage back with the command result."""
        for chunk in chunk_text(text, self.MAX_MESSAGE_LENGTH):
            msg = IncomingMessage(
                connector_id=self.connector_id,
                platform_chat_id="hue-bridge",
                platform_user_id="hue-bridge",
                platform_user_name="Hue Bridge",
                text=chunk,
                metadata={"synthetic": True},
            )
            await self._on_incoming_message(msg)

    # ------------------------------------------------------------------
    # Permission prompt (not supported)
    # ------------------------------------------------------------------

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Not applicable for Hue connectors."""
        return False

    # ------------------------------------------------------------------
    # Notification
    # ------------------------------------------------------------------

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a notification as a synthetic IncomingMessage."""
        indicators = {
            "session.started": ">>",
            "session.complete": "[OK]",
            "session.error": "[ERR]",
        }
        icon = indicators.get(event_type, "[i]")

        if event_type == "session.complete":
            duration = data.get("duration_seconds", 0)
            cost = data.get("total_cost", 0)
            text = (
                f"{icon} Session Complete\n\n"
                f"Duration: {duration:.1f}s\n"
                f"Cost: ${cost:.4f}"
            )
        elif event_type == "session.error":
            error_msg = data.get("error_message", "Unknown error")
            text = f"{icon} Session Error\n\n{error_msg}"
        else:
            text = f"{icon} {event_type}\n\n{data}"

        await self._emit_reply(text)
        return True

    # ------------------------------------------------------------------
    # Actions & Tools
    # ------------------------------------------------------------------

    async def execute_action(
        self,
        action_id: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a catalog action (see hue_actions.py)."""
        return await hue_actions.execute(self, action_id, params, context)

    def get_context_instructions(self, platform_chat_id: str) -> Optional[str]:
        return (
            "PHILIPS HUE TOOLS: Call hue_actions to discover what you can do "
            "(individual light control, room/group control, scenes, color "
            "temperature, effects, and status queries), each with its input "
            "schema, then call hue_invoke with {action, params} to act. Use "
            "list_lights, list_groups, and list_scenes to discover valid IDs "
            "before acting on them."
        )

    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a Hue tool call by routing to the appropriate action."""
        if tool_id != "hue_control":
            return {"success": False, "error": f"Unknown tool: {tool_id}"}

        command = params.get("command")
        if not command:
            return {"success": False, "error": "command is required"}

        # Route the tool command to the corresponding action
        action_params = {k: v for k, v in params.items() if k != "command"}
        return await self.execute_action(command, action_params)

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    async def test_connection(self) -> Dict[str, Any]:
        """Test the bridge connection by fetching /config."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                url = f"http://{self.config['bridge_ip']}/api/{self.config['api_key']}/config"
                response = await client.get(url)

            if response.status_code == 200:
                data = response.json()
                bridge_name = data.get("name", "Unknown")
                api_version = data.get("apiversion", "Unknown")
                return {
                    "success": True,
                    "message": (
                        f"Connected to Hue Bridge '{bridge_name}' "
                        f"(API version {api_version})"
                    ),
                }

            return {
                "success": False,
                "message": f"Bridge returned {response.status_code}",
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}
