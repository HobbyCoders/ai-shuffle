<!--
title: Automations
description: Automation CRUD, triggers, runs, approvals, and statistics endpoints
order: 8
-->

# Automations API

Endpoints for managing automations — scheduled and triggered AI workflows with budget controls and approval gates.

Every endpoint on this page needs owner access. API-user keys (`aih_...`) get `403`. The examples send an owner token from `POST /api/v1/owner/login` (see [Owner password](/docs/api-reference/authentication#owner-password)).

## Discovery

### GET /api/v1/automations/trigger-types

List available trigger types.

**Auth:** Owner only

**Response:**
```json
["cron", "interval", "connector_event", "webhook", "connector_trigger"]
```

## Automation CRUD

### GET /api/v1/automations

List all automations.

**Auth:** Owner only

### POST /api/v1/automations

Create a new automation.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/automations \
  -H "Authorization: Bearer $OWNER_TOKEN" \
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
    "profile_id": "your-agent-id",
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
| `prompt` | string | No | Template with `{{variable}}` placeholders |
| `action_steps` | object[] | No | Steps to run instead of, or after, the prompt |
| `trigger_type` | string | No | `cron`, `interval`, `connector_event`, `webhook` or `connector_trigger`. Use this with `trigger_config`, or use `triggers` |
| `trigger_config` | object | No | Trigger-specific configuration (see below) |
| `triggers` | object[] | No | Several triggers, each `{"type": ..., "config": {...}}`. Replaces `trigger_type`/`trigger_config` |
| `profile_id` | string | Yes | Agent to run (an agent id from `GET /api/v1/agents`) |
| `project_id` | string | No | Project context |
| `output_config` | object | No | Where to send results |
| `max_cost_per_run` | number | No | USD limit per run |
| `max_cost_per_day` | number | No | Daily budget |
| `max_cost_total` | number | No | Lifetime budget |
| `max_runs_per_day` | integer | No | Max executions per day |
| `max_turns` | integer | No | Conversation turns per run (default 1, null = unlimited) |
| `timeout_seconds` | integer | No | Timeout in seconds (default 300, null = unlimited) |
| `requires_approval` | boolean | No | Require manual approval |
| `enabled` | boolean | No | Enable on create (default: false) |

### Trigger Configurations

**Cron:**
```json
{
  "expression": "0 9 * * 1-5",
  "timezone": "America/New_York"
}
```

**Interval** (`seconds` must be at least 60):
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

A webhook trigger generates a secret when you omit one. See the `/trigger` endpoint below for how to sign requests.

**Connector Event:**
```json
{
  "event_type": "session.complete",
  "connector_id": "conn_abc123",
  "filter": { "status": "ok" }
}
```

Connector events fire only for sessions linked to a connector. `connector_id` limits the trigger to one connector. Every `filter` key must equal the same field in the event data.

### Prompt Templates

Use `{{variable}}` placeholders in prompts. What is available depends on the trigger:

| Trigger | Variables |
|---------|-----------|
| Every run | `{{trigger}}` (`schedule`, `manual`, `webhook`, `connector_event` or `connector_trigger`) |
| Cron, interval and manual runs | `{{fired_at}}` |
| Webhook | `{{payload.<field>}}` — the POSTed JSON |
| Connector event | `{{event_type}}`, `{{event.<field>}}` |
| Connector trigger | `{{connector_id}}`, `{{trigger_id}}`, `{{data.<field>}}` |

```
Summarize the session: {{event.session_id}}
Deploy finished for version {{payload.version}}
```

Dot notation reaches nested fields. Placeholders that do not resolve are left as-is.

### GET /api/v1/automations/{automation_id}

Get automation details.

### PATCH /api/v1/automations/{automation_id}

Update an automation.

**Auth:** Owner only

### DELETE /api/v1/automations/{automation_id}

Delete an automation.

**Auth:** Owner only

## Lifecycle

### POST /api/v1/automations/{automation_id}/enable

Enable and schedule the automation.

```bash
curl -X POST http://localhost:8000/api/v1/automations/auto_abc123/enable \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

### POST /api/v1/automations/{automation_id}/disable

Disable and unschedule the automation.

### POST /api/v1/automations/{automation_id}/trigger

Run the automation now, or deliver a webhook payload. Owner access is required either way.

- **Without `X-Webhook-Signature`** this is a manual run. It returns `409` if the automation is disabled. Any body is ignored.
- **With `X-Webhook-Signature`** (automations with a webhook trigger), the JSON body is the payload. Sign it with the trigger's `secret`: compute HMAC-SHA256 over the body re-serialized with sorted keys and no spaces (`json.dumps(body, separators=(",", ":"), sort_keys=True)`), and send `sha256=<hex digest>`. A wrong signature returns `403`.

```bash
# Manual run
curl -X POST http://localhost:8000/api/v1/automations/auto_abc123/trigger \
  -H "Authorization: Bearer $OWNER_TOKEN"

# Webhook payload
BODY='{"event":"deploy_complete","version":"1.2.3"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | sed 's/^.* //')
curl -X POST http://localhost:8000/api/v1/automations/auto_abc123/trigger \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: sha256=$SIG" \
  -d "$BODY"
```

The example body is already in sorted-key, no-space form, so its bytes match what the server signs.

### POST /api/v1/automations/{automation_id}/test

Test automation configuration without executing. Renders the prompt template and checks budget.

```bash
curl -X POST http://localhost:8000/api/v1/automations/auto_abc123/test \
  -H "Authorization: Bearer $OWNER_TOKEN"
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
  -H "Authorization: Bearer $OWNER_TOKEN"
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
