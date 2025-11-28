# AI Hub API Reference

Complete API documentation for integrating external applications with AI Hub.

## Base URL

```
<Server-IP>/api/v1
```

## Authentication

**All API requests require authentication via API key.**

API keys are created by the administrator through the AI Hub web interface (Settings > API Users). Each API key can be configured with:

- **Project**: Restricts the API user to work within a specific project workspace
- **Profile**: Restricts the API user to use a specific agent profile

### Using Your API Key

Include your API key in the `Authorization` header as a Bearer token:

```bash
curl -X POST http://localhost:8000/api/v1/conversation/stream \
  -H "Authorization: Bearer aih_your_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello, Claude!"}'
```

### Getting an API Key

Contact your AI Hub administrator to create an API user for your application. They will provide you with:

1. An API key (starts with `aih_`)
2. Information about which project and profile your key is configured for

**Important**: API keys are shown only once when created. Store them securely.

---

## Query Endpoints

These are the main AI interaction endpoints for your application.

### POST /api/v1/conversation/stream

**Recommended endpoint** - SSE streaming conversation with real-time responses.

**Request**:
```bash
curl -X POST http://localhost:8000/api/v1/conversation/stream \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Write a hello world function in Python",
    "session_id": null
  }'
```

**Request Body**:
```json
{
  "prompt": "Your message to Claude",
  "session_id": "optional-session-id-to-continue",
  "overrides": {
    "model": "opus",
    "system_prompt_append": "Additional instructions",
    "max_turns": 5
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | Yes | Your message/instruction to Claude |
| `session_id` | string | No | Session ID to continue a previous conversation (null = new session) |
| `overrides` | object | No | Runtime overrides (see below) |

**Note**: The `profile` and `project` fields in the request are ignored when using API key authentication. Your API key's configured project and profile are always used.

**Override Options**:
| Field | Type | Description |
|-------|------|-------------|
| `model` | string | Override model: "sonnet", "opus", or "haiku" |
| `system_prompt_append` | string | Additional instructions to append to system prompt |
| `max_turns` | integer | Maximum conversation turns |

**Response**: Server-Sent Events stream (see [SSE Events](#sse-event-types) section)

---

### POST /api/v1/conversation

Non-streaming conversation (returns complete response).

**Request**:
```bash
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write a hello world function in Python"}'
```

**Response**:
```json
{
  "response": "Here's a hello world function in Python:\n\n```python\ndef hello_world() -> str:\n    return \"Hello, World!\"\n```",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "metadata": {
    "model": "sonnet",
    "duration_ms": 1523,
    "total_cost_usd": 0.0042,
    "num_turns": 1
  }
}
```

---

### POST /api/v1/query

One-shot query - creates a new session each time (no conversation continuity).

**Request Body**: Same as `/conversation`

**Response**: Same as `/conversation`

---

### POST /api/v1/query/stream

SSE streaming one-shot query.

**Request Body**: Same as `/conversation`

**Response**: Server-Sent Events stream

---

### POST /api/v1/session/{session_id}/interrupt

Interrupt an active streaming session.

**Request**:
```bash
curl -X POST http://localhost:8000/api/v1/session/your-session-id/interrupt \
  -H "Authorization: Bearer aih_your_api_key"
```

**Response**:
```json
{
  "status": "interrupted",
  "session_id": "your-session-id"
}
```

**Errors**:
- `404`: No active session found with that ID

---

## SSE Event Types

When using streaming endpoints (`/conversation/stream`, `/query/stream`), you receive Server-Sent Events:

### init
Sent at stream start with session information.
```json
{
  "type": "init",
  "session_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### text
Text content chunks (partial response).
```json
{
  "type": "text",
  "content": "Here's a "
}
```

### tool_use
Tool invocation by Claude (file operations, bash commands, etc.).
```json
{
  "type": "tool_use",
  "name": "Write",
  "input": {
    "file_path": "/workspace/hello.py",
    "content": "def hello(): pass"
  }
}
```

### tool_result
Result of a tool execution.
```json
{
  "type": "tool_result",
  "name": "Write",
  "output": "File written successfully"
}
```

### done
Stream completion with final metadata.
```json
{
  "type": "done",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "metadata": {
    "model": "sonnet",
    "duration_ms": 5234,
    "total_cost_usd": 0.0156,
    "num_turns": 3
  }
}
```

### interrupted
Session was interrupted by user request.
```json
{
  "type": "interrupted",
  "message": "Query was interrupted"
}
```

### error
An error occurred during processing.
```json
{
  "type": "error",
  "message": "Error description"
}
```

---

## Session Management

### GET /api/v1/sessions

List your sessions. API users only see sessions created with their API key.

**Request**:
```bash
curl http://localhost:8000/api/v1/sessions?limit=20 \
  -H "Authorization: Bearer aih_your_api_key"
```

**Query Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | 50 | Max results (1-100) |
| `offset` | integer | 0 | Pagination offset |

**Response**:
```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "profile_id": "claude-code",
    "project_id": "my-project",
    "title": "Hello world function",
    "status": "active",
    "total_cost_usd": 0.0156,
    "turn_count": 3,
    "created_at": "2024-01-01T12:00:00Z",
    "updated_at": "2024-01-01T12:05:00Z"
  }
]
```

---

### GET /api/v1/sessions/{session_id}

Get a specific session with its message history.

**Request**:
```bash
curl http://localhost:8000/api/v1/sessions/your-session-id \
  -H "Authorization: Bearer aih_your_api_key"
```

**Response**:
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "profile_id": "claude-code",
  "project_id": "my-project",
  "title": "Hello world function",
  "status": "active",
  "total_cost_usd": 0.0156,
  "turn_count": 3,
  "created_at": "2024-01-01T12:00:00Z",
  "updated_at": "2024-01-01T12:05:00Z",
  "messages": [
    {
      "id": 1,
      "role": "user",
      "content": "Write a hello world function",
      "created_at": "2024-01-01T12:00:00Z"
    },
    {
      "id": 2,
      "role": "assistant",
      "content": "Here's a hello world function...",
      "metadata": {"model": "sonnet"},
      "created_at": "2024-01-01T12:00:05Z"
    }
  ]
}
```

---

### DELETE /api/v1/sessions/{session_id}

Delete a session and its message history.

**Request**:
```bash
curl -X DELETE http://localhost:8000/api/v1/sessions/your-session-id \
  -H "Authorization: Bearer aih_your_api_key"
```

**Response**: 204 No Content

---

## System Endpoints

### GET /health

Health check (no authentication required).

**Response**:
```json
{
  "status": "healthy",
  "service": "ai-hub",
  "version": "2.1.0",
  "claude_authenticated": true
}
```

---

## Integration Examples

### Python with requests

```python
import requests
import json

API_KEY = "aih_your_api_key_here"
BASE_URL = "http://localhost:8000/api/v1"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# Simple query (non-streaming)
response = requests.post(
    f"{BASE_URL}/conversation",
    headers=headers,
    json={"prompt": "Write a hello world function in Python"}
)
result = response.json()
print(result["response"])

# Continue the conversation
session_id = result["session_id"]
response = requests.post(
    f"{BASE_URL}/conversation",
    headers=headers,
    json={
        "prompt": "Now add type hints to that function",
        "session_id": session_id
    }
)
print(response.json()["response"])
```

### Python with SSE Streaming

```python
import requests
import json

API_KEY = "aih_your_api_key_here"
BASE_URL = "http://localhost:8000/api/v1"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# Streaming query
response = requests.post(
    f"{BASE_URL}/conversation/stream",
    headers=headers,
    json={"prompt": "Write a hello world function"},
    stream=True
)

session_id = None
for line in response.iter_lines():
    if line:
        line = line.decode('utf-8')
        if line.startswith('data: '):
            event = json.loads(line[6:])

            if event['type'] == 'init':
                session_id = event['session_id']
                print(f"Session started: {session_id}")
            elif event['type'] == 'text':
                print(event['content'], end='', flush=True)
            elif event['type'] == 'tool_use':
                print(f"\n[Using tool: {event['name']}]")
            elif event['type'] == 'done':
                print(f"\n\nCompleted. Cost: ${event['metadata'].get('total_cost_usd', 0):.4f}")

# Continue the conversation with the same session
response = requests.post(
    f"{BASE_URL}/conversation/stream",
    headers=headers,
    json={
        "prompt": "Add error handling",
        "session_id": session_id
    },
    stream=True
)
# ... process stream
```

### JavaScript/TypeScript

```typescript
const API_KEY = 'aih_your_api_key_here';
const BASE_URL = 'http://localhost:8000/api/v1';

// Non-streaming query
async function query(prompt: string, sessionId?: string) {
  const response = await fetch(`${BASE_URL}/conversation`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${API_KEY}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ prompt, session_id: sessionId })
  });

  return response.json();
}

// Usage
const result = await query('Write a hello world function');
console.log(result.response);

// Continue conversation
const followUp = await query('Add type hints', result.session_id);
console.log(followUp.response);
```

### JavaScript SSE Streaming

```typescript
const API_KEY = 'aih_your_api_key_here';
const BASE_URL = 'http://localhost:8000/api/v1';

async function streamQuery(prompt: string, sessionId?: string) {
  const response = await fetch(`${BASE_URL}/conversation/stream`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${API_KEY}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ prompt, session_id: sessionId })
  });

  const reader = response.body?.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let newSessionId: string | undefined;

  while (true) {
    const { done, value } = await reader!.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const event = JSON.parse(line.slice(6));

        switch (event.type) {
          case 'init':
            newSessionId = event.session_id;
            break;
          case 'text':
            process.stdout.write(event.content);
            break;
          case 'tool_use':
            console.log(`\n[Tool: ${event.name}]`);
            break;
          case 'done':
            console.log(`\nCost: $${event.metadata.total_cost_usd}`);
            break;
          case 'error':
            console.error(`Error: ${event.message}`);
            break;
        }
      }
    }
  }

  return newSessionId;
}

// Usage
const sessionId = await streamQuery('Write a hello world function');
await streamQuery('Add error handling', sessionId);
```

### cURL Examples

```bash
# Simple query
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write hello world in Python"}'

# Streaming query
curl -X POST http://localhost:8000/api/v1/conversation/stream \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write hello world in Python"}'

# Continue a conversation
curl -X POST http://localhost:8000/api/v1/conversation \
  -H "Authorization: Bearer aih_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Add type hints", "session_id": "your-session-id"}'

# List sessions
curl http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer aih_your_api_key"

# Get session details
curl http://localhost:8000/api/v1/sessions/your-session-id \
  -H "Authorization: Bearer aih_your_api_key"

# Interrupt active session
curl -X POST http://localhost:8000/api/v1/session/your-session-id/interrupt \
  -H "Authorization: Bearer aih_your_api_key"

# Health check (no auth needed)
curl http://localhost:8000/health
```

---

## Error Responses

All errors follow this format:

```json
{
  "detail": "Error message description"
}
```

### Common HTTP Status Codes

| Code | Description |
|------|-------------|
| 400 | Bad Request - Invalid parameters or missing required fields |
| 401 | Unauthorized - Invalid or missing API key |
| 404 | Not Found - Session or resource doesn't exist |
| 500 | Internal Server Error - Server-side failure |

### Common Errors

| Error | Cause | Solution |
|-------|-------|----------|
| "Not authenticated" | Missing or invalid API key | Check your Authorization header |
| "Claude CLI not authenticated" | Server's Claude connection issue | Contact administrator |
| "Session not found" | Invalid session_id | Use a valid session ID or omit for new session |
| "Profile not found" | Invalid profile configured | Contact administrator to fix API user config |
| "Project not found" | Invalid project configured | Contact administrator to fix API user config |

---

## Best Practices

### Session Management

1. **Save session IDs**: Store the `session_id` from responses to continue conversations
2. **New sessions**: Omit `session_id` or set it to `null` for fresh conversations
3. **Clean up**: Delete old sessions you no longer need

### Streaming

1. **Prefer streaming**: Use `/conversation/stream` for better user experience
2. **Handle all event types**: Process `text`, `tool_use`, `done`, and `error` events
3. **Buffer handling**: SSE events may arrive in chunks, buffer partial lines

### Error Handling

1. **Retry on 5xx**: Server errors may be transient
2. **Don't retry on 4xx**: Client errors require fixing the request
3. **Check health**: Use `/health` endpoint to verify server status

### Security

1. **Keep keys secret**: Never commit API keys to version control
2. **Use environment variables**: Store API keys in environment variables
3. **Rotate keys**: Request new keys if you suspect compromise

---

## Rate Limits

Currently no rate limiting is enforced at the API level. However:

- Claude API has its own rate limits
- Large queries consume more resources
- Consider implementing client-side throttling for high-volume applications

---

## Workspace Isolation

Each API user operates in an isolated workspace:

- **Project restriction**: Your API key may be restricted to a specific project directory
- **Profile restriction**: Your API key may be required to use a specific agent profile
- **Session isolation**: Sessions are tagged with your API user ID

This ensures applications don't interfere with each other and provides accountability for API usage.
