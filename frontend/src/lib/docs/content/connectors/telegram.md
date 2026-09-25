<!--
title: Telegram
description: Connect AI Shuffle to Telegram using a Bot created through BotFather
order: 1
-->

# Telegram Bot Connector

## Overview

The Telegram connector lets you interact with your AI Shuffle agents through a Telegram bot. Users can send messages directly to the bot or add it to group chats, and the agent will respond in real time with rich text formatting and interactive buttons.

Telegram bots are created through the official **@BotFather** bot on Telegram. The process takes only a few minutes and gives you a bot token that AI Shuffle uses to send and receive messages via the Telegram Bot API. The connector supports both private (one-on-one) and group conversations, and you list the chats the bot is allowed to respond in.

## Prerequisites

- A Telegram account
- The Telegram app installed on your phone or desktop (needed to interact with @BotFather)
- AI Shuffle backend running and accessible

The connector receives updates using Telegram long-polling (via aiogram), so no publicly reachable URL or webhook setup is required.

## Setup Guide

### Step 1: Create a Bot via @BotFather

1. Open Telegram and search for **@BotFather** (the official bot with a blue checkmark).
2. Start a conversation and send the command:
   ```
   /newbot
   ```
3. BotFather will ask for a **display name** for your bot. This is the name users see in conversations. Enter something like:
   ```
   My AI Assistant
   ```
4. Next, BotFather asks for a **username**. This must end in `bot` and be unique across all of Telegram. For example:
   ```
   my_ai_shuffle_bot
   ```
5. BotFather will respond with a message containing your **bot token**. It looks like this:
   ```
   7123456789:AAHfiqksKZ8WmR2zMn5x_example_token
   ```
   Save this token securely. You will need it for the AI Shuffle configuration.

### Step 2: Configure Bot Settings (Optional)

While still in BotFather, you can customize your bot:

- `/setdescription` -- Set the text shown when users first open the bot
- `/setabouttext` -- Set the bio text shown on the bot's profile
- `/setuserpic` -- Upload a profile picture for the bot
- `/setcommands` -- Define slash commands (e.g., `/start`, `/help`)

### Step 3: Get Chat IDs

The connector only responds in chats you list, so you need at least one chat ID. There are several ways to find them:

**For private chats:**
1. Send a message to your bot.
2. Open the following URL in your browser (replace `<BOT_TOKEN>` with your actual token):
   ```
   https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
   ```
3. Look for the `"chat":{"id": ...}` field in the JSON response. Private chat IDs are positive numbers.

**For group chats:**
1. Add the bot to the group.
2. Send a message in the group.
3. Check the `getUpdates` URL as above. Group chat IDs are negative numbers (e.g., `-1001234567890`).

**Using the /chatid command:**
You can also temporarily set up a command handler that replies with the current chat ID for easy discovery.

### Step 4: Configure the Connector in AI Shuffle

1. Open the AI Shuffle web UI and go to **Build > Plugins**, choose the **Apps** filter and open **Telegram**.
2. Click **Install Telegram** if prompted, then click **Connect account**.
3. Fill in the configuration fields:
   - **Bot Token**: Paste the token from BotFather
   - **Allowed Chat IDs**: Enter at least one chat ID. The bot only responds in these chats.
4. Click **Create Connector**, then turn on the connection's **Activate** switch.

The connection needs an API user with an agent, or every message is refused. See [Common Setup Steps](/docs/connectors#common-setup-steps).

### Step 5: Configure Group Chat Privacy (If Using Groups)

By default, Telegram bots in groups only receive messages that are direct commands (starting with `/`) or that explicitly mention the bot. To let your bot see all messages in a group:

1. Open BotFather and send:
   ```
   /setprivacy
   ```
2. Select your bot.
3. Choose **Disable** to turn off privacy mode.

With privacy mode disabled, the bot receives all messages in group chats. If you prefer the bot to only respond when mentioned, leave privacy mode enabled and instruct users to use `/` commands or `@mention` the bot.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `bot_token` | string | Yes | The bot token obtained from @BotFather. Format: `<numeric_id>:<alphanumeric_string>`. |
| `allowed_chat_ids` | array of strings | Yes (to enable) | Telegram chat IDs the bot is allowed to respond in. At least one entry is required before the connector can be enabled. Private chats have positive IDs; groups have negative IDs. |
| `enabled_action_categories` | array of strings | No | Restrict the AI's actions to these categories. Empty = all. Valid values: `messages`, `reactions`, `pins`, `polls`, `media`, `chat`, `moderation`. |
| `auto_approve_destructive` | string | No | Set to `true` to skip the in-channel Approve/Deny prompt for destructive AI actions (message deletion, bans, unpinning, leaving a chat). Default: prompts are required. |

## Capabilities

The Telegram connector supports the following capabilities:

| Capability | Supported | Notes |
|------------|-----------|-------|
| Typing indicator | Yes | Shows "typing..." status in the chat while the agent is processing |
| Reactions | Yes | Can react to messages with emoji |
| Rich text | Yes | Telegram MarkdownV2 formatting |
| File upload | Yes | Your agent can send photos and documents through actions |
| Buttons | Yes | Inline keyboard buttons |
| Threads | No | Telegram does not have a native thread model (topic groups are handled as separate chats) |

**Message Length Limit:** 4,096 characters per message. Messages exceeding this limit are automatically split into multiple messages.

## AI Actions

See [How AI actions work](/docs/connectors#how-ai-actions-work).

Your agent can use 19 actions: send, edit, delete and forward messages, reactions, pin/unpin, polls, photos and documents, chat info, chat title and description, and member bans.

- Message actions only work in the chat the session started from and in the connector's `allowed_chat_ids`.
- Destructive actions (`delete_message`, `unpin_all_messages`, `ban_chat_member`, `leave_chat`) post an **Approve/Deny** button prompt in the chat and only run after a person taps Approve.
- Group-admin actions (chat title/description, bans, leaving) require the bot to actually be an admin in that chat; Telegram's Bot API rejects the call otherwise and the error is surfaced verbatim.

## Troubleshooting

### Bot is not responding to messages

- **Check the bot token.** Verify that the token in AI Shuffle matches the one from BotFather exactly. Regenerate the token with `/token` in BotFather if needed.
- **Check connector status.** In the AI Shuffle UI, verify the Telegram connection shows as "Connected" under **Build > Plugins > Telegram**.
- **Check allowed chat IDs.** Make sure the chat you are messaging from is in the list. Use the `getUpdates` method to confirm the correct chat ID.
- **Check the backend logs.** The connector uses Telegram long-polling, so the backend must be running and able to reach `api.telegram.org`. Look for polling errors in the logs and confirm the connector shows as "Connected".

### Bot does not see messages in group chats

- **Privacy mode is enabled by default.** Use `/setprivacy` in BotFather and set it to **Disable** for the bot to receive all group messages.
- **Bot was added after privacy change.** Remove the bot from the group and add it back for the new privacy setting to take effect.

### Messages are being truncated

- Telegram enforces a **4,096-character limit** per message. The connector splits long responses automatically, but if formatting breaks at the split point, consider configuring your agent to produce shorter responses.

### "Unauthorized" or 401 errors in logs

- The bot token is invalid or has been revoked. Go to BotFather and use `/token` to generate a new token, then update the connector configuration.

### Rate limiting (429 errors)

- Telegram rate-limits bots to approximately 30 messages per second globally and 1 message per second per chat. The connector does not retry: a rate-limited message fails and the error is logged. Reduce message volume, or limit the bot to fewer chats with `allowed_chat_ids`.
