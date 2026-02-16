<!--
title: Automations
description: Scheduled and triggered AI workflows that run automatically
order: 1
-->

# Automations

Automations let you create AI workflows that run on a schedule, in response to events, or via webhooks -- without manual intervention. Use them for daily summaries, automated responses to connector events, scheduled reports, and any repeatable AI task.

## Overview

An automation consists of:

- A **trigger** that determines when it runs (cron schedule, interval, webhook, or connector event)
- A **prompt template** that defines what Claude should do
- A **profile** that controls Claude's behavior (model, permissions, tools)
- **Budget controls** that cap cost and usage
- An optional **approval workflow** for human-in-the-loop oversight
- **Output configuration** that routes results to a connector, webhook, session, or nowhere

Automations are admin-only. All CRUD operations require admin authentication.

## Trigger Types

### Cron

Runs on a cron schedule using standard 5-field cron expressions.

| Field    | Values         | Example          |
|----------|----------------|------------------|
| Minute   | 0-59           | `0`              |
| Hour     | 0-23           | `9`              |
| Day      | 1-31           | `*`              |
| Month    | 1-12           | `*`              |
| Weekday  | 0-6 (Sun=0)    | `1-5`            |

**trigger_config:**

```json
{
  "cron_expression": "0 9 * * 1-5",
  "timezone": "America/New_York"
}
```

The example above runs at 9:00 AM Eastern time on weekdays (Monday through Friday).

Common patterns:

- `0 9 * * 1-5` -- Weekdays at 9 AM
- `0 0 * * 0` -- Sunday at midnight
- `*/30 * * * *` -- Every 30 minutes
- `0 8 1 * *` -- First of every month at 8 AM

### Interval

Runs repeatedly at a fixed interval. The minimum interval is 60 seconds.

**trigger_config:**

```json
{
  "interval_seconds": 300
}
```

This example runs every 5 minutes.

### Webhook

Triggered by an HTTP POST request. The automation generates a secret for authentication. Include the secret in the `X-Webhook-Signature` header when calling the trigger endpoint.

**trigger_config:**

```json
{
  "secret": "auto-generated-on-creation"
}
```

To trigger a webhook automation:

```bash
curl -X POST http://localhost:8000/api/v1/automations/{automation_id}/trigger \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "X-Webhook-Signature: THE_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"data": "your payload here"}'
```

The JSON payload is available in the prompt template via `{{event}}` variables.

### Connector Event

Fires when a specific event occurs on a connected platform (Slack, Discord, etc.).

**trigger_config:**

```json
{
  "connector_id": "connector-abc123",
  "event_type": "session.complete"
}
```

Supported event types:

| Event                  | Description                              |
|------------------------|------------------------------------------|
| `session.complete`     | A session finished successfully           |
| `session.error`        | A session encountered an error            |
| `session.started`      | A new session was started                 |
| `permission.requested` | Claude is waiting for permission approval |
| `input.needed`         | Claude is waiting for user input          |

## Prompt Templates

Prompts support `{{variable}}` placeholder syntax. Variables are populated from trigger data at runtime.

```
Summarize the latest activity for project {{project_name}}.
Focus on: {{focus_areas}}

Session ID: {{event.session_id}}
Event type: {{event.type}}
```

Dot notation is supported for nested data, such as `{{event.session_id}}` or `{{event.payload.message}}`.

For webhook triggers, the entire JSON payload is available under `{{event}}`. For connector event triggers, the event data includes fields like `session_id`, `type`, and `payload`.

### Dry-Run Testing

You can render a prompt without executing it:

```bash
curl -X POST http://localhost:8000/api/v1/automations/{automation_id}/test \
  -H "Authorization: Bearer YOUR_TOKEN"
```

This returns the rendered prompt and budget validation result without spending tokens.

## Output Configuration

Control where automation results are sent.

| Output Type   | Description                                       |
|---------------|---------------------------------------------------|
| `session`     | Results stored in a new session (default)          |
| `connector`   | Route results to a connector (e.g., post to Slack) |
| `webhook`     | POST results to an external URL                    |
| `none`        | Discard output (fire-and-forget)                   |

**Example output_config:**

```json
{
  "type": "connector",
  "connector_id": "connector-slack-abc123"
}
```

## Budget Controls

Prevent runaway costs with granular budget limits.

| Field              | Type    | Description                                      |
|--------------------|---------|--------------------------------------------------|
| `max_cost_per_run` | float   | Maximum USD cost for a single run                 |
| `max_cost_per_day` | float   | Maximum total USD cost across all runs in one day |
| `max_cost_total`   | float   | Lifetime cost cap for this automation             |
| `max_runs_per_day` | integer | Maximum number of executions per day              |
| `max_turns`        | integer | Max conversation turns per run (1-25)             |
| `timeout_seconds`  | integer | Execution timeout per run (30-1800 seconds)       |

When any limit is exceeded, the run is blocked and the automation reports the reason in its status.

## Approval Workflow

When `requires_approval` is `true`, automations pause before executing and wait for admin approval.

**Flow:**

1. Trigger fires and creates a run with status `pending`
2. An approval request is created with the rendered prompt preview
3. Admin reviews and approves or denies via the API or UI
4. If approved, the run executes; if denied, the run is cancelled

### List Pending Approvals

```bash
curl http://localhost:8000/api/v1/automations/approvals/pending \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Approve a Run

```bash
curl -X POST http://localhost:8000/api/v1/automations/approvals/{approval_id}/approve \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Deny a Run

```bash
curl -X POST http://localhost:8000/api/v1/automations/approvals/{approval_id}/deny \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Run Tracking

Every execution creates a run record with detailed tracking.

**Run statuses:**

`pending` -> `running` -> `completed` or `failed`

If approval is required: `pending` -> `approved` -> `running` -> `completed` or `failed`

If denied: `pending` -> `cancelled`

Each run records:

- **cost_usd** -- Actual API cost
- **tokens_in / tokens_out** -- Token counts
- **duration_ms** -- Execution time
- **prompt_rendered** -- The prompt after template variable substitution
- **response** -- Claude's output
- **error** -- Error message if the run failed
- **output_status** -- Whether the output was successfully delivered

### View Run History

```bash
curl "http://localhost:8000/api/v1/automations/{automation_id}/runs?limit=20&offset=0" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### View Statistics

```bash
curl http://localhost:8000/api/v1/automations/{automation_id}/stats \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Returns aggregate data: total runs, success/failure counts, total cost, average duration, and today's usage.

## API Reference

### Create an Automation

```bash
curl -X POST http://localhost:8000/api/v1/automations \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Daily Project Summary",
    "description": "Summarizes project activity every morning",
    "trigger_type": "cron",
    "trigger_config": {
      "cron_expression": "0 9 * * 1-5",
      "timezone": "America/New_York"
    },
    "prompt": "Summarize all git commits from the past 24 hours in project {{project_name}}. Include a brief description of each change and highlight anything that needs attention.",
    "profile_id": "default",
    "project_id": "my-project",
    "output_config": {"type": "session"},
    "max_turns": 3,
    "timeout_seconds": 300,
    "max_cost_per_run": 0.50,
    "max_cost_per_day": 5.00,
    "max_runs_per_day": 10,
    "requires_approval": false
  }'
```

### Enable / Disable

```bash
# Enable (starts scheduling for cron/interval triggers)
curl -X POST http://localhost:8000/api/v1/automations/{id}/enable \
  -H "Authorization: Bearer YOUR_TOKEN"

# Disable (stops scheduling)
curl -X POST http://localhost:8000/api/v1/automations/{id}/disable \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Manual Trigger

```bash
curl -X POST http://localhost:8000/api/v1/automations/{id}/trigger \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Practical Examples

### Daily Stand-up Summary

Generate a morning summary of recent activity across all sessions:

```json
{
  "name": "Morning Stand-up",
  "trigger_type": "cron",
  "trigger_config": {"cron_expression": "0 9 * * 1-5", "timezone": "US/Pacific"},
  "prompt": "Review all sessions from the past 24 hours. Summarize what was accomplished, what's in progress, and flag any errors or blockers.",
  "profile_id": "default",
  "max_turns": 5,
  "max_cost_per_run": 1.00
}
```

### Auto-Respond to Connector Events

Automatically respond when a session encounters an error:

```json
{
  "name": "Error Auto-Triage",
  "trigger_type": "connector_event",
  "trigger_config": {
    "connector_id": "connector-slack-001",
    "event_type": "session.error"
  },
  "prompt": "Session {{event.session_id}} failed with an error. Analyze the error, suggest a fix, and post a summary to the connector.",
  "profile_id": "default",
  "output_config": {"type": "connector", "connector_id": "connector-slack-001"},
  "requires_approval": true
}
```

### Scheduled Code Review

Run a weekly code review on recent changes:

```json
{
  "name": "Weekly Code Review",
  "trigger_type": "cron",
  "trigger_config": {"cron_expression": "0 14 * * 5", "timezone": "UTC"},
  "prompt": "Review all git commits from the past week. Identify potential issues, suggest improvements, and summarize code quality trends.",
  "profile_id": "code-reviewer",
  "project_id": "main-app",
  "max_turns": 10,
  "timeout_seconds": 600,
  "max_cost_per_run": 2.00
}
```
