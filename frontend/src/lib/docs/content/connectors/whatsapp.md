<!--
title: WhatsApp
description: Connect AI Shuffle to WhatsApp using the WhatsApp Business API through Meta's platform
order: 3
-->

# WhatsApp Business API Connector

## Overview

The WhatsApp connector integrates your AI Shuffle agent with WhatsApp through the official WhatsApp Business API, powered by Meta's Cloud API. This allows your agent to send and receive messages on WhatsApp, the world's most widely used messaging platform with over two billion users.

Unlike the standard WhatsApp app, the Business API is designed for programmatic access. Messages arrive via webhooks that Meta sends to your server, and responses are sent through the Cloud API. The connector handles the full message lifecycle, including read receipts and interactive buttons for permission prompts. You list the phone numbers the bot is allowed to respond to.

## Prerequisites

- A [Meta Business Account](https://business.facebook.com/) (free to create)
- A [Meta Developer Account](https://developers.facebook.com/)
- A phone number for the WhatsApp Business account (can use the test number provided by Meta during development)
- AI Shuffle backend running with a **publicly reachable HTTPS URL** (required for webhook delivery)
- A verified domain or tunneling service (e.g., ngrok) for development

## Setup Guide

### Step 1: Create a Meta App

1. Go to [Meta for Developers](https://developers.facebook.com/) and log in.
2. Click **My Apps** in the top navigation, then **Create App**.
3. Select the **Business** app type and click **Next**.
4. Enter an app name (e.g., "AI Shuffle WhatsApp") and select your Meta Business Account.
5. Click **Create App**.

### Step 2: Add WhatsApp to Your App

1. In your app dashboard, find **WhatsApp** in the product list and click **Set Up**.
2. Follow the prompts to connect your Meta Business Account if not already linked.
3. Meta provides a **test phone number** and a few test recipient numbers you can use during development.

### Step 3: Get Your Credentials

From the **WhatsApp > API Setup** page in your app dashboard:

1. **Phone Number ID**: Displayed under the selected phone number. It is a numeric string like `106543218765432`.
2. **Temporary Access Token**: Click **Generate** to create a temporary token (expires in 24 hours). For production, you need a permanent System User token (see Step 5).

Note the token format:
```
EAABsbCS1iHgBO0abc123exampleTokenThatIsVeryLongAndContainsManyCharacters...
```

### Step 4: Configure the Webhook

The webhook is how Meta delivers incoming messages to your AI Shuffle instance.

1. In your app dashboard, go to **WhatsApp > Configuration**.
2. Under **Webhook**, click **Edit**.
3. Set the **Callback URL** to your AI Shuffle webhook endpoint:
   ```
   https://your-domain.com/api/v1/connectors/whatsapp/webhook
   ```
4. Set the **Verify Token** to a secret string of your choosing. You will enter this same string in the AI Shuffle connector configuration. For example:
   ```
   my_secret_verify_token_2024
   ```
5. Click **Verify and Save**. Meta will send a verification request to your callback URL. AI Shuffle must be running and reachable for this to succeed.
6. After verification, subscribe to the **messages** webhook field by clicking **Subscribe** next to it.

### Step 5: Generate a Permanent Access Token (Production)

Temporary tokens expire after 24 hours. For production use:

1. Go to [Meta Business Settings](https://business.facebook.com/settings/).
2. Navigate to **Users > System Users**.
3. Click **Add** to create a new System User. Set the role to **Admin**.
4. Click **Add Assets**, select your WhatsApp app, and grant full control.
5. Click **Generate New Token**, select your app, and include the `whatsapp_business_messaging` and `whatsapp_business_management` permissions.
6. Copy the token. This token does not expire.

### Step 6: Configure the Connector in AI Shuffle

1. Open the AI Shuffle web UI and go to **Build > Plugins**, choose the **Apps** filter and open **WhatsApp**.
2. Click **Install WhatsApp** if prompted, then click **Connect account**.
3. Fill in the configuration fields:
   - **Phone Number ID**: The phone number ID from Step 3
   - **Access Token**: The permanent System User token (or temporary token for testing)
   - **Verify Token**: The verify token string you set in Step 4
   - **App Secret** (required): Your Meta app secret (App settings > Basic). Every webhook must be signed with it.
   - **Allowed Phone Numbers** (required to enable): Phone numbers as digits with country code and no `+` or spaces (e.g., `14155551234`), the same form WhatsApp uses in its webhooks
4. Click **Create Connector**, then turn on the connection's **Activate** switch.

The connection needs an API user with an agent, or every message is refused. See [Common Setup Steps](/docs/connectors#common-setup-steps).

## Configuration Reference

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `phone_number_id` | string | Yes | The WhatsApp Business phone number ID from the Meta dashboard. This identifies which phone number the bot sends messages from. |
| `access_token` | string | Yes | A valid access token for the WhatsApp Business API. Use a System User token for production (does not expire) or a temporary token for development (expires in 24 hours). |
| `verify_token` | string | Yes | A secret string used to verify webhook requests from Meta. Must match the value configured in the Meta app dashboard webhook settings. |
| `app_secret` | string | Yes | The Meta app secret (App settings, Basic). Every incoming webhook must carry a valid `X-Hub-Signature-256` header made with it; payloads with a missing or mismatched signature are rejected, and a connector without an app secret accepts no webhooks. |
| `allowed_phone_numbers` | array of strings | Yes (to enable) | Phone numbers the bot is allowed to communicate with, as digits with country code and no `+` or spaces (e.g., `14155551234`). At least one entry is required before the connector can be enabled. |
| `enabled_action_categories` | array of strings | No | Restrict the AI's actions to these categories. Empty = all. Valid values: `messages`, `reactions`, `templates`, `media`, `account`. |
| `auto_approve_destructive` | string | No | Has no effect: WhatsApp has no destructive actions. |

## Capabilities

The WhatsApp connector supports the following capabilities:

| Capability | Supported | Notes |
|------------|-----------|-------|
| Typing indicator | No | The connector does not send a "typing..." indicator |
| Read receipts | Yes | Sends read receipts (blue check marks) when messages are received and processed |
| Reactions | Yes | Available to the AI via the `send_reaction`/`remove_reaction` actions (see below). Not sent automatically. |
| Rich text | No | Text is sent as-is. WhatsApp renders its own syntax: bold (`*text*`), italic (`_text_`), strikethrough (`~text~`) and monospace (`` ```text``` ``) |
| File upload | No | Your agent can send images, documents and locations through actions. Audio, video and stickers are not supported. |
| Buttons | Yes | Supported by the platform. Tool approvals are answered by the owner in the app; the chat only sees a waiting notice |
| Threads | No | WhatsApp does not have a thread model |

**Message Length Limit:** 4,096 characters per message. Messages exceeding this limit are automatically split.

## AI Actions

See [How AI actions work](/docs/connectors#how-ai-actions-work).

Your agent can use 9 actions: send message, mark read, send/remove reaction, send template, send image/document/location, and get phone number info.

- Message actions only work in the conversation the session started from and the connector's `allowed_phone_numbers`.
- **There are no destructive actions.** Every current action runs immediately without an approval prompt.
- Sending a template message still requires an approved Meta message template, and outbound free-form messages are still subject to WhatsApp's 24-hour conversation window (see Troubleshooting).

## Troubleshooting

### Webhook verification fails

- **AI Shuffle not reachable.** The callback URL must be publicly accessible over HTTPS. During development, use a tunneling service:
  ```bash
  ngrok http 8000
  ```
  Then use the ngrok HTTPS URL as your callback.
- **Verify token mismatch.** The verify token in the Meta dashboard must exactly match the `verify_token` field in the AI Shuffle connector configuration.
- **HTTPS required.** Meta only delivers webhooks to HTTPS endpoints. Self-signed certificates are not accepted.

### Bot receives messages but does not respond

- **Access token expired.** If using a temporary token, it expires after 24 hours. Generate a new one or set up a permanent System User token (Step 5).
- **Permissions missing.** The access token must have the `whatsapp_business_messaging` permission.
- **24-hour messaging window.** WhatsApp Business API enforces a 24-hour conversation window. The bot can only send messages within 24 hours of the user's last message. After 24 hours, you must use an approved message template to re-initiate the conversation.

### "Phone number not registered" error

- The phone number associated with `phone_number_id` must be registered and verified in the WhatsApp Business Platform. Check the **WhatsApp > Phone Numbers** section in your Meta app dashboard.

### Messages are delivered but formatting looks wrong

- WhatsApp uses its own Markdown-like formatting syntax. Standard Markdown (e.g., `**bold**`) does not work. The connector sends the agent's text as-is. Ask the agent (for example in its instructions) to use WhatsApp formatting: `*bold*`, `_italic_`, `~strike~`.

### Rate limiting or "Too many requests" errors

- The WhatsApp Business API has rate limits based on your phone number quality rating and messaging tier. New numbers start in Tier 1 (1,000 unique contacts per 24 hours). As your quality rating improves, Meta automatically upgrades your tier. Monitor your quality rating in the Meta Business dashboard under **WhatsApp > Phone Numbers > Quality Rating**.

### Test numbers not receiving messages

- Meta's test phone numbers can only send to numbers registered as test recipients. Add recipient numbers in **WhatsApp > API Setup > To** field in the Meta dashboard.
