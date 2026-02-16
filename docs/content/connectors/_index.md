<!--
title: Connectors Overview
description: Overview of the connector system for integrating external platforms
order: 0
-->

# Connectors

Connectors bridge AI Shuffle with external messaging platforms, smart home systems, and services. When a connector is active, messages from the platform flow into Claude, and responses flow back automatically.

## How Connectors Work

```
External Platform  ──▶  Connector  ──▶  AI Shuffle  ──▶  Claude
     (Telegram)          (Bridge)       (Session)       (Response)
                                                           │
External Platform  ◀──  Connector  ◀──  AI Shuffle  ◀─────┘
```

1. A message arrives from the external platform (e.g., a Telegram message)
2. The connector normalizes it into an `IncomingMessage`
3. AI Shuffle creates or continues a session for that conversation
4. Claude processes the message and generates a response
5. The response is sent back through the connector to the platform

## Available Connectors

| Connector | Type | Key Features |
|-----------|------|-------------|
| **Telegram** | Messaging | Bot API, group chats, inline keyboards |
| **Discord** | Messaging | Bot with slash commands, multiple channels |
| **WhatsApp** | Messaging | Business API, phone number verification |
| **Slack** | Messaging | Bot with app tokens, channel management |
| **Line** | Messaging | Channel API, rich messages |
| **Signal** | Messaging | Signal CLI integration, end-to-end encrypted |
| **MS Teams** | Messaging | Azure AD app, team channels |
| **iMessage** | Messaging | Via BlueBubbles server |
| **BlueBubbles** | Messaging | macOS iMessage bridge |
| **Home Assistant** | Smart Home | Entity control, automations |
| **Philips Hue** | Smart Home | Light control, scenes |
| **Spotify** | Music | Playback control, playlists |
| **Gmail** | Email | Send/receive emails |
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
2. **Add the connector** in AI Shuffle (Settings or API)
3. **Enter configuration** — tokens, channel IDs, etc.
4. **Test the connection** to verify credentials work
5. **Activate** the connector to start receiving messages

## Connector Sessions

Each unique conversation (e.g., a Telegram chat, a Discord channel) gets its own AI Shuffle session. This means:
- Conversation history is preserved across messages
- The AI remembers context from earlier in the chat
- Sessions can be viewed and managed in the AI Shuffle UI

## API Management

All connectors can be managed via the REST API:

```bash
# List connector types
curl http://localhost:8000/api/v1/connectors/types \
  -H "Authorization: Bearer aih_your_key"

# Create a connector
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Telegram Bot",
    "connector_type": "telegram",
    "config": { "bot_token": "123:ABC..." }
  }'

# Activate it
curl -X POST http://localhost:8000/api/v1/connectors/{id}/activate \
  -H "Authorization: Bearer aih_your_key"
```

See the [API Reference](/docs/api-reference/connectors) for full details.
