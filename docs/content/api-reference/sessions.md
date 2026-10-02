<!--
title: Sessions
description: Chat session management, search, export, and import endpoints
order: 2
-->

# Sessions API

Endpoints for managing chat sessions — listing, searching, creating, updating, deleting, exporting, and importing.

An API key sees and acts only on the sessions it started (see [Own Sessions](/docs/api-reference/api-users#own-sessions)). A route that takes a `session_id` returns `403` for the owner's session, another key's, or one outside the key's project or agents, and `404` for an id that does not exist. The owner can use every session.

## Read the Suggested Next Reply

### GET /api/v1/sessions/{session_id}/composer-suggestion

**Auth:** Required, with the same session access checks as session detail.

Returns `{ "session_id": "…", "turn_id": "…", "suggestion": "…" }`, or `null` when no current eligible suggestion is available. This read never calls a model.

With **Suggest next reply** enabled (**Settings → General**), the latest generated hint is saved per code session, even if no browser is watching. The empty composer loads it on return/reconnect and after a turn completes. Switching sessions or reloading does not regenerate it. A newer turn, a cleared/rewound answer, or disabling the feature prevents the stale hint from being shown. An existing draft is never replaced. Pressing Tab or double-clicking/double-tapping the empty composer accepts the gray hint into the draft; it does not send the message. A single click/tap only focuses the input. Enter does not accept the hint. Escape dismisses it for that browser's current visit.

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
| `api_user_id` | string | Only the sessions of this API user |
| `active_only` | boolean | Only active-group sessions |
| `limit` | integer | Max results (default: 50, max: 100) |
| `offset` | integer | Pagination offset |

```bash
curl "http://localhost:8000/api/v1/sessions?project_id=proj123&limit=10" \
  -H "Authorization: Bearer aih_your_key"
```

For an API key the list holds only its own sessions: `admin_only`, `api_users_only` and `api_user_id` are ignored, `project_id` is replaced by the key's project when it has one, and a `profile_id` outside the key's agents returns `403`. Search applies the same limits.

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

`queued` is `true` when the retry waits behind a running turn. If a retry is already queued, that turn is returned with `"duplicate": true`. Claude Code sessions cannot be resumed this way and return `409`. When the provider cut the last reply off mid-stream, that partial reply stays in the transcript marked interrupted. If no tool call in it finished before the error, the retry starts again from the user's message and the partial reply stays on a side branch; if one did, the partial reply and its tool calls stay on the re-sent path, and the model reads them and the calls' error results before it continues.

### POST /api/v1/sessions/batch-delete

Delete multiple sessions at once.

```bash
curl -X POST http://localhost:8000/api/v1/sessions/batch-delete \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"session_ids": ["sess_abc123", "sess_def456"]}'
```

Returns `{"deleted_count": 1, "total_requested": 2, "errors": [...]}` (`errors` is `null` when every session was deleted). A session that does not exist or that the caller may not delete is skipped and listed in `errors`.

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

### PUT /api/v1/sessions/{session_id}/workspace-folders

Set the extra project folders a code or voice session can work in, besides its own project. The list replaces the previous one.

**Auth:** Required. Every listed project must be one the caller can access.

```bash
curl -X PUT http://localhost:8000/api/v1/sessions/sess_abc123/workspace-folders \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"workspace_project_ids": ["proj_def456"]}'
```

`workspace_project_ids` (required) holds up to 50 project ids; duplicates and the session's own project are dropped. Returns `{"workspace_project_ids": [...]}` as saved. Returns `400` for a chat-mode session and `404` when a newly added project does not exist.

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

## Rewind

### GET /api/v1/commands/rewind/checkpoints/{session_id}

List the messages a session can be rewound to.

**Auth:** Required

Returns `success`, `session_id`, `error` and `checkpoints`, each with `uuid`, `index`, `role` (`user` or `assistant`), `message_preview`, `full_message` and `timestamp`.

### POST /api/v1/commands/rewind/execute/{session_id}

Rewind the conversation to a message: later messages are dropped from what the agent sees on its next turn.

**Auth:** Required

```bash
curl -X POST http://localhost:8000/api/v1/commands/rewind/execute/sess_abc123 \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"target_uuid": "5f1c...", "target_role": "user", "include_response": true}'
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `target_uuid` | string | `""` | `uuid` of the checkpoint to rewind to |
| `target_role` | string | `user` | `user` or `assistant` |
| `include_response` | boolean | `true` | Keep the reply to the target message; `false` also drops the target message |
| `restore_chat` | boolean | `true` | Rewind the conversation |
| `restore_files` | boolean | `false` | Not supported: reported in `error` while the conversation still rewinds |
| `target_content` | string | | With `target_occurrence`, finds a message that has no `uuid` yet by its text |
| `target_occurrence` | integer | | 0-based position among the user messages with that exact text |

Returns `success`, `message`, `chat_rewound`, `files_rewound`, `messages_removed`, `error`, `partial_success` and `warning`. When the target cannot be found, the rewind is refused with `success: false` and `error`.

## Swarms

A swarm is a set of tasks an agent hands to other agents, some waiting on others. These two endpoints use the `/api/sessions` prefix, without `/v1`.

**Auth:** Required. An API key must own the session.

### GET /api/sessions/{session_id}/swarms

```json
{
  "swarms": [
    {
      "id": "swarm_9c1e...",
      "session_id": "sess_abc123",
      "title": "Audit the API",
      "status": "running",
      "created_at": "...",
      "updated_at": "...",
      "tasks": [
        { "id": "scan", "title": "Scan routes", "agent": "Explore", "depends_on": [], "status": "completed", "worker_session_id": "...", "result": "...", "error": null, "started_at": "...", "finished_at": "..." }
      ]
    }
  ]
}
```

`status` (swarm and task) is `queued`, `running`, `completed`, `failed`, `cancelled` or `interrupted`.

### POST /api/sessions/{session_id}/swarms/{swarm_id}/cancel

Cancel a swarm and its unfinished tasks. Returns `{"swarm": {...}}`, `404` for an unknown swarm, or `409` when this worker cannot control it.

## Private Access

When an agent asks for access to a folder on this worker, a machine over SSH, or an HTTPS API service, the owner answers here. Access is temporary, lasts until it expires or the worker restarts, and each write, command or state-changing API request the agent attempts needs a separate approval (unless the session is in Bypass).

**Auth:** Owner only, and only from the desktop app window, the worker's HTTPS remote access address, or a browser on the machine running a worker the desktop app did not start. Other connections get `403`, including plain `http://` LAN addresses and `X-Forwarded-Proto` headers sent to port 8000 (see [Remote access](/docs/getting-started/remote-access#private-access-details)). Refused (`403`) for agent previews.

### GET /api/v1/chat-access/{session_id}

Returns `requests` (open asks: `id`, `kind` (`folder`, `machine` or `service`), `purpose`, `expires_at`), `grants` (`id`, `label`, `kind`, `target`, `expires_at`, `writable`, `fingerprint`), `actions` (actions waiting for approval: `id`, `grant_id`, `operation` (`write`, `command` or `http`), `arguments`, `expires_at`), `saved_connections` (`id`, `kind`, `label`, `target`, `fingerprint`; never a secret), `vault_available`, `folder_available` and `restart_revokes`.

### POST /api/v1/chat-access/{session_id}/requests/{request_id}

Grant a request, with a new connection or a saved one (`saved_id`), not both.

```json
{
  "connection": {
    "kind": "machine",
    "label": "Build server",
    "host": "192.0.2.10",
    "port": 22,
    "username": "deploy",
    "fingerprint": "SHA256:...",
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----...",
    "writable": false
  },
  "minutes": 60,
  "save_connection": false
}
```

`connection.kind` must match the request. For `folder`, send `path` instead of the SSH fields. For `service`, send `url` (an `https://` address, optionally with a path prefix), `secret` (the API key or token, 8 to 8,192 visible characters), `auth_header` (default `Authorization`), `auth_scheme` (default `Bearer`; empty sends the secret alone, for headers such as `X-API-Key`) and `writable` (allow POST, PUT, PATCH and DELETE). The worker completes a verified TLS handshake before granting; self-signed certificates are refused and no request or credential is sent at grant time. `label` is 1 to 80 characters; `minutes` is 1 to 480 (default 60); `fingerprint` (`SHA256:...`) is optional; without it the server's key is pinned on first connect. `password`, `private_key`, `passphrase` and `secret` are never returned. Returns `{"granted": true, "saved": false}`. Connection failures return `400` with a message; secrets are never echoed.

### DELETE /api/v1/chat-access/{session_id}/requests/{request_id}

Decline a request. Returns `{"declined": true}`.

### DELETE /api/v1/chat-access/{session_id}/grants/{grant_id}

End a grant now. Returns `{"revoked": true}`.

### POST /api/v1/chat-access/{session_id}/actions/{action_id}

Approve or refuse one pending write, command or API request. Body: exactly `{"allow": true}` or `{"allow": false}`. Returns `{"accepted": true}`.

### DELETE /api/v1/chat-access/{session_id}/saved/{connection_id}

Forget a saved connection and end any grant that uses it. Returns `{"deleted": true}`.

A request, grant or action that no longer exists returns `409` (`400` when granting).

For a session that runs on another device, these endpoints forward to that device over the worker network, and grants, approvals and saved connections stay there. A new connection with a password, key or API secret is sent only when the other device is reached over HTTPS; otherwise the grant returns `409` and you can choose a saved connection or enter it on that device. While the other device is unreachable, `GET` returns an empty snapshot and changes return `503`.

## Browser Panel

### POST /api/v1/sessions/{session_id}/browser/panel/start

Start the session's shared browser (the Browser panel) without navigating it or taking control from the agent. No body.

**Auth:** Required

Returns the browser state: `runtime_id`, `revision`, `available`, `busy`, `target_id`, `active_tab_id`, `tabs` (each with `id`, `url`, `title`, `current`), `viewport` and `control` (`owner` is `agent` or `human`). Returns `503` with a reason when the browser cannot start.

## Moving Sessions Between Devices

When several devices share one account, each session runs on one device at a time. These endpoints change which device runs it.

### POST /api/v1/sessions/{session_id}/adopt

Run the session on this device from now on (**Continue here** in the device menu). The body is optional.

Plain chat sessions with no project move by conversation history alone. For a session in a project, cross-worker resume requires a Git project with a single `origin` remote pointing to the same repository on both workers. GitHub and other Git hosts are supported. Both workers must be online, and the receiving worker must already have a clone of the project.

After confirmation, the owning worker commits all non-ignored changes (including new files and deletions) and pushes the session branch without force. The receiving worker fetches that branch, creates a worktree or fast-forwards an existing clean branch checkout, then copies the conversation and native session context. Only after these steps succeed does ownership move. Ignored files, dependencies and local credentials are not copied.

There is no AI Shuffle file-bundle or total-context size cap for this handoff. Repository data travels through Git rather than the worker-to-worker response; Git-host limits and network timeouts still apply. Both workers need a version that supports Git-based resume.

Returns `409` for missing or mismatched remotes, an offline or busy owner, rejected pushes, dirty or divergent receiving checkouts, and unsupported Git states (conflicts, an unfinished operation, submodules, sparse or assume-unchanged files). Unignored `.env`, `.pem` and `.key` files also block automatic publication. Ownership stays on the original worker if a step fails. A commit or push that already succeeded is **not undone**; resolve the reported problem and retry.

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
| `share_safe` | boolean | Filter recognized credentials from the export only (default: false). Does not change stored history. |

**Private backup:** The chat header's **Download private backup (.jsonl)** preserves raw history for backup/re-import. It may contain secrets; keep it private. Existing requests without `share_safe=true` retain their original behavior.

**Sharing:** Choose **Export for sharing (.md)** to remove recognized secrets from a separate Markdown download. Review all content before posting: this is defense in depth, not arbitrary-secret detection, and is not a raw backup. Sharing files end in `-share-safe.md` (or `.json`/`.jsonl` for those API formats). Responses include `X-Transcript-Privacy: share-safe` or `private-raw`. Malformed raw JSONL returns `422` for sharing exports, never an unfiltered fallback.

```bash
# Export as Markdown (default)
curl "http://localhost:8000/api/v1/sessions/sess_abc123/export?format=markdown" \
  -H "Authorization: Bearer aih_your_key" \
  -o session.md

# Export as JSON
curl "http://localhost:8000/api/v1/sessions/sess_abc123/export?format=json" \
  -H "Authorization: Bearer aih_your_key" \
  -o session.json

# Export for sharing (recognized credentials filtered; review before posting)
curl "http://localhost:8000/api/v1/sessions/sess_abc123/export?format=markdown&share_safe=true" \
  -H "Authorization: Bearer aih_your_key" \
  -o session-share-safe.md

# Private raw backup (JSONL); may contain secrets, do not share
curl "http://localhost:8000/api/v1/sessions/sess_abc123/export?format=jsonl" \
  -H "Authorization: Bearer aih_your_key" \
  -o session.jsonl
```

### POST /api/v1/sessions/import

Import a session from JSON file.

**Auth:** Required

The imported session belongs to the caller. For an API key, an export that names an agent outside the key's assigned agents returns `403`, and a key limited to one project files the session under that project (an export naming another project returns `403`). For the owner, and for an API key with no project limit, an export naming a project this install does not have is imported without a project.

Errors: `400` for a file whose name does not end in `.json`, invalid JSON, JSON that is not an object, a missing `session` or `messages` field, a `session` that is not an object or `messages` that is not an array, a message that is not an object, a `title`, `profile_id` or `project_id` that is not a string, a `total_cost_usd`, `total_tokens_in`, `total_tokens_out` or `turn_count` that is not a number, or a message the transcript cannot hold (the `detail` names the message by number: `metadata` or `metadata.timestamp` of the wrong type, a `role`, `type`, `subtype`, `toolName`, `tool_name`, `agentType` or `agentDescription` that is not a string, an `id`, `toolId` or `tool_id` that is neither a string nor a number, or `content` that is neither a string nor an array); `413` for a file over 50 MB. These are checked before anything is saved. `500` with "Could not write the imported transcript; nothing was imported." when the transcript cannot be written; the new session is removed again, so nothing is left behind.

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

## Create an Empty Session

### POST /api/v1/sessions

Create a session without a first message, on this install. The composer's call button uses it: it creates the conversation from the current picks and then starts a call on it. Send the first message later over the chat WebSocket with the returned `session_id`.

**Auth:** Required

All fields are optional. `project_id` or `project_v2_id` (not both) files the session under a project; with neither it is projectless. `mode` is `code` (default) or `chat`; `voice` is refused with 400. `worktree_id` runs the session in an existing worktree of `project_id`. `overrides` takes the same `model`, `effort`, `permission_mode`, `context_window`, `fast` and `workspace_project_ids` as a query and stores them as the session's run settings. Agent rules match `with-worktree` below.

```bash
curl -X POST http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "proj123",
    "profile_id": "your-agent-id",
    "overrides": {"model": "sonnet", "permission_mode": "default"}
  }'
```

Returns `{"session_id": "...", "mode": "code"}`.

## Worktree Sessions

### POST /api/v1/sessions/with-worktree

Create a session linked to a git worktree for isolated development.

**Auth:** Required

For an API key with assigned agents, `profile_id` must be one of them (403 otherwise). When it is omitted the key's only agent is used, or 400 if it has several; the project's default agent is not used. With a `prompt`, `overrides` apply to the first turn; without one they are stored as the session's run settings.

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
