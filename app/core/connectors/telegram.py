"""TelegramConnector — Telegram bot integration via aiogram."""

import asyncio
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
    chunk_text,
)

logger = logging.getLogger(__name__)

try:
    from aiogram import Bot, Dispatcher, types
    from aiogram.enums import ParseMode
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    AIOGRAM_AVAILABLE = True
except ImportError:
    AIOGRAM_AVAILABLE = False


class TelegramConnector(BaseConnector):
    """
    Connector for Telegram using the aiogram async framework.

    Config keys:
        bot_token (str): Telegram bot API token.
        allowed_chat_ids (list[str], optional): If set, only messages from
            these chat IDs will be processed.
    """

    MAX_MESSAGE_LENGTH = 4096
    CAPABILITIES = ConnectorCapabilities(
        typing_indicator=True, reactions=True, buttons=True,
        rich_text=True, file_upload=True,
    )

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
        TriggerDef(
            id="command_received",
            name="Command Received",
            description="Fires when a bot command is received",
            payload_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "args": {"type": "string"},
                    "platform_chat_id": {"type": "string"},
                    "platform_user_id": {"type": "string"},
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
            id="telegram_send",
            name="Send a message via Telegram",
            description="Send a message via Telegram",
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
        self._bot: Optional[Any] = None
        self._dp: Optional[Any] = None
        self._permission_sessions: Dict[str, str] = {}  # request_id -> session_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    _SDK_AVAILABLE = AIOGRAM_AVAILABLE
    _SDK_NAME = "aiogram"

    async def _do_connect(self) -> None:
        """Start the Telegram bot and begin polling for updates."""
        token = self.config["bot_token"]

        self._bot = Bot(token=token)
        self._dp = Dispatcher()

        # Register handlers
        self._dp.message.register(self._handle_message)
        self._dp.callback_query.register(self._handle_callback_query)

        self._task = asyncio.create_task(self._run_polling())

    async def _run_polling(self) -> None:
        """Background task that runs the aiogram polling loop."""
        try:
            await self._dp.start_polling(self._bot)
        except asyncio.CancelledError:
            logger.info("TelegramConnector %s polling cancelled", self.connector_id)
        except Exception as exc:
            self._status = ConnectorStatus.ERROR
            self._status_message = str(exc)
            logger.exception("TelegramConnector %s polling error", self.connector_id)
            try:
                from app.db import database
                database.update_connector_status(self.connector_id, "error", error=str(exc))
            except Exception:
                logger.debug("Failed to update DB status after polling error", exc_info=True)

    async def disconnect(self) -> None:
        """Stop polling and close the bot session."""
        try:
            if self._dp is not None:
                await self._dp.stop_polling()
            if self._bot is not None:
                await self._bot.session.close()
            if self._task is not None and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        except Exception as exc:
            logger.warning("Error during TelegramConnector disconnect: %s", exc)
        finally:
            self._status = ConnectorStatus.DISCONNECTED
            self._status_message = None
            logger.info("TelegramConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Incoming handlers
    # ------------------------------------------------------------------

    async def _handle_message(self, message: Any) -> None:
        """Process an incoming Telegram message."""
        try:
            chat_id = str(message.chat.id)

            # Filter by allowed chat IDs when configured
            allowed = self.config.get("allowed_chat_ids")
            if allowed and chat_id not in [str(cid) for cid in allowed]:
                logger.debug("Ignoring message from non-allowed chat %s", chat_id)
                return

            user = message.from_user
            msg = IncomingMessage(
                connector_id=self.connector_id,
                platform_chat_id=chat_id,
                platform_user_id=str(user.id) if user else "unknown",
                platform_user_name=user.full_name if user else "Unknown",
                text=message.text or "",
                platform_message_id=str(message.message_id),
                metadata={"chat_type": message.chat.type},
            )
            await self._on_incoming_message(msg)

        except Exception as exc:
            logger.exception("Error handling Telegram message: %s", exc)

    async def _handle_callback_query(self, callback_query: Any) -> None:
        """Handle inline-keyboard button presses (permission responses)."""
        try:
            data = callback_query.data or ""
            if not data.startswith("perm:"):
                return

            parts = data.split(":")
            if len(parts) != 3:
                logger.warning("Invalid callback_data format: %s", data)
                return

            _, request_id, decision = parts
            approved = decision == "approve"

            session_id = self._permission_sessions.get(request_id, "")

            await self._on_permission_response(request_id, session_id, approved)

            status_text = "Approved" if approved else "Denied"
            await callback_query.answer(text=f"Permission {status_text}")

        except Exception as exc:
            logger.exception("Error handling Telegram callback query: %s", exc)

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a text message to a Telegram chat."""
        if self._bot is None:
            logger.error("Cannot send message: bot is not initialised")
            return False

        try:
            parse_mode = None
            if message.parse_mode == "markdown":
                parse_mode = ParseMode.MARKDOWN_V2

            keyboard = None
            if message.buttons:
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=btn["label"],
                                callback_data=btn["callback_data"],
                            )
                        ]
                        for btn in message.buttons
                    ]
                )

            chunks = chunk_text(message.text, self.MAX_MESSAGE_LENGTH)
            for i, text in enumerate(chunks):
                await self._bot.send_message(
                    chat_id=int(message.platform_chat_id),
                    text=text,
                    parse_mode=parse_mode,
                    # Only attach keyboard to the last chunk
                    reply_markup=keyboard if i == len(chunks) - 1 else None,
                )
            return True

        except Exception as exc:
            logger.exception(
                "Failed to send Telegram message to %s: %s",
                message.platform_chat_id, exc,
            )
            return False

    async def send_typing_indicator(self, platform_chat_id: str) -> None:
        """Send a 'typing' chat action."""
        if self._bot and AIOGRAM_AVAILABLE:
            try:
                await self._bot.send_chat_action(
                    chat_id=int(platform_chat_id), action="typing"
                )
            except Exception:
                logger.debug("Telegram typing indicator failed", exc_info=True)

    async def send_ack_reaction(
        self, platform_chat_id: str, platform_message_id: str
    ) -> None:
        """React with an eyes emoji to acknowledge receipt."""
        if self._bot and AIOGRAM_AVAILABLE:
            try:
                await self._bot.set_message_reaction(
                    chat_id=int(platform_chat_id),
                    message_id=int(platform_message_id),
                    reaction=[types.ReactionTypeEmoji(emoji="\U0001F440")],
                )
            except Exception:
                # setMessageReaction requires Bot API 7.2+
                logger.debug("Telegram ack reaction failed (may need Bot API 7.2+)", exc_info=True)

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Send an interactive permission prompt with Approve / Deny buttons."""
        if self._bot is None:
            logger.error("Cannot send permission prompt: bot is not initialised")
            return False

        try:
            # Store session mapping so we can look it up in the callback
            self._permission_sessions[prompt.request_id] = prompt.session_id

            text = (
                f"Permission Request\n\n"
                f"Tool: {prompt.tool_name}\n"
                f"Description: {prompt.description}"
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Approve",
                            callback_data=f"perm:{prompt.request_id}:approve",
                        ),
                        InlineKeyboardButton(
                            text="Deny",
                            callback_data=f"perm:{prompt.request_id}:deny",
                        ),
                    ]
                ]
            )

            await self._bot.send_message(
                chat_id=int(platform_chat_id),
                text=text,
                reply_markup=keyboard,
            )
            return True

        except Exception as exc:
            logger.exception(
                "Failed to send Telegram permission prompt to %s: %s",
                platform_chat_id, exc,
            )
            return False

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a notification message about a system event."""
        if self._bot is None:
            logger.error("Cannot send notification: bot is not initialised")
            return False

        try:
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

            await self._bot.send_message(
                chat_id=int(platform_chat_id),
                text=text,
            )
            return True

        except Exception as exc:
            logger.exception(
                "Failed to send Telegram notification to %s: %s",
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
        if tool_id == "telegram_send":
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
        """Validate the bot token by calling getMe."""
        if not AIOGRAM_AVAILABLE:
            return {"success": False, "message": "aiogram is not installed"}

        try:
            bot = Bot(token=self.config["bot_token"])
            try:
                me = await bot.get_me()
                return {
                    "success": True,
                    "message": f"Authenticated as @{me.username}",
                }
            finally:
                await bot.session.close()
        except Exception as exc:
            return {"success": False, "message": str(exc)}
