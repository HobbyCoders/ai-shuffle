<!--
title: LINE
description: Connect AI Shuffle to LINE using the LINE Messaging API for reach across Asia-Pacific markets
order: 5
-->

# LINE Messaging API Connector

## Overview

The LINE connector integrates your AI Shuffle agent with LINE, one of the most popular messaging platforms in Japan, Taiwan, Thailand, and Indonesia with over 200 million monthly active users. Through the LINE Messaging API, your agent can send text messages and quick-reply buttons, and post permission prompts as rich Flex Messages to LINE users.

The connector uses the LINE Messaging API to send messages. Outbound delivery uses push messages (sent proactively, which may incur costs depending on your plan). Setting up requires creating a channel in the LINE Developers Console and configuring its credentials in AI Shuffle.

> **Note:** The current connector implementation handles **outbound** messages only. The inbound webhook handler exists in the code (`handle_webhook_payload`) but the AI Shuffle API does not yet expose a LINE webhook route, so incoming LINE messages are not delivered to the agent. The steps below describe configuring credentials and disabling auto-reply; the webhook section documents where inbound support is headed once a route is added.

## Prerequisites

- A [LINE account](https://line.me/) (personal account is sufficient to get started)
- Access to the [LINE Developers Console](https://developers.line.biz/console/)
- A LINE Official Account (created automatically when you create a Messaging API channel)
- AI Shuffle backend running with outbound HTTPS access to the LINE Messaging API (`api.line.me`)

## Setup Guide

### Step 1: Create a LINE Developers Account

1. Go to the [LINE Developers Console](https://developers.line.biz/console/).
2. Log in with your LINE account.
3. If this is your first time, you will be prompted to create a **Developer** profile. Enter your name and email.

### Step 2: Create a Provider

1. In the LINE Developers Console, click **Create** under **Providers** on the home screen.
2. Enter a provider name (e.g., "AI Shuffle" or your company/project name).
3. Click **Create**.

A provider is an organizational unit that groups your channels (apps). It can represent a company, team, or individual developer.

### Step 3: Create a Messaging API Channel

1. Inside your provider, click **Create a Messaging API channel**.
2. Fill in the required fields:
   - **Channel name**: The display name users see (e.g., "AI Shuffle Bot")
   - **Channel description**: A brief description of the bot
   - **Category** and **Subcategory**: Select the most appropriate categories
   - **Email address**: A contact email
3. Agree to the terms and click **Create**.

### Step 4: Get Your Channel Credentials

After creating the channel, navigate to the channel settings:

1. **Channel Secret**: Found on the **Basic settings** tab. Click **Issue** if it has not been generated yet. This is used to verify webhook signatures.
   ```
   Example: a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6
   ```

2. **Channel Access Token**: Go to the **Messaging API** tab, scroll to **Channel access token**, and click **Issue**. This is a long-lived token used for API calls.
   ```
   Example: eyJhbGciOiJIUzI1NiJ9.exampleTokenContent...
   ```

### Step 5: Webhook (inbound) — not yet available

AI Shuffle does not currently expose a LINE webhook route, so there is no AI Shuffle URL to register here and inbound LINE messages are not delivered to the agent yet. You can leave the **Webhook settings** section unconfigured.

The connector's other API endpoints live under the `/api/v1/connectors` prefix (for example, the WhatsApp webhook is served at `/api/v1/connectors/whatsapp/webhook`). If LINE inbound support is added in a future release, the route would follow the same `/api/v1/connectors/...` scheme.

### Step 6: Disable Auto-Reply Messages

By default, LINE Official Accounts send automatic greeting and away messages. These interfere with the AI agent responses:

1. On the **Messaging API** tab, click the link to open the **LINE Official Account Manager**.
2. Go to **Settings > Response settings**.
3. Set **Auto-response** to **Disabled**.
4. Set **Greeting message** to **Disabled** (or customize it if you want a one-time welcome message).

### Step 7: Configure the Connector in AI Shuffle

1. Open the AI Shuffle web UI and navigate to **Settings > Connectors**.
2. Click **Add Connector** and select **LINE**.
3. Fill in the configuration fields:
   - **Channel Access Token**: Paste the token from Step 4
   - **Channel Secret**: Paste the secret from Step 4
   - **Allowed User IDs** (optional): A list of LINE user IDs; when set, only messages from these users are processed
   - **Allowed Group IDs** (optional): A list of LINE group IDs; when set, only group messages from these groups are processed
4. Click **Save** and then **Enable** the connector.

### Step 8: Add the Bot as a Friend

To test the bot:

1. On the **Messaging API** tab in the LINE Developers Console, find the **QR code** section.
2. Scan the QR code with the LINE app on your phone.
3. Add the bot as a friend and start chatting.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `channel_access_token` | string | Yes | The long-lived channel access token from the LINE Developers Console. Used to authenticate all Messaging API calls (sending messages, getting user profiles, etc.). |
| `channel_secret` | string | Yes | The channel secret from the LINE Developers Console. Used to validate the signature of incoming webhook requests to ensure they originate from LINE's servers. |
| `allowed_user_ids` | list | No | If set, only messages from these LINE user IDs are processed; messages from other users are ignored. |
| `allowed_group_ids` | list | No | If set, only group messages from these LINE group IDs are processed; messages from other groups are ignored. |

## Capabilities

The LINE connector supports the following capabilities:

| Capability | Supported | Notes |
|------------|-----------|-------|
| Typing indicator | No | The connector does not send a loading/typing indicator while the agent processes a response |
| Reactions | No | LINE does not support bot reactions on messages via the Messaging API |
| Rich text | Partial | The connector declares rich-text support and uses LINE Flex Messages for permission prompts. Regular agent replies are sent as plain text messages; Markdown is not converted to Flex layouts. |
| File upload | No | The connector sends text and quick-reply buttons only; it does not send images, video, audio, or files. (Incoming media messages, if inbound support is added later, are surfaced to the agent as a text placeholder such as `[Image]`.) |
| Buttons | Yes | Buttons attached to an outgoing message are rendered as LINE quick-reply items (up to 13 per message); labels are truncated to 20 characters |
| Threads | No | LINE does not have a thread model |

**Message Length Limit:** 5,000 characters per text message. Messages exceeding this limit are automatically split into multiple text messages. LINE also limits the number of message objects in a single API call to 5 "bubbles".

## AI Actions (Action Gateway)

Sessions bound to a LINE chat get two MCP tools instead of one tool per feature:

- **`line_actions`** — discovery. Returns the action catalog (7 actions: send text/sticker/image messages, get user profile, get group summary, leave a group, get message quota), each with its input schema. The catalog is filtered by this install's `enabled_action_categories`.
- **`line_invoke`** — execution. Takes `{action, params}`, validates `params` server-side, and dispatches.

Safety rails:

- Message-level actions only work on the session's bound chat and the connector's `allowed_user_ids` / `allowed_group_ids`.
- The **destructive** action (`leave_group`) posts a Flex Message **Approve/Deny** prompt and only runs after a human taps Approve (2-minute timeout denies). Set `auto_approve_destructive: true` to skip the prompt.

## Troubleshooting

### Bot does not respond to messages

- **Inbound delivery is not yet wired up.** AI Shuffle does not currently expose a LINE webhook route, so messages a user sends to the bot are not delivered to the agent. The connector can send (push) messages but cannot receive them yet. See the note in the Overview.
- **Auto-reply is intercepting messages.** If the LINE Official Account has auto-reply enabled, it may be responding on its own. Disable auto-reply in the LINE Official Account Manager (Step 6).

### Connector fails to connect or send

- The `channel_access_token` in the connector configuration does not match (or has expired against) the token in the LINE Developers Console. Re-issue the token and ensure there are no trailing spaces or missing characters. The connector validates the token by calling LINE's bot-info endpoint when it connects.
- Outbound HTTPS to `api.line.me` is blocked. Ensure the AI Shuffle backend can reach the LINE Messaging API.

### Messages from the bot appear as plain text (no formatting)

- LINE does not support Markdown natively, and the connector sends regular agent replies as plain text messages. Rich formatting on LINE requires Flex Messages, which the connector only uses for permission prompts, so Markdown in agent responses renders as plain text.

### "Exceeds the rate limit" errors

- The LINE Messaging API enforces rate limits on push messages, and this connector sends all outbound messages as push messages. The default free-plan quota is limited (commonly 200–500 free messages per month, depending on region and plan), with higher tiers available on paid plans. Monitor your usage in the LINE Official Account Manager under **Statistics**.

### Users cannot find the bot

- The bot must be added as a friend before users can message it. Share the bot's LINE URL (`https://line.me/R/ti/p/@yourbotid`), QR code, or LINE ID with users. The bot does not appear in LINE's search unless you apply for a verified or premium account.
