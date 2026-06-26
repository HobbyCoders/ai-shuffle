<!--
title: Automations
description: Automation CRUD, triggers, runs, approvals, and statistics endpoints
order: 8
-->

# Automations API

Endpoints for managing automations — scheduled and triggered AI workflows with budget controls and approval gates.

## Discovery

### GET /api/v1/automations/trigger-types

List available trigger types.

**Auth:** Required

**Response:**
```json
["cron", "interval", "webhook", "connector_event"]
```

## Automation CRUD

### GET /api/v1/automations

List all automations.

**Auth:** Admin only

### POST /api/v1/automations

Create a new automation.

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/automations \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Daily Summary",
    "description": "Generate a daily project summary at 9am",
    "prompt": "Generate a summary of all changes made in the last 24 hours. Include files modified, key decisions, and next steps.",
    "trigger_type": "cron",
    "trigger_config": {
      "expression": "0 9 * * 1-5",
      "timezone": "America/New_York"
    },
    "profile_id": "default",
    "project_id": "my-project",
    "max_cost_per_run": 0.10,
    "max_cost_per_day": 0.50,
    "max_turns": 5,
    "timeout_seconds": 120
  }'
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Automation name |
| `description` | string | No | Description |
| `prompt` | string | Yes | Template with `{{variable}}` placeholders |
| `trigger_type` | string | Yes | cron, interval, webhook, connector_event |
| `trigger_config` | object | Yes | Trigger-specific configuration |
| `profile_id` | string | Yes | Profile to use for execution |
| `project_id` | string | No | Project context |
| `output_config` | object | No | Where to send results |
| `max_cost_per_run` | number | No | USD limit per run |
| `max_cost_per_day` | number | No | Daily budget |
| `max_cost_total` | number | No | Lifetime budget |
| `max_runs_per_day` | integer | No | Max executions per day |
| `max_turns` | integer | No | Conversation turns (1-25) |
| `timeout_seconds` | integer | No | Timeout (30-1800) |
| `requires_approval` | boolean | No | Require manual approval |

### Trigger Configurations

**Cron:**
```json
{
  "expression": "0 9 * * 1-5",
  "timezone": "America/New_York"
}
```

**Interval:**
```json
{
  "seconds": 3600
}
```

**Webhook:**
```json
{
  "secret": "auto-generated-if-omitted"
}
```

**Connector Event:**
```json
{
  "event_type": "session.complete",
  "filter": { "connector_id": "conn_abc123" }
}
```

### Prompt Templates

Use `{{variable}}` placeholders in prompts. For webhook and connector event triggers, event data is available:

```
Summarize the session: {{event.session_id}}
The connector {{event.connector_name}} reported: {{event.message}}
```

Supports dot notation for nested fields.

### GET /api/v1/automations/{automation_id}

Get automation details.

### PATCH /api/v1/automations/{automation_id}

Update an automation.

**Auth:** Admin only

### DELETE /api/v1/automations/{automation_id}

Delete an automation.

**Auth:** Admin only

## Lifecycle

### POST /api/v1/automations/{automation_id}/enable

Enable and schedule the automation.

```bash
curl -X POST http://localhost:8000/api/v1/automations/auto_abc123/enable \
  -H "Authorization: Bearer aih_your_key"
```

### POST /api/v1/automations/{automation_id}/disable

Disable and unschedule the automation.

### POST /api/v1/automations/{automation_id}/trigger

Manually trigger the automation (or serve as the webhook endpoint).

```bash
# Manual trigger
curl -X POST http://localhost:8000/api/v1/automations/auto_abc123/trigger \
  -H "Authorization: Bearer aih_your_key"

# Webhook trigger with data
curl -X POST http://localhost:8000/api/v1/automations/auto_abc123/trigger \
  -H "Content-Type: application/json" \
  -d '{"event": "deploy_complete", "version": "1.2.3"}'
```

### POST /api/v1/automations/{automation_id}/test

Test automation configuration without executing. Renders the prompt template and checks budget.

```bash
curl -X POST http://localhost:8000/api/v1/automations/auto_abc123/test \
  -H "Authorization: Bearer aih_your_key"
```

## Runs

### GET /api/v1/automations/{automation_id}/runs

Get execution history.

**Response:**
```json
[
  {
    "id": "run_abc123",
    "status": "completed",
    "started_at": "2025-01-15T09:00:00Z",
    "completed_at": "2025-01-15T09:02:30Z",
    "cost_usd": 0.045,
    "tokens_in": 2000,
    "tokens_out": 1500,
    "duration_ms": 150000
  }
]
```

### GET /api/v1/automations/{automation_id}/runs/{run_id}

Get details for a specific run.

## Approvals

For automations with `requires_approval: true`, runs are held until approved.

### GET /api/v1/automations/approvals/pending

List all pending approvals.

```bash
curl http://localhost:8000/api/v1/automations/approvals/pending \
  -H "Authorization: Bearer aih_your_key"
```

### POST /api/v1/automations/approvals/{approval_id}/approve

Approve a pending run.

### POST /api/v1/automations/approvals/{approval_id}/deny

Deny a pending run.

## Statistics

### GET /api/v1/automations/{automation_id}/stats

Get statistics for a single automation.

**Response:**
```json
{
  "automation_id": "auto_abc123",
  "total_runs": 150,
  "successful_runs": 142,
  "failed_runs": 8,
  "total_cost_usd": 3.45,
  "avg_duration_ms": 95000,
  "today_runs": 12,
  "today_cost_usd": 0.28
}
```
