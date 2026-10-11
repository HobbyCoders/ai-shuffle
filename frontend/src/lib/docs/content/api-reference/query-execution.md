<!--
title: Query & Execution
description: One-shot queries, conversations, streaming, and background execution
order: 3
-->

# Query & Execution API

Endpoints for sending prompts to an agent and receiving responses. Supports one-shot queries, stateful conversations, streaming, and background execution.

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
    "profile": "your-agent-id",
    "project": "my-project"
  }'
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | Yes | The message to send to the agent |
| `profile` | string | No | Agent id to run (from `GET /api/v1/agents`). Required (400 if missing), except that a key assigned exactly one agent runs that agent when this is omitted. A key with assigned agents gets 403 for any other agent. |
| `project` | string | No | Project context. If the API user has a configured project, that project is always used. |
| `overrides` | object | No | Optional per-query overrides (see below) |

**Overrides object:**

| Field | Type | Description |
|-------|------|-------------|
| `model` | string | Model override (e.g. `sonnet`, or a custom model id) |
| `permission_mode` | string | `default`, `auto`, `acceptEdits`, `bypassPermissions`. `plan` is accepted and runs as `default`; a chat session always runs as `default`. See [Tool permissions](#tool-permissions) |
| `system_prompt_append` | string | Text appended to the agent's system prompt |
| `max_turns` | integer | Maximum conversation turns |
| `thinking_mode` | string | `default`, `disabled`, `adaptive`, `think`, `think_harder`, `ultra_think` |
| `effort` | string | `low`, `medium`, `high`, `max`, `xhigh` |
| `context_window` | integer | Context window for this turn. Ignored unless it falls within the model's range, from its Standard window up to its full Extended window |
| `fast` | boolean | Fast mode for this turn. Omit to use the session's setting |
| `workspace_project_ids` | string[] | Up to 50 other project ids whose folders the agent may also use. A key limited to one project cannot add others |

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
    "profile": "your-agent-id"
  }'
```

**SSE Event Format:**

Each frame names its type on an `event:` line and carries the JSON on a `data:` line. The stream starts with `init` and ends with `done`, `interrupted` or `error`.
```
event: init
data: {"type": "init", "session_id": "sess_abc123"}

event: text
data: {"type": "text", "content": "Here's a "}

event: tool_use
data: {"type": "tool_use", "name": "Write", "input": {...}}

event: tool_result
data: {"type": "tool_result", "name": "Write", "output": "File created"}

event: done
data: {"type": "done", "session_id": "sess_abc123", "metadata": {...}}
```

On failure the stream sends `event: error` with `{"type": "error", "message": "..."}`.

## Conversation Mode

Stateful conversations that maintain context across multiple exchanges.

### POST /api/v1/conversation

Send a conversation turn. If `session_id` is provided in the body, the existing session is continued; otherwise a new session is created. Agent and project are only used when creating a new session.

**Auth:** API key required

```bash
# Start a new conversation (no session_id)
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Help me build a REST API with FastAPI",
    "profile": "your-agent-id",
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
| `prompt` | string | Yes | The message to send to the agent |
| `session_id` | string | No | If provided, continues that session. It must be one this API key started (403 otherwise) |
| `profile` | string | No | Agent id for a new session. Required (400 if missing), except that a key assigned exactly one agent runs that agent when this is omitted. A key with assigned agents gets 403 for any other agent. |
| `project` | string | No | Project context for a new session |
| `overrides` | object | No | Optional per-query overrides (see One-Shot Query) |

**Response:** Same shape as `POST /api/v1/query` (`response`, `session_id`, `metadata`).

### POST /api/v1/conversation/stream

Same as `POST /api/v1/conversation`, but streams the response via Server-Sent Events. Pass `session_id` in the body to continue an existing conversation.

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

Start a conversation that runs in the background. Returns immediately after launching the query; follow the response on the session's event stream (`GET /api/v1/sessions/{session_id}/events`, see [Sessions](/docs/api-reference/sessions)). Work continues even if the HTTP connection drops, the phone is locked, or the browser tab is backgrounded. Use the interrupt endpoint below to stop it.

```bash
curl -X POST http://localhost:8000/api/v1/conversation/start \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Refactor the entire auth module",
    "profile": "your-agent-id",
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

### Agent Selection
- `profile` is required; a request without one is rejected with a 400
- An API user with assigned agents (`profile_ids`) can only run those: any other `profile` is rejected with a 403. Omitting `profile` runs the assigned agent when there is exactly one, and is rejected with a 400 naming the allowed ids when there are several

### Project Context
- If `project` is specified, the agent has access to that project's files
- API users may be restricted to a specific project

### Tool Permissions
- `POST /api/v1/query`, `/query/stream`, `/conversation/stream` and `/conversation/start` have nobody to answer a permission prompt, so the permission mode decides alone
- `acceptEdits` approves only file edits (Edit, Write, MultiEdit, NotebookEdit); every other tool call is treated as under `default`
- On the built-in Claude models (the Claude Code CLI), `default` and `acceptEdits` deny a tool call the CLI asks about at once and the agent is told why (under `acceptEdits` the CLI runs file edits itself); `bypassPermissions` allows it; `auto` allows it except the screen-control (computer use) access request, which it denies
- On other providers, `bypassPermissions` runs the tool and `acceptEdits` runs a file edit; a call that would prompt (every write-class call under `default`, everything but a file edit under `acceptEdits`, and the screen-control access request short of `bypassPermissions`) waits and is denied after 15 minutes. A session that still has `auto` runs as `default` there
- `POST /api/v1/conversation` runs like a session in the app: its prompts appear in the app and are denied if nobody answers within 5 minutes

### Cost Tracking
- Non-streaming responses include cost and token data under `metadata`
- `metadata.total_cost_usd` is the query cost; `metadata.tokens_in` / `metadata.tokens_out` are the token counts
- Use automation budget controls for automated queries
