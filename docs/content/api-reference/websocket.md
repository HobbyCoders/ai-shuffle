<!--
title: WebSocket
description: Real-time WebSocket endpoints for chat control and sync
order: 4
-->

# WebSocket API

Real-time bidirectional communication for chat control: starting and stopping turns, permission prompts and questions. Message content (assistant text, thinking, tool calls and results) does not travel on these sockets. It streams over the session event feed, `GET /api/v1/sessions/{session_id}/events` (see [Sessions](/docs/api-reference/sessions)).

## Chat WebSocket

### WS /api/v1/ws/chat

Primary WebSocket connection for chat. It starts and stops turns, carries permission requests and questions, and keeps several devices on the same session in step.

There is no `session_id` path parameter — the session is selected per message. Send a `query` with a `session_id` to continue an existing session, or omit it to create a new one (the server replies with `session_created`). Authentication is via the `token` query parameter or a session cookie.

**Connection:**
```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/ws/chat?token=aih_your_key');

ws.onopen = () => {
  console.log('Connected');
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Received:', data.type, data);
};
```

**Sending a message:**
```javascript
ws.send(JSON.stringify({
  type: 'query',
  prompt: 'Help me fix this bug',
  session_id: 'sess_abc123',   // omit to start a new session
  profile: 'your-agent-id',   // an agent id from GET /api/v1/agents
  project: 'my-project'
}));
```

### Message Types (Server → Client)

| Type | Description | Key Fields |
|------|-------------|------------|
| `user_streaming_sessions` | Sent once on connect: your sessions that are running a turn now (for an API key, its own sessions) | `sessions` (each with `session_id`, `project_id`, `worktree_id`, `worktree_branch`), `session_ids` |
| `session_attention_snapshot` | Sent once on connect: your sessions with a prompt still waiting for an answer | `sessions` (each with `session_id`, `count`, `kind` of `permission` or `question`) |
| `session_attention` | A session's count of waiting prompts changed (sent to every device of the owner) | `session_id`, `count`, `kind` |
| `history` | Message history for a session (on `load_session`) | `session_id`, `session`, `messages`, `isStreaming` |
| `session_created` | New session created for an omitted `session_id` | `session_id`, `title`, `request_id` (echoed if sent) |
| `tool_use` | A tool call started (for tracking pending tools) | `session_id`, `id`, `name`, `input` (replaced by a truncation marker when over 4 KB) |
| `tool_result` | A tool call finished | `session_id`, `tool_use_id`, `name`, `output`, `is_error` |
| `permission_request` | A tool needs approval | `session_id`, `request_id`, `tool_name`, `tool_input`, `queue_position`, `queue_total` |
| `user_question` | The agent asks the user a question | `session_id`, `request_id`, `tool_use_id`, `questions` |
| `done` | Turn complete | `session_id`, `metadata` (`total_cost_usd`, `tokens_in`, `tokens_out`, `model`, `duration_ms`, ...) |
| `interrupted` | Turn ended by an interrupt | `session_id`, `message` |
| `interrupt_confirmed` / `interrupt_failed` | Reply to `interrupt_for_query` | `session_id`, `message` (on failure) |
| `query_queued` | The `query` was queued behind a running turn (or handed to the device that runs the session) instead of starting now | `session_id` |
| `session_not_found` | Reply to `load_session` for an unknown session (and, for an API key, for another principal's session) | `session_id` |
| `session_closed` | Reply to `close_session` | `session_id` (null when no session was selected) |
| `permission_response_ack` | Reply to `permission_response`. `result` is `{"resolved": true, "auto_resolved_count", "auto_resolved_ids"}` on success (plus `"deferred": true` for a deferral), or `{"error": "Request not found"}` when the request is unknown or already answered | `session_id`, `request_id`, `result` |
| `user_question_response_ack` | Reply to `user_question_response` | `session_id`, `request_id`, `success`, `reason` (`not_pending` when the question was already answered) |
| `pending_permissions` | Reply to `get_pending_permissions` | `session_id`, `pending`, `deferred` |
| `pending_questions` | Reply to `get_pending_questions` | `session_id`, `questions` |
| `tabs_sync` | Another of your devices sent `tabs_update` | `tabs`, `activeTabId`, `source_device_id` |
| `permission_queue_update` | The permission queue changed. Three forms: deferred requests were put back in the queue (`requeued_ids`); a request was deferred (`resolved_ids` and `deferred_ids`, both holding its id); a remembered rule answered other waiting requests (`resolved_ids`) | `session_id`, `remaining_count`, plus `requeued_ids`, or `resolved_ids` and `deferred_ids`, or `resolved_ids` |
| `sync` | Multi-device update. With `event_type: "queue_updated"`, `data.queued_messages` is the session's current queue | `event_type`, `session_id`, `data` |
| `error` | Error occurred | `message`, `session_id` (when the error concerns a session) |
| `ping` | Keep-alive, every 30 seconds; answer with `pong` | `timestamp` (always null) |

Every server frame also carries `_seq`, a number that increases by one per frame on the connection. It is a delivery diagnostic: a gap means a frame was dropped or replaced by a newer one of the same kind. Recover state from the event feed and the pending lists, not from the socket.

Other devices watching the same session receive the same control frames. Content reaches every device through the session event feed.

### Message Types (Client → Server)

| Type | Description | Key Fields |
|------|-------------|------------|
| `query` | Start a new query (creates a session if `session_id` is omitted) | `prompt`, `session_id`, `profile`, `project` |
| `queue_message` | Queue a message to send after the current stream completes | `content`, `session_id`, `profile`, `project` |
| `remove_queued_message` | Remove one queued message | `message_id`, `session_id` |
| `clear_queued_messages` | Remove every queued message for the session | `session_id` |
| `load_session` | Load/switch to a session and receive its history | `session_id` |
| `watch_session` | Follow a session without loading its history. The server sends its queue, if any, as a `sync` frame | `session_id` |
| `close_session` | Stop following the current session; replies `session_closed` (with a null `session_id` when none was selected) | — |
| `stop` | Stop the current turn, its background work and queued messages | `session_id`, `turn_id` (optional) |
| `permission_response` | Approve, deny or defer a tool use | `request_id`, `session_id`, `decision`, `remember`, `pattern` |
| `get_pending_permissions` | List open and deferred permission requests for the current session | — |
| `requeue_deferred_permissions` | Send deferred permission requests again as new `permission_request` frames | `session_id` |
| `user_question_response` | Answer a `user_question` | `request_id`, `session_id`, `answers`, `dismissed`, `skipped` |
| `get_pending_questions` | List unanswered questions for the current session | — |
| `interrupt_for_query` | Interrupt the running turn so a new `query` can follow; replies `interrupt_confirmed` or `interrupt_failed` | `session_id` |
| `tabs_update` | Share this device's open tabs with your other devices, which receive `tabs_sync`; no reply | `tabs_data` (`tabs`, `activeTabId`) |
| `pong` | Keep-alive response to a `ping` | — |

Every client frame is a JSON object with a string `type`. A frame with an unknown `type` is answered with an `error` frame (`Unknown message type: ...`), and so is one whose `type` is missing or not a string (`Missing message type`). A frame that is not a JSON object (invalid JSON, an array, a string, a binary frame) gets `Invalid message: expected a JSON object`. The connection stays open, but the 11th frame on a connection that is not a JSON object closes it with code `1007`. A frame may be at most 10 MiB; a larger one closes the connection (code `1009`).

A connection may send 30 frames in any 10 seconds (`pong` does not count). A frame over the limit is not processed and is answered with `{"type": "error", "message": "Rate limited: too many messages. Please slow down."}`; the connection stays open.

An API key acts only on its own sessions, the ones it started. Every frame that names a session (`query` or `queue_message` with a `session_id`, `watch_session`, `stop`, `interrupt_for_query`, `permission_response`, `requeue_deferred_permissions`, `user_question_response`, `remove_queued_message`, `clear_queued_messages`) is refused with an `error` frame when the session belongs to the owner or another key, or is outside the key's project or agents. `interrupt_for_query` replies `interrupt_failed` instead. The refusal carries `session_id` and `message` (`Access denied to this session`; for the key's own session whose agent is no longer one of its agents, the message names that agent instead) and changes nothing; it is the WebSocket equivalent of the REST 403. A session id that does not exist gets the same refusal, and `load_session` answers another principal's session with `session_not_found`, as it does an unknown id, so neither reveals whether a session exists. The owner's connection is not limited this way.

For an API key with assigned agents, `query` and `queue_message` apply the same agent restriction as the REST endpoints. On `query`, a `profile` outside the key's agents, or an omitted one when the key has several, is refused with an `error` frame and nothing runs. On `queue_message` a named `profile` must be one of the key's agents; an omitted one keeps the session's agent, which must be one of them too.

### Permission Handling

When the agent wants to use a tool that requires approval:

```javascript
// Server sends permission request
// { type: "permission_request", session_id: "sess_abc123", tool_name: "Bash", tool_input: { command: "rm -rf /tmp/old" }, request_id: "req_123" }

// Client approves or denies via a "decision" of "allow" or "deny".
// "remember" optionally persists the choice: "none", "session", or "profile" (the agent).
// On an API key's connection "profile" is kept to the session, like "session".
// "pattern" optionally narrows a remembered rule.
ws.send(JSON.stringify({
  type: 'permission_response',
  session_id: 'sess_abc123',
  request_id: 'req_123',
  decision: 'allow',
  remember: 'none'
}));
```

An agent-wide (`"profile"`) rule applies to every session on that agent, so only the owner can create one. An API key that sends `remember: "profile"` gets a rule for its own session only, the same as `"session"`. A `decision` of `"defer"` sets the request aside without answering it; `remember` and `pattern` are ignored. Deferred requests come back in `pending_permissions.deferred`, and `requeue_deferred_permissions` sends them again. The server confirms every answer with `permission_response_ack`.

`get_pending_permissions` and `get_pending_questions` act on the session selected by the last `load_session` or `watch_session` on this socket. Send them after reconnecting to restore prompts that are still open. With no session selected, each replies with empty lists and a null `session_id`.

`requeue_deferred_permissions` sends each deferred request again as a `permission_request`, then one `permission_queue_update` with the `requeued_ids` (empty when nothing was deferred) and the `remaining_count` of open requests.

### Answering Questions

A `user_question` frame carries `questions`. Reply with `answers`, an object keyed by each question's `question` text. List questions the user skipped (by the same key) in `skipped`, or send `"dismissed": true` to close the prompt without answering.

```javascript
ws.send(JSON.stringify({
  type: 'user_question_response',
  session_id: 'sess_abc123',
  request_id: 'req_456',
  answers: { 'Which database?': 'PostgreSQL' }
}));
```

### Message Queue

A `query` sent while a turn is running is queued and answered with `query_queued`. `queue_message` adds to the same queue explicitly. After every change the server sends the whole queue:

```json
{
  "type": "sync",
  "event_type": "queue_updated",
  "session_id": "sess_abc123",
  "data": {
    "queued_messages": [
      { "id": "turn_789", "session_id": "sess_abc123", "content": "Also update the tests", "profile_id": "your-agent-id", "project_id": "my-project", "position": 0, "created_at": "2025-01-15T10:36:00Z" }
    ]
  }
}
```

Remove an entry with `{"type": "remove_queued_message", "message_id": "turn_789", "session_id": "sess_abc123"}`, or all of them with `clear_queued_messages`. A queued message the running turn has already read cannot be removed; the next `queue_updated` shows what is left.

A `query` queued behind a running turn is also offered to that turn, which can read it at its next step instead of waiting for the turn to end. It stays in the queue until the turn reads it, and removing it before then takes it back. A message the running turn cannot take (one for a different agent, model, effort, permission mode or set of `workspace_project_ids`, or any message on an agent backend that cannot accept messages mid-turn) runs as its own turn after the current one. For a session that runs on another device, the message is forwarded without `workspace_project_ids` and always keeps the session's own workspace folders, so the device you type on cannot add or remove folders.

### Connection Lifecycle

1. **Connect** — open the socket with a `token` query param or session cookie
2. **Authenticate** — session cookie or API key validated on connection
3. **Select a session** — send `query` (with or without a `session_id`) or `load_session`
4. **Receive content** — assistant text, thinking and tool output stream over `GET /api/v1/sessions/{session_id}/events`. The socket carries control frames: `tool_use`/`tool_result`, `permission_request`, `user_question`, then `done`, `interrupted` or `error`
5. **Handle permissions** — approve/deny tool use as needed
6. **Reconnect** — send `load_session` to receive a fresh `history`, and reopen the event feed from the last `seq` you saw

## Sync WebSockets

Two additional endpoints power multi-device synchronization:

- **WS /api/v1/ws/sessions/{session_id}** — per-session sync stream for a single session.
- **WS /api/v1/ws/global** — global sync stream for user-level events across sessions.

## Connection Parameters

| Parameter | Description |
|-----------|-------------|
| `token` | Query parameter — API key (e.g. `?token=aih_your_key`) or owner token. Optional with cookie auth on `/ws/chat` and `/ws/global`; required on `/ws/sessions/{session_id}` |
| `device_id` | Query parameter — unique device identifier for multi-device sync. Optional on `/ws/chat`; required on `/ws/sessions/{session_id}` and `/ws/global` |
| `session_id` | Path parameter on `/ws/sessions/{session_id}` (not on `/ws/chat` or `/ws/global`) |

Authentication is performed via:
- Session cookie (for the owner's web UI)
- API key or owner token in the query string: `?token=aih_your_key`

A disabled API key fails authentication (`4001`). An open socket's key is checked again every 30 seconds whether or not the client sends anything (on `/ws/chat` also before every frame is acted on), so a key that is disabled, deleted or rotated while connected loses its sockets with `4001`: at once when the change is made through the API users routes, otherwise within about 30 seconds. Paired devices (the Android app, the mobile shell) are watched the same way: unpairing a device (`DELETE /api/v1/pairing/clients/{client_id}` or `DELETE /api/v1/mobile/devices/{device_id}`) closes every socket it holds with `4001` at once, and so does the device logging out (`POST /api/v1/auth/logout`); a device revoked another way or one whose session token is replaced loses them within about 30 seconds. This covers every socket kind: `/ws/chat`, `/ws/sessions/{session_id}`, `/ws/global`, the browser panel socket, the voice audio socket and streaming (realtime) transcription; the voice and transcription sockets also drop their provider connection. A check that cannot complete (a database or account-service outage) is retried and never closes a live socket.

## Error Handling

WebSocket close codes:
- `1000` — Normal closure
- `1007` — Too many frames that were not JSON objects (`/ws/chat`)
- `1009` — Frame larger than 10 MiB
- `1013` — Authorization temporarily unavailable; retry later
- `4001` — Authentication failed, the API key was disabled, deleted or rotated while connected, or the paired device was unpaired, logged out or had its session token replaced
- `4003` — Access denied: for an API key, the session is not one it started, or it belongs to another project or agent (`/ws/sessions/{session_id}`)
- `4004` — Session not found
