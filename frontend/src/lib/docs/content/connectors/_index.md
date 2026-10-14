<!--
title: Connectors Overview
description: Connect AI Shuffle to messaging apps, email, music, smart home and other services
order: 0
-->

# Connectors

Connectors link AI Shuffle to the apps and services you already use.

- **Chat connectors** let you talk to your agent from Telegram, Discord, WhatsApp, Slack, Signal or iMessage. Your message goes to the agent, and its reply comes back in the same chat.
- **Sending-only connectors** (LINE, BlueBubbles) let your agent send messages but not receive them. Microsoft Teams can be connected but cannot send or receive messages yet.
- **Tool connectors** (Gmail, Spotify, Philips Hue, Home Assistant and Personal Finance) give your agent tools, such as reading email or turning on a light.
- **Webhook** sends and receives plain web requests, for anything else.

## Available connectors

| Connector | What it does |
| --- | --- |
| [Telegram](/docs/connectors/telegram) | Chat with your agent through a Telegram bot |
| [Discord](/docs/connectors/discord) | Chat with your agent through a Discord bot |
| [WhatsApp](/docs/connectors/whatsapp) | Chat with your agent on WhatsApp Business |
| [Slack](/docs/connectors/slack) | Chat with your agent through a Slack app |
| [Signal](/docs/connectors/signal) | Chat with your agent on Signal |
| [iMessage](/docs/connectors/imessage) | Chat with your agent through iMessage. Needs AI Shuffle on a Mac (macOS app coming soon) |
| [LINE](/docs/connectors/line) | Send LINE messages |
| [BlueBubbles](/docs/connectors/bluebubbles) | Send iMessages through a BlueBubbles server |
| [Microsoft Teams](/docs/connectors/msteams) | Not available yet; not offered for installation |
| [Gmail](/docs/connectors/gmail) | Search, read, send and organize email |
| [Spotify](/docs/connectors/spotify) | Control playback and playlists |
| [Philips Hue](/docs/connectors/hue) | Control lights and scenes |
| [Home Assistant](/docs/connectors/homeassistant) | Control your smart home |
| Personal Finance | Read-only bank balances and transactions |
| [Webhook](/docs/connectors/webhook) | Send and receive web requests |

## Common setup steps

1. **Get your credentials** from the other service, such as a bot token or an API key. Each connector's page explains how.
2. **Install the connector app:** open **Settings → Plugins**, choose the **Apps** filter, open the app and choose **Install** if asked. This needs internet access.
3. **Add the connection:** choose **Connect account**, fill in the form and choose **Create Connector**.
4. **Add at least one allowed sender** (chat and sending connectors). Only the accounts or chats on this list can use your agent, and the connector will not start without one.
5. **Pick an agent.** The connector answers with that agent. Under **Advanced settings** you can reuse an existing API user instead, with its own agents.
6. **Test the connection** to check your credentials.
7. **Turn it on** with the **Activate** switch.

Connectors that answer while you are away should use an API key rather than a subscription. See [Modes and costs](/docs/getting-started/modes-and-costs).

## How AI actions work

Most connectors give your agent actions, such as sending a message, pinning a message or turning on a light.

- **Chat connectors:** only chats that started on that connector get its actions, and they stay within that chat.
- **Gmail, Spotify, Philips Hue, Home Assistant and Personal Finance:** every chat can use them while the connector is on.
- **Action categories:** each connector's settings let you choose which kinds of actions your agent may use. Leave it empty to allow all.
- **Destructive actions,** such as deleting a message or banning a member, ask someone in the chat to approve first. With no answer within 2 minutes, the action is denied. You can turn this check off in the connector's settings.
- **Automations** can run connector actions as a step. Those do not ask for approval.

## Conversations

Each conversation on a chat connector, such as one Telegram chat or one Discord channel, becomes its own chat in AI Shuffle. The agent remembers what was said earlier, and you can read every conversation in the app.

## API management

You can also manage connectors from your own code with an owner sign-in. See the [Connectors API](/docs/api-reference/connectors). Each connector's page lists its settings.
