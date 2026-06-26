<!--
title: WebSocket
description: Real-time WebSocket endpoints for chat streaming and CLI bridge
order: 4
-->

# WebSocket API

Real-time bidirectional communication for chat streaming and interactive CLI operations.

## Chat WebSocket

### WS /api/v1/ws/chat

Primary WebSocket connection for real-time chat. Handles message streaming, tool use visualization, and multi-device session synchronization.

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
  profile: 'claude-code',
  project: 'my-project'
}));
```

### Message Types (Server → Client)

| Type | Description | Key Fields |
|------|-------------|------------|
| `history` | Full message history for a session (on connect or session switch) | `session_id`, `messages`, `isStreaming` |
| `start` | Query started, streaming will begin | `session_id`, `message_id` |
| `chunk` | Text content chunk | `content`, `message_id` |
| `stream_delta` | Real-time streaming delta (when partial messages are enabled) | `content`, `message_id` |
| `tool_use` | Tool being used | `tool_name`, `tool_input` |
| `tool_result` | Tool result | `tool_name`, `output`, `error` |
| `done` | Query complete with metadata | `total_cost_usd`, `tokens_in`, `tokens_out` |
| `error` | Error occurred | `message` |
| `ping` | Keep-alive | — |
| `permission_request` | Tool needs approval | `tool_name`, `tool_input`, `request_id`, `session_id` |
| `session_created` | New session created for an omitted `session_id` | `session_id`, `worktree_id` |
| `stopped` | Query interrupted by `stop` | `session_id` |

Multi-device sync events (broadcast when another device is streaming the same session):

| Type | Description | Key Fields |
|------|-------------|------------|
| `stream_start` | Another device started streaming | `session_id`, `message_id` |
| `stream_chunk` | Streaming chunk from another device | `session_id`, `content` |
| `stream_end` | Another device finished streaming | `session_id`, `metadata`, `interrupted` |

### Message Types (Client → Server)

| Type | Description | Key Fields |
|------|-------------|------------|
| `query` | Start a new query (creates a session if `session_id` is omitted) | `prompt`, `session_id`, `profile`, `project` |
| `queue_message` | Queue a message to send after the current stream completes | `content`, `session_id`, `profile`, `project` |
| `load_session` | Load/switch to a session and receive its history | `session_id` |
| `stop` | Interrupt the current query | `session_id` |
| `permission_response` | Approve/deny a tool use | `request_id`, `session_id`, `decision`, `remember` |
| `pong` | Keep-alive response to a `ping` | — |

### Permission Handling

When Claude wants to use a tool that requires approval:

```javascript
// Server sends permission request
// { type: "permission_request", session_id: "sess_abc123", tool_name: "Bash", tool_input: { command: "rm -rf /tmp/old" }, request_id: "req_123" }

// Client approves or denies via a "decision" of "allow" or "deny".
// "remember" optionally persists the choice: "none", "session", or "profile".
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
4. **Receive streaming** — `start`, `chunk`/`stream_delta`, `tool_use`/`tool_result`, then `done`
5. **Handle permissions** — approve/deny tool use as needed
6. **Reconnect** — send `load_session` to receive a fresh `history` and resume any in-progress stream

## CLI Bridge WebSocket

### WS /api/v1/ws/cli/{session_id}

Interactive CLI bridge for special commands like `/rewind`.

**Connection:**
```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/ws/cli/sess_abc123');
```

This is used internally by the web UI for commands that require interactive CLI operations.

## Sync WebSockets

Two additional endpoints power multi-device synchronization:

- **WS /api/v1/ws/sessions/{session_id}** — per-session sync stream for a single session.
- **WS /api/v1/ws/global** — global sync stream for user-level events across sessions.

## Connection Parameters

| Parameter | Description |
|-----------|-------------|
| `token` | Query parameter — API key (e.g. `?token=aih_your_key`); optional when using cookie auth |
| `device_id` | Query parameter (chat endpoint) — unique device identifier for multi-device sync; generated if omitted |
| `session_id` | Path parameter on the CLI and sync endpoints (not on `/ws/chat`) |

Authentication is performed via:
- Session cookie (for admin web UI)
- API key in query string: `?token=aih_your_key`

## Error Handling

WebSocket close codes:
- `1000` — Normal closure
- `1011` — Internal error
- `4001` — Authentication failed
- `4004` — Session not found
- `4005` — No SDK session (CLI bridge)
