<!--
title: Hooks
description: Execute custom code at key points in AI workflow
order: 2
-->

# Hooks

Hooks let you run your own code at key points during an AI session. Use them to check or block tool calls, log what agents do, or tell external systems when an agent finishes.

## Overview

A hook is a piece of logic -- a shell command or a webhook call -- that AI Shuffle runs when a specific event occurs during the agent's workflow. Each hook can be enabled or disabled individually. Manage hooks in **Build → Hooks** (App, Project and Plugin tabs) or through the API.

**What runs today:**

- `PreToolUse` runs bash hooks before a tool and can block it. Webhook and message hooks on `PreToolUse` are skipped.
- `Stop` runs bash and webhook hooks when the agent finishes its turn. A bash hook's output or a webhook's JSON response can keep it working.
- The other seven events (`PostToolUse`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`, `SubagentStop`, `PreCompact`, `Notification`) run bash and webhook hooks in the background. They cannot block or change anything.
- `PreToolUse` and `Stop` hooks run only in sessions that belong to a project. Hooks on the other seven events also run in sessions with no project (app-wide hooks only).
- Message hooks are not supported. New ones cannot be created, and an existing one delivers nothing: each run is recorded as failed. See [Message Hooks](#message-hooks).
- Hooks that plugins contribute through `hooks.json` are not affected by these limits; see [Plugin-Contributed Hooks](#plugin-contributed-hooks).

Key concepts:

- **Project or app-wide:** A hook belongs to one project, or to the whole app (`/api/v1/hooks/app`) and runs for every project
- **Types:** Bash (run a shell command) and Webhook (HTTP POST to a URL). A third type, Message, cannot be created; existing message hooks are shown as unsupported
- **Blocking:** Only a bash `PreToolUse` hook can block a tool. A bash or webhook `Stop` hook can keep the agent working. Hooks on every other event run in the background
- **Tool matching:** Optionally filter tool events (`PreToolUse`, `PostToolUse`) by tool name pattern
- **Plugin hooks:** Installed plugins can contribute read-only hooks

## Hook Types

### Bash Hooks

Run a shell command when the event fires. For `PreToolUse` and `Stop`, AI Shuffle waits for the command to finish. On the other events the command runs in the background and the session does not wait for it.

| Field     | Type    | Description                                    |
|-----------|---------|------------------------------------------------|
| `command` | string  | Shell command to execute                        |
| `timeout` | integer | Timeout in milliseconds (1000-300000, default 30000) |

The `timeout` applies to each hook on its own. When several hooks match one `PreToolUse` or `Stop` event, they run one after another and each gets its full timeout. A hook that runs out of time is stopped, with everything it started.

The event's hooks together also have a deadline: the sum of their timeouts plus 5 seconds per hook, never below 60 seconds. A running session keeps the deadline it computed when it started, so a timeout raised mid-session can still be cut short by the old deadline until the next session. When `PreToolUse` hooks pass that deadline, the tool call is denied (see [Blocking a Tool Call](#blocking-a-tool-call)). When `Stop` hooks pass it, the agent stops as usual.

A `PreToolUse` hook receives the tool call as JSON on stdin and runs in the session's working directory:

```json
{
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": { "command": "ls -la" },
  "session_id": "...",
  "cwd": "/workspace/my-project"
}
```

**Environment variables passed to `PreToolUse` bash hooks:**

| Variable         | Description                                      |
|------------------|--------------------------------------------------|
| `HOOK_EVENT`     | `PreToolUse`                                      |
| `HOOK_TOOL_NAME` | The tool about to run                             |
| `HOOK_DATA`      | The same JSON as stdin, shortened when it is large (see [Size limits](#size-limits)) |

**Environment variables passed to bash hooks on the other events** (no stdin):

| Variable         | Description                                      |
|------------------|--------------------------------------------------|
| `HOOK_EVENT`     | The event type, for example `PostToolUse`         |
| `HOOK_TIMESTAMP` | When the event fired (ISO 8601)                   |
| `HOOK_DATA`      | The event's data as JSON                          |
| `HOOK_<KEY>`     | One variable per top-level key of the event data, for example `HOOK_TOOL_NAME`. Values are plain text, not JSON |

A `Stop` bash hook gets the same variables, also with no stdin. `HOOK_DATA` holds the stop payload: `session_id`, `cwd`, `stop_hook_active`, `stop_reason`, the turn's final `text` (first 16,000 characters) and `project_id`.

#### Size limits

Each `HOOK_*` value is capped at 64 KiB. A longer `HOOK_<KEY>` value is cut and ends in `...[truncated]`. `HOOK_DATA` is always valid JSON: when it would be too long, its longest strings (for example a large `tool_input.content`) are shortened, each ending in `...[truncated]`, and `"truncated": true` is added at the top level.

A `PreToolUse` guard should read the tool call from stdin, which always carries it in full. A guard that reads `HOOK_DATA` instead should block when `truncated` is `true`, because the part it looks for may have been cut.

#### Environment

Hook commands (bash hooks, `PreToolUse` guards and plugin hooks) do not inherit the app's secrets. Variables such as `DATABASE_URL`, `PG*`, `OWNER_PASSWORD`, `SECRET_KEY`, and names ending in `_TOKEN`, `_SECRET`, `_PASSWORD` or `_API_KEY` are removed. Model-provider keys (for example `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`), `FAL_KEY`, `GH_TOKEN` and `GITHUB_TOKEN` are kept. To pass another variable through, list it in `FLUID_CHILD_ENV_PASSTHROUGH` (comma separated) in the app's environment.

**Example bash hook command:**

```bash
echo "[$(date)] $HOOK_EVENT $HOOK_TOOL_NAME" >> /var/log/ai-shuffle-hooks.log
```

### Blocking a Tool Call

A bash `PreToolUse` hook decides what happens to the tool call:

- **Exit code 2** denies the call. Anything the hook wrote to stderr is shown as the reason.
- **JSON on stdout** such as `{"permissionDecision": "deny", "permissionDecisionReason": "..."}` denies it. `allow` and `ask` work the same way. `{"decision": "block", "reason": "..."}` also denies it.
- **Exit code 0 with no output** has no opinion: the call goes through the normal permission flow.

A single hook that times out, crashes or exits with another code does not block: it has no opinion, and the next hook runs. The call is denied instead when the guard gave no answer at all:

- a hook cannot start (for example the shell is missing);
- the `PreToolUse` hooks together run past their deadline (see [Bash Hooks](#bash-hooks)), or the hook runner itself fails;
- on Claude subscription sessions, where the Claude CLI runs the tools, the `PreToolUse` hooks for one call have not decided within 570 seconds. The CLI is told to wait 600 seconds for them, so it never runs the call on its own after giving up.

The deny carries the reason, and the agent sees the call as denied. Output that is not valid JSON is ignored, so a JSON decision with a typo does not block anything. When several hooks match, the first one that returns a decision wins. Only bash hooks can block a tool; webhook and message hooks on `PreToolUse` are skipped.

### Stop Hooks

A `Stop` hook runs when the agent finishes its turn. Either of these keeps the agent working instead of stopping, and the reason tells it why:

- a bash `Stop` hook that exits with code 0 and prints `{"decision": "block", "reason": "..."}`;
- a webhook `Stop` hook whose server answers with a 2xx status and the JSON body `{"decision": "block", "reason": "..."}`.

Any other webhook response, including an error status, only notifies. The hooks run in order and the first block wins.

### Webhook Hooks

Send an HTTP POST request to a URL when the event fires. Optionally sign the payload with HMAC-SHA256 for verification.

| Field    | Type   | Description                                          |
|----------|--------|------------------------------------------------------|
| `url`    | string | URL to POST to                                        |
| `secret` | string | Optional HMAC-SHA256 signing secret                   |
| `timeout`| integer| Timeout in milliseconds (1000-300000, default 30000)  |

The body is JSON describing the event: `{"event": ..., "timestamp": ..., "data": {...}}`. A `Stop` hook also sends `working_dir` and `project_id`, and its `data` holds the same stop payload a bash hook gets in `HOOK_DATA`. A `Stop` webhook can keep the agent working through its response (see [Stop Hooks](#stop-hooks)); on other events the response is ignored. Every request carries `X-Hook-Event` and `X-Hook-Timestamp` headers. When a `secret` is set, it also carries `X-Hook-Signature: sha256=<hex>`, the HMAC-SHA256 of the raw body keyed by the secret, so the receiving server can check the request is authentic.

### Message Hooks

Message hooks are not supported, and new ones cannot be created (the API returns `422`). No run of an existing message hook delivers its message: it would not appear in the chat or reach the agent. Each run is recorded as failed, and testing one shows the rendered message together with that error. Delete it, or switch it to a webhook or bash hook.

| Field              | Type   | Description                                              |
|--------------------|--------|----------------------------------------------------------|
| `message_template` | string | Message template (`{event}`, `{tool_name}` and other event fields). Not delivered |

## Hook Events

| Event | What runs |
|-------|-----------|
| `PreToolUse` | Bash hooks, before a tool. Can block it. Project sessions only. |
| `Stop` | Bash and webhook hooks, when the agent finishes its turn. A bash hook's output or a webhook's JSON response can keep it working. Project sessions only. |
| `PostToolUse`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`, `SubagentStop`, `PreCompact`, `Notification` | Bash and webhook hooks, in the background. Cannot block or change anything. Also run in sessions with no project (app-wide hooks). |

`Notification` fires when the session stops to wait for you, for example on a permission prompt. `SessionEnd` fires once, when the session closes. Both fire on Claude subscription sessions too.

## Tool Matcher

The `tool_matcher` field is an optional regex pattern that limits a tool hook (`PreToolUse`, `PostToolUse`) to matching tool names. The whole tool name must match. A pattern that is not a valid regex is compared as plain text, so it matches only a tool with exactly that name; other hooks on the event still run. A warning is logged once per pattern.

**Examples:**

| Pattern           | Matches                                |
|-------------------|----------------------------------------|
| `Bash`            | Only Bash tool calls                   |
| `Bash\|Edit`      | Bash or Edit tool calls                |
| `mcp__.*`         | All MCP tool calls                     |
| `Bash\|Write\|Edit` | Bash, Write, or Edit calls            |

If `tool_matcher` is not set or is empty, the hook fires for all tools.

## Plugin-Contributed Hooks

Installed plugins can contribute hooks in their `hooks/hooks.json`, which are discovered automatically. Plugin hooks are **read-only** -- you cannot edit or delete them through the hooks UI (they appear on the **Plugin** tab). They are managed by the plugin itself.

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

### List and Create App-Wide Hooks

```bash
curl http://localhost:8000/api/v1/hooks/app \
  -H "Authorization: Bearer YOUR_TOKEN"
```

`POST /api/v1/hooks/app` takes the same body as a project hook (below) and creates a hook that runs for every project.

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
    "event": "PreToolUse",
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

Create a bash hook that logs every tool call to a file. It prints nothing, so it never blocks:

```json
{
  "name": "Tool Usage Logger",
  "event": "PreToolUse",
  "hook_type": "bash",
  "command": "cat >> /var/log/tool-usage.jsonl; echo >> /var/log/tool-usage.jsonl",
  "timeout": 5000,
  "enabled": true
}
```

### Block Dangerous Bash Commands

Use a PreToolUse hook to stop `rm -rf /` before it runs. The hook reads the tool call from stdin and exits with code 2 to block it:

```json
{
  "name": "Block rm -rf /",
  "event": "PreToolUse",
  "hook_type": "bash",
  "command": "grep -q 'rm -rf /' && { echo 'rm -rf / is not allowed' >&2; exit 2; } || exit 0",
  "timeout": 3000,
  "enabled": true,
  "tool_matcher": "Bash"
}
```

The agent sees the call as denied, with the stderr text as the reason.

### Notify a Server When the Agent Finishes

Send a signed webhook each time the agent finishes its turn:

```json
{
  "name": "Turn Finished Notification",
  "event": "Stop",
  "hook_type": "webhook",
  "url": "https://your-server.example.com/ai-shuffle/stop",
  "secret": "your-hmac-secret",
  "timeout": 10000,
  "enabled": true
}
```
