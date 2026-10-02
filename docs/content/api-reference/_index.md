<!--
title: API Overview
description: Introduction to the AI Shuffle REST API
order: 0
-->

# API Reference

AI Shuffle provides a REST API for its browser and external integrations. Owner access (local or signed in), paired devices, and scoped integration API keys have separate authorization roles.

## Base URL

```
http://localhost:8000/api/v1
```

## Authentication

### Local access

The local listener grants owner access to localhost requests without a login. Other devices on your LAN sign in with the owner password, unless the owner turns on **Trusted LAN**. Phones reach a worker through **Devices → Remote access** and their own pairing. See [Authentication](/docs/api-reference/authentication) for the details.

```bash
# Direct request from the same machine (loopback)
curl http://localhost:8000/api/v1/sessions
```

### API Key (Bearer Token)

Used for external integrations, connectors, and programmatic access. Create API users in **Settings → Integrations**. They are application credentials, not human account logins or administrator credentials. Supplying an API key retains its restricted role even on the local network.

```bash
curl http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer aih_your_api_key_here"
```

API keys start with `aih_` and are shown only once at creation time.

### API User Scoping

API users can be restricted to:
- **Specific project** — Only access sessions/resources in that project
- **Specific agents** — Only run their assigned agents (`profile_ids`). Naming another agent returns `403`; omitting the agent runs the only assigned one, or returns `400` when several are assigned
- These restrictions are enforced server-side, not just hidden from listings

### Auth levels

Each endpoint lists its **Auth** level:
- **Required** — the owner or any API key. API keys only see their own data where noted.
- **Owner only** — owner access. API keys get `403 Owner access required`.
- **API key required** — an API key (`aih_...`).

Examples for owner-only endpoints send `$OWNER_TOKEN`: an owner session token from `POST /api/v1/owner/login` with `"bearer": true` (see [Owner password](/docs/api-reference/authentication#owner-password)).

### Naming

The product calls agent configurations **agents**. Request and response fields keep the historical names `profile`, `profile_id` and `profile_ids`; they hold an agent id.

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

## WebSocket

Real-time chat uses WebSocket connections:

```
ws://localhost:8000/api/v1/ws/chat
```

The `session_id` is sent in the message body (e.g. `{"type": "query", "prompt": "...", "session_id": "...", "profile": "..."}`), not in the URL path. See the [WebSocket documentation](/docs/api-reference/websocket) for protocol details.

## Endpoint Categories

| Category | Prefix | Description |
|----------|--------|-------------|
| [Authentication](/docs/api-reference/authentication) | `/auth`, `/owner`, `/workspace`, `/filesystem` | Owner password and sign-in, setup, Claude CLI, GitHub, workspace folder |
| [Sessions](/docs/api-reference/sessions) | `/sessions`, `/commands/rewind` | Chat session CRUD, search, export, clear and retry, workspace folders, background workers, rewind, browser panel, moving between devices |
| [Query & Execution](/docs/api-reference/query-execution) | `/query`, `/conversation` | One-shot queries, conversations, streaming |
| [WebSocket](/docs/api-reference/websocket) | `/ws` | Real-time chat control, permissions, questions, message queue |
| [Agents](/docs/api-reference/agents) | `/agents`, `/generate` | Agent configuration CRUD, nested-agent management, drafting agents with AI |
| [Projects](/docs/api-reference/projects) | `/projects` | Project CRUD, files, project agents, memory files, project plugins, links |
| [Connectors](/docs/api-reference/connectors) | `/connectors` | Platform connector management, Gmail sign-in, Plaid |
| [Automations](/docs/api-reference/automations) | `/automations` | Scheduled/triggered workflows |
| [Hooks](/docs/api-reference/hooks) | `/hooks` | Event hook management |
| [MCP](/docs/api-reference/mcp) | `/mcp` | MCP server configuration |
| [Plugins](/docs/api-reference/plugins) | `/plugins` | Marketplaces, installing, updating and enabling plugins |
| [API Users](/docs/api-reference/api-users) | `/api-users` | API key management |
| [Model Providers](/docs/api-reference/model-providers) | `/model-providers`, `/models` | Other model providers, sign-in, usage tracking, model registry |
| [Devices & Network](/docs/api-reference/devices) | `/network`, `/cluster`, `/pairing`, `/mobile/hub` | Worker network, installs, pairing codes, paired devices, Shuffle account |
| [Notes](/docs/api-reference/notes) | `/notes` | Notes and attachments |
| [Voice](/docs/api-reference/voice) | `/voice`, `/settings/voice` | Voice settings and voice calls |
| [Miscellaneous](/docs/api-reference/miscellaneous) | Various | Tags, analytics, git (including Ship It and worktrees), GitHub pull requests and actions, settings, loop guard, computer use, license, files |

Internal routes are not part of this reference: anything under `/api/v1/internal`, worker-to-worker traffic (`/api/v1/network/peer/*` and the network join handshake), the pairing and account-link handshakes (`/api/v1/pairing/exchange`, `/api/v1/mobile/hub/endpoint`, `/ingress`, `/link`, `/enroll`), and the desktop app's local listener.
