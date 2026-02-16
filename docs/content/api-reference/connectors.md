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

## Event Logs

### GET /api/v1/connectors/{connector_id}/events

Get the event log for a connector.

```bash
curl "http://localhost:8000/api/v1/connectors/conn_abc123/events?limit=50" \
  -H "Authorization: Bearer aih_your_key"
```

### DELETE /api/v1/connectors/{connector_id}/events

Clear the event log.

## Connector Statuses

| Status | Description |
|--------|-------------|
| `connected` | Active and receiving messages |
| `disconnected` | Stopped, not receiving |
| `connecting` | In the process of connecting |
| `error` | Connection failed |
