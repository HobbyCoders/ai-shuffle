"""DiscordConnector — Discord bot integration via discord.py."""

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
    import discord
    from discord import Intents

    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False


class DiscordConnector(BaseConnector):
    """
    Connector for Discord using the discord.py library.

    MAX_MESSAGE_LENGTH is set to 2000 per Discord's message limit.

    Config keys:
        bot_token (str): Discord bot token.
        guild_ids (list[str], optional): If set, only messages from
            these guild (server) IDs will be processed.
        allowed_channel_ids (list[str], optional): If set, only messages
            from these channel IDs will be processed.
        command_prefix (str, optional): If set, only messages starting
            with this prefix will be treated as commands.
    """

    MAX_MESSAGE_LENGTH = 2000
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
            id="discord_send",
            name="Send a message via Discord",
            description="Send a message via Discord",
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
        self._client: Optional[Any] = None
        self._permission_prompts: Dict[str, Dict[str, str]] = {}  # request_id -> metadata

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start the Discord client and connect to the gateway."""
        if not DISCORD_AVAILABLE:
            self._status = ConnectorStatus.ERROR
            self._status_message = "discord.py is not installed"
            logger.error("Cannot start DiscordConnector: discord.py is not installed")
            return False

        try:
            self._status = ConnectorStatus.CONNECTING
            token = self.config["bot_token"]

            intents = Intents.default()
            intents.message_content = True

            connector = self  # capture for inner class

            class _Client(discord.Client):
                async def on_ready(self_client):
                    connector._status = ConnectorStatus.CONNECTED
                    connector._status_message = None
                    logger.info(
                        "DiscordConnector %s ready as %s",
                        connector.connector_id,
                        self_client.user,
                    )
                    # Persist connected status to DB
                    try:
                        from app.db import database as _db
                        _db.update_connector_status(connector.connector_id, "connected")
                    except Exception:
                        logger.debug("Failed to update DB status after on_ready", exc_info=True)

                async def on_message(self_client, message):
                    logger.debug(
                        "on_message fired: author=%s bot=%s channel=%s guild=%s content=%r",
                        message.author,
                        message.author.bot,
                        message.channel.id,
                        message.guild.id if message.guild else None,
                        (message.content or "")[:50],
                    )
                    await connector._on_message_handler(message)

                async def on_interaction(self_client, interaction):
                    await connector._on_interaction_handler(interaction)

            self._client = _Client(intents=intents)
            self._task = asyncio.create_task(self._run_client(token))
            # Status stays CONNECTING until on_ready fires
            logger.info("DiscordConnector %s connecting", self.connector_id)
            return True

        except Exception as exc:
            self._status = ConnectorStatus.ERROR
            self._status_message = str(exc)
            logger.exception("DiscordConnector %s failed to connect", self.connector_id)
            return False

    async def _run_client(self, token: str) -> None:
        """Background task that runs the discord.py event loop."""
        try:
            await self._client.start(token)
        except asyncio.CancelledError:
            logger.info("DiscordConnector %s client task cancelled", self.connector_id)
        except Exception as exc:
            self._status = ConnectorStatus.ERROR
            self._status_message = str(exc)
            logger.exception("DiscordConnector %s client error", self.connector_id)
            # Persist the error status to the database so the UI reflects it
            try:
                from app.db import database
                database.update_connector_status(
                    self.connector_id, "error",
                    error=str(exc),
                )
            except Exception:
                logger.debug("Failed to update DB status after client error", exc_info=True)

    async def disconnect(self) -> None:
        """Close the Discord client and cancel the background task."""
        try:
            if self._client is not None:
                await self._client.close()
            if self._task is not None and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        except Exception as exc:
            logger.warning("Error during DiscordConnector disconnect: %s", exc)
        finally:
            self._status = ConnectorStatus.DISCONNECTED
            self._status_message = None
            logger.info("DiscordConnector %s disconnected", self.connector_id)

    # ------------------------------------------------------------------
    # Incoming handlers
    # ------------------------------------------------------------------

    async def _on_message_handler(self, message: Any) -> None:
        """Process an incoming Discord message."""
        try:
            # Skip messages from bots (including self)
            if message.author.bot:
                logger.debug("Skipping bot message from %s", message.author)
                return

            guild_id = str(message.guild.id) if message.guild else None
            channel_id = str(message.channel.id)

            logger.info(
                "Processing message from %s in guild=%s channel=%s: %r",
                message.author, guild_id, channel_id, (message.content or "")[:80],
            )

            # Filter by guild IDs
            allowed_guilds = self.config.get("guild_ids")
            if allowed_guilds:
                # Normalize: config may store a bare string instead of a list
                if isinstance(allowed_guilds, str):
                    allowed_guilds = [allowed_guilds]
                if guild_id not in [str(g) for g in allowed_guilds]:
                    logger.info("Filtered out: guild %s not in allowed_guilds %s", guild_id, allowed_guilds)
                    return

            # Filter by channel IDs
            allowed_channels = self.config.get("allowed_channel_ids")
            if allowed_channels:
                # Normalize: config may store a bare string instead of a list
                if isinstance(allowed_channels, str):
                    allowed_channels = [allowed_channels]
                if channel_id not in [str(c) for c in allowed_channels]:
                    logger.info("Filtered out: channel %s not in allowed_channel_ids %s", channel_id, allowed_channels)
                    return

            text = message.content or ""

            # Filter by command prefix
            prefix = self.config.get("command_prefix")
            if prefix and not text.startswith(prefix):
                logger.info("Filtered out: message %r does not start with prefix %r", text[:50], prefix)
                return

            logger.info("Message accepted — forwarding to query engine")
            msg = IncomingMessage(
                connector_id=self.connector_id,
                platform_chat_id=channel_id,
                platform_user_id=str(message.author.id),
                platform_user_name=str(message.author),
                text=text,
                platform_message_id=str(message.id),
                metadata={
                    "guild_id": guild_id,
                    "channel_name": getattr(message.channel, "name", None),
                },
            )
            await self._on_incoming_message(msg)

        except Exception as exc:
            logger.exception("Error handling Discord message: %s", exc)

    async def _on_interaction_handler(self, interaction: Any) -> None:
        """Handle button interactions for permission responses."""
        try:
            if not hasattr(interaction, "data") or interaction.data is None:
                return

            custom_id = interaction.data.get("custom_id", "")
            if not custom_id.startswith("perm:"):
                return

            parts = custom_id.split(":")
            if len(parts) != 3:
                logger.warning("Invalid interaction custom_id format: %s", custom_id)
                return

            _, request_id, decision = parts
            approved = decision == "approve"

            meta = self._permission_prompts.get(request_id, {})
            session_id = meta.get("session_id", "")

            await self._on_permission_response(request_id, session_id, approved)

            status_text = "Approved" if approved else "Denied"
            await interaction.response.send_message(
                f"Permission {status_text}", ephemeral=True,
            )

        except Exception as exc:
            logger.exception("Error handling Discord interaction: %s", exc)

    # ------------------------------------------------------------------
    # Outgoing
    # ------------------------------------------------------------------

    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a text message to a Discord channel."""
        if self._client is None:
            logger.error("Cannot send message: client is not initialised")
            return False

        try:
            channel = self._client.get_channel(int(message.platform_chat_id))
            if channel is None:
                logger.error(
                    "Channel %s not found in cache. "
                    "The bot may lack access to this channel or "
                    "the channel ID is invalid.",
                    message.platform_chat_id,
                )
                return False

            chunks = list(chunk_text(message.text, self.MAX_MESSAGE_LENGTH))
            logger.info(
                "Sending %d chunk(s) to Discord channel %s (%s)",
                len(chunks),
                message.platform_chat_id,
                getattr(channel, "name", "DM"),
            )
            for text in chunks:
                await channel.send(text)
            return True

        except Exception as exc:
            logger.exception(
                "Failed to send Discord message to %s: %s",
                message.platform_chat_id, exc,
            )
            return False

    async def send_typing_indicator(self, platform_chat_id: str) -> None:
        """Trigger the typing indicator in a Discord channel."""
        if self._client and DISCORD_AVAILABLE:
            try:
                channel = self._client.get_channel(int(platform_chat_id))
                if channel:
                    await channel.trigger_typing()
            except Exception:
                logger.debug("Discord typing indicator failed", exc_info=True)

    async def send_ack_reaction(
        self, platform_chat_id: str, platform_message_id: str
    ) -> None:
        """React with eyes emoji to acknowledge receipt."""
        if self._client and DISCORD_AVAILABLE:
            try:
                channel = self._client.get_channel(int(platform_chat_id))
                if channel:
                    msg = await channel.fetch_message(int(platform_message_id))
                    await msg.add_reaction("\U0001F440")
            except Exception:
                logger.debug("Discord ack reaction failed", exc_info=True)

    async def send_permission_prompt(
        self, platform_chat_id: str, prompt: PermissionPrompt
    ) -> bool:
        """Send an embed with Approve / Deny buttons to a Discord channel."""
        if self._client is None:
            logger.error("Cannot send permission prompt: client is not initialised")
            return False

        try:
            channel = self._client.get_channel(int(platform_chat_id))
            if channel is None:
                logger.error("Channel %s not found", platform_chat_id)
                return False

            # Store metadata for the callback handler
            self._permission_prompts[prompt.request_id] = {
                "session_id": prompt.session_id,
            }

            embed = discord.Embed(
                title="Permission Request",
                description=prompt.description,
                color=discord.Color.orange(),
            )
            embed.add_field(name="Tool", value=prompt.tool_name, inline=False)

            view = discord.ui.View(timeout=None)
            approve_button = discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=f"perm:{prompt.request_id}:approve",
            )
            deny_button = discord.ui.Button(
                label="Deny",
                style=discord.ButtonStyle.danger,
                custom_id=f"perm:{prompt.request_id}:deny",
            )
            view.add_item(approve_button)
            view.add_item(deny_button)

            await channel.send(embed=embed, view=view)
            return True

        except Exception as exc:
            logger.exception(
                "Failed to send Discord permission prompt to %s: %s",
                platform_chat_id, exc,
            )
            return False

    async def send_notification(
        self, platform_chat_id: str, event_type: str, data: Dict[str, Any]
    ) -> bool:
        """Send a rich embed notification to a Discord channel."""
        if self._client is None:
            logger.error("Cannot send notification: client is not initialised")
            return False

        try:
            channel = self._client.get_channel(int(platform_chat_id))
            if channel is None:
                logger.error("Channel %s not found", platform_chat_id)
                return False

            color_map = {
                "session.started": discord.Color.blue(),
                "session.complete": discord.Color.green(),
                "session.error": discord.Color.red(),
                "permission.requested": discord.Color.orange(),
            }
            color = color_map.get(event_type, discord.Color.greyple())

            embed = discord.Embed(
                title=event_type,
                color=color,
            )

            if event_type == "session.complete":
                duration = data.get("duration_seconds", 0)
                cost = data.get("total_cost", 0)
                embed.add_field(name="Duration", value=f"{duration:.1f}s", inline=True)
                embed.add_field(name="Cost", value=f"${cost:.4f}", inline=True)
            elif event_type == "session.error":
                error_msg = data.get("error_message", "Unknown error")
                embed.description = error_msg
            else:
                embed.description = str(data)

            await channel.send(embed=embed)
            return True

        except Exception as exc:
            logger.exception(
                "Failed to send Discord notification to %s: %s",
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
        if tool_id == "discord_send":
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
        """Validate the bot token by attempting to log in."""
        if not DISCORD_AVAILABLE:
            return {"success": False, "message": "discord.py is not installed"}

        try:
            client = discord.Client(intents=Intents.default())
            try:
                await client.login(self.config["bot_token"])
                return {
                    "success": True,
                    "message": f"Authenticated as {client.user}",
                }
            finally:
                await client.close()
        except Exception as exc:
            return {"success": False, "message": str(exc)}
