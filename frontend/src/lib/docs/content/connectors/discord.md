<!--
title: Discord
description: Chat with your agent through a Discord bot
order: 2
-->

# Discord

Chat with your agent in your Discord servers through your own bot. It can answer in channels and threads, react, share files and, if you allow it, help run your server.

## You need

- A Discord account.
- **Manage Server** permission on the server, or a server admin who can invite the bot.

## Set up

### 1. Create the application and bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and choose **New Application**.
2. Name it, for example "AI Shuffle Bot", and choose **Create**. Note the **Application ID**.
3. Open **Bot** in the sidebar. Under **Token**, choose **Reset Token** and copy the token. Keep it private.
4. Under **Privileged Gateway Intents**, turn on **Message Content Intent** and save. Without it, the bot cannot read messages.

### 2. Invite the bot to your server

Open this link in your browser, with your Application ID:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APPLICATION_ID&permissions=277025770560&scope=bot
```

Pick your server and choose **Authorize**. This gives the bot the basics: send messages (also in threads), read history, add reactions and attach files.

You can give the bot more Discord permissions, such as Manage Roles or Ban Members. The agent can then use the matching actions.

### 3. Connect it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **Discord Bot**. Choose **Install Discord Bot** if asked.
2. Choose **Connect account**, enter a name and paste the **Bot Token**.
3. Choose **Validate**. AI Shuffle confirms the bot and lists the servers it can see. If it is in none yet, use the **Open Discord invite** link, then **Re-validate**.
4. Tick the servers and channels the bot should answer in. At least one is required.
5. Optional: choose a **Command Prefix**, such as `!`. The bot then only answers messages that start with it.
6. Pick the agent, choose **Create Connector**, then turn on **Activate**.

See [Common setup steps](/docs/connectors#common-setup-steps).

Prefer typing IDs? Choose **Enter IDs manually**. To copy IDs in Discord, turn on **User Settings → Advanced → Developer Mode**, then right-click a server or channel and choose **Copy ID**.

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `bot_token` | Yes | The bot's token |
| `guild_ids` | One of these two | The servers the bot answers in |
| `allowed_channel_ids` | One of these two | The channels the bot answers in |
| `command_prefix` | No | Only answer messages that start with this |
| `enabled_action_categories` | No | Limit the agent to some kinds of actions: `messages`, `reactions`, `threads`, `channels`, `roles`, `members`, `moderation`, `webhooks`, `invites`, `emojis`, `events`, `server`. Empty allows all |
| `auto_approve_destructive` | No | `true` skips the Approve/Deny prompt for destructive actions |

## What your agent can do

See [How AI actions work](/docs/connectors#how-ai-actions-work). The agent can work with messages, embeds, polls, files, reactions, pins, threads, channels, roles, members, moderation, webhooks, invites, custom emojis, scheduled events and server info, as far as the bot's Discord permissions allow.

- Messages stay in the channel the chat started from, your allowed channels and their threads.
- Destructive actions, such as deleting, kicking, banning or purging, ask for **Approve** in the channel first.
- The bot token is never shown to the agent.
- Messages longer than 2,000 characters are split into several messages.

## Troubleshooting

**The bot is online but does not answer.**

- Turn on **Message Content Intent** in the Developer Portal. This is the most common cause.
- If you set a command prefix, start your message with it.
- Check the bot can read and send messages in that channel.

**The bot is offline.**

- The token was reset. Paste the new one into the connection.
- Check that the connection is activated and shows **Connected**.
- Only one program can use a bot token at a time. Stop any other copy using it.

**"Missing Access" errors.** The bot lacks a permission there. Invite it again with the link above, and check channel permission overrides.

**"Disallowed Intents" error.** Turn on **Message Content Intent** under **Bot → Privileged Gateway Intents**.

**The bot answers where it should not.** Limit it with allowed channels, or remove its permissions in those channels.
