<!--
title: WhatsApp
description: Chat with your agent on WhatsApp Business
order: 3
-->

# WhatsApp

Chat with your agent on WhatsApp through the official WhatsApp Business Platform from Meta.

## You need

- A [Meta Business account](https://business.facebook.com/) and a [Meta developer account](https://developers.facebook.com/). Both are free.
- A phone number for WhatsApp Business. Meta gives you a test number to start with.
- A public HTTPS address for AI Shuffle, so Meta can deliver messages. See [Remote access](/docs/getting-started/remote-access).

## Set up

### 1. Create a Meta app

1. In [Meta for Developers](https://developers.facebook.com/), choose **My Apps → Create App**.
2. Pick the **Business** type, name the app and choose your business account.
3. In the app, find **WhatsApp** and choose **Set Up**.

### 2. Copy your details

On **WhatsApp → API Setup**, copy:

- The **Phone Number ID**, a long number.
- An **access token**. The temporary token there lasts 24 hours, which is fine for a first test.

For a token that does not expire:

1. Open [Meta Business Settings](https://business.facebook.com/settings/) and go to **Users → System Users**.
2. Add a system user with the **Admin** role, and give it full control of your app under **Add Assets**.
3. Choose **Generate New Token** with the `whatsapp_business_messaging` and `whatsapp_business_management` permissions.

Also copy your **App Secret** from **App settings → Basic**.

### 3. Point Meta at AI Shuffle

1. Open **WhatsApp → Configuration** and choose **Edit** under **Webhook**.
2. Set the **Callback URL** to `https://your-domain.com/api/v1/connectors/whatsapp/webhook`.
3. Set the **Verify Token** to a secret word of your choice. You enter the same word in AI Shuffle.
4. Choose **Verify and Save**. AI Shuffle must be running and reachable.
5. Subscribe to the **messages** field.

### 4. Connect it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **WhatsApp**. Choose **Install WhatsApp** if asked.
2. Choose **Connect account** and fill in the Phone Number ID, access token, verify token and app secret.
3. Add the **Allowed Phone Numbers**: digits with country code, no `+` or spaces, such as `14155551234`.
4. Pick the agent, choose **Create Connector**, then turn on **Activate**.

See [Common setup steps](/docs/connectors#common-setup-steps).

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `phone_number_id` | Yes | The number the bot sends from |
| `access_token` | Yes | Your WhatsApp access token |
| `verify_token` | Yes | The secret word you set in Meta |
| `app_secret` | Yes | Your Meta app secret. Messages that are not from Meta are refused |
| `allowed_phone_numbers` | Yes | The numbers the bot answers |
| `enabled_action_categories` | No | Limit the agent to some kinds of actions: `messages`, `reactions`, `templates`, `media`, `account`. Empty allows all |

## What your agent can do

See [How AI actions work](/docs/connectors#how-ai-actions-work). The agent can send messages, react, mark messages read, send approved message templates, send images, documents and locations, and look up the phone number's details.

- None of these actions are destructive, so none ask for approval.
- Replies use WhatsApp formatting: `*bold*`, `_italic_`, `~strike~`. Standard Markdown such as `**bold**` shows as plain text, so ask your agent to use WhatsApp formatting.
- Messages longer than 4,096 characters are split into several messages.

## Troubleshooting

**Meta cannot verify the webhook.**

- AI Shuffle must be reachable over HTTPS with a real certificate. Self-signed certificates do not work.
- The verify token must match exactly in Meta and AI Shuffle.

**The bot gets messages but does not reply.**

- A temporary token stops working after 24 hours. Use a system user token.
- The token needs the `whatsapp_business_messaging` permission.
- WhatsApp only lets a business reply within 24 hours of the person's last message. After that, only an approved template can start the conversation again.

**"Phone number not registered".** Register and verify the number under **WhatsApp → Phone Numbers** in Meta.

**"Too many requests".** New numbers can message 1,000 people a day. Meta raises the limit as your quality rating improves.

**Test numbers do not get messages.** Meta's test number can only message numbers added under **WhatsApp → API Setup → To**.
