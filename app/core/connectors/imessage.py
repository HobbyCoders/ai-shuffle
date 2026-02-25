"""iMessageConnector — iMessage integration via imsg CLI (macOS only)."""

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from app.core.connectors.base import (
    ActionDef,
    BaseConnector,
    ConnectorCapabilities,
    ConnectorStatus,
    IncomingMessage,
    OutgoingMessage,
    PermissionPrompt,
    IncomingMessageCallback,
    PermissionResponseCallback,
    ToolDef,
    TriggerDef,
)

logger = logging.getLogger(__name__)

# Default timeout for RPC operations
DEFAULT_RPC_TIMEOUT = 10.0
# Echo detection window (seconds)
ECHO_TTL = 5.0


class IMessageRpcClient:
    """
    JSON-RPC 2.0 client that communicates with the ``imsg`` CLI tool
    via stdin/stdout of a subprocess.
    """

    def __init__(self, cli_path: str = "imsg"):
        self._cli_path = cli_path
        self._process: Optional[asyncio.subprocess.Process] = None
        self._request_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._notification_callback: Optional[Any] = None

    async def start(self) -> None:
        """Spawn the imsg rpc subprocess."""
        self._process = await asyncio.create_subprocess_exec(
            self._cli_path, "rpc",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())

    async def stop(self) -> None:
        """Stop the subprocess."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()

    def set_notification_callback(self, callback: Any) -> None:
        """Set callback for incoming RPC notifications."""
        self._notification_callback = callback

    async def request(self, method: str, params: Optional[Dict] = None) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("RPC process not started")

        self._request_id += 1
        req_id = self._request_id

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        data = json.dumps(payload) + "\n"
        self._process.stdin.write(data.encode())
        await self._process.stdin.drain()

        try:
            result = await asyncio.wait_for(future, timeout=DEFAULT_RPC_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"RPC request '{method}' timed out")

        return result

    async def _read_stdout(self) -> None:
        """Read JSON-RPC responses and notifications from stdout."""
        try:
            while self._process and self._process.stdout:
                line = await self._process.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode().strip())
                except json.JSONDecodeError:
                    logger.debug("Invalid JSON from imsg RPC: %s", line.decode(errors="replace")[:100])
                    continue

                req_id = msg.get("id")
                if req_id is not None and req_id in self._pending:
                    # Response to a request
                    future = self._pending.pop(req_id)
                    if "error" in msg:
                        err = msg["error"]
                        future.set_exception(
                            RuntimeError(
                                f"RPC error {err.get('code', '?')}: {err.get('message', '')}"
                            )
                        )
                    else:
                        future.set_result(msg.get("result"))
                elif "method" in msg:
                    # Notification (incoming message)
                    if self._notification_callback:
                        asyncio.create_task(
                            self._notification_callback(msg.get("params", {}))
                        )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("iMessage RPC reader error: %s", exc)


class IMessageConnector(BaseConnector):
    """
    Connector for iMessage using the ``imsg`` CLI tool (macOS only).

    The imsg CLI provides a JSON-RPC interface over stdin/stdout
    for sending messages and a watch subscription for incoming messages.

    Config keys:
        cli_path (str, optional): Path to the imsg binary. Defaults to "imsg".
        service (str, optional): Default service — "imessage", "sms", or "auto".
            Defaults to "auto".
        allowed_handles (list[str], optional): If set, only messages from these
            phone numbers or email addresses will be processed.
    """

    CAPABILITIES = ConnectorCapabilities()

    TRIGGERS = [
        TriggerDef(
            id="message_received",
            name="Message Received",
            description="Fires when a new message is received",
            payload_schema={
                "type": "object",
                "properties": {
                    "platform_chat_id": {"type": "string"},
                    "platform_user_id": {"type": "string"},
                    "platform_user_name": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        ),
    ]

    ACTIONS = [
        ActionDef(
            id="send_message",
            name="Send Message",
            description="Send a message to a chat",
            input_schema={
                "type": "object",
                "properties": {
                    "platform_chat_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["platform_chat_id", "text"],
            },
        ),
    ]

    TOOLS = [
        ToolDef(
            id="imessage_send",
            name="Send a message via iMessage",
            description="Send a message via iMessage",
            input_schema={
                "type": "object",
                "properties": {
                    "platform_chat_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["platform_chat_id", "text"],
            },
        ),
    ]

    def __init__(
        self,
        connector_id: str,
        connector_type: str,
        config: Dict[str, Any],
        on_incoming_message: IncomingMessageCallback,
        on_permission_response: PermissionResponseCallback,
        **kwargs,
    ):
        super().__init__(
            connector_id, connector_type, config,
            on_incoming_message, on_permission_response,
            **kwargs,
        )
        self._rpc: Optional[IMessageRpcClient] = None
        self._permission_sessions: Dict[str, str] = {}  # request_id -> session_id
        self._recent_sent: Dict[str, float] = {}  # chat_id -> timestamp (echo detection)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start the imsg RPC subprocess and subscribe to incoming messages."""
        try:
            self._status = ConnectorStatus.CONNECTING
            cli_path = self.config.get("cli_path", "imsg")

            self._rpc = IMessageRpcClient(cli_path=cli_path)
            self._rpc.set_notification_callback(self._on_rpc_notification)
            await self._rpc.start()

            # Verify RPC is working
            try:
                await self._rpc.request("chats.list", {"limit": 1})
            except Exception as exc:
                self._status = ConnectorStatus.ERROR
                self._status_message = f"RPC probe failed: {exc}"
                await self._rpc.stop()
                return False

            # Subscribe to incoming messages
            await self._rpc.request("watch.subscribe", {"attachments": False})

            self._status = ConnectorStatus.CONNECTED
            self._status_message = None
            logger.info("IMessageConnector %s connected", self.connector_id)
            return True

        except FileNotFoundError:
            self._status = ConnectorStatus.ERROR
            self._status_message = "imsg CLI not found (macOS only)"
            logger.error("Cannot start IMessageConnector: imsg CLI not found")
            return False

        except Exception as exc:
            self._status = ConnectorStatus.ERROR
            self._status_message = str(exc)
            logger.exception("IMessageConnector %s failed to connect", self.connector_id)
            return False

    async def disconnect(self) -> None:
        """Stop the RPC subprocess."""
        try:
            if self._rpc:
                try:
                    await self._rpc.request("watch.unsubscribe", {"subscription": 1})
                except Exception:
                    pass
                await self._rpc.stop()
        except Exception as exc:
            logger.warning("Error during IMessageConnector disconnect: %s", exc)
        finally:
            self._status = ConnectorStatus.DISCONNECTED
            self._status_message = None
            logger.info("IMessageConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Incoming handler
    # ------------------------------------------------------------------

    async def _on_rpc_notification(self, params: Dict[str, Any]) -> None:
        """Handle an incoming RPC notification (watch event)."""
        try:
            # Skip messages from self
            if params.get("is_from_me"):
                return

            sender = params.get("sender", "")
            chat_id = params.get("chat_identifier") or params.get("chat_id") or sender
            text = params.get("text", "") or ""

            # Echo detection: skip if we recently sent to this chat
            import time
            now = time.time()
            last_sent = self._recent_sent.get(str(chat_id), 0)
            if now - last_sent < ECHO_TTL and not text:
                return

            # Filter by allowed handles
            allowed = self.config.get("allowed_handles")
            if allowed and sender not in [str(h) for h in allowed]:
                logger.debug(
                    "Ignoring iMessage from non-allowed handle %s", sender
                )
                return

            # Check for permission responses
            if text and self._check_permission_response(text):
                return

            msg = IncomingMessage(
                connector_id=self.connector_id,
                platform_chat_id=str(chat_id),
                platform_user_id=sender,
                platform_user_name=params.get("sender_name", sender),
                text=text,
                platform_message_id=params.get("guid") or params.get("id"),
                metadata={
                    "service": params.get("service"),
                    "is_group": params.get("is_group", False),
                    "chat_name": params.get("chat_name"),
                },
            )
            await self._on_incoming_message(msg)

        except Exception as exc:
            logger.exception("Error handling iMessage notification: %s", exc)

    def _check_permission_response(self, text: str) -> bool:
        """Check if text is a simple permission response."""
        text_lower = text.strip().lower()
        for request_id, session_id in list(self._permission_sessions.items()):
            if text_lower in ("approve", "yes", "allow", "y"):
                asyncio.create_task(
                    self._on_permission_response(request_id, session_id, True)
                )
                del self._permission_sessions[request_id]
                return True
            elif text_lower in ("deny", "no", "reject", "n"):
                asyncio.create_task(
                    self._on_permission_response(request_id, session_id, False)
                )
                del self._permission_sessions[request_id]
                return True
        return False

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a text message via the imsg RPC."""
        if not self._rpc:
            logger.error("Cannot send message: RPC client is not initialised")
            return False

        try:
            service = self.config.get("service", "auto")
            params: Dict[str, Any] = {
                "text": message.text,
                "to": message.platform_chat_id,
                "service": service,
            }

            await self._rpc.request("send", params)

            # Track for echo detection
            import time
            self._recent_sent[message.platform_chat_id] = time.time()

            return True

        except Exception as exc:
            logger.exception(
                "Failed to send iMessage to %s: %s",
                message.platform_chat_id, exc,
            )
            return False

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Send a text-based permission prompt (iMessage has no interactive buttons)."""
        if not self._rpc:
            logger.error("Cannot send permission prompt: RPC client is not initialised")
            return False

        self._permission_sessions[prompt.request_id] = prompt.session_id

        text = (
            f"Permission Request\n\n"
            f"Tool: {prompt.tool_name}\n"
            f"{prompt.description}\n\n"
            f"Reply 'approve' or 'deny'"
        )

        try:
            service = self.config.get("service", "auto")
            await self._rpc.request("send", {
                "text": text,
                "to": platform_chat_id,
                "service": service,
            })
            return True

        except Exception as exc:
            logger.exception(
                "Failed to send iMessage permission prompt to %s: %s",
                platform_chat_id, exc,
            )
            return False

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a text notification message."""
        if not self._rpc:
            logger.error("Cannot send notification: RPC client is not initialised")
            return False

        indicators = {
            "session.started": "\u25b6\ufe0f",
            "session.complete": "\u2705",
            "session.error": "\u274c",
            "permission.requested": "\ud83d\udd10",
        }
        icon = indicators.get(event_type, "\ud83d\udce3")

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

        try:
            service = self.config.get("service", "auto")
            await self._rpc.request("send", {
                "text": text,
                "to": platform_chat_id,
                "service": service,
            })
            return True

        except Exception as exc:
            logger.exception(
                "Failed to send iMessage notification to %s: %s",
                platform_chat_id, exc,
            )
            return False

    # ------------------------------------------------------------------
    # Actions & Tools
    # ------------------------------------------------------------------

    async def execute_action(self, action_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a named action."""
        if action_id == "send_message":
            message = OutgoingMessage(
                platform_chat_id=params["platform_chat_id"],
                text=params["text"],
            )
            success = await self.send_message(message)
            if success:
                return {"success": True, "message": "Message sent"}
            return {"success": False, "error": "Failed to send message"}

        return {"success": False, "error": f"Unknown action: {action_id}"}

    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a named tool call."""
        if tool_id == "imessage_send":
            message = OutgoingMessage(
                platform_chat_id=params["platform_chat_id"],
                text=params["text"],
            )
            success = await self.send_message(message)
            if success:
                return {"success": True, "message": "Message sent"}
            return {"success": False, "error": "Failed to send message"}

        return {"success": False, "error": f"Unknown tool: {tool_id}"}

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    async def test_connection(self) -> Dict[str, Any]:
        """Verify the imsg CLI is available and RPC is functional."""
        cli_path = self.config.get("cli_path", "imsg")

        try:
            rpc = IMessageRpcClient(cli_path=cli_path)
            await rpc.start()
            try:
                result = await rpc.request("chats.list", {"limit": 1})
                return {
                    "success": True,
                    "message": "Connected to imsg RPC successfully",
                }
            finally:
                await rpc.stop()
        except FileNotFoundError:
            return {
                "success": False,
                "message": f"imsg CLI not found at '{cli_path}' (macOS only)",
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}
