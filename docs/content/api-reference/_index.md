<!--
title: API Overview
description: Introduction to the AI Shuffle REST API
order: 0
-->

# API Reference

AI Shuffle provides a comprehensive REST API for programmatic access to all features. The API supports both admin session-based authentication and API key authentication for external integrations.

## Base URL

```
http://localhost:8000/api/v1
```

## Authentication

### Admin Session (Cookie-based)

Used by the web UI. After login, a session cookie is set automatically.

```bash
# Login
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your_password"}' \
  -c cookies.txt

# Use session cookie
curl http://localhost:8000/api/v1/sessions \
  -b cookies.txt
```

### API Key (Bearer Token)

Used for external integrations and programmatic access. Create API users in Settings.

```bash
curl http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer aih_your_api_key_here"
```

API keys start with `aih_` and are shown only once at creation time.

### API User Scoping

API users can be restricted to:
- **Specific project** — Only access sessions/resources in that project
- **Specific profiles** — Only use certain profiles for queries
- These restrictions are enforced server-side on all endpoints

## Common Response Patterns

### Success Response

```json
{
  "id": "abc123",
  "name": "My Resource",
  "created_at": "2025-01-15T10:30:00Z"
}
```

### List Response

```json
[
  { "id": "abc123", "name": "First" },
  { "id": "def456", "name": "Second" }
]
```

### Paginated Response

```json
{
  "items": [...],
  "total": 42,
  "has_more": true,
  "offset": 0,
  "limit": 20
}
```

### Error Response

```json
{
  "detail": "Description of what went wrong"
}
```

HTTP status codes: `400` (bad request), `401` (unauthorized), `403` (forbidden), `404` (not found), `422` (validation error), `429` (rate limited), `500` (server error).

## Rate Limiting

The API enforces rate limits per-IP and per-user. When rate limited, you'll receive a `429` response with a `Retry-After` header.

## WebSocket

Real-time chat uses WebSocket connections:

```
ws://localhost:8000/api/v1/ws/chat
```

The `session_id` is sent in the message body (e.g. `{"type": "query", "prompt": "...", "session_id": "...", "profile": "..."}`), not in the URL path. See the [WebSocket documentation](/docs/api-reference/websocket) for protocol details.

## Endpoint Categories

| Category | Prefix | Description |
|----------|--------|-------------|
| [Authentication](/docs/api-reference/authentication) | `/auth` | Login, setup, Claude CLI, GitHub |
| [Sessions](/docs/api-reference/sessions) | `/sessions` | Chat session CRUD, search, export |
| [Query & Execution](/docs/api-reference/query-execution) | `/query`, `/conversation` | One-shot queries, conversations, streaming |
| [WebSocket](/docs/api-reference/websocket) | `/ws` | Real-time chat and CLI bridge |
| [Agents](/docs/api-reference/agents) | `/agents` | Agent configuration CRUD and nested-agent management |
| [Projects](/docs/api-reference/projects) | `/projects` | Project CRUD, files, git |
| [Connectors](/docs/api-reference/connectors) | `/connectors` | Platform connector management |
| [Automations](/docs/api-reference/automations) | `/automations` | Scheduled/triggered workflows |
| [Hooks](/docs/api-reference/hooks) | `/hooks` | Event hook management |
| [MCP](/docs/api-reference/mcp) | `/mcp` | MCP server configuration |
| [API Users](/docs/api-reference/api-users) | `/api-users` | API key management |
| [Miscellaneous](/docs/api-reference/miscellaneous) | Various | Tags, search, analytics, git, files |
