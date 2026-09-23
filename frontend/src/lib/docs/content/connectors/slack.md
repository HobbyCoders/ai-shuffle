<!--
title: Slack
description: Connect AI Shuffle to Slack using a Slack App with Socket Mode for real-time communication
order: 4
-->

# Slack Bot Connector

## Overview

The Slack connector integrates your AI Shuffle agent with Slack workspaces using the Slack API and Socket Mode. Your agent can respond to direct messages, participate in channel conversations, join and reply in threads, react to messages with emoji, and share files -- all within the familiar Slack interface.

Socket Mode is the recommended connection method because it does not require a publicly reachable URL. Instead of receiving HTTP webhooks, the bot establishes a WebSocket connection to Slack's servers, making it ideal for development environments, firewalled networks, and self-hosted deployments. The connector uses the `xoxb-` bot token for API calls and the `xapp-` app-level token for the Socket Mode WebSocket connection.

## Prerequisites

- A Slack workspace where you have permission to install apps (or are a workspace admin)
- Access to the [Slack API portal](https://api.slack.com/apps)
- AI Shuffle backend running (no public URL required thanks to Socket Mode)

## Setup Guide

### Step 1: Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**.
2. Select **From scratch**.
3. Enter an app name (e.g., "AI Shuffle") and select the workspace where you want to install it.
4. Click **Create App**.

### Step 2: Enable Socket Mode

Socket Mode allows the bot to receive events over a WebSocket connection instead of HTTP webhooks.

1. In the left sidebar, click **Socket Mode**.
2. Toggle **Enable Socket Mode** to on.
3. You will be prompted to create an **App-Level Token**. Give it a name (e.g., "socket-token") and add the `connections:write` scope.
4. Click **Generate**. Copy the token that starts with `xapp-`. Store it securely.
   ```
   EXAMPLE-SLACK-APP-TOKEN...
   ```

### Step 3: Configure Bot Token Scopes (OAuth & Permissions)

1. In the left sidebar, click **OAuth & Permissions**.
2. Scroll to **Scopes > Bot Token Scopes** and add the following scopes:

   | Scope | Purpose |
   |-------|---------|
   | `app_mentions:read` | Detect when users @mention the bot |
   | `channels:history` | Read messages in public channels |
   | `channels:read` | View basic channel info |
   | `chat:write` | Send messages |
   | `files:read` | Access files shared in conversations |
   | `files:write` | Upload files |
   | `groups:history` | Read messages in private channels |
   | `groups:read` | View basic info about private channels |
   | `im:history` | Read direct messages |
   | `im:read` | View basic info about direct messages |
   | `im:write` | Start direct message conversations |
   | `reactions:read` | View emoji reactions |
   | `reactions:write` | Add emoji reactions |
   | `users:read` | View basic user info |

3. Scroll back to the top and click **Install to Workspace**, then **Allow**.
4. Copy the **Bot User OAuth Token** that starts with `xoxb-`. Store it securely.
   ```
   EXAMPLE-SLACK-BOT-TOKEN
   ```

### Step 4: Subscribe to Events

1. In the left sidebar, click **Event Subscriptions**.
2. Toggle **Enable Events** to on.
3. Under **Subscribe to Bot Events**, add the following events:

   | Event | Purpose |
   |-------|---------|
   | `message.channels` | Messages in public channels |
   | `message.groups` | Messages in private channels |
   | `message.im` | Direct messages |
   | `app_mention` | When the bot is @mentioned |

4. Click **Save Changes**.

### Step 5: Get Channel IDs (Optional)

To restrict the bot to specific channels:

1. In Slack, right-click on a channel name.
2. Select **View channel details** (or **Copy link**).
3. The channel ID is the last segment of the channel URL, starting with `C` (public) or `G` (private). For example, in `https://yourworkspace.slack.com/archives/C01ABCD2EFG`, the channel ID is `C01ABCD2EFG`.

Alternatively, you can right-click a channel, select **Copy link**, and extract the ID from the URL.

### Step 6: Configure the Connector in AI Shuffle

1. Open the AI Shuffle web UI and navigate to **Settings > Connectors**.
2. Click **Add Connector** and select **Slack**.
3. Fill in the configuration fields:
   - **Bot Token**: Paste the `xoxb-` token from Step 3
   - **App Token**: Paste the `xapp-` token from Step 2
   - **Allowed Channel IDs** (optional): Channel IDs to restrict the bot to
   - **Command Prefix** (optional): If set, the bot only responds to messages that start with this prefix
4. Click **Save** and then **Enable** the connector.

### Step 7: Invite the Bot to Channels

The bot must be invited to channels before it can read or send messages there:

1. In Slack, go to the channel where you want the bot.
2. Type the following message:
   ```
   /invite @AI Shuffle
   ```
   (Replace "AI Shuffle" with your bot's actual display name.)

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `bot_token` | string | Yes | The Bot User OAuth Token (starts with `xoxb-`). Used for all Slack API calls including sending messages, reading channels, and uploading files. |
| `app_token` | string | Yes | The App-Level Token (starts with `xapp-`). Used exclusively for establishing the Socket Mode WebSocket connection. Must have the `connections:write` scope. |
| `allowed_channel_ids` | array of strings | No | List of Slack channel IDs the bot is allowed to respond in. If empty, the bot responds in all channels it has been invited to. Channel IDs start with `C` (public) or `G` (private). |
| `command_prefix` | string | No | If set, only messages that start with this prefix are processed. If empty, the bot processes all messages in allowed channels. |
| `enabled_action_categories` | array of strings | No | Restrict the AI's actions to these categories. Empty = all. Valid values: `messages`, `reactions`, `pins`, `channels`, `users`, `files`, `workspace`. |
| `auto_approve_destructive` | string | No | Set to `true` to skip the in-channel Approve/Deny prompt for destructive AI actions (message deletion, channel archiving). Default: prompts are required. |

## Capabilities

The Slack connector supports the following capabilities:

| Capability | Supported | Notes |
|------------|-----------|-------|
| Acknowledgement | Partial | The bot adds an `:eyes:` reaction to acknowledge receipt while processing. There is no live typing indicator. |
| Reactions | Yes | Can add and read emoji reactions on messages |
| Rich text | Yes | Full Slack `mrkdwn` formatting: bold, italic, strikethrough, code, code blocks, blockquotes, lists, and links |
| File upload | Yes | Upload files up to the workspace's file size limit (default 1 GB for paid plans) |
| Threads | Yes | Can create threaded replies and follow conversation threads |
| Buttons | Yes | Used for destructive-action prompts. Tool approvals are answered by the owner in the app; the channel only sees a waiting notice |

**Message Length Limit:** 4,000 characters per message. Messages exceeding this limit are automatically split. Slack has a hard limit of 40,000 characters but recommends keeping messages under 4,000 for readability.

## AI Actions (Action Gateway)

Sessions bound to a Slack channel get two MCP tools instead of one tool per feature:

- **`slack_actions`** — discovery. Returns the action catalog (~21 actions: send/read/edit/delete/schedule messages, threads, reactions, pins, channel management, user lookup, file upload, workspace info), each with its input schema and the OAuth scope it needs. The catalog is filtered by this install's `enabled_action_categories`.
- **`slack_invoke`** — execution. Takes `{action, params}`, validates `params` server-side, and dispatches.

Safety rails:

- Message-level actions only work in the session's bound channel and the connector's `allowed_channel_ids`.
- Actions tagged **destructive** (`delete_message`, `archive_channel`) post an in-channel Block Kit **Approve/Deny** prompt and only run after a human clicks Approve (2-minute timeout denies). Set `auto_approve_destructive: true` to skip the prompt.
- Slack enforces OAuth scopes itself: an action whose scope is missing from the bot token fails with `missing_scope` — add the scope under **OAuth & Permissions** and reinstall the app to light it up.

## Troubleshooting

### Bot does not respond to messages

- **Bot not invited to the channel.** The bot must be explicitly invited to each channel with `/invite @BotName`. It can only see messages in channels it has joined.
- **Missing event subscriptions.** Verify that the `message.channels`, `message.groups`, `message.im`, and `app_mention` events are subscribed under **Event Subscriptions**.
- **Socket Mode not connected.** Check the connector status in AI Shuffle. If it shows "Disconnected," verify the `xapp-` token is correct and has the `connections:write` scope.

### "missing_scope" errors in logs

- The bot token is missing required OAuth scopes. Go to **OAuth & Permissions** in the Slack app settings, add the missing scope, and **reinstall the app to the workspace** (Slack requires reinstallation when scopes change).

### Bot responds to every message (too noisy)

- Configure `allowed_channel_ids` to restrict the bot to specific channels.
- Alternatively, set a `command_prefix` so the bot only processes messages that start with that prefix.

### "invalid_auth" or "token_revoked" errors

- The bot token or app token is invalid. This can happen if the app was reinstalled (which generates new tokens) or if an admin revoked the tokens. Generate new tokens in the Slack app settings and update the connector configuration.

### Messages appear out of order

- Slack message delivery through Socket Mode is generally ordered, but during reconnections, messages may arrive out of sequence. The connector includes message deduplication and ordering logic, but if issues persist, check the AI Shuffle logs for WebSocket reconnection events.

### Bot cannot upload files

- Verify the `files:write` scope is included in the bot token. After adding it, you must reinstall the app to the workspace for the new scope to take effect.
