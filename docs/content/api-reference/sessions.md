<!--
title: Sessions
description: Chat session management, search, export, and import endpoints
order: 2
-->

# Sessions API

Endpoints for managing chat sessions — listing, searching, creating, updating, deleting, exporting, and importing.

## List Sessions

### GET /api/v1/sessions

List sessions with optional filters.

**Auth:** Required

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | string | Filter by project |
| `profile_id` | string | Filter by profile |
| `status` | string | Filter by status (active, archived) |
| `tag_id` | string | Filter by tag |
| `admin_only` | boolean | Only admin sessions |
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

## Search Sessions

### GET /api/v1/sessions/search/query

Search sessions by title and content.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `q` | string | Search query (required) |
| `project_id` | string | Filter by project |
| `profile_id` | string | Filter by profile |
| `limit` | integer | Max results (default: 20) |

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
  "profile_id": "default",
  "messages": [
    {
      "role": "user",
      "content": "Help me fix the login flow",
      "timestamp": "2025-01-15T10:30:00Z"
    },
    {
      "role": "assistant",
      "content": "I'll look at the auth code...",
      "timestamp": "2025-01-15T10:30:05Z"
    }
  ],
  "created_at": "2025-01-15T10:30:00Z",
  "updated_at": "2025-01-15T10:35:00Z",
  "is_active": false,
  "tags": []
}
```

### GET /api/v1/sessions/{session_id}/live

Get live session state for streaming. Returns current partial messages and streaming status.

### GET /api/v1/sessions/{session_id}/state

Get session state for client reconnection.

## Session Management

### PATCH /api/v1/sessions/{session_id}

Update session metadata.

```bash
curl -X PATCH http://localhost:8000/api/v1/sessions/sess_abc123 \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"title": "New Title", "status": "archived"}'
```

### DELETE /api/v1/sessions/{session_id}

Delete a session and all its messages.

```bash
curl -X DELETE http://localhost:8000/api/v1/sessions/sess_abc123 \
  -H "Authorization: Bearer aih_your_key"
```

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

Set whether a session is in the Active group. Send a JSON body with the new value; the endpoint returns the updated session. Passing `false` marks the session complete (it drops into Recent); passing `true` moves it back to Active.

```bash
curl -X PATCH http://localhost:8000/api/v1/sessions/sess_abc123/active \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"is_active": false}'
```

## Session Sync

### GET /api/v1/sessions/{session_id}/sync

Polling fallback for WebSocket. Get changes since a sync ID.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `sync_id` | string | Last sync ID received |

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

**Auth:** Admin only

### POST /api/v1/sessions/import-jsonl

Import a Claude Code CLI JSONL session.

**Auth:** Admin only

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

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/sessions/with-worktree \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "proj123",
    "profile_id": "default",
    "branch_name": "feature/new-feature",
    "create_new_branch": true,
    "base_branch": "main"
  }'
```
