<!--
title: Discord
description: Connect AI Shuffle to Discord using a bot application from the Discord Developer Portal
order: 2
-->

# Discord Bot Connector

## Overview

The Discord connector integrates your AI Shuffle agent with Discord, one of the most popular communication platforms for communities and teams. Once configured, your agent can respond to messages in Discord channels, participate in threads, react to messages, and share files -- all through a Discord bot account.

Setting up the connector involves creating a Discord Application in the Developer Portal, adding a Bot user to it, and inviting the bot to your server(s). The connector communicates with Discord using the Gateway API (WebSocket) for real-time message reception and the REST API for sending responses. You list the servers or channels the bot may operate in, so you control exactly where it responds.

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
- Manage Messages (optional, for cleanup)

You can grant the bot **any additional permissions** you want the AI to be able to use (Manage Roles, Manage Channels, Kick/Ban Members, Manage Webhooks, Manage Events, ...). The AI's available actions automatically reflect whatever the bot is actually allowed to do in the server — see [AI Actions](#ai-actions) below.

The permissions integer for the baseline scopes is `277025770560`. Use this invite URL format:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APPLICATION_ID&permissions=277025770560&scope=bot
```

Replace `YOUR_APPLICATION_ID` with the actual Application ID, then open the URL in your browser. Select the server you want to add the bot to and click **Authorize**.

### Step 5: Get Guild and Channel IDs (Optional)

The bot only responds in servers or channels you list. The guided setup in Step 6 can find these for you. To enter them by hand, you need their IDs:

1. In Discord, go to **User Settings > Advanced** and enable **Developer Mode**.
2. Right-click on a **server name** and select **Copy Server ID** to get the guild ID.
3. Right-click on a **channel name** and select **Copy Channel ID** to get the channel ID.

### Step 6: Configure the Connector in AI Shuffle

AI Shuffle offers a **guided setup** that discovers the servers and channels your bot can see, so you can pick them from a list instead of copying IDs by hand. (Step 5 is only needed if you prefer to enter IDs manually.)

1. Open the AI Shuffle web UI and go to **Settings > Plugins**, choose the **Apps** filter and open **Discord Bot**.
2. Click **Install Discord Bot** if prompted, then click **Connect account**.
3. Enter a **Connector Name** and paste the **Bot Token** from Step 2. To choose an existing **API User** instead of auto-creating one, open **Advanced settings**.
4. Click **Validate**. AI Shuffle logs into Discord with the token and confirms the bot identity ("Authenticated as ...") along with how many servers it can see.
   - If the bot is not in any server yet, an **Open Discord invite** link appears -- use it to add the bot, then click **Re-validate**.
5. Select the servers (guilds) and text channels the bot should operate in from the checklists. You must select at least one server or channel before the connector can be enabled.
6. Choose a **Command Prefix** from the preset chips (`!`, `?`, `.`), pick **None**, or choose **Custom** to type your own.
7. Click **Create Connector**, then turn on the connection's **Activate** switch.

The connection needs an API user with an agent, or every message is refused. See [Common Setup Steps](/docs/connectors#common-setup-steps).

Prefer to type IDs yourself? Click **Enter IDs manually** under the Bot Token field to switch to plain text inputs for Guild IDs, Allowed Channel IDs, and Command Prefix (see Step 5 for how to obtain the IDs).

When editing an existing Discord connector, the stored token is masked. Click **Validate** to re-run discovery against the saved token (you do not need to re-enter it), or paste a new token first to validate that instead.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `bot_token` | string | Yes | The bot token from the Discord Developer Portal. This authenticates the bot with the Discord Gateway and REST APIs. |
| `guild_ids` | array of strings | Yes (to enable, or `allowed_channel_ids`) | Discord server (guild) IDs where the bot should operate. `guild_ids` or `allowed_channel_ids` must be set before the connector can be enabled. |
| `allowed_channel_ids` | array of strings | Yes (to enable, or `guild_ids`) | Channel IDs the bot is allowed to respond in. This filter applies in addition to `guild_ids`. |
| `command_prefix` | string | No | If set, only messages starting with this prefix are processed. Defaults to none: all messages in allowed channels are processed. |
| `enabled_action_categories` | array of strings | No | Restrict the AI's actions to these categories. Empty = all categories. Valid values: `messages`, `reactions`, `threads`, `channels`, `roles`, `members`, `moderation`, `webhooks`, `invites`, `emojis`, `events`, `server`. |
| `auto_approve_destructive` | string | No | Set to `true` to skip the in-channel Approve/Deny prompt for destructive AI actions (deletes, bans, purges, ...). Default: prompts are required. |

## Capabilities

The Discord connector supports the following capabilities:

| Capability | Supported | Notes |
|------------|-----------|-------|
| Typing indicator | Yes | Shows the bot as "typing..." while the agent processes a response |
| Reactions | Yes | Can add emoji reactions to user messages |
| Rich text | Yes | Full Discord Markdown support including bold, italic, code blocks, blockquotes, spoiler tags, and embeds |
| File upload | Yes | Supports file attachments up to 25 MB (or higher with server boost level) |
| Threads | Yes | The AI can create, list, and manage threads through the action gateway |
| Buttons | Yes | Supports interactive message components including buttons and select menus |

**Message Length Limit:** 2,000 characters per message. Messages exceeding this limit are automatically split across multiple messages. Embed descriptions have a separate limit of 4,096 characters.

## AI Actions

See [How AI actions work](/docs/connectors#how-ai-actions-work).

Your agent can use about 45 actions across messages, embeds, polls, files, reactions, pins, threads, channels, roles, members, moderation, webhooks, invites, custom emojis, scheduled events and server info. The list your agent sees also depends on the permissions the bot holds in the server: grant the bot a new Discord permission and the matching actions become available.

- Message actions only work in the channel the session started from, the connector's `allowed_channel_ids`, and threads in those channels. Server-level actions are limited to the server containing that channel.
- Destructive actions (delete message/channel/role, kick, ban, timeout, purge, webhook create/delete, ...) post an **Approve/Deny** button prompt in the channel and only run after a person clicks Approve.
- The bot token never passes through the AI; AI Shuffle makes all Discord calls itself.

## Troubleshooting

### Bot appears online but does not respond to messages

- **Message Content Intent not enabled.** This is the most common issue. Go to the Developer Portal, open your application's Bot settings, and ensure **Message Content Intent** is toggled on. Without it, the bot receives empty message bodies.
- **Command prefix mismatch.** If `command_prefix` is set to `!`, the bot only responds to messages starting with `!`. Either change the prefix or remove it to respond to all messages.
- **Channel permissions.** Verify the bot has **Read Messages** and **Send Messages** permissions in the target channel. Check both the server role permissions and any channel-specific permission overrides.

### Bot is offline in Discord

- **Invalid token.** If the token was regenerated in the Developer Portal, the old token no longer works. Update the connector configuration with the new token.
- **Connector not enabled.** Check **Settings > Plugins > Discord Bot** to ensure the connection is activated and shows a "Connected" status.
- **Rate limit or session limit.** Discord limits bots to one active Gateway session per token (by default). Ensure no other instance is running with the same token.

### "Missing Access" or 403 errors

- The bot lacks required permissions in the server or channel. Re-invite the bot using the invite link from Step 4, which includes all necessary permission scopes.
- For thread access, the bot needs **Send Messages in Threads** permission.

### Messages are being truncated or split awkwardly

- Discord enforces a strict **2,000-character limit** per message. The connector splits at the last line break before the limit, or mid-text if there is none. If code blocks are being broken, consider configuring your agent to produce shorter code examples or use file attachments for long code.

### Bot responds in unwanted channels

- Configure `allowed_channel_ids` to restrict the bot to specific channels. Alternatively, remove the bot's permissions from channels where it should not respond using Discord's channel permission overrides.

### "Disallowed Intents" error at startup

- Your bot is trying to use a privileged intent that has not been enabled. Go to the Developer Portal and enable all required intents under **Bot > Privileged Gateway Intents**: **Message Content Intent** is required; **Server Members Intent** and **Presence Intent** are optional.
