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
    "profile_id": "default",
    "project_id": "my-project"
  }'
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | Yes | The message to send to Claude |
| `profile_id` | string | No | Profile to use (defaults to API user's profile) |
| `project_id` | string | No | Project context |
| `max_turns` | integer | No | Maximum conversation turns |
| `system_prompt` | string | No | Override the profile's system prompt |

**Response:**
```json
{
  "session_id": "sess_abc123",
  "response": "Async/await in Python is a way to write...",
  "cost_usd": 0.003,
  "tokens_in": 150,
  "tokens_out": 500,
  "duration_ms": 2500
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
    "profile_id": "default"
  }'
```

**SSE Event Format:**
```
data: {"type": "text", "content": "Here's a "}
data: {"type": "text", "content": "sorting function..."}
data: {"type": "tool_use", "tool": "Write", "input": {...}}
data: {"type": "tool_result", "output": "File created"}
data: {"type": "done", "session_id": "sess_abc123", "cost_usd": 0.005}
```

## Conversation Mode

Stateful conversations that maintain context across multiple exchanges.

### POST /api/v1/conversation/start

Start a new conversation session.

**Auth:** API key required

```bash
curl -X POST http://localhost:8000/api/v1/conversation/start \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Help me build a REST API with FastAPI",
    "profile_id": "default",
    "project_id": "my-project"
  }'
```

**Response:**
```json
{
  "session_id": "sess_abc123",
  "response": "I'd be happy to help you build...",
  "cost_usd": 0.004
}
```

### POST /api/v1/conversation/{session_id}

Continue an existing conversation.

```bash
curl -X POST http://localhost:8000/api/v1/conversation/sess_abc123 \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Now add authentication to it"
  }'
```

### POST /api/v1/conversation/{session_id}/stream

Continue a conversation with SSE streaming.

```bash
curl -X POST http://localhost:8000/api/v1/conversation/sess_abc123/stream \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Add input validation"}'
```

### POST /api/v1/conversation/{session_id}/interrupt

Interrupt an active (streaming) session.

```bash
curl -X POST http://localhost:8000/api/v1/conversation/sess_abc123/interrupt \
  -H "Authorization: Bearer aih_your_key"
```

## Background Queries

For long-running queries that you don't want to block on.

### POST /api/v1/query/background

Start a background query. Returns immediately with a task ID.

**Auth:** API key required

```bash
curl -X POST http://localhost:8000/api/v1/query/background \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Refactor the entire auth module",
    "profile_id": "default",
    "project_id": "my-project"
  }'
```

**Response:**
```json
{
  "task_id": "task_abc123",
  "session_id": "sess_def456",
  "status": "running"
}
```

### GET /api/v1/query/background/{task_id}

Poll the status of a background query.

```bash
curl http://localhost:8000/api/v1/query/background/task_abc123 \
  -H "Authorization: Bearer aih_your_key"
```

**Response (in progress):**
```json
{
  "task_id": "task_abc123",
  "status": "running",
  "session_id": "sess_def456"
}
```

**Response (complete):**
```json
{
  "task_id": "task_abc123",
  "status": "completed",
  "session_id": "sess_def456",
  "response": "I've refactored the auth module...",
  "cost_usd": 0.025,
  "tokens_in": 5000,
  "tokens_out": 3000,
  "duration_ms": 45000
}
```

## Usage Notes

### Profile Selection
- If `profile_id` is not specified, the API user's default profile is used
- API users may be restricted to specific profiles

### Project Context
- If `project_id` is specified, Claude has access to that project's files
- API users may be restricted to a specific project

### Cost Tracking
- All query responses include `cost_usd` for usage tracking
- Token counts (`tokens_in`, `tokens_out`) are also returned
- Use automation budget controls for automated queries

### Rate Limits
- API queries are subject to rate limiting
- Streaming connections count as one request
- Background queries have a separate concurrency limit
