<!--
title: LINE
description: Connect AI Shuffle to LINE using the LINE Messaging API for reach across Asia-Pacific markets
order: 5
-->

# LINE Messaging API Connector

## Overview

The LINE connector integrates your AI Shuffle agent with LINE, one of the most popular messaging platforms in Japan, Taiwan, Thailand, and Indonesia with over 200 million monthly active users. Through the LINE Messaging API, AI Shuffle can send text messages, stickers and images to LINE users.

The connector uses the LINE Messaging API to send messages. Outbound delivery uses push messages (sent proactively, which may incur costs depending on your plan). Setting up requires creating a channel in the LINE Developers Console and configuring its credentials in AI Shuffle.

> **Note:** The LINE connector does not receive messages. AI Shuffle has no LINE webhook URL, so messages users send to the bot never reach your agent. Because no LINE chat reaches your agent, your agent does not get LINE actions in its sessions either. You can send LINE messages from an [automation](/docs/features/automations) with a **Connector Action** step.

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

### Step 5: Leave the Webhook Unset

AI Shuffle has no LINE webhook URL, so there is nothing to register here. Leave the **Webhook settings** section unconfigured.

### Step 6: Disable Auto-Reply Messages

By default, LINE Official Accounts send automatic greeting and away messages. Turn them off if you do not want users to receive them:

1. On the **Messaging API** tab, click the link to open the **LINE Official Account Manager**.
2. Go to **Settings > Response settings**.
3. Set **Auto-response** to **Disabled**.
4. Set **Greeting message** to **Disabled** (or customize it if you want a one-time welcome message).

### Step 7: Configure the Connector in AI Shuffle

1. Open the AI Shuffle web UI and go to **Build > Plugins**, choose the **Apps** filter and open **LINE**.
2. Click **Install LINE** if prompted, then click **Connect account**.
3. Fill in the configuration fields:
   - **Channel Access Token**: Paste the token from Step 4
   - **Channel Secret**: Paste the secret from Step 4
   - **Allowed User IDs**: At least one LINE user ID. The connector will not start without one. Your own user ID is shown as **Your user ID** on the channel's **Basic settings** tab.
   - **Allowed Group IDs** (optional): A list of LINE group IDs
4. Click **Create Connector**, then turn on the connection's **Activate** switch.

### Step 8: Add the Bot as a Friend

Users need to add the bot as a friend before it can send them messages:

1. On the **Messaging API** tab in the LINE Developers Console, find the **QR code** section.
2. Scan the QR code with the LINE app on your phone.
3. Add the bot as a friend.

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `channel_access_token` | string | Yes | The long-lived channel access token from the LINE Developers Console. Used to authenticate all Messaging API calls (sending messages, getting user profiles, etc.). |
| `channel_secret` | string | Yes | The channel secret from the LINE Developers Console. |
| `allowed_user_ids` | list | Yes (to enable) | LINE user IDs the connector is allowed to work with. `allowed_user_ids` is required; `allowed_group_ids` alone does not satisfy the requirement. |
| `allowed_group_ids` | list | No | LINE group IDs the connector is allowed to work with. |

## Capabilities

The LINE connector supports the following capabilities:

| Capability | Supported | Notes |
|------------|-----------|-------|
| Typing indicator | No | The connector does not send a loading/typing indicator while the agent processes a response |
| Reactions | No | LINE does not support bot reactions on messages via the Messaging API |
| Rich text | No | Text is sent as plain text messages; Markdown is not converted |
| File upload | No | The `send_sticker` and `send_image` actions can send stickers and images. Video, audio and files are not supported. |
| Buttons | Yes | Buttons attached to an outgoing message are rendered as LINE quick-reply items (up to 13 per message); labels are truncated to 20 characters |
| Threads | No | LINE does not have a thread model |

**Message Length Limit:** 5,000 characters per text message. Messages exceeding this limit are automatically split into multiple text messages. LINE also limits the number of message objects in a single API call to 5 "bubbles".

## AI Actions

LINE has 7 actions: send text, sticker and image messages, get user profile, get group summary, leave a group, and get message quota.

Your agent does not get these actions in its sessions, because no LINE chat ever reaches it. You can run them from an [automation](/docs/features/automations) with a **Connector Action** step. Actions run this way, including `leave_group`, do not ask for approval.

## Troubleshooting

### Bot does not respond to messages

- **LINE messages are not received.** AI Shuffle has no LINE webhook URL, so messages users send to the bot never reach your agent. The connector can send (push) messages only. See the note in the Overview.
- **Auto-reply is answering instead.** If the LINE Official Account has auto-reply enabled, it responds on its own. Disable auto-reply in the LINE Official Account Manager (Step 6).

### Connector fails to connect or send

- The `channel_access_token` in the connector configuration does not match (or has expired against) the token in the LINE Developers Console. Re-issue the token and ensure there are no trailing spaces or missing characters. The connector validates the token by calling LINE's bot-info endpoint when it connects.
- Outbound HTTPS to `api.line.me` is blocked. Ensure the AI Shuffle backend can reach the LINE Messaging API.

### Messages from the bot appear as plain text (no formatting)

- LINE does not render Markdown, and the connector sends text as plain text messages, so Markdown shows up as raw characters.

### "Exceeds the rate limit" errors

- The LINE Messaging API enforces rate limits on push messages, and this connector sends all outbound messages as push messages. The default free-plan quota is limited (commonly 200–500 free messages per month, depending on region and plan), with higher tiers available on paid plans. Monitor your usage in the LINE Official Account Manager under **Statistics**.

### Users cannot find the bot

- The bot must be added as a friend before users can message it. Share the bot's LINE URL (`https://line.me/R/ti/p/@yourbotid`), QR code, or LINE ID with users. The bot does not appear in LINE's search unless you apply for a verified or premium account.
