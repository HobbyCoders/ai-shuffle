<!--
title: Hooks
description: Hook CRUD, testing, and plugin hook endpoints
order: 9
-->

# Hooks API

Endpoints for managing hooks — custom bash scripts, webhook calls or messages attached to events in a session.

Every endpoint on this page needs owner access. API-user keys (`aih_...`) get `403`. The examples send an owner token from `POST /api/v1/owner/login` (see [Owner password](/docs/api-reference/authentication#owner-password)).

> **How hooks run.**
> - `PreToolUse` — bash hooks only, before the tool. They can block it (see [Blocking a tool call](#blocking-a-tool-call)). Webhook and message hooks on `PreToolUse` are skipped.
> - `Stop` — bash and webhook hooks. A hook can keep the agent working with `{"decision": "block"}`.
> - `PostToolUse`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`, `SubagentStop`, `PreCompact`, `Notification` — bash and webhook hooks run in the background. They cannot block or change anything.
>
> `PreToolUse` and `Stop` hooks run only in sessions that belong to a project. Hooks on the other seven events also run in sessions with no project (app-level hooks). **Limitation:** message hooks are never delivered, on any event. Hooks contributed by plugins are not affected by these limits.

## Discovery

### GET /api/v1/hooks/events

List valid hook event types.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/hooks/events \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
[
  "PreToolUse",
  "PostToolUse",
  "SessionStart",
  "SessionEnd",
  "Stop",
  "UserPromptSubmit",
  "SubagentStop",
  "PreCompact",
  "Notification"
]
```

## Hook CRUD

### GET /api/v1/hooks/app

List all app-level hooks (not scoped to any project). These fire for all sessions.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/hooks/app \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

### GET /api/v1/hooks/project/{project_id}

List all hooks for a project (project-scoped hooks plus app-level hooks).

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/hooks/project/proj_abc123 \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

### POST /api/v1/hooks/app

Create a new app-level hook (fires for all sessions).

**Auth:** Owner only

```bash
# Bash hook
curl -X POST http://localhost:8000/api/v1/hooks/app \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Log tool usage",
    "event": "PreToolUse",
    "hook_type": "bash",
    "command": "echo \"Tool $HOOK_TOOL_NAME used\" >> /tmp/tool-log.txt",
    "timeout": 5000,
    "enabled": true,
    "tool_matcher": "Bash|Write"
  }'
```

### POST /api/v1/hooks/project/{project_id}

Create a new hook scoped to a project. The project ID comes from the URL path.

**Auth:** Owner only

```bash
# Webhook hook
curl -X POST http://localhost:8000/api/v1/hooks/project/proj_abc123 \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Notify when the agent stops",
    "event": "Stop",
    "hook_type": "webhook",
    "url": "https://hooks.slack.com/services/T.../B.../xxx",
    "secret": "my-webhook-secret",
    "timeout": 10000,
    "enabled": true
  }'
```

**Request Body** (shared by both create endpoints):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Display name (1-100 chars) |
| `event` | string | Yes | Hook event type |
| `hook_type` | string | Yes | "bash", "webhook", or "message" |
| `command` | string | Bash only | Shell command to execute |
| `url` | string | Webhook only | URL to POST to |
| `secret` | string | Webhook only | HMAC-SHA256 signing secret |
| `message_template` | string | Message only | Prompt template sent as a message in the active chat |
| `timeout` | integer | No | Timeout in ms (1000-300000, default: 30000) |
| `enabled` | boolean | No | Whether hook is active (default: true) |
| `tool_matcher` | string | No | Tool name or regex the whole tool name must match (`PreToolUse`, `PostToolUse`) |

### GET /api/v1/hooks/{hook_id}

Get hook details.

**Auth:** Owner only

### PATCH /api/v1/hooks/{hook_id}

Update a hook. All fields are optional.

**Auth:** Owner only

### DELETE /api/v1/hooks/{hook_id}

Delete a hook.

**Auth:** Owner only

## Testing

### POST /api/v1/hooks/{hook_id}/test

Test a hook with sample data.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/hooks/hook_abc123/test \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
{
  "success": true,
  "duration_ms": 150,
  "output": "Tool Bash used",
  "error": null
}
```

## Plugin Hooks

### GET /api/v1/hooks/plugins/all

Get all hooks contributed by installed plugins. These are read-only.

**Auth:** Owner only

## Bash Hook Input

A `PreToolUse` bash hook receives the tool call as a JSON object on stdin:

```json
{
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": { "command": "git status" },
  "session_id": "sess_def456",
  "cwd": "/workspace/my-project"
}
```

The same JSON is in the `HOOK_DATA` environment variable, next to `HOOK_EVENT` and `HOOK_TOOL_NAME`.

Bash hooks on the seven background events get no stdin. They receive environment variables instead:

| Variable | Description |
|----------|-------------|
| `HOOK_EVENT` | Event type (e.g. `PostToolUse`) |
| `HOOK_TIMESTAMP` | ISO 8601 timestamp |
| `HOOK_DATA` | The event data as JSON |
| `HOOK_<KEY>` | One per top-level key of the event data (e.g. `HOOK_TOOL_NAME`). Plain text, not JSON |

A `Stop` bash hook gets the same variables, also with no stdin. `HOOK_DATA` holds the stop payload, such as `session_id`, `cwd`, `stop_hook_active`, `stop_reason`, the turn's final `text` (first 16,000 characters) and `project_id`.

## Blocking a tool call

A `PreToolUse` bash hook runs before the tool, and the tool waits for it. To block the call, either exit with code `2` (stderr becomes the reason), or print a JSON decision:

```json
{ "permissionDecision": "deny", "permissionDecisionReason": "Deploys are frozen" }
```

`permissionDecision` can be `allow`, `deny` or `ask`. `{"decision": "block", "reason": "..."}` also blocks. Exit code `0` with no JSON leaves the normal permission flow in charge. Timeouts, crashes and other exit codes never block. A `Stop` hook can print `{"decision": "block", "reason": "..."}` to keep the agent going.

## Webhook Hook Payload

Webhook hooks receive a POST request with:

**Headers:**
```
Content-Type: application/json
X-Hook-Event: Stop
X-Hook-Timestamp: <ISO 8601 timestamp, when the event has one>
X-Hook-Signature: sha256=<hmac_hex_digest>  (if secret configured)
```

**Body** on the background events:
```json
{
  "event": "PostToolUse",
  "timestamp": "2025-01-15T10:30:00Z",
  "data": {
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash"
  }
}
```

**Body** of a `Stop` hook:
```json
{
  "event": "Stop",
  "timestamp": "2025-01-15T10:30:00Z",
  "working_dir": "/workspace/my-project",
  "project_id": "proj_abc123",
  "data": {
    "hook_event_name": "Stop",
    "session_id": "sess_def456",
    "stop_reason": "end_turn",
    "project_id": "proj_abc123"
  }
}
```

A test run (`POST /api/v1/hooks/{hook_id}/test`) sends `{"event": ..., "timestamp": ..., "data": {...}}` with sample data.

To verify the signature:
```python
import hmac, hashlib

expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
assert f"sha256={expected}" == request.headers["X-Hook-Signature"]
```
