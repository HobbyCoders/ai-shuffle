<!--
title: Projects
description: Project CRUD, file management, git integration, and worktree endpoints
order: 6
-->

# Projects API

Endpoints for managing projects — directory references with file browsing, git integration, and worktree support.

## Project CRUD

### GET /api/v1/projects

List all projects. API users see only their assigned project.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/projects \
  -H "Authorization: Bearer aih_your_key"
```

### GET /api/v1/projects/{project_id}

Get a specific project.

### POST /api/v1/projects

Create a new project.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/projects \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "my-web-app",
    "name": "My Web App",
    "path": "/home/user/projects/my-web-app",
    "description": "SvelteKit web application",
    "settings": {"default_profile_id": "your-agent-id"}
  }'
```

`id` is required (lowercase letters, digits and hyphens). `path` points the project at an existing directory and only applies to local installs; without it the project lives in the workspace under its `id`. `settings.default_profile_id` is the agent used when a new session in this project names none.

### PUT /api/v1/projects/{project_id}

Update a project.

**Auth:** Owner only

### DELETE /api/v1/projects/{project_id}

Delete a project from the database (does not delete files).

**Auth:** Owner only

## Project Files

### GET /api/v1/projects/{project_id}/files

List files in a project directory.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | Subdirectory path (default: root) |
| `show_hidden` | boolean | Show hidden files (default: false) |

```bash
curl "http://localhost:8000/api/v1/projects/proj123/files?path=src" \
  -H "Authorization: Bearer aih_your_key"
```

### POST /api/v1/projects/{project_id}/upload

Upload a file to the project.

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj123/upload \
  -H "Authorization: Bearer aih_your_key" \
  -F "file=@document.pdf" \
  -F "path=uploads/"
```

**Form fields:**

| Field | Type | Description |
|-------|------|-------------|
| `file` | file | The file to upload (required) |
| `path` | string | Target directory within the project (default: root) |
| `session_id` | string | Associate with a session |
| `worktree_id` | string | Upload to specific worktree |

### GET /api/v1/projects/{project_id}/files/download

Download a file from the project.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | File path within the project (required) |

### POST /api/v1/projects/{project_id}/files/folder

Create a folder.

```bash
curl -X POST "http://localhost:8000/api/v1/projects/proj123/files/folder?path=src/components" \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"name": "new-feature"}'
```

The `path` query parameter is the parent directory (default: root); the body carries the new folder's `name`.

### PUT /api/v1/projects/{project_id}/files/rename

Rename a file or folder. Pass its path as `?path=` and the new name in the body: `{"new_name": "..."}`.

### DELETE /api/v1/projects/{project_id}/files

Delete a file or folder given by the `path` query parameter (required).

## Chat History

### GET /api/v1/projects/{project_id}/chat-history

List available chat history sessions for @ mention support.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `search` | string | Filter by title |
| `limit` | integer | Max results (default: 50, max: 100) |
| `worktree_id` | string | Filter by worktree |

## Worktree Management

### POST /api/v1/projects/{project_id}/sync-worktrees

Sync worktree database records with actual git worktrees on disk.

## Session Import

### POST /api/v1/projects/{project_id}/sync-sessions

Import Claude Code CLI sessions for this project.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `profile_id` | string | Agent to assign to imported sessions |
