<!--
title: LINE
description: Connect AI Shuffle to LINE using the LINE Messaging API for reach across Asia-Pacific markets
order: 5
-->

# LINE Messaging API Connector

## Overview

The LINE connector integrates your AI Shuffle agent with LINE, one of the most popular messaging platforms in Japan, Taiwan, Thailand, and Indonesia with over 200 million monthly active users. Through the LINE Messaging API, your agent can send and receive text messages, share images and files, and display rich content to LINE users.

The connector uses LINE's webhook system to receive incoming messages and the Messaging API to send replies. Setting up requires creating a channel in the LINE Developers Console and configuring a webhook URL. The connector supports both reply messages (sent in response to user messages, which are free) and push messages (proactively sent, which may incur costs depending on your plan).

## Prerequisites

- A [LINE account](https://line.me/) (personal account is sufficient to get started)
- Access to the [LINE Developers Console](https://developers.line.biz/console/)
- A LINE Official Account (created automatically when you create a Messaging API channel)
- AI Shuffle backend running with a **publicly reachable HTTPS URL** (required for webhook delivery)
- A verified domain or tunneling service (e.g., ngrok) for development

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

### Step 5: Configure the Webhook

1. On the **Messaging API** tab, find the **Webhook settings** section.
2. Set the **Webhook URL** to your AI Shuffle endpoint:
   ```
   https://your-domain.com/api/connectors/line/webhook
   ```
3. Click **Verify** to test the connection. AI Shuffle must be running and reachable.
4. Toggle **Use webhook** to enabled.

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

## Capabilities

The LINE connector supports the following capabilities:

| Capability | Supported | Notes |
|------------|-----------|-------|
| Typing indicator | Yes | Shows a loading animation in the chat while the agent processes a response (available for one-on-one chats only) |
| Reactions | No | LINE does not support bot reactions on messages via the Messaging API |
| Rich text | Yes | Supports LINE's Flex Messages for rich layouts, as well as basic text formatting. Standard Markdown is converted to plain text. |
| File upload | Yes | Send images (JPEG, PNG), video (MP4), audio (M4A), and files. Images must be hosted at a public URL or uploaded through LINE's content API. |
| Buttons | No | Template messages with buttons are supported through LINE's template message types but require additional configuration |
| Threads | No | LINE does not have a thread model |

**Message Length Limit:** 5,000 characters per text message. Messages exceeding this limit are automatically split. LINE also limits the number of messages per reply to 5 "bubbles" (individual message objects).

## Troubleshooting

### Bot does not respond to messages

- **Webhook not enabled.** In the LINE Developers Console, verify that the webhook URL is set and **Use webhook** is toggled on.
- **Auto-reply is intercepting messages.** If the LINE Official Account has auto-reply enabled, it may be responding instead of the AI agent. Disable auto-reply in the LINE Official Account Manager (Step 6).
- **Webhook URL not reachable.** Click **Verify** on the webhook URL in the console to test connectivity. During development, ensure your tunnel (e.g., ngrok) is running.

### "Invalid signature" errors in logs

- The `channel_secret` in the connector configuration does not match the channel secret in the LINE Developers Console. Copy the secret again and ensure there are no trailing spaces or missing characters.
- Webhook requests may be arriving from a different channel if you have multiple LINE channels configured.

### Bot responds with an error about "reply token expired"

- LINE reply tokens are valid for only 30 seconds after the user sends a message. If the agent takes longer than 30 seconds to respond, the reply token expires. The connector falls back to using a push message instead, but push messages may count toward your monthly quota depending on your LINE Official Account plan.

### Messages from the bot appear as plain text (no formatting)

- LINE does not support Markdown natively. Rich formatting requires LINE's Flex Message format or template messages. The connector converts basic formatting (bold, code blocks) into Flex Messages when possible, but complex Markdown may render as plain text.

### "Exceeds the rate limit" errors

- The LINE Messaging API enforces rate limits on push messages. The default limit is 100,000 push messages per month on the free plan. Reply messages (responses to user-initiated messages) are unlimited. Monitor your usage in the LINE Official Account Manager under **Statistics**.

### Users cannot find the bot

- The bot must be added as a friend before users can message it. Share the bot's LINE URL (`https://line.me/R/ti/p/@yourbotid`), QR code, or LINE ID with users. The bot does not appear in LINE's search unless you apply for a verified or premium account.
