<!--
title: Connectors
description: Connector CRUD, lifecycle management, and webhook endpoints
order: 7
-->

# Connectors API

Endpoints for managing platform connectors — creating, configuring, testing, and controlling connections to external messaging platforms and services.

## Discovery

### GET /api/v1/connectors/types

List all available connector types with their configuration schemas.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/connectors/types \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "type": "telegram",
    "name": "Telegram",
    "description": "Telegram Bot connector",
    "config_schema": {
      "bot_token": { "type": "string", "required": true, "description": "Bot token from @BotFather" },
      "allowed_chat_ids": { "type": "array", "required": false, "description": "Restrict to specific chat IDs" }
    },
    "capabilities": ["typing_indicator", "reactions", "rich_text", "file_upload", "buttons"]
  }
]
```

### GET /api/v1/connectors/events

List valid connector event types.

```bash
curl http://localhost:8000/api/v1/connectors/events \
  -H "Authorization: Bearer aih_your_key"
```

## Connector CRUD

### GET /api/v1/connectors

List all connectors.

**Auth:** Admin only

```bash
curl http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "id": "conn_abc123",
    "name": "My Telegram Bot",
    "connector_type": "telegram",
    "enabled": true,
    "status": "connected",
    "config": {
      "bot_token": "****MASKED",
      "allowed_chat_ids": []
    },
    "created_at": "2025-01-15T10:00:00Z"
  }
]
```

Note: Sensitive config fields are masked in responses (showing only last 4 characters).

### POST /api/v1/connectors

Create a new connector. Automatically creates an associated API user.

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/connectors \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Telegram Bot",
    "connector_type": "telegram",
    "config": {
      "bot_token": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    },
    "profile_id": "default",
    "project_id": "my-project"
  }'
```

### GET /api/v1/connectors/{connector_id}

Get connector details.

### PATCH /api/v1/connectors/{connector_id}

Update connector configuration.

**Auth:** Admin only

```bash
curl -X PATCH http://localhost:8000/api/v1/connectors/conn_abc123 \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Renamed Bot",
    "config": {
      "allowed_chat_ids": [12345678]
    }
  }'
```

### DELETE /api/v1/connectors/{connector_id}

Delete a connector and its associated API user.

**Auth:** Admin only

## Lifecycle Management

### POST /api/v1/connectors/{connector_id}/test

Test the connector's connection without activating it.

```bash
curl -X POST http://localhost:8000/api/v1/connectors/conn_abc123/test \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{
  "success": true,
  "message": "Connection successful"
}
```

### POST /api/v1/connectors/{connector_id}/activate

Enable and start the connector. Begins receiving messages.

```bash
curl -X POST http://localhost:8000/api/v1/connectors/conn_abc123/activate \
  -H "Authorization: Bearer aih_your_key"
```

### POST /api/v1/connectors/{connector_id}/deactivate

Stop and disable the connector.

### POST /api/v1/connectors/{connector_id}/restart

Restart the connector (deactivate then activate).

## Status & Inspection

### GET /api/v1/connectors/{connector_id}/status

Get the runtime status of a connector, including whether its instance is currently running.

**Auth:** Admin only

```bash
curl http://localhost:8000/api/v1/connectors/conn_abc123/status \
  -H "Authorization: Bearer aih_your_key"
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

**Auth:** Admin only

```bash
curl http://localhost:8000/api/v1/connectors/conn_abc123/logs \
  -H "Authorization: Bearer aih_your_key"
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

**Auth:** Admin only

### GET /api/v1/connectors/{connector_id}/actions

List the actions exposed by the connector's type.

**Auth:** Admin only

### GET /api/v1/connectors/{connector_id}/tools

List the tools exposed by the connector's type.

**Auth:** Admin only

### POST /api/v1/connectors/{connector_id}/actions/{action_id}/execute

Execute a connector action directly against a running connector instance. The JSON request body is passed through as the action parameters. Returns `409` if the connector is not running and `400` for an unknown action.

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/connectors/conn_abc123/actions/send_message/execute \
  -H "Authorization: Bearer aih_your_key" \
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

WhatsApp (Meta) webhook verification endpoint. Meta calls this with the
`hub.mode`, `hub.verify_token`, and `hub.challenge` query parameters; the
challenge is echoed back when the verify token matches a configured WhatsApp
connector.

**Auth:** None (public — verified via the connector's verify token)

### POST /api/v1/connectors/whatsapp/webhook

Receive incoming WhatsApp messages. Requests are matched to a running WhatsApp
connector by `phone_number_id` and verified via the Meta `X-Hub-Signature-256`
header when an app secret is configured.

**Auth:** None (public — verified via Meta signature)

### POST /api/v1/connectors/hooks/{webhook_slug}

Generic inbound webhook endpoint. External services POST JSON here to trigger AI
processing; the `webhook_slug` must match a webhook connector's configured
`inbound_path`. Authenticated via a Bearer token (`Authorization` header or
`X-Webhook-Token`) or an HMAC signature (`X-Webhook-Signature` header).

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
