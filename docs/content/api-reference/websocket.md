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
| `history` | Message history for a session (on `load_session`) | `session_id`, `session`, `messages`, `isStreaming` |
| `session_created` | New session created for an omitted `session_id` | `session_id`, `title`, `request_id` (echoed if sent) |
| `tool_use` | A tool call started (for tracking pending tools) | `session_id`, `id`, `name`, `input` (replaced by a truncation marker when over 4 KB) |
| `tool_result` | A tool call finished | `session_id`, `tool_use_id`, `name`, `output`, `is_error` |
| `permission_request` | A tool needs approval | `session_id`, `request_id`, `tool_name`, `tool_input` |
| `user_question` | The agent asks the user a question | `session_id`, `request_id`, `tool_use_id`, `questions` |
| `done` | Turn complete | `session_id`, `metadata` (`total_cost_usd`, `tokens_in`, `tokens_out`, `model`, `duration_ms`, ...) |
| `interrupted` | Turn ended by an interrupt | `session_id`, `message` |
| `interrupt_confirmed` / `interrupt_failed` | Reply to `interrupt_for_query` | `session_id`, `message` (on failure) |
| `error` | Error occurred | `message` |
| `ping` | Keep-alive | — |

Other devices watching the same session receive the same control frames. Content reaches every device through the session event feed.

### Message Types (Client → Server)

| Type | Description | Key Fields |
|------|-------------|------------|
| `query` | Start a new query (creates a session if `session_id` is omitted) | `prompt`, `session_id`, `profile`, `project` |
| `queue_message` | Queue a message to send after the current stream completes | `content`, `session_id`, `profile`, `project` |
| `load_session` | Load/switch to a session and receive its history | `session_id` |
| `stop` | Stop the current turn, its background work and queued messages | `session_id`, `turn_id` (optional) |
| `permission_response` | Approve/deny a tool use | `request_id`, `session_id`, `decision`, `remember` |
| `user_question_response` | Answer a `user_question` | `request_id`, `session_id`, `answers` |
| `interrupt_for_query` | Interrupt the running turn so a new `query` can follow; replies `interrupt_confirmed` or `interrupt_failed` | `session_id` |
| `pong` | Keep-alive response to a `ping` | — |

### Permission Handling

When the agent wants to use a tool that requires approval:

```javascript
// Server sends permission request
// { type: "permission_request", session_id: "sess_abc123", tool_name: "Bash", tool_input: { command: "rm -rf /tmp/old" }, request_id: "req_123" }

// Client approves or denies via a "decision" of "allow" or "deny".
// "remember" optionally persists the choice: "none", "session", or "profile" (the agent).
ws.send(JSON.stringify({
  type: 'permission_response',
  session_id: 'sess_abc123',
  request_id: 'req_123',
  decision: 'allow',
  remember: 'none'
}));
```

### Connection Lifecycle

1. **Connect** — open the socket with a `token` query param or session cookie
2. **Authenticate** — session cookie or API key validated on connection
3. **Select a session** — send `query` (with or without a `session_id`) or `load_session`
4. **Receive content** — assistant text, thinking and tool output stream over `GET /api/v1/sessions/{session_id}/events`. The socket carries control frames: `tool_use`/`tool_result`, `permission_request`, `user_question`, then `done`, `interrupted` or `error`
5. **Handle permissions** — approve/deny tool use as needed
6. **Reconnect** — send `load_session` to receive a fresh `history`, and reopen the event feed from the last `seq` you saw

## CLI Bridge WebSocket

### WS /api/v1/ws/cli/{session_id}

Interactive CLI bridge for special commands like `/rewind`.

**Connection:**
```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/ws/cli/sess_abc123');
```

Legacy interactive CLI bridge. The web UI no longer uses it.

## Sync WebSockets

Two additional endpoints power multi-device synchronization:

- **WS /api/v1/ws/sessions/{session_id}** — per-session sync stream for a single session.
- **WS /api/v1/ws/global** — global sync stream for user-level events across sessions.

## Connection Parameters

| Parameter | Description |
|-----------|-------------|
| `token` | Query parameter — API key (e.g. `?token=aih_your_key`). Optional with cookie auth on `/ws/chat` and `/ws/global`; required on `/ws/sessions/{session_id}` |
| `device_id` | Query parameter — unique device identifier for multi-device sync. Optional on `/ws/chat`; required on `/ws/sessions/{session_id}` and `/ws/global` |
| `session_id` | Path parameter on the CLI and sync endpoints (not on `/ws/chat`) |

Authentication is performed via:
- Session cookie (for the owner's web UI)
- API key in query string: `?token=aih_your_key`

## Error Handling

WebSocket close codes:
- `1000` — Normal closure
- `1013` — Authorization temporarily unavailable; retry later
- `4001` — Authentication failed
- `4003` — Access denied: the session belongs to another project or agent (`/ws/sessions/{session_id}`)
- `4004` — Session not found
- `4005` — No CLI session to bridge (CLI bridge)
