<!--
title: Miscellaneous
description: Tags, search, analytics, git, GitHub, files, settings, and other endpoints
order: 13
-->

# Miscellaneous API

Additional API endpoints for tags, search, analytics, git operations, settings, and more.

## Tags

Organize sessions with custom tags.

### GET /api/v1/tags

List all tags.

```bash
curl http://localhost:8000/api/v1/tags \
  -H "Authorization: Bearer aih_your_key"
```

### POST /api/v1/tags

Create a tag.

```bash
curl -X POST http://localhost:8000/api/v1/tags \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"name": "bugfix", "color": "#ef4444"}'
```

### POST /api/v1/sessions/{session_id}/tags/{tag_id}

Add a tag to a session.

### DELETE /api/v1/sessions/{session_id}/tags/{tag_id}

Remove a tag from a session.

## Search

### GET /api/v1/search

Full-text search across all sessions.

```bash
curl "http://localhost:8000/api/v1/search?q=authentication&limit=10" \
  -H "Authorization: Bearer aih_your_key"
```

## Analytics

### GET /api/v1/analytics/usage

Get usage analytics (session counts, message counts, etc.).

**Auth:** Admin only

### GET /api/v1/analytics/cost

Get cost breakdown by profile, project, and time period.

**Auth:** Admin only

## Git Integration

### GET /api/v1/git/status/{project_id}

Get git status for a project.

```bash
curl http://localhost:8000/api/v1/git/status/proj_abc123 \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{
  "current_branch": "main",
  "is_clean": false,
  "staged_files": ["src/app.py"],
  "unstaged_files": ["README.md"],
  "untracked_files": ["new-file.txt"]
}
```

### POST /api/v1/git/commit

Commit changes.

**Auth:** Admin only

### GET /api/v1/git/log/{project_id}

Get commit log.

### POST /api/v1/git/branch/{project_id}

Create or switch branch.

## GitHub Integration

### GET /api/v1/github/repos

List user's GitHub repositories.

**Auth:** Admin only (requires GitHub auth)

### GET /api/v1/github/issues/{repo}

Get issues for a repository.

### POST /api/v1/github/issues/{repo}

Create an issue.

## Settings & Integrations

### GET /api/v1/settings/integrations

Get integration settings (API key status for various services).

**Auth:** Admin only

### POST /api/v1/settings/integrations/{service}

Save an API key for a service (openai, gemini, etc.).

**Auth:** Admin only

### DELETE /api/v1/settings/integrations/{service}

Remove an API key.

### POST /api/v1/settings/integrations/{service}/test

Test an integration key.

## Preferences

### GET /api/v1/preferences

Get user preferences.

### PUT /api/v1/preferences

Update preferences.

```bash
curl -X PUT http://localhost:8000/api/v1/preferences \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"theme": "dark", "sidebar_collapsed": false}'
```

## Generated Media

### GET /api/v1/images

List AI-generated images.

### DELETE /api/v1/images/{image_id}

Delete a generated image.

### GET /api/v1/videos

List AI-generated videos.

### DELETE /api/v1/videos/{video_id}

Delete a generated video.

## Shared Files

### POST /api/v1/shared-files

Share a file (creates a shareable link).

### GET /api/v1/shared-files/{share_id}

Access a shared file.

## Templates

### GET /api/v1/templates

List prompt templates.

### POST /api/v1/templates

Create a prompt template.

## System

### GET /health

Health check (no auth).

### GET /api/v1/version

Get API and Claude Code versions.

### GET /api/v1/stats

Get system usage statistics.

### GET /api/v1/tools

List available tools by category.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `refresh` | boolean | Force refresh from Claude CLI |

### GET /api/v1/tools/builtin

List built-in Claude Code tools only.

## Documentation

### GET /api/v1/documentation

List all documentation pages (this docs system).

### GET /api/v1/documentation/{category}/{slug}

Get a specific documentation page with full markdown content.

### GET /api/v1/documentation/search?q={query}

Search across all documentation.
