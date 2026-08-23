<!--
title: Query & Execution
description: One-shot queries, conversations, streaming, and background execution
order: 3
-->

# Query & Execution API

Endpoints for sending prompts to Claude and receiving responses. Supports one-shot queries, stateful conversations, streaming, and background execution.

## One-Shot Query

### POST /api/v1/query

Execute a single query. Creates a new session, processes the prompt, and returns the complete response.

**Auth:** API key required

```bash
curl -X POST http://localhost:8000/api/v1/query \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain async/await in Python",
    "profile": "claude-code",
    "project": "my-project"
  }'
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | Yes | The message to send to Claude |
| `profile` | string | No | Profile to use. Optional — uses the API user's configured profile when omitted; rejected with 400 if neither supplies one |
| `project` | string | No | Project context |
| `overrides` | object | No | Optional per-query overrides (see below) |

**Overrides object:**

| Field | Type | Description |
|-------|------|-------------|
| `model` | string | Model override (e.g. `sonnet`, or a custom model id) |
| `permission_mode` | string | `default`, `auto`, `acceptEdits`, `plan`, `bypassPermissions` |
| `system_prompt_append` | string | Text appended to the profile's system prompt |
| `max_turns` | integer | Maximum conversation turns |
| `thinking_mode` | string | `default`, `disabled`, `adaptive`, `think`, `think_harder`, `ultra_think` |
| `effort` | string | `low`, `medium`, `high`, `max`, `xhigh` |

**Response:**
```json
{
  "response": "Async/await in Python is a way to write...",
  "session_id": "sess_abc123",
  "metadata": {
    "model": "sonnet",
    "duration_ms": 2500,
    "total_cost_usd": 0.003,
    "tokens_in": 150,
    "tokens_out": 500,
    "num_turns": 1
  }
}
```

### POST /api/v1/query/stream

Same as above but streams the response via Server-Sent Events (SSE).

```bash
curl -X POST http://localhost:8000/api/v1/query/stream \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Write a Python function to sort a list",
    "profile": "claude-code"
  }'
```

**SSE Event Format:**
```
data: {"type": "text", "content": "Here's a "}
data: {"type": "text", "content": "sorting function..."}
data: {"type": "tool_use", "name": "Write", "input": {...}}
data: {"type": "tool_result", "output": "File created"}
data: {"type": "done", "session_id": "sess_abc123"}
```

## Conversation Mode

Stateful conversations that maintain context across multiple exchanges.

### POST /api/v1/conversation

Send a conversation turn. If `session_id` is provided in the body, the existing
session is continued; otherwise a new session is created. Profile and project are
only used when creating a new session.

**Auth:** API key required

```bash
# Start a new conversation (no session_id)
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Help me build a REST API with FastAPI",
    "profile": "claude-code",
    "project": "my-project"
  }'

# Continue an existing conversation (pass session_id in the body)
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Now add authentication to it",
    "session_id": "sess_abc123"
  }'
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | Yes | The message to send to Claude |
| `session_id` | string | No | If provided, continues that session |
| `profile` | string | No | Profile for a new session. Optional — uses the API user's configured profile when omitted; rejected with 400 if neither supplies one |
| `project` | string | No | Project context for a new session |
| `overrides` | object | No | Optional per-query overrides (see One-Shot Query) |

**Response:** Same shape as `POST /api/v1/query` (`response`, `session_id`, `metadata`).

### POST /api/v1/conversation/stream

Same as `POST /api/v1/conversation`, but streams the response via Server-Sent
Events. Pass `session_id` in the body to continue an existing conversation.

```bash
curl -X POST http://localhost:8000/api/v1/conversation/stream \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Add input validation",
    "session_id": "sess_abc123"
  }'
```

### POST /api/v1/conversation/start

Start a conversation that runs in the background. Returns immediately after
launching the query; the actual response is streamed over the session WebSocket
(`/ws/sessions/{session_id}`). Work continues even if the HTTP connection drops,
the phone is locked, or the browser tab is backgrounded. Use the interrupt
endpoint below to stop it.

```bash
curl -X POST http://localhost:8000/api/v1/conversation/start \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Refactor the entire auth module",
    "profile": "claude-code",
    "project": "my-project"
  }'
```

**Response:**
```json
{
  "session_id": "sess_abc123",
  "status": "started",
  "message_id": "streaming-sess_abc123-1719400000.0"
}
```

### POST /api/v1/session/{session_id}/interrupt

Interrupt an active (streaming) session. The session ID is a path parameter.

```bash
curl -X POST http://localhost:8000/api/v1/session/sess_abc123/interrupt \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{
  "status": "interrupted",
  "session_id": "sess_abc123"
}
```

### GET /api/v1/streaming/active

List the sessions that are currently streaming (actively generating a response).

```bash
curl http://localhost:8000/api/v1/streaming/active \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{
  "active_sessions": ["sess_abc123", "sess_def456"]
}
```

## Usage Notes

### Profile Selection
- If `profile` is not specified, the API user's configured profile is used; if neither the request nor the API user supplies one, the request is rejected with a 400
- API users may be restricted to specific profiles

### Project Context
- If `project` is specified, Claude has access to that project's files
- API users may be restricted to a specific project

### Cost Tracking
- Non-streaming responses include cost and token data under `metadata`
- `metadata.total_cost_usd` is the query cost; `metadata.tokens_in` / `metadata.tokens_out` are the token counts
- Use automation budget controls for automated queries

### Rate Limits
- API queries are subject to rate limiting
- Streaming connections count as one request
