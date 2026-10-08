<!--
title: Telegram
description: Chat with your agent through a Telegram bot
order: 1
-->

# Telegram

Chat with your agent through your own Telegram bot, one-on-one or in a group. Replies support formatting and buttons.

No public address or port forwarding is needed.

## You need

- A Telegram account and the Telegram app.

## Set up

### 1. Create a bot

1. In Telegram, open **@BotFather** (the official one, with a blue check mark).
2. Send `/newbot`.
3. Enter a display name, such as `My AI Assistant`.
4. Enter a username that ends in `bot`, such as `my_ai_shuffle_bot`.
5. BotFather replies with a **bot token**, like `7123456789:AAHfiqksKZ8WmR2zMn5x_example_token`. Keep it private.

Optional: `/setdescription`, `/setabouttext`, `/setuserpic` and `/setcommands` in BotFather customize your bot.

### 2. Find your chat ID

The bot only answers in chats you list.

1. Send your bot a message (or, for a group, add the bot and send a message there).
2. Open `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` in a browser, with your token.
3. Find `"chat":{"id": ...}`. Private chats have positive numbers. Groups have negative numbers, such as `-1001234567890`.

### 3. Connect it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **Telegram**. Choose **Install Telegram** if asked.
2. Choose **Connect account**.
3. Paste the **Bot Token** and add at least one **Allowed Chat ID**.
4. Pick the agent, choose **Create Connector**, then turn on **Activate**.

See [Common setup steps](/docs/connectors#common-setup-steps).

### 4. Groups (optional)

In groups, a bot normally only sees commands (starting with `/`) and messages that mention it. To let it see every message, send `/setprivacy` to BotFather, choose your bot and pick **Disable**. Then remove the bot from the group and add it back.

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `bot_token` | Yes | The token from BotFather |
| `allowed_chat_ids` | Yes | The chats the bot answers in |
| `enabled_action_categories` | No | Limit the agent to some kinds of actions: `messages`, `reactions`, `pins`, `polls`, `media`, `chat`, `moderation`. Empty allows all |
| `auto_approve_destructive` | No | `true` skips the Approve/Deny prompt for destructive actions |

## What your agent can do

See [How AI actions work](/docs/connectors#how-ai-actions-work). The agent can send, edit, delete and forward messages, react, pin and unpin, make polls, send photos and documents, change a chat's title and description, and ban members.

- Deleting messages, unpinning all, banning and leaving a chat ask for **Approve** in the chat first.
- Admin actions only work when the bot is an admin of that chat.
- Messages longer than 4,096 characters are split into several messages.

## Troubleshooting

**The bot does not answer.**

- Check the token. In BotFather, `/token` gives you a new one if needed.
- Check that the connection shows **Connected** in **Settings → Plugins → Telegram**.
- Check that the chat ID is in your allowed list.
- The worker needs internet access to `api.telegram.org`.

**The bot ignores group messages.** Turn off privacy mode as in step 4, then add the bot to the group again.

**"Unauthorized" errors.** The token was replaced or revoked. Get a new one with `/token` and update the connection.

**Some messages fail in busy chats.** Telegram limits how fast a bot can send, about one message per second per chat. Messages over the limit are not retried.
