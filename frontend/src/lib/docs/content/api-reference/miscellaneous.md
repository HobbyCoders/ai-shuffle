<!--
title: Miscellaneous
description: Tags, analytics, git, GitHub, files, settings, and other endpoints
order: 13
-->

# Miscellaneous API

Additional API endpoints for tags, analytics, git operations, settings, and more.

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

Session search lives under Sessions: `GET /api/v1/sessions/search/query?q=...` (see [Sessions](/docs/api-reference/sessions#search-sessions)). There is no `/api/v1/search` endpoint.

```bash
curl "http://localhost:8000/api/v1/sessions/search/query?q=authentication&limit=10" \
  -H "Authorization: Bearer aih_your_key"
```

## Analytics

### GET /api/v1/analytics/summary

Get a usage summary (session counts, message counts, etc.).

**Auth:** Required. API users see only their own usage.

### GET /api/v1/analytics/costs

Get a cost breakdown. `group_by` is `profile` (by agent, the default), `user` or `date`; `start_date` and `end_date` (`YYYY-MM-DD`) narrow the range.

**Auth:** Required. API users see only their own usage.

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
  "is_detached": false,
  "is_clean": false,
  "staged": [{"file": "src/app.py", "status": "M"}],
  "modified": ["README.md"],
  "untracked": ["new-file.txt"],
  "ahead": 0,
  "behind": 0
}
```

A staged file's `status` is the git index code: `A` (added), `M` (modified), `D` (deleted), `R` (renamed) or `C` (copied). The response also carries `head_commit`, `remote_url` and `differs_from_base`.

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

`start_point` (optional) is the branch or commit to start from.

### DELETE /api/v1/projects/{project_id}/git/branches/{branch_name}

Delete a branch. Add `?force=true` to delete a branch that is not fully merged. The checked-out branch cannot be deleted.

### Other git operations

**Auth:** Required. Unless noted, these also accept optional `worktree_id` or `session_id` query parameters to act on a worktree or a session's checkout instead of the project folder. The write operations return `{"success": true, "message": "..."}`. Fetch, pull, push, stage, unstage and discard add `status` (the fresh git status), and pull, stage, unstage and discard add `diff_stats`.

| Method | Path | Body or query | Notes |
|--------|------|---------------|-------|
| `POST` | `/git/checkout` | Body: `{"ref": "main", "auto_stash": false}` | Switch branch or commit in the project folder. Returns `409` for uncommitted changes unless `auto_stash` is `true`. Not allowed with `worktree_id` |
| `POST` | `/git/fetch` | Query: `remote` (default `origin`) | Returns `502` if the remote cannot be reached |
| `POST` | `/git/pull` | Body: `{"remote": "origin", "strategy": "merge", "auto_stash": false}` | `strategy` is `merge` or `rebase`. On a conflict the pull is aborted and the response has `success: false`, `has_conflicts: true` and `conflicts` |
| `POST` | `/git/push` | Body: `{"remote": "origin", "branch": null, "set_upstream": false, "force": false}` | `branch` defaults to the current branch |
| `POST` | `/git/stage` | Body: `{"files": ["src/app.py"]}` | |
| `POST` | `/git/unstage` | Body: `{"files": ["src/app.py"]}` | Keeps the changes in the working tree |
| `POST` | `/git/discard` | Body: `{"files": ["src/app.py"]}` | Reverts the files; untracked files are deleted. Optional `file_statuses` maps a file to `modified`, `staged` or `untracked`; otherwise it is read from git status |
| `GET` | `/git/diff-stats` | Query: `include_unstaged` (default `true`) | Returns `additions`, `deletions`, `files_changed` |
| `GET` | `/git/changed-files` | | Returns `files` (each with `file`, `status`, `category`, `additions`, `deletions`, `is_binary`), `total_additions`, `total_deletions` |
| `POST` | `/git/file-diff` | Body: `{"file_path": "src/app.py", "staged": false}` | Returns `file`, `diff` (unified diff), `is_binary`, `truncated` |

Paths in the table are relative to `/api/v1/projects/{project_id}`.

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj_abc123/git/pull \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"strategy": "rebase"}'
```

## GitHub Integration

GitHub endpoints are scoped to a project's connected repository.

**Auth:** Required. Apart from `status`, these also need the server's GitHub CLI to be signed in (see [GitHub Integration](/docs/api-reference/authentication#github-integration)); otherwise they return `401` "GitHub CLI is not authenticated".

### GET /api/v1/projects/{project_id}/github/status

Get the GitHub connection status for a project (linked repository, auth state).

### GET /api/v1/projects/{project_id}/github/pulls

List pull requests for the project's repository.

### POST /api/v1/projects/{project_id}/github/pulls

Create a pull request.

### GET /api/v1/projects/{project_id}/github/pulls/{number}

Get one pull request. Returns `number`, `title`, `body`, `state`, `head_branch`, `base_branch`, `author`, `url`, `created_at`, `updated_at` and `mergeable`.

### POST /api/v1/projects/{project_id}/github/pulls/{number}/merge

Merge a pull request. The body is optional; `method` is `merge` (default), `squash` or `rebase`.

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj_abc123/github/pulls/42/merge \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"method": "squash"}'
```

Returns `{"status": "ok", "message": "Pull request #42 merged successfully"}`, or `400` with GitHub's reason.

### DELETE /api/v1/projects/{project_id}/github/pulls/{number}

Close a pull request without merging. Returns `{"status": "ok", "message": "Pull request #42 closed"}`.

### GET /api/v1/projects/{project_id}/github/actions

List GitHub Actions workflow runs for the project's repository. `?limit=` sets how many (default 10, max 50).

### GET /api/v1/projects/{project_id}/github/actions/{run_id}

Get one workflow run: `id`, `name`, `status`, `conclusion`, `branch`, `event`, `url`, `created_at`.

### POST /api/v1/projects/{project_id}/github/actions/{run_id}/rerun

Run a workflow again. Returns `{"status": "ok", "message": "Workflow #123 re-run triggered"}`.

## Settings & Integrations

### GET /api/v1/settings/integrations

Get integration settings (API key status for various services).

**Auth:** Required

### POST /api/v1/settings/integrations/fal-ai

Save the fal.ai API key (used for all AI media tools: image, video, 3D, TTS, STT).

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/settings/integrations/fal-ai \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"api_key": "your_fal_key"}'
```

### DELETE /api/v1/settings/integrations/fal-ai

Remove the fal.ai API key.

**Auth:** Owner only

### POST /api/v1/settings/integrations/anthropic

Save the Anthropic API key used by Claude Code.

**Auth:** Owner only

### DELETE /api/v1/settings/integrations/anthropic

Remove the Anthropic API key.

**Auth:** Owner only

### GET /api/v1/settings/text-generation

The model and effort behind every internal one-shot AI call: session titles, commit messages, PR descriptions, branch names, and generated agents. Agents do not carry a model, and generation runs outside any session, so this one setting governs all of it.

**Response:**

```json
{
  "text_generation_model": "prov:prov-or:openai/gpt-5.6",
  "text_generation_effort": "low"
}
```

`text_generation_model` is the raw picker value: a bare id or alias (`sonnet`), or a `prov:<providerId>:<modelId>` composite for a gateway model. Empty means unset, which resolves to the default model at call time. `text_generation_effort` is `low` | `medium` | `high` | `xhigh` | `max`, or empty for the provider's own default.

### PUT /api/v1/settings/text-generation

Partial update — only the keys present in the body are written; an empty string clears one back to its default. A composite model is validated against the provider row, so a model that provider does not serve is refused here rather than failing every generation afterwards.

**Auth:** Owner only

```bash
curl -X PUT http://localhost:8000/api/v1/settings/text-generation \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text_generation_model": "haiku", "text_generation_effort": "low"}'
```

## Preferences

User preferences are stored and accessed per key. The owner and each API user have their own preferences.

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
  -d '{"key": "theme", "value": "dark"}'
```

The body must include `key` as well as `value`. Send `expected_version` (the `version` from a previous read) to get `409` instead of overwriting a newer value. Add `?only_if_missing=true` to write only when no value is set.

### DELETE /api/v1/preferences/{key}

Delete a preference.

## Generated Media

The listing and by-name routes read the shared workspace and return 403 for a project-scoped API key. Scoped keys use the `by-path` routes with a path inside their own project, worktree or checkout directories.

### GET /api/generated-images/

List AI-generated images.

### GET /api/generated-videos/

List AI-generated videos.

### GET /api/generated-images/by-path?path=...

Retrieve a generated image by absolute path. Also available for audio and videos.

## Files

Serve files referenced by sessions (read-only).

### GET /api/files/

List served files. Unscoped keys only.

### GET /api/files/{filename}

Retrieve a served file by name. Unscoped keys only.

### GET /api/files/by-path?path=...

Retrieve a served file by absolute path. This is the route the agent's chat download links use, and the only file route a project-scoped key can read.

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

### GET /api/v1/tools/surface

Every tool a session can be given, grouped and discovered live from the running registries — the harness builtins, the app's bridge servers, and each configured MCP server. There is no hardcoded list.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `refresh` | boolean | Bust the per-server MCP tool-list cache (10 minutes otherwise) |

**Response:**

```json
{
  "groups": [
    {
      "id": "harness",
      "label": "Harness tools",
      "kind": "harness",
      "server": null,
      "available": true,
      "admin_only": false,
      "tools": [
        { "name": "Read", "description": "Read a file.", "risk": "read" }
      ]
    }
  ],
  "discovered_at": "2026-08-23T12:00:00Z",
  "errors": [],
  "is_admin": true
}
```

`kind` is one of `harness`, `app`, `ai`, `computer_use`, `browser`, `app_config` or `mcp`; `server` names the MCP server for `mcp` groups and is null otherwise. A group whose tools are unusable in this install (an AI tool with no API key, computer use turned off) reports `available: false`, and owner-only groups carry `admin_only: true` rather than being hidden. A server that fails to answer still appears — with an empty `tools` list plus an entry in `errors` — so a broken server is visible instead of silently missing.

This route replaced `GET /api/v1/tools`, `/api/v1/tools/builtin` and `/api/v1/tools/discover`, which are gone.

## Documentation

### GET /api/v1/documentation

List all documentation pages (this docs system).

### GET /api/v1/documentation/{category}/{slug}

Get a specific documentation page with full markdown content.

### GET /api/v1/documentation/search?q={query}

Search across all documentation.
