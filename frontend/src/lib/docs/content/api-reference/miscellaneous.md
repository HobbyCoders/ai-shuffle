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

### POST /api/v1/tags/session/{session_id}/{tag_id}

Add a tag to a session.

### DELETE /api/v1/tags/session/{session_id}/{tag_id}

Remove a tag from a session.

## Search

### GET /api/v1/search

Full-text search across all sessions.

```bash
curl "http://localhost:8000/api/v1/search?q=authentication&limit=10" \
  -H "Authorization: Bearer aih_your_key"
```

## Analytics

### GET /api/v1/analytics/summary

Get a usage summary (session counts, message counts, etc.).

**Auth:** Admin only

### GET /api/v1/analytics/costs

Get cost breakdown by profile, project, and time period.

**Auth:** Admin only

### GET /api/v1/analytics/trends

Get usage trends over time.

### GET /api/v1/analytics/top-sessions

List the highest-usage sessions.

## Git Integration

### GET /api/v1/projects/{project_id}/git/status

Get git status for a project.

```bash
curl http://localhost:8000/api/v1/projects/proj_abc123/git/status \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{
  "is_git_repo": true,
  "current_branch": "main",
  "is_clean": false,
  "staged": [{"file": "src/app.py", "status": "modified"}],
  "modified": ["README.md"],
  "untracked": ["new-file.txt"],
  "ahead": 0,
  "behind": 0
}
```

### POST /api/v1/projects/{project_id}/git/commit

Commit changes.

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj_abc123/git/commit \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"message": "Fix bug", "include_unstaged": true}'
```

### GET /api/v1/projects/{project_id}/git/graph

Get the commit graph (history).

### GET /api/v1/projects/{project_id}/git/branches

List branches.

### POST /api/v1/projects/{project_id}/git/branches

Create a new branch.

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj_abc123/git/branches \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"name": "feature-x"}'
```

## GitHub Integration

GitHub endpoints are scoped to a project's connected repository.

### GET /api/v1/projects/{project_id}/github/status

Get the GitHub connection status for a project (linked repository, auth state).

### GET /api/v1/projects/{project_id}/github/pulls

List pull requests for the project's repository.

### POST /api/v1/projects/{project_id}/github/pulls

Create a pull request.

### GET /api/v1/projects/{project_id}/github/actions

List GitHub Actions workflow runs for the project's repository.

## Settings & Integrations

### GET /api/v1/settings/integrations

Get integration settings (API key status for various services).

**Auth:** Admin only

### POST /api/v1/settings/integrations/fal-ai

Save the fal.ai API key (used for all AI media tools: image, video, 3D, TTS, STT).

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/settings/integrations/fal-ai \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"api_key": "your_fal_key"}'
```

### DELETE /api/v1/settings/integrations/fal-ai

Remove the fal.ai API key.

**Auth:** Admin only

### POST /api/v1/settings/integrations/anthropic

Save the Anthropic API key used by Claude Code.

**Auth:** Admin only

### DELETE /api/v1/settings/integrations/anthropic

Remove the Anthropic API key.

**Auth:** Admin only

## Preferences

User preferences are stored and accessed per key.

### GET /api/v1/preferences/all

Get all preferences for the current user.

### GET /api/v1/preferences/{key}

Get a single preference value.

### PUT /api/v1/preferences/{key}

Set a preference value. (`POST` is also accepted, primarily for `navigator.sendBeacon()` saves on page unload.)

```bash
curl -X PUT http://localhost:8000/api/v1/preferences/theme \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"value": "dark"}'
```

### DELETE /api/v1/preferences/{key}

Delete a preference.

## Generated Media

### GET /api/generated-images/

List AI-generated images.

### GET /api/generated-videos/

List AI-generated videos.

## Files

Serve files referenced by sessions (read-only).

### GET /api/files/

List served files.

### GET /api/files/{filename}

Retrieve a served file by name.

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
