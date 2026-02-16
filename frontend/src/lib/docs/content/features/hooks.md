<!--
title: Hooks
description: Execute custom code at key points in AI workflow
order: 2
-->

# Hooks

Hooks let you execute custom code at key points during an AI session. Use them to log tool usage, enforce safety policies, send notifications, block dangerous operations, or integrate with external systems.

## Overview

A hook is a piece of logic -- either a shell command or a webhook call -- that AI Shuffle runs when a specific event occurs during Claude's workflow. Hooks are scoped to a project and can be enabled or disabled individually.

Key concepts:

- **Per-project:** Each hook belongs to a specific project
- **Two types:** Bash (run a shell command) and Webhook (HTTP POST to a URL)
- **Nine event types:** Cover the full session lifecycle from start to finish
- **Tool matching:** Optionally filter tool-related events by tool name pattern
- **Plugin hooks:** Installed plugins can contribute read-only hooks

## Hook Types

### Bash Hooks

Execute a shell command when the event fires. The command receives event data through environment variables.

| Field     | Type    | Description                                    |
|-----------|---------|------------------------------------------------|
| `command` | string  | Shell command to execute                        |
| `timeout` | integer | Timeout in milliseconds (1000-300000, default 30000) |

**Environment variables passed to bash hooks:**

| Variable         | Description                                      |
|------------------|--------------------------------------------------|
| `HOOK_EVENT`     | The event type (e.g., `PreToolUse`)               |
| `HOOK_TIMESTAMP` | ISO 8601 timestamp of the event                   |
| `HOOK_DATA`      | JSON-encoded event payload                         |
| `HOOK_*`         | Additional event-specific variables                |

**Example bash hook command:**

```bash
echo "[$(date)] $HOOK_EVENT: $HOOK_DATA" >> /var/log/ai-shuffle-hooks.log
```

### Webhook Hooks

Send an HTTP POST request to a URL when the event fires. Optionally sign the payload with HMAC-SHA256 for verification.

| Field    | Type   | Description                                          |
|----------|--------|------------------------------------------------------|
| `url`    | string | URL to POST to                                        |
| `secret` | string | Optional HMAC-SHA256 signing secret                   |
| `timeout`| integer| Timeout in milliseconds (1000-300000, default 30000)  |

When a `secret` is provided, the request includes an HMAC-SHA256 signature header that the receiving server can use to verify the request is authentic.

## Hook Events

AI Shuffle supports 9 hook event types that cover the full lifecycle of an AI session.

### PreToolUse

Fires **before** Claude executes a tool. Use this to approve, modify, or block tool calls.

- Supports `tool_matcher` to filter by tool name
- Common use: block dangerous commands, require approval for destructive operations

### PostToolUse

Fires **after** Claude finishes executing a tool. Use this to log results, audit tool usage, or trigger follow-up actions.

- Supports `tool_matcher` to filter by tool name
- Common use: log all Bash command executions, track file modifications

### SessionStart

Fires when a new AI session begins.

- Common use: initialize logging, set up monitoring, record session metadata

### SessionEnd

Fires when an AI session completes (successfully or with an error).

- Common use: send completion notifications, generate session summaries, clean up resources

### Stop

Fires when Claude's response generation stops.

- Common use: log stop reasons, trigger follow-up workflows

### UserPromptSubmit

Fires when a user submits a prompt to Claude.

- Common use: audit user inputs, enforce content policies, log prompts

### SubagentStop

Fires when a subagent finishes its task.

- Common use: log subagent results, chain subagent workflows

### PreCompact

Fires before a conversation context is compacted (trimmed for token limits).

- Common use: save full context before compaction, log compaction events

### Notification

Fires when Claude generates a notification.

- Common use: forward notifications to external channels (Slack, email, etc.)

## Tool Matcher

The `tool_matcher` field is an optional regex pattern that filters `PreToolUse` and `PostToolUse` events to only fire for matching tool names.

**Examples:**

| Pattern           | Matches                                |
|-------------------|----------------------------------------|
| `Bash`            | Only Bash tool calls                   |
| `Bash\|Edit`      | Bash or Edit tool calls                |
| `mcp__.*`         | All MCP tool calls                     |
| `Bash\|Write\|Edit` | Bash, Write, or Edit calls            |

If `tool_matcher` is not set or is empty, the hook fires for all tools.

## Plugin-Contributed Hooks

Installed plugins can contribute hooks that are automatically discovered. Plugin hooks are **read-only** -- you cannot edit or delete them through the hooks UI. They are managed by the plugin itself.

To view all plugin-contributed hooks:

```bash
curl http://localhost:8000/api/v1/hooks/plugins/all \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Each plugin hook returns:

- `plugin_id` -- The plugin that contributes it
- `plugin_name` -- Human-readable plugin name
- `event` -- The hook event type
- `hook_type` -- Either `prompt` or `command`
- `description` -- What the hook does
- `tool_matcher` -- Optional tool filter pattern

## API Reference

### List Hook Event Types

```bash
curl http://localhost:8000/api/v1/hooks/events \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Returns: `["PreToolUse", "PostToolUse", "Stop", "SessionStart", "SessionEnd", "UserPromptSubmit", "PreCompact", "SubagentStop", "Notification"]`

### List Hooks for a Project

```bash
curl http://localhost:8000/api/v1/hooks/project/{project_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Create a Hook

```bash
curl -X POST http://localhost:8000/api/v1/hooks/project/{project_id} \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Log Bash Commands",
    "event": "PostToolUse",
    "hook_type": "bash",
    "command": "echo \"[$(date)] Tool used: $HOOK_DATA\" >> /tmp/tool-log.txt",
    "timeout": 5000,
    "enabled": true,
    "tool_matcher": "Bash"
  }'
```

### Update a Hook

```bash
curl -X PATCH http://localhost:8000/api/v1/hooks/{hook_id} \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": false
  }'
```

### Delete a Hook

```bash
curl -X DELETE http://localhost:8000/api/v1/hooks/{hook_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Test a Hook

Run a hook with sample event data to verify it works:

```bash
curl -X POST http://localhost:8000/api/v1/hooks/{hook_id}/test \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Returns a `HookTestResult` with `success`, `duration_ms`, `output`, and `error` fields.

## Practical Examples

### Log All Tool Usage

Create a bash hook to log every tool invocation to a file:

```json
{
  "name": "Tool Usage Logger",
  "event": "PostToolUse",
  "hook_type": "bash",
  "command": "echo \"{\\\"timestamp\\\": \\\"$HOOK_TIMESTAMP\\\", \\\"event\\\": \\\"$HOOK_EVENT\\\", \\\"data\\\": $HOOK_DATA}\" >> /var/log/tool-usage.jsonl",
  "timeout": 5000,
  "enabled": true
}
```

### Block Dangerous Bash Commands

Use a PreToolUse hook to intercept and block potentially dangerous shell commands:

```json
{
  "name": "Block rm -rf",
  "event": "PreToolUse",
  "hook_type": "bash",
  "command": "echo $HOOK_DATA | grep -q 'rm -rf /' && exit 1 || exit 0",
  "timeout": 3000,
  "enabled": true,
  "tool_matcher": "Bash"
}
```

A non-zero exit code from a PreToolUse bash hook blocks the tool from executing.

### Slack Notification on Session End

Send a webhook notification when a session completes:

```json
{
  "name": "Session Complete Notification",
  "event": "SessionEnd",
  "hook_type": "webhook",
  "url": "https://hooks.slack.com/services/T00/B00/xxxx",
  "secret": "your-hmac-secret",
  "timeout": 10000,
  "enabled": true
}
```

### Audit User Prompts

Log every user prompt for compliance auditing:

```json
{
  "name": "Prompt Auditor",
  "event": "UserPromptSubmit",
  "hook_type": "webhook",
  "url": "https://your-audit-service.example.com/api/log",
  "secret": "audit-signing-key",
  "timeout": 5000,
  "enabled": true
}
```
