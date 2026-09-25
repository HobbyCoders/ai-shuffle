<!--
title: Connectors
description: Connector CRUD, lifecycle management, and webhook endpoints
order: 7
-->

# Connectors API

Endpoints for managing platform connectors — creating, configuring, testing, and controlling connections to external messaging platforms and services.

Every endpoint on this page except the inbound webhooks needs owner access. API-user keys (`aih_...`) get `403`. The examples send an owner token from `POST /api/v1/owner/login` (see [Owner password](/docs/api-reference/authentication#owner-password)).

## Discovery

### GET /api/v1/connectors/types

List all available connector types with their configuration fields.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/connectors/types \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
[
  {
    "type": "telegram",
    "name": "Telegram",
    "description": "Telegram bot via Bot API",
    "config_fields": [
      { "key": "bot_token", "label": "Bot Token", "type": "password", "required": true },
      { "key": "allowed_chat_ids", "label": "Allowed Chat IDs", "type": "list", "required": false }
    ]
  }
]
```

### GET /api/v1/connectors/events

List valid connector event types (for a connector's `events`).

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/connectors/events \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

## Discord Guided Setup

### POST /api/v1/connectors/discord/discover

Log into Discord with a bot token and return the bot's identity plus the guilds (servers) and text channels it can see. Powers the guided Discord setup UI so the owner can pick guilds/channels from a list instead of copying IDs. This performs an HTTP-only login (no Gateway connection) and does not modify anything.

**Auth:** Owner only

Provide **exactly one** of the following in the request body:

- `bot_token` -- a plaintext bot token to validate. A masked value (starting with `****`) is rejected.
- `connector_id` -- the ID of an existing Discord connector; its stored token is decrypted server-side and used. Useful when editing (the token stays masked in the UI).

```bash
curl -X POST http://localhost:8000/api/v1/connectors/discord/discover \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"bot_token": "YOUR_BOT_TOKEN"}'
```

**Response:**
```json
{
  "bot": { "id": "123456789012345678", "name": "AI Shuffle Bot" },
  "application_id": "123456789012345678",
  "guilds": [
    {
      "id": "223456789012345678",
      "name": "My Server",
      "channels": [
        { "id": "323456789012345678", "name": "general" }
      ],
      "error": null
    }
  ]
}
```

All IDs are returned as strings (Discord snowflakes exceed JavaScript's safe integer range). Only text channels are listed. If the bot cannot read a guild's channels, that guild's `error` field describes the problem and its `channels` array is empty; the guild is still returned so it can be selected.

**Errors:**

| Status | Condition |
|--------|-----------|
| 400 | Neither or both of `bot_token`/`connector_id` provided, or a masked token was sent |
| 404 | `connector_id` not found or is not a Discord connector |
| 400 | Discord rejected the token (invalid bot token) |
| 502 | Discord discovery failed (timeout or upstream error) |

The token is never logged or echoed in the response.

## Connector CRUD

### GET /api/v1/connectors

List all connectors.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
[
  {
    "id": "conn_abc123",
    "name": "My Telegram Bot",
    "type": "telegram",
    "is_active": true,
    "status": "connected",
    "status_message": null,
    "config": {
      "bot_token": "****MASKED",
      "allowed_chat_ids": ["12345678"]
    },
    "events": [],
    "api_user_id": "a1b2c3d4",
    "plugin_id": "telegram@shuffle-connectors",
    "failure_count": 0,
    "created_at": "2025-01-15T10:00:00Z",
    "updated_at": "2025-01-15T10:00:00Z"
  }
]
```

Note: Sensitive config fields are masked in responses (showing only last 4 characters).

### POST /api/v1/connectors

Create a new connector. The app's runtime must be installed and enabled first (**Build → Plugins → Apps**). Unless you pass `api_user_id`, an API user is created for the connector; `profile_ids` sets which agents it may run. Incoming messages are refused while that API user has no agent.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Telegram Bot",
    "type": "telegram",
    "config": {
      "bot_token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
      "allowed_chat_ids": ["12345678"]
    },
    "profile_ids": ["your-agent-id"],
    "project_id": "my-project"
  }'
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Display name |
| `type` | string | Yes | Connector type from `GET /api/v1/connectors/types` |
| `config` | object | No | Values for the type's `config_fields` |
| `events` | string[] | No | Event types to deliver (from `GET /api/v1/connectors/events`) |
| `profile_ids` | string[] | No | Agents the auto-created API user may run |
| `project_id` | string | No | Project for the auto-created API user |
| `api_user_id` | string | No | Link an existing API user instead of creating one |

### GET /api/v1/connectors/{connector_id}

Get connector details.

**Auth:** Owner only

### PATCH /api/v1/connectors/{connector_id}

Update a connector. Every field is optional: `name`, `config`, `events`, `is_active`.

**Auth:** Owner only

```bash
curl -X PATCH http://localhost:8000/api/v1/connectors/conn_abc123 \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Renamed Bot",
    "config": {
      "allowed_chat_ids": ["12345678"]
    }
  }'
```

### DELETE /api/v1/connectors/{connector_id}

Delete a connector. An API user that was created for it is deleted too.

**Auth:** Owner only

## Lifecycle Management

### POST /api/v1/connectors/{connector_id}/test

Test the connector's connection without activating it.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/connectors/conn_abc123/test \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
{
  "success": true,
  "message": "Connection successful"
}
```

### POST /api/v1/connectors/{connector_id}/activate

Enable and start the connector. Chat connectors need at least one allowed sender first; otherwise this returns `400` "Add at least one allowed sender before enabling this connector".

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/connectors/conn_abc123/activate \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

### POST /api/v1/connectors/{connector_id}/deactivate

Stop and disable the connector.

**Auth:** Owner only

### POST /api/v1/connectors/{connector_id}/restart

Restart the connector (deactivate then activate).

**Auth:** Owner only

## Status & Inspection

### GET /api/v1/connectors/{connector_id}/status

Get the runtime status of a connector, including whether its instance is currently running.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/connectors/conn_abc123/status \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
{
  "id": "conn_abc123",
  "status": "connected",
  "status_message": null,
  "last_connected_at": "2025-01-15T10:00:00Z",
  "last_event_at": "2025-01-15T10:05:00Z",
  "failure_count": 0,
  "is_running": true
}
```

### GET /api/v1/connectors/{connector_id}/logs

Get the recent event logs for a connector.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/connectors/conn_abc123/logs \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
[
  {
    "id": "evt_abc123",
    "connector_id": "conn_abc123",
    "event_type": "message_received",
    "direction": "inbound",
    "payload": {},
    "status": "ok",
    "error": null,
    "created_at": "2025-01-15T10:05:00Z"
  }
]
```

### GET /api/v1/connectors/{connector_id}/triggers

List the triggers exposed by the connector's type.

**Auth:** Owner only

### GET /api/v1/connectors/{connector_id}/actions

List the actions exposed by the connector's type.

**Auth:** Owner only

### GET /api/v1/connectors/{connector_id}/tools

List the tools exposed by the connector's type.

**Auth:** Owner only

### POST /api/v1/connectors/{connector_id}/actions/{action_id}/execute

Execute a connector action directly against a running connector instance. The JSON request body is passed through as the action parameters. Returns `409` if the connector is not running and `400` for an unknown action.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/connectors/conn_abc123/actions/send_message/execute \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "chat_id": 12345678, "text": "Hello" }'
```

**Response:**
```json
{
  "status": "ok",
  "action_id": "send_message",
  "result": {}
}
```

## Inbound Webhooks

### GET /api/v1/connectors/whatsapp/webhook

WhatsApp (Meta) webhook verification endpoint. Meta calls this with the `hub.mode`, `hub.verify_token`, and `hub.challenge` query parameters; the challenge is echoed back when the verify token matches a configured WhatsApp connector.

**Auth:** None (public — verified via the connector's verify token)

### POST /api/v1/connectors/whatsapp/webhook

Receive incoming WhatsApp messages. Requests are matched to a running WhatsApp connector by `phone_number_id`. Every request must carry a valid Meta `X-Hub-Signature-256` header made with the connector's app secret; a connector without an app secret accepts nothing.

**Auth:** None (public — verified via Meta signature)

### POST /api/v1/connectors/hooks/{webhook_slug}

Generic inbound webhook endpoint. External services POST JSON here to trigger AI processing; the `webhook_slug` must match a webhook connector's configured `inbound_path`. Authenticated via a Bearer token (`Authorization` header or `X-Webhook-Token`) or an HMAC signature (`X-Webhook-Signature` header).

**Auth:** None (public — verified via the connector's token or HMAC signature)

```bash
curl -X POST http://localhost:8000/api/v1/connectors/hooks/my-hook \
  -H "Authorization: Bearer my_webhook_token" \
  -H "Content-Type: application/json" \
  -d '{ "message": "Hello from my service" }'
```

## Connector Statuses

| Status | Description |
|--------|-------------|
| `connected` | Active and receiving messages |
| `disconnected` | Stopped, not receiving |
| `connecting` | In the process of connecting |
| `error` | Connection failed |
