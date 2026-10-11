<!--
title: Sessions
description: Chat session management, search, export, and import endpoints
order: 2
-->

# Sessions API

Endpoints for managing chat sessions — listing, searching, creating, updating, deleting, exporting, and importing.

AI Shuffle no longer ships a default built-in agent. For ordinary session creation, supply an existing agent ID or configure a project default. Without either, the API selects the first configured agent; with no agents it returns `409` and asks you to create one. Install the optional starter team or create an agent first. Starter-team IDs are newly generated, not fixed IDs such as `general-purpose`.

An API key sees and acts only on the sessions it started (see [Own Sessions](/docs/api-reference/api-users#own-sessions)). A route that takes a `session_id` returns `403` for the owner's session, another key's, or one outside the key's project or agents, and `404` for an id that does not exist. The owner can use every session.

## User-message revision

Session list (including paginated/cursor lists) and detail responses include
`last_user_message_at`: a nullable, timezone-aware UTC timestamp, and
`last_user_message_seq`: the nullable integer event sequence of the latest
qualifying authoritative `message.user` event. `last_user_message_epoch` is its
nullable string event-log identity (the same lowest-event ID as the SSE log epoch).
The timestamp advances
when a user message is persisted, including the initial prompt, a delivered
mid-turn steer, a command, or a voice utterance. REST conversations, connector
messages, forwarded prompts on the executing worker, and delivered
owner-configured automation prompts also count. Autonomous agent notes,
wakes, and backend-authored voice notes do not count, even when their transport
role is `user`; their source provenance is preserved in both message stores.

Queuing alone does not advance it. The authoritative event-log insertion and
the legacy message insertion both update the session revision in their own
transaction. Retrying an already committed stable event ID does not advance
it or rebroadcast an answer. Event-log backfill/copy paths do not advance it.
Existing sessions keep `null` until their next user message; it is not
backfilled from turn counts or working time. Authoritative event inserts stamp
all three fields atomically, using the last newly inserted qualifying user event's
sequence (not the batch's final event). Stable-ID retries retain the original
sequence and epoch; rollbacks change none of them. Legacy-only message writes
update the timestamp but preserve the event sequence/epoch, which remain `null`
if never stamped.

After commit, the global WebSocket feed emits `user_session_updated` with
`session_id`, `updates.last_user_message_at`, `updates.last_user_message_seq`,
and `updates.last_user_message_epoch`,
even when that session is not open on the receiving tab. These match the persisted
revision. Account/paired subscribers and other workers receive a session-list
invalidation and reload their authorized rows; worker replication carries all three fields.

Use event order, not wall-clock timestamps, for causal comparisons: a user answer
is later than a question only when all positions are non-null,
`last_user_message_epoch == question.epoch`, and
`last_user_message_seq > question.seq`, within the same session. Ignore lower
sequences from stale deliveries only within an equal epoch. Epochs are opaque
identities, not ordered counters. Discard old question anchors on SSE reset; do
not accept a snapshot from a different epoch as an answer to the current question.
This handles same-transaction prompts/questions and delayed pre-clear snapshots.

Event-backed detail and `/live` messages expose `seq` and `epoch`. The rendered
message `seq` is the last contributing event sequence (including folded assistant
deltas), not the first delta. Rich Fluid/store transcripts retain their IDs,
metadata and presentation; their positions are reconciled with the event
projection by stable event/tool IDs or exact text in occurrence order, before
pagination/truncation. Ambiguous occurrence counts or an unavailable event log
leave positions null rather than inventing a causal anchor. Legacy messages
without an event position have nullable `seq`/`epoch`; pagination offsets are not
event sequences. The SSE stream retains its event `seq` and supplies its log epoch
through the existing reset/caught-up protocol. Legacy WebSocket message frames
expose nullable positions too. Clearing resets `last_user_message_seq` and
`last_user_message_epoch` to `null`; the next log has a different epoch even though
its sequences restart at 1.

`last_prose_is_question` is not provided: deciding whether the last prose asks
a question still requires the replayed conversation, including tool questions,
agent-delivery metadata, and streaming state.

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
| `exclude_archived` | boolean | Leave out archived sessions (default: `false`). `has_more` then counts only the sessions you will get back |

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

When `from_seq` is `0` and the conversation has been compacted, the replay skips the compacted part, except for its durable `system.compaction_boundary` audit records. Those arrive first with their original `id`, sequence and timestamp. Then one synthetic `event: system.compaction_boundary` frame (no `id`; `data.elidedEvents` counts the skipped prefix) precedes the events from the first turn kept after compaction. A reconnect with `from_seq` above `0` replays from that point and never skips.

After the replay, the stream sends one `event: caught_up` frame (`{"seq": N}`, no `id`). Idle streams send `: keepalive` comment lines.

### GET /api/v1/sessions/{session_id}/live

Get live session state for streaming. Returns current partial messages and streaming status.

The response's `session_meta.last_user_message_at` is a nullable UTC ISO 8601
string with the same value and [user-message revision](#user-message-revision)
semantics as the session row, serialized identically to list/detail responses
(with a `Z` suffix for UTC). It is `null` until a qualifying user message is
persisted. It is included both with messages (the default) and with
`?include_messages=false`, which skips loading the transcript. Clients can use
this metadata-only snapshot to seed the revision without fetching session detail.
`session_meta.last_user_message_seq` and `session_meta.last_user_message_epoch`
accompany it in both forms, with the same nullable values as list/detail and the
session row.

### GET /api/v1/sessions/{session_id}/state

Get session state for client reconnection. The response includes the same
nullable UTC ISO 8601 `last_user_message_at` and nullable integer
`last_user_message_seq`, plus nullable string `last_user_message_epoch`, at the
top level. This metadata-only route has no transcript.

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

### PUT /api/v1/sessions/{session_id}/run-settings

Change effort or Fast mode while a chat runs, or save a context-window choice separately:

```json
{ "context_window": 200000 }
```

The chosen window must fall within the model's range: from its Standard window (the smaller entry in `context_window_choices`) up to its Extended window (the larger, the model's full context). A value in between raises the Standard cap, so the chat compacts later than the default. The response acknowledges the saved `context_window`. It takes effect on the next turn, including a wake or retry; an in-flight request is not interrupted. A saved pin stays a ceiling when the model changes. An older chat whose picker choice was only remembered in its browser needs that choice selected again to save it on the worker. Unpinned chats keep their model's default window.

### POST /api/v1/sessions/{session_id}/compact

Compact the chat's context now. This sends the chat a `/compact` turn. A Claude Code chat runs Claude Code's own `/compact`. Any other chat runs the app's compaction: older tool results are trimmed and older turns are summarized, the newest work is kept as it is, and a summarized turn can still be read back with `HistoryExpand`. The chat shows **Context Compacted** when it worked, or **Compaction failed** with the reason.

Successful manual and automatic compactions leave durable transcript entries with their timestamp, trigger and available token counts (and configured window when known). Reloading preserves earlier compaction entries even when older conversation turns are summarized. Summary text stays in recoverable history rather than the default visible row.

**Auth:** Required

```json
{ "turn_id": "...", "session_id": "sess_abc123" }
```

**Errors:** `409` while a turn runs or waits in the queue, `409` for an archived chat, `400` for a chat that cannot be compacted (voice).

### GET /api/v1/sessions/{session_id}/cache-state

Whether a message sent now would read a large context with a cold provider cache. The composer asks this before such a send and offers to compact first.

**Auth:** Required

```json
{
  "leg": "delegated",
  "context_tokens": 412000,
  "last_call_at": 1791172200.0,
  "idle_seconds": 5400.0,
  "cache_ttl_seconds": 3600.0,
  "threshold_tokens": 150000,
  "cold": true,
  "large": true,
  "warn": true
}
```

`cold` is `true` when the last model call is older than `cache_ttl_seconds`: 3600 seconds for Claude Code, which keeps its cache entries for an hour, and 300 seconds for the other providers. `large` is `true` from `threshold_tokens` (150,000) up. `warn` is both. `context_tokens` is the last context reading, or the size of the last prompt when there is none.

### GET /api/v1/sessions/{session_id}/limit-resume

The chat's scheduled resume after a usage limit (`pending`, or `null`), and the ids of the background Task workers it waits on that a usage limit paused (`workers`).

**Auth:** Required

```json
{
  "session_id": "sess_abc123",
  "pending": {"fire_at": 1791173520.4, "reset_at": 1791173400.0, "provider": "claude", "limit_key": "claude:29852890", "compact": true},
  "workers": []
}
```

### POST /api/v1/sessions/{session_id}/limit-resume/cancel

Cancel the chat's scheduled resume. With `?task_id=<worker id>` it cancels the resume of that paused worker instead; the worker is then stopped. Returns `{"session_id": "...", "task_id": null, "cancelled": true}`, with `cancelled` `false` when nothing was scheduled. Returns `404` for a `task_id` that is not a paused worker of this chat.

**Auth:** Required

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

In a worker network, this is shared sidebar state, not an execution command. Any worker holding the session can save the change for an authorized caller even when the execution owner is offline. The change is durable locally and syncs on reconnect; concurrent edits use the network's last-writer-wins order. Owner catalog updates do not overwrite this membership. Settling does not interrupt running work, change placement, or transfer ownership. Update all workers for consistent membership on every device.

Add `"archived": true` to archive the session (it always leaves Active) or `"archived": false` to restore an archived one. Archiving changes the session's status on the worker that runs it, so unlike settling it needs that worker online: an offline owner returns `503` and a refused change `409`.

In the app, archived conversations live under **Settings → Archived chats**, not in the sidebar. This page loads archived history independently of sidebar filters and provides pagination for older chats. Opening a conversation leaves it archived; **Restore** moves it to Settled. Desktop session rows expose Archive on hover or keyboard focus, and mobile rows expose it with a right swipe; a left swipe settles a row, or brings a settled one back to Active.

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

Background tasks do not survive a worker restart. When the worker starts again it marks every task the previous run left running as stopped (`status` `killed`, `reason` `server_restart`), so `activeTasks` and the sidebar no longer show it as running. The session that started those tasks gets one automatic turn listing which ones were interrupted. Processes the previous run started for agents and background shells (and the processes those started) are stopped at startup when the worker can confirm by process id, start time and command line that they are its own.

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
        { "id": "scan", "title": "Scan routes", "agent": "agent_installed_uuid", "depends_on": [], "status": "completed", "worker_session_id": "...", "result": "...", "error": null, "started_at": "...", "finished_at": "..." }
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

A main chat's snapshot also lists the requests, actions and grants of the subagents (Task and Swarm children) it started on this worker, at any depth. Each of those carries `session_id`, the subagent's session, and `subagent`, its chat title. Answer, decline, approve or revoke them at `/api/v1/chat-access/{that session_id}/…`; the main chat's own id refuses them. The app shows no private access UI in a subagent's own chat.


Service setup does not hold an agent tool open while the owner creates a token. `RequestChatAccess(kind="service")` returns immediately with `status: "awaiting_user"`, `request_id`, `kind` and a preparation `expires_at`. The private card can be reopened for 24 hours on the same running worker. Identical tool retries reuse the request without extending that deadline; cancellation, expiry, ownership change and worker restart invalidate it. Preparation grants no access. After the owner submits and confirms readiness in the conversation, the agent uses `ChatResources` to obtain the approved handle. While preparation is pending, that tool can return `pending_requests` containing only request IDs, status, kind and expiry, never setup details or tokens.

Service requests in the owner snapshot also include `status: "awaiting_user"`, `service_provider` (`cloudflare` or `custom`) and `suggested_url`. These are non-secret preparation hints, not approved scope. The model can supply a known public HTTPS API base; credentials, query strings, fragments and private addresses are not allowed as endpoint hints. The Cloudflare preset supplies `https://api.cloudflare.com/client/v4`. Unknown or private endpoints must be entered in the private card, not the conversation. Folder requests can include an editable `suggested_path`; machine and folder requests still wait up to four minutes.

**Plugin services.** When an installed plugin declares a sign-in for a service, the agent can call `RequestChatAccess(kind="service", plugin="<plugin id>", service="<service name>")` instead of giving hints. The plugin must be switched on and approved. The request in the snapshot then also carries `plugin` and `service`, and `suggested_url` is the address the plugin declares. Show the sign-in state from [GET /api/v1/plugins/oauth/status](/docs/api-reference/plugins#get-apiv1pluginsoauthstatus) and let the owner sign in there before granting. Sign-ins are stored per worker: for a session that runs on another device, sign in on that device. A grant for such a request also lists `plugin` and `service`, and its `target` is the declared address.

Tokens go only in the private card, never in chat, tool arguments or URLs. Saved connections are kept encrypted, only when the owner chooses, and each session still needs its own grant. Closing a card does not grant or cancel the request. A grant's lifetime (1 to 480 minutes) starts when the owner submits it; one-shot action approvals keep their own expiry.

For Cloudflare, use a [scoped API token](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/), not the Global API Key. Select only the Read permissions and account/zone resources needed for the requested inventory, with an appropriate provider token expiry. A read-only grant only sends GET and HEAD requests; it does not reduce the token's permissions at Cloudflare. Other services need their own documented API base and credential header.

Cloudflare [supports Authorization Code OAuth, including PKCE for public clients](https://developers.cloudflare.com/fundamentals/oauth/create-an-oauth-client/). That requires a registered client, configured redirect URLs and scopes; a publicly authorizable client also requires publisher domain verification. AI Shuffle does not ship a registered Cloudflare OAuth integration here. The guided private-token path is the available option for services no plugin declares; OAuth sign-in exists only for services an installed plugin declares (see above).

### POST /api/v1/chat-access/{session_id}/requests/{request_id}

Explicitly grant a current request, with a new connection or a saved one (`saved_id`), not both. Failed validation or connection checks leave preparation available for correction. No private input is returned or stored as a preparation draft.

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

`connection.kind` must match the request. For `folder`, send `path` instead of the SSH fields. For `service`, send `url` (an `https://` address, optionally with a path prefix), `secret` (the API key or token, 8 to 8,192 visible characters), `auth_header` (default `Authorization`), `auth_scheme` (default `Bearer`; empty sends the secret alone, for headers such as `X-API-Key`) and `writable` (allow POST, PUT, PATCH and DELETE). The worker completes a verified TLS handshake before granting; no request or credential is sent at grant time. Self-signed certificates are refused unless the owner explicitly supplies their SHA-256 certificate fingerprint in `fingerprint` through the private card. That exact certificate must still be valid and match the host; no global TLS check is disabled. `label` is 1 to 80 characters; `minutes` is 1 to 480 (default 60); `fingerprint` (`SHA256:...`) is optional; without it the server's key is pinned on first connect. `password`, `private_key`, `passphrase` and `secret` are never returned. Returns `{"granted": true, "saved": false}`. Connection failures return `400` with a message; secrets are never echoed.

A plugin service request is answered with `{"sign_in": true, "writable": false, "minutes": 60}` and no `connection` or `saved_id`; anything else returns `400`. The owner must already be signed in to that service on this worker (`400` otherwise, and the request stays open). `writable` is allowed only when the plugin declares `write` access. The grant holds no token: each `ChatHttpRequest` reads the current access token, sends it as `Authorization: Bearer`, and on a `401` refreshes the sign-in once and repeats the request once. Requests stay under the plugin's declared address, read-only and approval rules are the same as for other service grants, and the tokens are redacted from responses. If the plugin is switched off or its declaration changes, the grant stops working; if the refresh is refused, the saved sign-in is removed and the agent is told to have the owner sign in again.

### DELETE /api/v1/chat-access/{session_id}/requests/{request_id}

Decline a request. Returns `{"declined": true}`.

### DELETE /api/v1/chat-access/{session_id}/grants/{grant_id}

End a grant now. Returns `{"revoked": true}`.

### POST /api/v1/chat-access/{session_id}/actions/{action_id}

Approve or refuse one pending write, command or API request. Body: exactly `{"allow": true}` or `{"allow": false}`. Returns `{"accepted": true}`.

### DELETE /api/v1/chat-access/{session_id}/saved/{connection_id}

Forget a saved connection and end any grant that uses it. Returns `{"deleted": true}`.

A cancelled, expired, answered, restarted or ownership-invalidated request cannot grant access and returns `409`, including late or duplicate submissions. A grant or action that no longer exists also returns `409`. Correctable connection/input failures return `400`, validation failures return `422`, and worker faults return `500`; retryable errors do not extend preparation. The same grant error statuses are preserved when forwarding between updated workers.

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

### Move when this turn ends

A session that is running a turn, has messages queued behind it, has a scheduled wake-up about to fire, or is parked on background work (subagents, Monitors, background shells), cannot move until that work ends. These endpoints queue the same hand-off the agent's `HandOffSession` tool uses. They run on the worker that owns the session, whichever device you call.

| Method | Path | Body | Result |
|---|---|---|---|
| `GET` | `/api/v1/sessions/{session_id}/handoff` | none | `{"busy": {"running": bool, "tasks": [names], "queued": int, "wake": bool}, "pending": {"worker", "target_id", "by", "moving"} or null}` |
| `POST` | `/api/v1/sessions/{session_id}/handoff` | `target_instance_id` (optional; omitted means this device), `stop` (optional bool) | `{"queued": true, "worker": "DesktopWorker", "after": "turn" or "queued"}` |
| `DELETE` | `/api/v1/sessions/{session_id}/handoff` | none | `{"cancelled": bool}` |

**Auth:** Required

A queued move waits until the turn, the queued messages and the background work end, then commits, pushes and moves the session like `/move`. `after` is `queued` when queued messages run first. It waits at most 10 minutes; after that it is cancelled and the chat says what was still running. `stop: true` stops the work first, as the Stop button does.

Messages you send while a move waits are held, not run. They run on the new worker after the move, in order. If the move is cancelled, fails or runs out of time, they run on the current worker instead; if a failed move did in fact reach the new worker, they follow the session there. Held messages stay stored on the old worker until the new one has taken each of them, so a restart loses none; delivery is retried for 5 minutes, and a new worker drops a message it already took, so none runs twice. A message the new worker refuses, never takes, or (on an older release) does not confirm is named in the chat, or in a notification when the chat cannot be reached; an older worker gets each message once and is never sent it again. Messages held for a chat archived meanwhile are named the same way rather than run. A held message also cancels the session's pending wake-ups, as a sent message does.

A move the agent queued with `HandOffSession` resumes the chat on the new worker: after any held messages, one turn starts there with a short system-authored prompt naming both workers and the agent's hand-off note, so the agent continues the work. It is sent once (a resend after a lost reply or a restart is dropped by the new worker). A move you queue yourself, or one that is cancelled, fails or runs out of time, starts no turn, and neither does a move after you pressed Stop (on either worker). A new worker on an older release cannot take the resume turn; the outcome row then says so and the chat waits for your next message there.

Scheduled wake-ups, such as a resume after a usage limit, move with the session. A worker on an older release cannot keep them; the chat then names each one that will not run (once, and only after the new worker accepted the move). If a wake-up is added or changed while the move is in flight, the move is refused and can be retried.

Known limits: if the new worker stops between taking ownership and storing the moved wake-ups, those wake-ups are lost. Files attached to held messages stay on the old worker. A message sent to the new worker while the old one is still delivering held messages there can run before them. A Stop pressed on the old worker is not remembered if that worker restarts before the move's delivery finishes, so the resume turn can still start.

The session records `handoff` rows for queued, cancelled, started and the outcome (`moved`, `failed`, `timeout`), so every device shows the pending move; the `moved` row is written on the new worker, which then owns the session. For an agent's hand-off its `resume` field is `send`, or `unsupported` / `unknown` when the new worker cannot take the resume turn; a resume turn the new worker refused adds a `resume_failed` row. A queued move is stored on the owning worker, so it survives a restart of that worker; a move that cannot resume is recorded as cancelled. Returns `409` when a move is already queued, the target is unknown or offline, the owning worker cannot be reached, or an approval, question or access card is open ("Answer or dismiss the open prompt first.").

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

**Harness context:** The app adds private context to each prompt for the model, such as saved memory facts and environment details, inside `<system-reminder>` blocks. Session reads, the live event feed, Markdown and JSON exports, and sharing exports leave these blocks out of user messages. The private raw JSONL backup keeps them, because it is the exact history used for re-import. Only complete blocks in the app's own format, on their own lines and starting with one of the app's known headers, are left out; text you typed, including the literal words `<system-reminder>` inside a sentence, is kept.

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

### GET /api/v1/data-export/all

Download everything in one ZIP: every chat, every agent and the project list. This is **Settings → System → Export all data**. Owner only: API keys and paired devices without owner rights get `403`.

The ZIP is built while it downloads, one chat at a time, so a long history can take a few minutes. It holds one folder, `ai-shuffle-export-<date>-<time>/`:

| Path | Contents |
|------|----------|
| `README.txt` | What the export holds and what it leaves out. |
| `chats/<date>-<title>-<id>/chat-private.jsonl` | The chat's private raw backup, the same file as `format=jsonl` above. Missing for a chat with no saved history. |
| `chats/<date>-<title>-<id>/chat-private.md` | The same chat as Markdown, the same as `format=markdown`. |
| `agents/<agent_id>-agent.json` | The agent's export file, importable with `POST /api/v1/agents/import`. `env` is always `null`. |
| `projects.json` | The projects, as `GET /api/v1/projects` returns them. |
| `manifest.json` | `type: "ai-shuffle-export"`, the counts, and each chat's metadata, files and any `problems` (why a file is missing). |

**Never included:** AI provider sign-ins and API keys, tokens, saved credentials, app settings, the install key, `.env` files, and agents' environment variables. **Private:** chats are raw backups, so anything typed into a chat or printed by a tool is in the file. The response carries `X-Transcript-Privacy: private-raw`; keep the file private.

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
