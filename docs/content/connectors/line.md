<!--
title: LINE
description: Send LINE messages from AI Shuffle
order: 5
-->

# LINE

Send LINE messages, stickers and images from AI Shuffle through a LINE Official Account.

**Sending only.** Messages people send to the bot do not reach your agent, so you cannot chat with your agent on LINE. Send LINE messages from an [automation](/docs/features/automations) with a **Connector Action** step.

## You need

- A LINE account.

## Set up

### 1. Create a Messaging API channel

1. Sign in to the [LINE Developers Console](https://developers.line.biz/console/) with your LINE account.
2. Under **Providers**, choose **Create** and name the provider.
3. In the provider, choose **Create a Messaging API channel**, fill in the name, description, category and email, and choose **Create**.

### 2. Copy your details

- **Channel Secret:** on the **Basic settings** tab. Choose **Issue** if it is empty.
- **Your user ID:** also on **Basic settings**.
- **Channel Access Token:** on the **Messaging API** tab, under **Channel access token**, choose **Issue**.

Leave **Webhook settings** empty.

### 3. Turn off auto-replies

LINE Official Accounts send greeting and auto-reply messages. To turn them off, open the **LINE Official Account Manager** from the **Messaging API** tab, go to **Settings → Response settings**, and disable **Auto-response** and **Greeting message**.

### 4. Connect it in AI Shuffle

1. Open **Settings → Plugins**, choose **Apps** and open **LINE**. Choose **Install LINE** if asked.
2. Choose **Connect account** and paste the **Channel Access Token** and **Channel Secret**.
3. Add at least one **Allowed User ID**. Group IDs are optional.
4. Choose **Create Connector**, then turn on **Activate**.

### 5. Add the bot as a friend

LINE only delivers messages to people who added the bot. Scan the QR code on the **Messaging API** tab with the LINE app and add the bot.

## Settings

| Setting | Required | What it does |
| --- | --- | --- |
| `channel_access_token` | Yes | Your channel access token |
| `channel_secret` | Yes | Your channel secret |
| `allowed_user_ids` | Yes | The LINE users it can message |
| `allowed_group_ids` | No | The LINE groups it can message |

## Actions

LINE actions send text, stickers and images, look up a user's profile or a group, leave a group and check your message quota. Run them from an automation's **Connector Action** step. They do not ask for approval there.

Text is sent as plain text. Messages longer than 5,000 characters are split.

## Troubleshooting

**The bot does not answer.** LINE is sending only. If an automatic reply appears, turn off auto-replies (step 3).

**Sending fails.** Issue a new channel access token and paste it again, with no extra spaces. The worker needs internet access to `api.line.me`.

**"Exceeds the rate limit".** Free LINE plans include a small number of messages per month. Check your usage under **Statistics** in the LINE Official Account Manager.

**People cannot find the bot.** Share its QR code or link, `https://line.me/R/ti/p/@yourbotid`. Bots do not show up in LINE search unless verified.
