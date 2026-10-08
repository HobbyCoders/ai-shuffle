<!--
title: Slack
description: Chat with your agent through a Slack app
order: 4
-->

# Slack

Chat with your agent in Slack, in channels, threads and direct messages. It can react, pin messages and share files.

No public address or port forwarding is needed.

## You need

- A Slack workspace where you can install apps.

## Set up

### 1. Create the Slack app

1. Open [api.slack.com/apps](https://api.slack.com/apps) and choose **Create New App → From scratch**.
2. Name it, for example "AI Shuffle", pick your workspace and choose **Create App**.

### 2. Turn on Socket Mode

1. Open **Socket Mode** and turn on **Enable Socket Mode**.
2. Create an **App-Level Token** with the `connections:write` scope.
3. Copy the token. It starts with `xapp-`.

### 3. Add permissions and install

1. Open **OAuth & Permissions**. Under **Bot Token Scopes**, add:
   `app_mentions:read`, `channels:history`, `channels:read`, `chat:write`, `files:read`, `files:write`, `groups:history`, `groups:read`, `im:history`, `im:read`, `im:write`, `reactions:read`, `reactions:write`, `users:read`, `pins:read`, `pins:write`.
   Add `channels:manage` too if the agent should create and archive channels.
2. Choose **Install to Workspace**, then **Allow**.
3. Copy the **Bot User OAuth Token**. It starts with `xoxb-`.

### 4. Subscribe to events

Open **Event Subscriptions**, turn on **Enable Events** and add these bot events: `message.channels`, `message.groups`, `message.im` and `app_mention`. Save.

### 5. Find your channel IDs

The bot only answers in channels you list. Right-click a channel, choose **Copy link** and take the last part of the link, such as `C01ABCD2EFG`.

### 6. Connect it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **Slack**. Choose **Install Slack** if asked.
2. Choose **Connect account** and paste the **Bot Token** (`xoxb-`) and **App Token** (`xapp-`).
3. Add at least one **Allowed Channel ID**.
4. Optional: choose a **Command Prefix**. The bot then only answers messages that start with it.
5. Pick the agent, choose **Create Connector**, then turn on **Activate**.

See [Common setup steps](/docs/connectors#common-setup-steps).

### 7. Invite the bot

In each channel, type `/invite @AI Shuffle`, with your bot's name.

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `bot_token` | Yes | The `xoxb-` token |
| `app_token` | Yes | The `xapp-` token |
| `allowed_channel_ids` | Yes | The channels the bot answers in |
| `command_prefix` | No | Only answer messages that start with this |
| `enabled_action_categories` | No | Limit the agent to some kinds of actions: `messages`, `reactions`, `pins`, `channels`, `users`, `files`, `workspace`. Empty allows all |
| `auto_approve_destructive` | No | `true` skips the Approve/Deny prompt for destructive actions |

## What your agent can do

See [How AI actions work](/docs/connectors#how-ai-actions-work). The agent can send, read, edit, delete and schedule messages, reply in threads, react, pin, manage channels, look up people, upload files and read workspace info.

- Messages stay in the channel the chat started from and your allowed channels.
- Deleting a message and archiving a channel ask for **Approve** in the channel first.
- The bot adds an :eyes: reaction while it works on your message.
- Messages longer than 4,000 characters are split into several messages.

## Troubleshooting

**The bot does not answer.**

- Invite the bot to the channel with `/invite`.
- Check the four event subscriptions in step 4.
- Check that the connection shows **Connected**. If not, check the `xapp-` token and its `connections:write` scope.

**"missing_scope" errors.** Add the missing scope under **OAuth & Permissions**, then reinstall the app to the workspace. Slack needs a reinstall whenever scopes change.

**The bot answers too often.** Remove channels from the allowed list, or set a command prefix.

**"invalid_auth" or "token_revoked".** Reinstalling the app creates new tokens, and an admin can revoke them. Copy the new tokens into the connection.

**The bot cannot upload files.** Add `files:write` and reinstall the app.
