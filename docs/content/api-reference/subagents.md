<!--
title: Subagents
description: Subagent CRUD and management endpoints
order: 11
-->

# Subagents API

Endpoints for managing subagents — specialized AI agents with custom prompts, tool restrictions, and model selection.

## Subagent CRUD

### GET /api/v1/subagents

List all global subagents.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/subagents \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "id": "chat-history-summarizer",
    "name": "Chat History Summarizer",
    "description": "Reads and summarizes .jsonl chat history files",
    "prompt": "You are a chat history summarizer...",
    "tools": ["Read", "Grep", "Glob"],
    "model": "haiku",
    "is_builtin": true,
    "is_modified": false,
    "created_at": "2025-01-01T00:00:00Z"
  }
]
```

### GET /api/v1/subagents/{subagent_id}

Get a specific subagent.

### POST /api/v1/subagents

Create a new custom subagent.

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/subagents \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Code Reviewer",
    "description": "Reviews code changes for bugs, security issues, and best practices",
    "prompt": "You are an expert code reviewer. Analyze the code carefully and provide feedback on:\n1. Bugs and logic errors\n2. Security vulnerabilities\n3. Performance issues\n4. Code style and best practices\n\nBe specific and provide line numbers.",
    "tools": ["Read", "Grep", "Glob"],
    "model": "sonnet"
  }'
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Subagent display name |
| `description` | string | Yes | When to use this agent (shown to Claude) |
| `prompt` | string | Yes | System prompt for the subagent |
| `tools` | string[] | No | Allowed tools (null = inherit all from parent) |
| `model` | string | No | Model override: a preset shorthand (`haiku`, `sonnet`, `opus`, `fable`), any custom model id (passed through unchanged), or null to inherit. Legacy full model ids (`claude-sonnet-4-6`, `claude-opus-4-8`, etc.) are still recognized: the app displays them as the matching shorthand and rewrites them to it the next time the subagent is saved from the UI. |

### PUT /api/v1/subagents/{subagent_id}

Update a subagent. Works for both custom and built-in subagents.

**Auth:** Admin only

```bash
curl -X PUT http://localhost:8000/api/v1/subagents/chat-history-summarizer \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Updated system prompt for the summarizer..."
  }'
```

### DELETE /api/v1/subagents/{subagent_id}

Delete a custom subagent. Cannot delete built-in subagents.

**Auth:** Admin only

### POST /api/v1/subagents/{subagent_id}/revert

Revert a built-in subagent to its original configuration (undoes any customizations).

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/subagents/chat-history-summarizer/revert \
  -H "Authorization: Bearer aih_your_key"
```

## Subagent Configuration

### Model Selection

| Model | Best For | Cost |
|-------|----------|------|
| `haiku` | Fast, simple tasks | Lowest |
| `sonnet` | Balanced capability | Medium |
| `opus` | Complex reasoning | High |
| `fable` | Hardest reasoning, long-running tasks | Highest |
| `null` | Inherit from parent session | Varies |

Any other string is treated as a custom model ID and passed through unchanged. Legacy full model IDs (`claude-sonnet-4-6`, `claude-opus-4-8`, etc.) are still recognized: the app displays them as the matching shorthand and rewrites them to it the next time the subagent is saved from the UI.

### Tool Restrictions

- `null` or empty array: Subagent inherits all tools from the parent session
- Specific tool list: Only those tools are available

Common tool subsets:
- **Read-only:** `["Read", "Grep", "Glob"]` — for analysis tasks
- **Code review:** `["Read", "Grep", "Glob", "Bash"]` — read + run tests
- **Full access:** `null` — for tasks that need to modify files

### Built-in Subagents

| ID | Purpose | Default Model |
|----|---------|---------------|
| `chat-history-summarizer` | Summarize .jsonl chat histories | haiku |

Built-in subagents can be customized (PUT) and reverted (revert endpoint).
