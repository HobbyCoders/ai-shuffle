<!--
title: Connectors Overview
description: Overview of the connector system for integrating external platforms
order: 0
-->

# Connectors

Connectors link AI Shuffle to messaging platforms, smart home systems and other services. Chat connectors (Telegram, Discord, WhatsApp, Slack, Signal and iMessage) carry messages to your agent and send its replies back. LINE, BlueBubbles and Microsoft Teams do not receive messages. Gmail, Spotify, Philips Hue, Home Assistant and Personal Finance (Plaid) give your agent tools rather than chats.

## How Connectors Work

```
External Platform  ──▶  Connector  ──▶  AI Shuffle  ──▶  Agent
     (Telegram)          (Bridge)       (Session)       (Response)
                                                           │
External Platform  ◀──  Connector  ◀──  AI Shuffle  ◀─────┘
```

For a chat connector:

1. A message arrives from the external platform (e.g., a Telegram message)
2. The connector converts it into a standard message
3. AI Shuffle creates or continues a session for that conversation
4. Your agent processes the message and writes a response
5. The response is sent back through the connector to the platform

## Available Connectors

| Connector | Type | Key Features |
|-----------|------|-------------|
| **Telegram** | Messaging | Bot API, group chats, inline keyboards |
| **Discord** | Messaging | Bot with prefix commands, multiple channels |
| **WhatsApp** | Messaging | Business API, phone number verification |
| **Slack** | Messaging | Bot with app tokens, channel management |
| **LINE** | Messaging | Channel API, sending only |
| **Signal** | Messaging | Signal CLI integration, end-to-end encrypted |
| **MS Teams** | Messaging | Azure AD app registration; cannot send or receive messages |
| **iMessage** | Messaging | Via the `imsg` CLI on the macOS host |
| **BlueBubbles** | Messaging | macOS iMessage bridge, sending only |
| **Home Assistant** | Smart Home | Entity control, automations |
| **Philips Hue** | Smart Home | Light control, scenes |
| **Spotify** | Music | Playback control, playlists |
| **Gmail** | Email | Send, search, read and organize email |
| **Personal Finance (Plaid)** | Finance | Read-only bank balances and transactions |
| **Webhook** | Generic | Inbound/outbound HTTP webhooks |

## Connector Capabilities

Each connector declares what it supports:

| Capability | Description |
|-----------|-------------|
| `typing_indicator` | Show "typing..." in the platform |
| `reactions` | Add emoji reactions to messages |
| `read_receipts` | Mark messages as read |
| `threads` | Support threaded replies |
| `buttons` | Interactive buttons/keyboards |
| `rich_text` | Markdown or formatted text |
| `file_upload` | Send files/images |

## Common Setup Steps

1. **Create credentials** on the external platform (bot token, API key, etc.)
2. **Install the app's runtime**: open **Build > Plugins**, choose the **Apps** filter, open the app and click **Install <app name>** if prompted. Installing needs internet access to PyPI. You can only add a connection after the runtime is installed and enabled.
3. **Add the connection**: click **Connect account**, fill in the form and click **Create Connector**.
4. **Add at least one allowed sender** (chat connectors only). Telegram, Discord, WhatsApp, Slack, LINE, Signal, iMessage, Microsoft Teams and BlueBubbles will not start until their allowlist has an entry. Only listed senders or chats can use the bot.
5. **Assign an agent**: each connection runs as an API user. Chat connectors and inbound webhooks ask for an **Agent** when you create them, and the new API user gets that agent. To reuse an existing API user instead, pick it under **Advanced settings**; it keeps its own agents, which you manage in **Settings > API**. Without an agent, every incoming message is refused.
6. **Test the connection** with the **Test connection** button to verify credentials work
7. **Activate** the connection with its **Activate** switch

## How AI actions work

Most connectors give your agent a set of actions, such as sending a message, pinning a message or turning on a light. Your agent gets two tools per connector: one lists the available actions and the inputs each one takes, the other runs one action. AI Shuffle checks the inputs before it runs the action.

- **Chat connectors**: only sessions that started from a chat on that connector get its tools. Message actions stay within that chat and the connector's allowlist.
- **Gmail, Spotify, Philips Hue, Home Assistant and Plaid**: every agent session gets the tools while the connector is active.
- **`enabled_action_categories`** limits which categories of actions your agent can see and use. Leave it empty to allow all.
- **Destructive actions** (for example deleting a message or banning a member) ask a person in the chat to approve first. If nobody approves within 2 minutes, the action is denied. Set `auto_approve_destructive` to `true` to skip the prompt.
- **Automations** can also run connector actions, with a **Connector Action** step. Actions run from an automation do not ask for approval.

## Connector Sessions

Each unique conversation on a chat connector (e.g., a Telegram chat, a Discord channel) gets its own AI Shuffle session. This means:
- Conversation history is preserved across messages
- The AI remembers context from earlier in the chat
- Sessions can be viewed and managed in the AI Shuffle UI

## Lifecycle Notifications Across Installs

Each connector runs on its assigned install. When a session runs on another install sharing the same database, its lifecycle notifications are forwarded to the connector owner. Only chats linked to that session receive the notification. Webhooks and automation triggers are dispatched once by the originating install.

Delivery is best effort: an offline owner, disconnected database listener, full notification queue, or process restart can lose notifications. Failed or dropped deliveries are logged; missed notifications are not replayed. Repeated messages are suppressed by event ID within a bounded, 15-minute cache on the receiving install.

## API Management

Connectors can also be managed through the REST API. These calls need owner access. API-user keys (`aih_...`) are rejected with 403. From the machine running AI Shuffle, sign in with the owner password to get an owner token, then send it as a Bearer token (see [Owner password](/docs/api-reference/authentication#owner-password)):

```bash
# Get an owner token (local listener only)
curl -X POST http://localhost:8000/api/v1/owner/login \
  -H "Content-Type: application/json" \
  -d '{"password": "your-owner-password", "bearer": true}'
# The response's "token" field is the owner token
OWNER_TOKEN="<token>"

# List connector types
curl http://localhost:8000/api/v1/connectors/types \
  -H "Authorization: Bearer $OWNER_TOKEN"

# Create a connector
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Telegram Bot",
    "type": "telegram",
    "config": { "bot_token": "123:ABC...", "allowed_chat_ids": ["123456789"] },
    "profile_ids": ["<agent-id>"]
  }'

# Activate it
curl -X POST http://localhost:8000/api/v1/connectors/{id}/activate \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

`profile_ids` gives the new API user its agents. You can pass `api_user_id` instead to reuse an existing API user. The response's `is_active` field shows whether a connector is enabled. Each connector page lists the `config` fields for that connector.

See the [API Reference](/docs/api-reference/connectors) for full details.
