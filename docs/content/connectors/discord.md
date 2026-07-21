<!--
title: Discord
description: Connect AI Shuffle to Discord using a bot application from the Discord Developer Portal
order: 2
-->

# Discord Bot Connector

## Overview

The Discord connector integrates your AI Shuffle agent with Discord, one of the most popular communication platforms for communities and teams. Once configured, your agent can respond to messages in Discord channels, participate in threads, react to messages, and share files -- all through a Discord bot account.

Setting up the connector involves creating a Discord Application in the Developer Portal, adding a Bot user to it, and inviting the bot to your server(s). The connector communicates with Discord using the Gateway API (WebSocket) for real-time message reception and the REST API for sending responses. It supports guild-specific and channel-specific filtering so you can control exactly where the bot operates.

## Prerequisites

- A Discord account
- **Manage Server** permission on the Discord server(s) where you want to add the bot (or have a server admin invite it)
- AI Shuffle backend running and accessible
- Access to the [Discord Developer Portal](https://discord.com/developers/applications)

## Setup Guide

### Step 1: Create a Discord Application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** in the top-right corner.
3. Enter a name for your application (e.g., "AI Shuffle Bot") and click **Create**.
4. On the **General Information** page, note the **Application ID** -- you will need it for the invite link.

### Step 2: Create the Bot User

1. In the left sidebar of your application, click **Bot**.
2. Click **Add Bot**, then confirm with **Yes, do it!**.
3. Under the **Token** section, click **Reset Token** to generate a new bot token. Copy this token and store it securely. You will not be able to view it again.
   ```
   Example token format: EXAMPLE-DISCORD-BOT-TOKEN
   ```

### Step 3: Enable Message Content Intent

The bot needs the **Message Content** privileged intent to read message text:

1. On the **Bot** settings page, scroll down to **Privileged Gateway Intents**.
2. Enable **Message Content Intent**.
3. Click **Save Changes**.

Without this intent enabled, the bot will receive message events but the message content will be empty.

### Step 4: Invite the Bot to Your Server

Construct an invite URL using your **Application ID** (from Step 1). The bot needs the following permissions:

- Send Messages
- Send Messages in Threads
- Read Message History
- Add Reactions
- Attach Files
- Use Slash Commands
- Manage Messages (optional, for cleanup)

The permissions integer for these scopes is `277025770560`. Use this invite URL format:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APPLICATION_ID&permissions=277025770560&scope=bot%20applications.commands
```

Replace `YOUR_APPLICATION_ID` with the actual Application ID, then open the URL in your browser. Select the server you want to add the bot to and click **Authorize**.

### Step 5: Get Guild and Channel IDs (Optional)

To restrict the bot to specific servers or channels, you need their IDs:

1. In Discord, go to **User Settings > Advanced** and enable **Developer Mode**.
2. Right-click on a **server name** and select **Copy Server ID** to get the guild ID.
3. Right-click on a **channel name** and select **Copy Channel ID** to get the channel ID.

### Step 6: Configure the Connector in AI Shuffle

AI Shuffle offers a **guided setup** that discovers the servers and channels your bot can see, so you can pick them from a list instead of copying IDs by hand. (Step 5 is only needed if you prefer to enter IDs manually.)

1. Open the AI Shuffle web UI and navigate to **Settings > Connectors**.
2. Click **Add Connector** and select **Discord**.
3. Enter a **Connector Name**, choose an **API User** (or leave it to auto-create one), and paste the **Bot Token** from Step 2.
4. Click **Validate**. AI Shuffle logs into Discord with the token and confirms the bot identity ("Authenticated as ...") along with how many servers it can see.
   - If the bot is not in any server yet, an **Open Discord invite** link appears -- use it to add the bot, then click **Re-validate**.
5. Select the servers (guilds) and text channels the bot should operate in from the checklists. Leaving a list unchecked allows **all** visible servers/channels.
6. Choose a **Command Prefix** from the preset chips (`!`, `?`, `.`), pick **None**, or choose **Custom** to type your own.
7. Select the **Events** this connector should listen to, then click **Create Connector** and **Enable** it.

Prefer to type IDs yourself? Click **Enter IDs manually** under the Bot Token field to switch to plain text inputs for Guild IDs, Allowed Channel IDs, and Command Prefix (see Step 5 for how to obtain the IDs).

When editing an existing Discord connector, the stored token is masked. Click **Validate** to re-run discovery against the saved token (you do not need to re-enter it), or paste a new token first to validate that instead.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `bot_token` | string | Yes | The bot token from the Discord Developer Portal. This authenticates the bot with the Discord Gateway and REST APIs. |
| `guild_ids` | array of strings | No | List of Discord server (guild) IDs where the bot should operate. If empty, the bot responds in all servers it has been invited to. |
| `allowed_channel_ids` | array of strings | No | List of channel IDs the bot is allowed to respond in. If empty, the bot responds in all channels it can see. This filter applies in addition to `guild_ids`. |
| `command_prefix` | string | No | The prefix character(s) that trigger bot commands. Defaults to `!`. Set to an empty string to make the bot respond to all messages (use with caution). |

## Capabilities

The Discord connector supports the following capabilities:

| Capability | Supported | Notes |
|------------|-----------|-------|
| Typing indicator | Yes | Shows the bot as "typing..." while the agent processes a response |
| Reactions | Yes | Can add emoji reactions to user messages |
| Rich text | Yes | Full Discord Markdown support including bold, italic, code blocks, blockquotes, spoiler tags, and embeds |
| File upload | Yes | Supports file attachments up to 25 MB (or higher with server boost level) |
| Threads | No | The connector does not declare thread support; it does not create or manage threads as a dedicated capability |
| Buttons | Yes | Supports interactive message components including buttons and select menus |

**Message Length Limit:** 2,000 characters per message. Messages exceeding this limit are automatically split across multiple messages. Embed descriptions have a separate limit of 4,096 characters.

## Troubleshooting

### Bot appears online but does not respond to messages

- **Message Content Intent not enabled.** This is the most common issue. Go to the Developer Portal, open your application's Bot settings, and ensure **Message Content Intent** is toggled on. Without it, the bot receives empty message bodies.
- **Command prefix mismatch.** If `command_prefix` is set to `!`, the bot only responds to messages starting with `!`. Either change the prefix or remove it to respond to all messages.
- **Channel permissions.** Verify the bot has **Read Messages** and **Send Messages** permissions in the target channel. Check both the server role permissions and any channel-specific permission overrides.

### Bot is offline in Discord

- **Invalid token.** If the token was regenerated in the Developer Portal, the old token no longer works. Update the connector configuration with the new token.
- **Connector not enabled.** Check the AI Shuffle Connectors page to ensure the Discord connector is enabled and shows a "Connected" status.
- **Rate limit or session limit.** Discord limits bots to one active Gateway session per token (by default). Ensure no other instance is running with the same token.

### "Missing Access" or 403 errors

- The bot lacks required permissions in the server or channel. Re-invite the bot using the invite link from Step 4, which includes all necessary permission scopes.
- For thread access, the bot needs **Send Messages in Threads** permission.

### Messages are being truncated or split awkwardly

- Discord enforces a strict **2,000-character limit** per message. The connector splits at natural boundaries (newlines, sentence endings) when possible. If code blocks are being broken, consider configuring your agent to produce shorter code examples or use file attachments for long code.

### Bot responds in unwanted channels

- Configure `allowed_channel_ids` to restrict the bot to specific channels. Alternatively, remove the bot's permissions from channels where it should not respond using Discord's channel permission overrides.

### "Disallowed Intents" error at startup

- Your bot is trying to use a privileged intent that has not been enabled. Go to the Developer Portal and enable all required intents under **Bot > Privileged Gateway Intents**: **Message Content Intent** is required; **Server Members Intent** and **Presence Intent** are optional.
