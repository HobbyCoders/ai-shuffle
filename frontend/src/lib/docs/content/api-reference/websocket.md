<!--
title: WebSocket
description: Real-time WebSocket endpoints for chat streaming and CLI bridge
order: 4
-->

# WebSocket API

Real-time bidirectional communication for chat streaming and interactive CLI operations.

## Chat WebSocket

### WS /api/v1/ws/chat/{session_id}

Primary WebSocket connection for real-time chat. Handles message streaming, tool use visualization, and session state synchronization.

**Connection:**
```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/ws/chat/sess_abc123');

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
  type: 'user_message',
  content: 'Help me fix this bug',
  profile_id: 'default',
  project_id: 'my-project'
}));
```

### Message Types (Server → Client)

| Type | Description | Key Fields |
|------|-------------|------------|
| `assistant_message` | Text from Claude | `content`, `message_id` |
| `partial_message` | Streaming text chunk | `content`, `message_id` |
| `tool_use_start` | Tool execution begins | `tool_name`, `tool_input` |
| `tool_use_result` | Tool execution complete | `tool_name`, `output`, `error` |
| `thinking` | Model thinking block | `content` |
| `subagent_start` | Subagent launched | `agent_type`, `description` |
| `subagent_end` | Subagent completed | `agent_id`, `status` |
| `permission_request` | Tool needs approval | `tool_name`, `tool_input`, `request_id` |
| `session_complete` | Session finished | `cost_usd`, `tokens_in`, `tokens_out` |
| `session_error` | Session error | `error` |
| `sync` | State sync message | `sync_id`, `messages` |

### Message Types (Client → Server)

| Type | Description | Key Fields |
|------|-------------|------------|
| `user_message` | Send a message | `content`, `profile_id`, `project_id` |
| `permission_response` | Approve/deny tool use | `request_id`, `approved` |
| `interrupt` | Stop current execution | — |
| `ping` | Keep-alive | — |

### Permission Handling

When Claude wants to use a tool that requires approval:

```javascript
// Server sends permission request
// { type: "permission_request", tool_name: "Bash", tool_input: { command: "rm -rf /tmp/old" }, request_id: "req_123" }

// Client approves or denies
ws.send(JSON.stringify({
  type: 'permission_response',
  request_id: 'req_123',
  approved: true
}));
```

### Connection Lifecycle

1. **Connect** with session ID in URL
2. **Authenticate** — session cookie or API key validated on connection
3. **Send messages** — user messages trigger Claude processing
4. **Receive streaming** — partial messages stream in real-time
5. **Handle permissions** — approve/deny tool use as needed
6. **Reconnect** — use `/sessions/{id}/state` to restore state after disconnect

## CLI Bridge WebSocket

### WS /api/v1/ws/cli/{session_id}

Interactive CLI bridge for special commands like `/rewind`.

**Connection:**
```javascript
const ws = new WebSocket('ws://localhost:8000/api/v1/ws/cli/sess_abc123');
```

This is used internally by the web UI for commands that require interactive CLI operations.

## Connection Parameters

| Parameter | Description |
|-----------|-------------|
| `session_id` | Path parameter — the session to connect to |

Authentication is performed via:
- Session cookie (for admin web UI)
- API key in query string: `?token=aih_your_key`

## Error Handling

WebSocket close codes:
- `1000` — Normal closure
- `1008` — Policy violation (auth failure)
- `1011` — Internal error
- `4000` — Session not found
- `4001` — Session already connected
