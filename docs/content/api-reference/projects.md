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

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/projects \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Web App",
    "path": "/home/user/projects/my-web-app",
    "description": "SvelteKit web application",
    "default_profile_id": "default"
  }'
```

### PUT /api/v1/projects/{project_id}

Update a project.

**Auth:** Admin only

### DELETE /api/v1/projects/{project_id}

Delete a project from the database (does not delete files).

**Auth:** Admin only

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

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
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
curl -X POST http://localhost:8000/api/v1/projects/proj123/files/folder \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"path": "src/components/new-feature"}'
```

### PUT /api/v1/projects/{project_id}/files/rename

Rename a file or folder.

### DELETE /api/v1/projects/{project_id}/files

Delete a file or folder.

## Chat History

### GET /api/v1/projects/{project_id}/chat-history

List available chat history sessions for @ mention support.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `search` | string | Filter by title |
| `limit` | integer | Max results |
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
| `profile_id` | string | Profile to assign to imported sessions |
