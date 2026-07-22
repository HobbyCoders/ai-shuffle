"""DiscordConnector — Discord bot integration via discord.py."""

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

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
    TriggerDef,
    chunk_text,
)
from app.core.connectors import discord_actions

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
        typing_indicator=True, reactions=True, threads=True, buttons=True,
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

    # Full declarative action catalog (see discord_actions.py). Exposed to the
    # AI through the generic discord_actions/discord_invoke gateway tool pair
    # instead of one MCP tool per action.
    ACTIONS = discord_actions.DISCORD_ACTIONS

    # No per-tool MCP registration: the action gateway replaces the legacy
    # globally-exposed `discord_send` tool (which leaked to unlinked sessions).
    TOOLS: List[Any] = []

    SUPPORTS_ACTION_GATEWAY = True

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
        self._pending_action_approvals: Dict[str, asyncio.Future] = {}  # request_id -> Future[bool]
        # Emit the "typing indicator failed" warning at most once per connector
        # so a class of silent breakage (e.g. a removed discord.py API) surfaces
        # loudly the first time, without spamming the log on every 4s keep-alive.
        self._typing_warned: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    _SDK_AVAILABLE = DISCORD_AVAILABLE
    _SDK_NAME = "discord.py"

    async def connect(self) -> bool:
        """Start the Discord client and connect to the gateway.

        Overrides the base template because Discord sets CONNECTED
        asynchronously in the ``on_ready`` callback, not at the end of
        this method.
        """
        if self._SDK_AVAILABLE is False:
            sdk = self._SDK_NAME or "required SDK"
            self._status = ConnectorStatus.ERROR
            self._status_message = f"{sdk} is not installed"
            logger.error("Cannot start DiscordConnector: %s is not installed", sdk)
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

            # Destructive-action approval buttons (action gateway)
            if custom_id.startswith("actreq:"):
                parts = custom_id.split(":")
                if len(parts) == 3:
                    _, request_id, decision = parts
                    fut = self._pending_action_approvals.get(request_id)
                    if fut is not None and not fut.done():
                        fut.set_result(decision == "approve")
                    label = "Approved" if decision == "approve" else "Denied"
                    await interaction.response.send_message(
                        f"Action {label}", ephemeral=True,
                    )
                return

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
                    # discord.py 2.x removed Messageable.trigger_typing(). The
                    # replacement, channel.typing(), returns a Typing context
                    # manager that is ALSO awaitable — awaiting it fires a single
                    # one-shot typing indicator (the same semantics as the old
                    # trigger_typing()). Verified against discord.py 2.7.1:
                    # discord.context_managers.Typing defines __await__.
                    await channel.typing()
            except Exception:
                # Surface this once per connector at WARNING so a future silent
                # breakage of this API is visible; keep subsequent ticks quiet.
                if not self._typing_warned:
                    self._typing_warned = True
                    logger.warning(
                        "Discord typing indicator failed for connector %s "
                        "(suppressing further occurrences)",
                        self.connector_id,
                        exc_info=True,
                    )
                else:
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

    async def execute_action(
        self,
        action_id: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a catalog action (see discord_actions.py).

        ``context`` carries session scoping (bound channel, allow-list) when
        invoked through the action gateway; automations and API callers use
        the two-argument form and pass explicit targets in ``params``.
        """
        return await discord_actions.execute(self, action_id, params, context)

    async def execute_tool(self, tool_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Legacy tool entry point — kept for back-compat with old callers."""
        if tool_id == "discord_send":
            return await self.execute_action("send_message", params)
        return {"success": False, "error": f"Unknown tool: {tool_id}"}

    # ------------------------------------------------------------------
    # Action gateway hooks
    # ------------------------------------------------------------------

    async def get_action_availability(
        self, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Tuple[bool, Optional[str]]]:
        """Category gating (base) + the bot's live guild permissions."""
        out = await super().get_action_availability(context)

        guild = None
        bound = str((context or {}).get("bound_chat_id") or "")
        if self._client is not None and bound:
            try:
                channel = self._client.get_channel(int(bound))
                guild = getattr(channel, "guild", None)
            except (TypeError, ValueError):
                guild = None

        perms = discord_actions.bot_guild_permissions(self, guild) if guild is not None else None
        if perms is None:
            return out

        for action in self.ACTIONS:
            available, _reason = out.get(action.id, (True, None))
            if (
                available
                and action.required_permission
                and not getattr(perms, action.required_permission, False)
            ):
                out[action.id] = (
                    False,
                    f"Bot lacks the '{action.required_permission}' permission in this server.",
                )
        return out

    def get_context_instructions(self, platform_chat_id: str) -> Optional[str]:
        return (
            "DISCORD TOOLS: You are a full Discord participant. Call discord_actions "
            "to discover everything you can do here (messages, embeds, polls, files, "
            "reactions, pins, threads, channels, roles, members, moderation, webhooks, "
            "invites, scheduled events, server info) along with each action's input "
            "schema, then call discord_invoke with {action, params} to act. Prefer "
            "native features over plain text — a real poll instead of text-listed "
            "options, an embed for structured answers, a thread for long tangents. "
            "Message actions work on this channel and the connector's allowed "
            "channels only. Destructive actions (deletes, bans, purges) show the "
            "user an in-channel Approve/Deny prompt before running."
        )

    async def request_action_approval(
        self,
        platform_chat_id: str,
        action: ActionDef,
        params: Dict[str, Any],
        timeout: float = 120.0,
    ) -> Tuple[bool, str]:
        """Post an Approve/Deny embed for a destructive action and await the click."""
        if self._client is None:
            return False, "Discord client is not running."
        try:
            channel = self._client.get_channel(int(platform_chat_id))
        except (TypeError, ValueError):
            return False, f"Invalid approval channel id: {platform_chat_id!r}"
        if channel is None:
            return False, f"Approval channel {platform_chat_id} not found."

        request_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_action_approvals[request_id] = fut

        try:
            params_preview = json.dumps(params, default=str)[:900]
            embed = discord.Embed(
                title="Destructive Action Approval",
                description=(
                    f"The AI wants to run **{action.name}** (`{action.id}`). "
                    "Approve to proceed."
                ),
                color=discord.Color.red(),
            )
            embed.add_field(name="Parameters", value=f"```json\n{params_preview}\n```", inline=False)

            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(
                label="Approve", style=discord.ButtonStyle.success,
                custom_id=f"actreq:{request_id}:approve",
            ))
            view.add_item(discord.ui.Button(
                label="Deny", style=discord.ButtonStyle.danger,
                custom_id=f"actreq:{request_id}:deny",
            ))
            await channel.send(embed=embed, view=view)

            try:
                approved = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                return False, f"No approval response within {timeout:.0f}s — action cancelled."
            return (True, "Approved by user.") if approved else (False, "Denied by user.")
        except Exception as exc:
            logger.exception("Failed to run action approval flow")
            return False, f"Approval flow failed: {exc}"
        finally:
            self._pending_action_approvals.pop(request_id, None)

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


# ----------------------------------------------------------------------------
# Guided-setup discovery (module-level — no connector instance required)
# ----------------------------------------------------------------------------

async def discover_resources(bot_token: str) -> Dict[str, Any]:
    """Log into Discord (HTTP-only, no gateway) and enumerate the bot's
    identity plus the guilds and text channels it can see.

    This powers the guided connector-setup flow: instead of hand-typing guild
    and channel IDs, the client presents pickers built from this result.

    Args:
        bot_token: A Discord bot token.

    Returns:
        {
          "bot": {"id": str, "name": str},
          "application_id": str,
          "guilds": [
            {
              "id": str,
              "name": str,
              "channels": [{"id": str, "name": str}],
              "error": str | None,
            },
            ...
          ],
        }

        All IDs are strings because Discord snowflakes overflow JS numbers.
        A guild whose channels could not be fetched (missing access) is still
        returned with ``channels: []`` and a short ``error`` message.

    Raises:
        RuntimeError: discord.py is not installed.
        ValueError: the bot token is invalid.
        TimeoutError: discovery exceeded the timeout budget.
    """
    if not DISCORD_AVAILABLE:
        raise RuntimeError("discord.py not installed")

    async def _run() -> Dict[str, Any]:
        # HTTP-only client — no gateway connection.
        client = discord.Client(intents=discord.Intents.none())
        try:
            try:
                await client.login(bot_token)
            except discord.LoginFailure:
                raise ValueError("Invalid bot token")

            app_info = await client.application_info()

            guilds_out = []
            async for g in client.fetch_guilds(limit=200):
                entry: Dict[str, Any] = {
                    "id": str(g.id),
                    "name": g.name,
                    "channels": [],
                    "error": None,
                }
                try:
                    full = await client.fetch_guild(g.id)
                    channels = await full.fetch_channels()
                    entry["channels"] = [
                        {"id": str(c.id), "name": c.name}
                        for c in channels
                        if isinstance(c, discord.TextChannel)
                    ]
                except (discord.Forbidden, discord.HTTPException) as exc:
                    entry["error"] = str(exc) or exc.__class__.__name__
                guilds_out.append(entry)

            return {
                "bot": {
                    "id": str(client.user.id),
                    "name": client.user.name,
                },
                "application_id": str(app_info.id),
                "guilds": guilds_out,
            }
        finally:
            await client.close()

    try:
        return await asyncio.wait_for(_run(), timeout=20)
    except asyncio.TimeoutError:
        raise TimeoutError("Discord discovery timed out")
