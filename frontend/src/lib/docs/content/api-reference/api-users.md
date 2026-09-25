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
    "id": "3f9a1c2e",
    "name": "CI/CD Pipeline",
    "project_id": "proj_123",
    "profile_ids": ["your-agent-id"],
    "is_active": true,
    "last_used_at": "2025-01-15T10:30:00Z",
    "created_at": "2025-01-01T00:00:00Z"
  }
]
```

Each user also carries `description`, `model`, `effort` and `updated_at`. The key itself is never returned.

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
  "id": "3f9a1c2e",
  "name": "CI/CD Pipeline",
  "api_key": "aih_7x9k2m4n5p8q1r3s6t0u...",
  "project_id": "proj_123",
  "profile_ids": ["your-agent-id", "code-reviewer"]
}
```

Returns `201` with the new user's fields (as in the list) plus `api_key`.

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

Get API user details (key is not included). This and the routes below return `404` for an unknown id.

**Auth:** Owner only

### PATCH /api/v1/api-users/{user_id}

Update an API user. Takes the same fields as create, plus `is_active` (set `false` to disable the key without deleting it; the key's open WebSockets then close with `4001`).

**Auth:** Owner only

```bash
curl -X PATCH http://localhost:8000/api/v1/api-users/3f9a1c2e \
  -H "Origin: http://localhost:8000" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Updated Name",
    "profile_ids": ["your-agent-id"]
  }'
```

### DELETE /api/v1/api-users/{user_id}

Delete an API user and invalidate its key. The key's open WebSockets close with `4001`.

**Auth:** Owner only

## Key Rotation

### POST /api/v1/api-users/{user_id}/rotate-key

Generate a new API key for the user, invalidating the old one. The old key's open WebSockets close with `4001`; reconnect with the new one.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/api-users/3f9a1c2e/rotate-key \
  -H "Origin: http://localhost:8000"
```

**Response:**
```json
{
  "id": "3f9a1c2e",
  "api_key": "aih_new_key_here..."
}
```

## API Key Scoping

### Project Restriction

When `project_id` is set, the API user can only:
- Create sessions in that project (a session started without a project, over REST or the chat WebSocket, is filed under it)
- Access files in that project
- Query using that project's context

### Agent Restriction

When `profile_ids` is set, the server limits every agent run started with the key (the `/api/v1/query` and `/api/v1/conversation` endpoints, a chat WebSocket `query` or `queue_message`, and `POST /api/v1/voice/sessions`) to those agents:
- A `profile` outside the list is rejected with `403`
- An omitted `profile` runs the key's agent when exactly one is assigned; with several assigned, the request is rejected with `400` naming the allowed ids
- `GET /api/v1/agents` lists only the assigned agents, and `GET /api/v1/agents/{id}` returns `403` for any other

### Unrestricted

When neither is set, the API user has access to all projects and agents (but still acts only on its own sessions and cannot perform owner-only operations).

### Own Sessions

Every API key acts only on the sessions it started. It cannot read, continue, stop or answer prompts in the owner's sessions or another key's, even with the session id:

- Session lists and search return only the key's own sessions; the `admin_only`, `api_users_only` and `api_user_id` filters are ignored.
- REST session routes (read, update, delete, resume and the rest) and the conversation endpoints return `403`. An id that does not exist gets `404` from the session routes.
- The chat WebSocket answers with an `error` frame, and `load_session` answers `session_not_found`, the same reply as for an unknown id, so the socket does not reveal whether the session exists (see [WebSocket](/docs/api-reference/websocket)).
- `/ws/sessions/{session_id}` closes with `4003` (`4004` for an unknown id).
- Voice call routes return `403` (see [Voice](/docs/api-reference/voice#call-control)).

Worker sessions an agent starts (Task workers and the workers a voice call hands work to) belong to whoever owns the session that started them, and run as that caller. A worker started from a key's session runs with the key's tools and credentials, like a session the key started itself, and a worker on an agent outside the key's assigned agents is refused. A permission answer from an API key never creates an agent-wide rule; `remember: "profile"` is kept to that session.

### Other data a key cannot reach

Some data belongs to the owner's account, to the whole install or to another key. A key's requests and the agents in its sessions are held to their own part of it:

- **Agent tools.** In a key's session, `GetAppLogs`, `GetAppErrors`, `CronList`, `CronCreate`, `CronDelete` and `RemoteTrigger` refuse to run, and `GetUsage` returns no readings with the reason. `SessionSearch`, `SessionView` and the list of recent sessions the agent is given cover only the key's own sessions; another session reads as unknown.
- **Usage.** `GET /api/v1/usage/limits` returns an empty reading (`"available": false`, `"providers": []`) and never starts a provider refresh. Usage is the owner's subscription and provider billing (see [Usage Limits](/docs/api-reference/miscellaneous#usage-limits)).
- **Notes.** Each key has its own notes and sees only those, including through `@Notes/...` references in a prompt. The owner sees every note (see [Notes](/docs/api-reference/notes)).
- **MCP server settings.** The server lists show environment and header values, URL credentials and secret-looking arguments as `********` (see [MCP Servers](/docs/api-reference/mcp#get-apiv1mcpservers)).
- **Session import.** An imported session stays inside the key's agents and project (see [Import](/docs/api-reference/sessions#post-apiv1sessionsimport)).
- **Agent teams.** The `/api/v1/teams` routes, which list and stop an agent's teammates, reach only the teammates of the key's own sessions; another session answers 403.

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
