<!--
title: API Users
description: API user management and key rotation endpoints
order: 12
-->

# API Users

Endpoints for managing API users — creating and managing API keys for programmatic access to AI Shuffle.

## Overview

API users provide authentication for external integrations, connectors, and automated systems. Each API user has:

- A unique API key (prefix: `aih_`)
- Optional project and agent restrictions

API users are connector/application credentials, not human sign-in accounts. They never get owner access, even when a request arrives over the local network. The examples below use passwordless owner access from the same machine, which needs a same-origin `Origin` header on changes. Scripts on other machines can use an owner token instead (see [Owner password](/docs/api-reference/authentication#owner-password)).

## API User CRUD

### GET /api/v1/api-users

List all API users.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/api-users
```

**Response:**
```json
[
  {
    "id": "usr_abc123",
    "name": "CI/CD Pipeline",
    "project_id": "proj_123",
    "profile_ids": ["your-agent-id"],
    "is_active": true,
    "last_used_at": "2025-01-15T10:30:00Z",
    "created_at": "2025-01-01T00:00:00Z"
  }
]
```

### POST /api/v1/api-users

Create a new API user. Returns the plaintext API key **once** — it cannot be retrieved again.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/api-users \
  -H "Origin: http://localhost:8000" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "CI/CD Pipeline",
    "project_id": "proj_123",
    "profile_ids": ["your-agent-id", "code-reviewer"]
  }'
```

**Response:**
```json
{
  "id": "usr_abc123",
  "name": "CI/CD Pipeline",
  "api_key": "aih_7x9k2m4n5p8q1r3s6t0u...",
  "project_id": "proj_123",
  "profile_ids": ["your-agent-id", "code-reviewer"]
}
```

**Important:** Save the `api_key` immediately. It is only shown once.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Display name for the API user |
| `description` | string | No | Free-text description |
| `project_id` | string | No | Restrict to a specific project |
| `profile_ids` | string[] | No | Restrict to specific agents |
| `model` | string | No | Model every session for this API user runs on |
| `effort` | string | No | Reasoning effort for those sessions: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` or `ultra` |

### GET /api/v1/api-users/{user_id}

Get API user details (key is not included).

**Auth:** Owner only

### PATCH /api/v1/api-users/{user_id}

Update an API user. Takes the same fields as create, plus `is_active` (set `false` to disable the key without deleting it).

**Auth:** Owner only

```bash
curl -X PATCH http://localhost:8000/api/v1/api-users/usr_abc123 \
  -H "Origin: http://localhost:8000" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Updated Name",
    "profile_ids": ["your-agent-id"]
  }'
```

### DELETE /api/v1/api-users/{user_id}

Delete an API user and invalidate its key.

**Auth:** Owner only

## Key Rotation

### POST /api/v1/api-users/{user_id}/rotate-key

Generate a new API key for the user, invalidating the old one.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/api-users/usr_abc123/rotate-key \
  -H "Origin: http://localhost:8000"
```

**Response:**
```json
{
  "id": "usr_abc123",
  "api_key": "aih_new_key_here..."
}
```

## API Key Scoping

### Project Restriction

When `project_id` is set, the API user can only:
- Create sessions in that project
- Access files in that project
- Query using that project's context

### Agent Restriction

When `profile_ids` is set, the server limits every agent run started with the key (the `/api/v1/query` and `/api/v1/conversation` endpoints, a chat WebSocket `query` or `queue_message`, and `POST /api/v1/voice/sessions`) to those agents:
- A `profile` outside the list is rejected with `403`
- An omitted `profile` runs the key's agent when exactly one is assigned; with several assigned, the request is rejected with `400` naming the allowed ids
- `GET /api/v1/agents` lists only the assigned agents, and `GET /api/v1/agents/{id}` returns `403` for any other

### Unrestricted

When neither is set, the API user has access to all projects and agents (but still cannot perform owner-only operations).

## Using API Keys

Include the key in the `Authorization` header:

```bash
curl http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer aih_your_key_here"
```

For WebSocket connections, pass the key as a query parameter. The session is specified in the message body, not the URL:

```
ws://localhost:8000/api/v1/ws/chat?token=aih_your_key_here
```
