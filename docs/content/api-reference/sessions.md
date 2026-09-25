<!--
title: Sessions
description: Chat session management, search, export, and import endpoints
order: 2
-->

# Sessions API

Endpoints for managing chat sessions — listing, searching, creating, updating, deleting, exporting, and importing.

## Read the Suggested Next Reply

### GET /api/v1/sessions/{session_id}/composer-suggestion

**Auth:** Required, with the same session access checks as session detail.

Returns `{ "session_id": "…", "turn_id": "…", "suggestion": "…" }`, or `null` when no current eligible suggestion is available. This read never calls a model.

With **Suggest next reply** enabled (**Settings → Chat & Input**), the latest generated hint is saved per code session, even if no browser is watching. The empty composer loads it on return/reconnect and after a turn completes. Switching sessions or reloading does not regenerate it. A newer turn, a cleared/rewound answer, or disabling the feature prevents the stale hint from being shown. An existing draft is never replaced. Clicking the gray hint or pressing Tab/Enter accepts it into the draft; it does not send the message. Escape dismisses it for that browser's current visit.

## List Sessions

### GET /api/v1/sessions

List sessions with optional filters.

**Auth:** Required

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | string | Filter by project |
| `profile_id` | string | Filter by agent |
| `status` | string | Filter by status (active, archived) |
| `tag_id` | string | Filter by tag |
| `admin_only` | boolean | Only owner sessions |
| `api_users_only` | boolean | Only API user sessions |
| `active_only` | boolean | Only active-group sessions |
| `limit` | integer | Max results (default: 50) |
| `offset` | integer | Pagination offset |

```bash
curl "http://localhost:8000/api/v1/sessions?project_id=proj123&limit=10" \
  -H "Authorization: Bearer aih_your_key"
```

### GET /api/v1/sessions/paginated

Same as above but returns pagination metadata.

**Response:**
```json
{
  "items": [...],
  "total": 42,
  "has_more": true,
  "offset": 0,
  "limit": 20
}
```

### GET /api/v1/sessions/cursor

List sessions a page at a time using a cursor instead of an offset. Use this for long lists: each page continues exactly where the last one ended, even while sessions are being updated.

**Auth:** Required

**Query Parameters:** the same filters as `GET /api/v1/sessions`, plus:

| Parameter | Type | Description |
|-----------|------|-------------|
| `limit` | integer | Page size (default: 50, max: 100) |
| `cursor` | string | `next_cursor` from the previous page (omit for the first page) |
| `cursor_id` | string | `next_cursor_id` from the previous page |

```bash
curl "http://localhost:8000/api/v1/sessions/cursor?limit=50" \
  -H "Authorization: Bearer aih_your_key"

# Next page
curl "http://localhost:8000/api/v1/sessions/cursor?limit=50&cursor=2025-01-15T10:35:00Z&cursor_id=sess_abc123" \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{
  "items": [...],
  "next_cursor": "2025-01-15T10:35:00Z",
  "next_cursor_id": "sess_abc123",
  "has_more": true
}
```

Sessions are ordered by most recently updated. Pass both cursor values back unchanged; stop when `has_more` is `false`.

## Search Sessions

### GET /api/v1/sessions/search/query

Search sessions by title and content.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `q` | string | Search query (required) |
| `project_id` | string | Filter by project |
| `profile_id` | string | Filter by agent |
| `limit` | integer | Max results (default: 20, max: 50) |

```bash
curl "http://localhost:8000/api/v1/sessions/search/query?q=authentication" \
  -H "Authorization: Bearer aih_your_key"
```

### GET /api/v1/sessions/search/paginated

Search with pagination metadata.

## Session Details

### GET /api/v1/sessions/{session_id}

Get a session with full message history.

```bash
curl http://localhost:8000/api/v1/sessions/sess_abc123 \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{
  "id": "sess_abc123",
  "title": "Fix auth bug",
  "status": "active",
  "project_id": "proj123",
  "profile_id": "your-agent-id",
  "messages": [
    {
      "id": 101,
      "role": "user",
      "content": "Help me fix the login flow",
      "created_at": "2025-01-15T10:30:00Z"
    },
    {
      "id": 102,
      "role": "assistant",
      "content": "I'll look at the auth code...",
      "created_at": "2025-01-15T10:30:05Z"
    }
  ],
  "created_at": "2025-01-15T10:30:00Z",
  "updated_at": "2025-01-15T10:35:00Z",
  "is_active": false,
  "tags": []
}
```

### GET /api/v1/sessions/{session_id}/events

Stream the session's event log as Server-Sent Events. This is how clients receive assistant text, thinking, tool calls and tool results, live and in order.

**Auth:** Required, with the same session access checks as session detail.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `from_seq` | integer | Replay events after this sequence number, then stream live (default: 0, the whole transcript, or the part after the last compaction; see below) |

```bash
curl -N "http://localhost:8000/api/v1/sessions/sess_abc123/events?from_seq=0" \
  -H "Authorization: Bearer aih_your_key"
```

Each event is one frame. `id` is the event's sequence number; use the last one you saw as `from_seq` when you reconnect.

```
id: 42
event: assistant.text_delta
data: {"type": "assistant.text_delta", "seq": 42, "session_id": "sess_abc123", "turn_id": "...", "created_at": "...", ...}
```

Common event types include `turn.started`, `message.user`, `assistant.text_delta`, `assistant.thinking_delta`, `message.assistant`, `tool.use`, `tool.result`, `turn.usage`, `turn.completed` and `turn.aborted`.

In the replay, each run of consecutive `assistant.text_delta` (or `assistant.thinking_delta`) events from the same turn arrives as one merged frame: its `text` is the whole run and its `id` and `seq` are those of the run's last event. Live events after the replay are not merged.

When `from_seq` is `0` and the conversation has been compacted, the replay skips the compacted part. It starts with one `event: system.compaction_boundary` frame (no `id`; `data.elidedEvents` counts the skipped events) followed by the events from the first turn kept after compaction. A reconnect with `from_seq` above `0` replays from that point and never skips.

After the replay, the stream sends one `event: caught_up` frame (`{"seq": N}`, no `id`). Idle streams send `: keepalive` comment lines.

### GET /api/v1/sessions/{session_id}/live

Get live session state for streaming. Returns current partial messages and streaming status.

### GET /api/v1/sessions/{session_id}/state

Get session state for client reconnection.

### GET /api/v1/sessions/{session_id}/context-usage

Get how much of the model's context window the session is using, broken down by category.

**Auth:** Required, with the same session access checks as session detail.

```bash
curl http://localhost:8000/api/v1/sessions/sess_abc123/context-usage \
  -H "Authorization: Bearer aih_your_key"
```

**Response:** one of three shapes.

```json
{ "available": true, "source": "live", "usage": { ... } }
{ "available": true, "source": "persisted", "captured_at": "2025-01-15T10:35:00Z", "usage": { ... } }
{ "available": false, "reason": "not_connected" }
```

`source: "live"` comes from the running session. When the session is not running, the breakdown saved at the end of its last turn is returned as `source: "persisted"`. `reason` is `not_connected` (no saved breakdown), `timeout` or `unsupported`.

## Session Management

### PATCH /api/v1/sessions/{session_id}

Update session metadata.

`title` and `status` are query parameters, not a JSON body.

```bash
curl -X PATCH "http://localhost:8000/api/v1/sessions/sess_abc123?title=New%20Title&status=archived" \
  -H "Authorization: Bearer aih_your_key"
```

### DELETE /api/v1/sessions/{session_id}

Delete a session and all its messages.

```bash
curl -X DELETE http://localhost:8000/api/v1/sessions/sess_abc123 \
  -H "Authorization: Bearer aih_your_key"
```

### POST /api/v1/sessions/{session_id}/clear

Clear the conversation but keep the session. The id, title, project and worktree stay; the transcript is emptied and the next message starts a fresh conversation. Running background tasks for the session are stopped.

**Auth:** Required

```bash
curl -X POST http://localhost:8000/api/v1/sessions/sess_abc123/clear \
  -H "Authorization: Bearer aih_your_key"
```

Returns `{"status": "ok", "session_id": "sess_abc123"}`. Returns `409` while a turn is running; stop it first.

### POST /api/v1/sessions/{session_id}/resume

Run the last turn again from where it stopped, without adding a message. This is the **Retry** action after a provider error.

**Auth:** Required

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `if_busy` | string | `queue` (default) runs the retry after the current turn; `conflict` returns `409` instead |

```bash
curl -X POST http://localhost:8000/api/v1/sessions/sess_abc123/resume \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{ "turn_id": "...", "session_id": "sess_abc123", "queued": false }
```

`queued` is `true` when the retry waits behind a running turn. If a retry is already queued, that turn is returned with `"duplicate": true`. Claude Code sessions cannot be resumed this way and return `409`.

### POST /api/v1/sessions/batch-delete

Delete multiple sessions at once.

```bash
curl -X POST http://localhost:8000/api/v1/sessions/batch-delete \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"session_ids": ["sess_abc123", "sess_def456"]}'
```

### POST /api/v1/sessions/{session_id}/archive

Archive a session.

### PATCH /api/v1/sessions/{session_id}/active

Set whether a session is in the Active group. Send a JSON body with the new value; the endpoint returns the updated session. Passing `false` settles the session (it moves to Settled); passing `true` moves it back to Active.

```bash
curl -X PATCH http://localhost:8000/api/v1/sessions/sess_abc123/active \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"is_active": false}'
```

## Background Tasks

### POST /api/v1/sessions/{session_id}/tasks/{task_id}/stop

Stop one background task (a background agent or a background shell) while the rest of the session keeps running. Task ids are listed in `activeTasks` from `GET /api/v1/sessions/{session_id}/live`.

**Auth:** Required

```bash
curl -X POST http://localhost:8000/api/v1/sessions/sess_abc123/tasks/task_123/stop \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{ "session_id": "sess_abc123", "task_id": "task_123", "ok": true, "kind": "shell" }
```

A task that had already finished returns `"ok": true` with `"detail": "already finished"`, so a stale row in the UI clears. A task the app can no longer reach is marked stopped and also returns `"ok": true`. `"ok": false` (still `200`, with a `detail` message) means the stop did not fully work: a background shell whose child processes outlived it, or a task the app cannot find (`detail` is `session not active`, `session not connected` or `task not found`).

## Moving Sessions Between Devices

When several devices share one account, each session runs on one device at a time. These endpoints change which device runs it.

### POST /api/v1/sessions/{session_id}/adopt

Run the session on this device from now on (**Continue here** in the device menu). The body is optional.

**Auth:** Required

```bash
curl -X POST http://localhost:8000/api/v1/sessions/sess_abc123/adopt \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{ "session_id": "sess_abc123", "instance_id": "...", "adopted": true, "status": "adopted" }
```

`status` is `adopted`, or `already_local` (nothing changed, `adopted: false`). Send `{"follow": true}` to take the session without pinning it here, so a later message from another device moves it again; `status` is then `unpinned`. Returns `409` while the session is running on another device.

### POST /api/v1/sessions/{session_id}/move

Send the session to another device. Its next turns run there. `target_instance_id` is a device `id` from `GET /api/v1/cluster/installs`.

**Auth:** Required

```bash
curl -X POST http://localhost:8000/api/v1/sessions/sess_abc123/move \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"target_instance_id": "target-device-id"}'
```

**Response:**
```json
{ "session_id": "sess_abc123", "instance_id": "target-device-id", "moved": true, "status": "moved", "pending_live_run": false }
```

`status` is `moved`, or `already_there` (`moved: false`) when the device already runs it. Returns `404` for an unknown device and `409` when the device is offline, the session's project is not checked out there, or a turn is running.

## Session Sync

### GET /api/v1/sessions/{session_id}/sync

Polling fallback for WebSocket. Get changes since a sync ID.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `since_id` | integer | Return changes after this sync ID (default: 0) |

## Export & Import

### GET /api/v1/sessions/{session_id}/export

Export a session.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `format` | string | "markdown", "json", or "jsonl" (default: markdown) |

```bash
# Export as Markdown (default)
curl "http://localhost:8000/api/v1/sessions/sess_abc123/export?format=markdown" \
  -H "Authorization: Bearer aih_your_key" \
  -o session.md

# Export as JSON
curl "http://localhost:8000/api/v1/sessions/sess_abc123/export?format=json" \
  -H "Authorization: Bearer aih_your_key" \
  -o session.json

# Export the raw Claude session history (JSONL)
curl "http://localhost:8000/api/v1/sessions/sess_abc123/export?format=jsonl" \
  -H "Authorization: Bearer aih_your_key" \
  -o session.jsonl
```

### POST /api/v1/sessions/import

Import a session from JSON file.

**Auth:** Required

### POST /api/v1/sessions/import-jsonl

Import a Claude Code CLI JSONL session.

**Auth:** Owner only

## Session Forking

### POST /api/v1/sessions/{session_id}/fork

Fork a session at a specific message, creating a branch.

```bash
curl -X POST http://localhost:8000/api/v1/sessions/sess_abc123/fork \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"message_index": 5}'
```

## Worktree Sessions

### POST /api/v1/sessions/with-worktree

Create a session linked to a git worktree for isolated development.

**Auth:** Required

For an API key with assigned agents, `profile_id` must be one of them (403 otherwise). When it is omitted the key's only agent is used, or 400 if it has several; the project's default agent is not used.

```bash
curl -X POST http://localhost:8000/api/v1/sessions/with-worktree \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "proj123",
    "profile_id": "your-agent-id",
    "branch_name": "feature/new-feature",
    "create_new_branch": true,
    "base_branch": "main"
  }'
```
