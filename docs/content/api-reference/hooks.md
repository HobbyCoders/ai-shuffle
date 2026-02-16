<!--
title: Hooks
description: Hook CRUD, testing, and plugin hook endpoints
order: 9
-->

# Hooks API

Endpoints for managing hooks — custom bash scripts or webhook calls that execute at key points in the AI workflow.

## Discovery

### GET /api/v1/hooks/events

List valid hook event types.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/hooks/events \
  -H "Authorization: Bearer aih_your_key"
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

### GET /api/v1/hooks/project/{project_id}

List all hooks for a project.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/hooks/project/proj_abc123 \
  -H "Authorization: Bearer aih_your_key"
```

### POST /api/v1/hooks

Create a new hook.

**Auth:** Required

```bash
# Bash hook
curl -X POST http://localhost:8000/api/v1/hooks \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "proj_abc123",
    "event": "PostToolUse",
    "hook_type": "bash",
    "command": "echo \"Tool $HOOK_TOOL_NAME used\" >> /tmp/tool-log.txt",
    "timeout": 5000,
    "enabled": true,
    "tool_matcher": "Bash|Write"
  }'

# Webhook hook
curl -X POST http://localhost:8000/api/v1/hooks \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "proj_abc123",
    "event": "SessionEnd",
    "hook_type": "webhook",
    "url": "https://hooks.slack.com/services/T.../B.../xxx",
    "secret": "my-webhook-secret",
    "timeout": 10000,
    "enabled": true
  }'
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `project_id` | string | Yes | Project this hook belongs to |
| `event` | string | Yes | Hook event type |
| `hook_type` | string | Yes | "bash" or "webhook" |
| `command` | string | Bash only | Shell command to execute |
| `url` | string | Webhook only | URL to POST to |
| `secret` | string | Webhook only | HMAC-SHA256 signing secret |
| `timeout` | integer | No | Timeout in ms (1000-300000, default: 30000) |
| `enabled` | boolean | No | Whether hook is active (default: true) |
| `tool_matcher` | string | No | Regex for PreToolUse/PostToolUse filtering |

### GET /api/v1/hooks/{hook_id}

Get hook details.

### PUT /api/v1/hooks/{hook_id}

Update a hook.

### DELETE /api/v1/hooks/{hook_id}

Delete a hook.

## Testing

### POST /api/v1/hooks/{hook_id}/test

Test a hook with sample data.

```bash
curl -X POST http://localhost:8000/api/v1/hooks/hook_abc123/test \
  -H "Authorization: Bearer aih_your_key"
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

### GET /api/v1/hooks/plugin/{plugin_id}

Get hooks contributed by a plugin. These are read-only.

## Bash Hook Environment Variables

When a bash hook executes, these environment variables are available:

| Variable | Description |
|----------|-------------|
| `HOOK_EVENT` | Event type (e.g., "PostToolUse") |
| `HOOK_TIMESTAMP` | ISO 8601 timestamp |
| `HOOK_DATA` | Full event data as JSON |
| `HOOK_TOOL_NAME` | Tool name (for tool use events) |
| `HOOK_TOOL_INPUT` | Tool input as JSON (for tool use events) |
| `HOOK_SESSION_ID` | Session ID |

## Webhook Hook Payload

Webhook hooks receive a POST request with:

**Headers:**
```
Content-Type: application/json
X-Hook-Signature: sha256=<hmac_hex_digest>  (if secret configured)
```

**Body:**
```json
{
  "event": "PostToolUse",
  "timestamp": "2025-01-15T10:30:00Z",
  "hook_id": "hook_abc123",
  "data": {
    "tool_name": "Bash",
    "tool_input": { "command": "git status" },
    "session_id": "sess_def456"
  }
}
```

To verify the signature:
```python
import hmac, hashlib

expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
assert f"sha256={expected}" == request.headers["X-Hook-Signature"]
```
