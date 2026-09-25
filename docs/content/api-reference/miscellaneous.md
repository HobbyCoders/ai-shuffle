<!--
title: Miscellaneous
description: Tags, analytics, git, GitHub, files, settings, and other endpoints
order: 18
-->

# Miscellaneous API

Additional API endpoints for tags, analytics, git operations, settings, and more.

## Tags

Organize sessions with custom tags. **Auth:** Required for every tag endpoint.

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

`name` is 1 to 50 characters. `color` is a `#rrggbb` hex color (default `#6366f1`). Returns `201` with `id`, `name`, `color`, `created_at` and `updated_at`.

### GET /api/v1/tags/{tag_id}

Get one tag. Returns `404` for an unknown tag.

### PATCH /api/v1/tags/{tag_id}

Rename or recolor a tag. Send `name`, `color` or both; fields you leave out keep their value.

```bash
curl -X PATCH http://localhost:8000/api/v1/tags/tag-1a2b3c4d \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"color": "#22c55e"}'
```

### DELETE /api/v1/tags/{tag_id}

Delete a tag. Returns `204`, or `404` for an unknown tag.

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

## Usage Limits

### GET /api/v1/usage/limits

Get the latest usage readings: the Claude windows the Claude CLI reported while it ran, and a cached usage row for each other provider (Codex, OpenRouter, OpenAI, fal.ai, SuperGrok and others). A plain read never waits on a provider; rows that are due are refreshed in the background. Read again while `provider_refreshing` is `true` to get the new rows as they arrive.

**Auth:** Required.

**Query parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `refresh` | boolean | Ask for a refresh now (the usage popover sends this when it opens). Default `false` |

```bash
curl "http://localhost:8000/api/v1/usage/limits?refresh=true" \
  -H "Authorization: Bearer aih_your_key"
```

`refresh=true` is limited:

- Each server worker starts at most one refresh every 30 seconds. Inside that window the endpoint returns the cached rows.
- A refresh skips a provider whose row was refreshed or attempted in the last 30 seconds, including by another worker that shares the database.
- Each credential still makes at most one request per provider interval (300 seconds, or 900 seconds for OpenAI Admin).
- On the Claude side nothing is requested: the stored windows are saved and sent to connected clients again, at most once every 30 seconds.

**Response fields** (among others):

| Field | Description |
|-------|-------------|
| `windows[].observed_at` | Epoch ms when the Claude CLI last reported this window. Each window has its own, so an old window still shows its age after a newer event updates another one. The same field appears in `snapshots[].windows[]` |
| `providers` | One row per configured provider. `null` when the cached rows could not be read; keep the rows you already show instead of clearing them |
| `providers[].last_success_at` | Epoch ms of the last successful reading (same as `fetched_at`) |
| `providers[].last_attempt_at` | Epoch ms of the last attempt. A failed attempt moves only this field |
| `providers[].cooldown_until` | Epoch ms before which a refresh, forced or not, serves this cached row instead of asking the provider again. `null` when a refresh may ask now, and while a refused capability is paused (see `last_error_kind`) |
| `provider_refreshing` | `true` while a refresh is running |

When a Codex or SuperGrok banked reset is used (`POST /api/v1/model-providers/{provider_id}/codex/rate-limit-reset-credits/consume`, or `.../grok/...`) and the outcome is `reset`, the provider's windows that were 100% used are removed from its row. The other windows keep their last reading until the provider next reports usage.

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

A staged file's `status` is the git index code: `A` (added), `M` (modified), `D` (deleted), `R` (renamed), `C` (copied) or `T` (type changed, for example a file replaced by a symlink). A rename may instead appear as two entries, `D` for the old path and `A` for the new one, because the faster status reader does not detect renames. The response also carries `head_commit`, `remote_url` and `differs_from_base`.

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

### POST /api/v1/projects/{project_id}/git/init

Turn the project folder into a git repository. Optionally commit its files and create a GitHub repository as `origin`.

**Auth:** Required. Optional query `session_id` acts on that session's checkout.

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj_abc123/git/init \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"create_initial_commit": true, "create_github_repo": true, "github_repo_name": "my-app"}'
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `create_initial_commit` | boolean | `true` | Commit every file as "Initial commit" |
| `create_github_repo` | boolean | `false` | Create a GitHub repository (needs `github_repo_name` and a signed-in GitHub CLI) and push the initial commit to it |
| `github_repo_name` | string | | Up to 100 characters |
| `github_private` | boolean | `true` | Make the GitHub repository private |
| `default_branch` | string | `main` | Name of the first branch |

Returns `success`, `message`, `repository_id`, `github_repo_url` and `github_repo_name`. Returns `400` if the folder is already a repository or GitHub is not signed in.

### POST /api/v1/projects/{project_id}/git/clone

Clone a remote repository into the project folder. The folder must not already be a repository.

**Auth:** Required. Optional query `session_id` acts on that session's checkout.

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj_abc123/git/clone \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"clone_url": "https://github.com/acme/my-app.git"}'
```

`clone_url` (required, up to 500 characters) is an HTTPS or SSH URL. Returns `success`, `message` and `repository_id`; `400` if the folder is already a repository, `500` if the clone fails.

### POST /api/v1/projects/{project_id}/git/generate-commit-message

Write a commit message for the current changes with AI.

**Auth:** Required. Optional query `worktree_id` or `session_id`.

Body: `{"include_unstaged": true}` (default `true`; `false` uses only staged changes). Returns `{"message": "..."}`.

### POST /api/v1/projects/{project_id}/git/generate-pr-description

Write a pull-request title and description from the branch's commits with AI.

**Auth:** Required. Optional query `worktree_id` or `session_id`.

Body: `{"base_branch": "main"}` (default `main`). Returns `{"title": "...", "body": "..."}`.

### POST /api/v1/projects/{project_id}/git/generate-branch-name

Suggest a branch name for a task.

**Auth:** Required

```json
{ "message": "The login page times out after 5 seconds", "prefix": "fix" }
```

`message` (required) is 1 to 2000 characters. `prefix` is optional (for example `feat`, `fix`, `chore`); without it one is chosen from the message. Returns `{"branch_name": "fix/login-page-timeout", "display_name": "Login Page Timeout"}`. If AI is unavailable, the name is made from the message's words.

### POST /api/v1/projects/{project_id}/git/ship

Ship It: commit all changes, push, open a pull request (or reuse the open one), merge it, and optionally clean up. It runs in the background; the response is the first snapshot of the operation.

**Auth:** Required

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj_abc123/git/ship \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"merge_method": "squash", "cleanup": true}'
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `include_unstaged` | boolean | `true` | Stage all changes before committing |
| `merge_method` | string | `squash` | `merge`, `squash` or `rebase` |
| `base_branch` | string | repository default | Base of the pull request |
| `draft` | boolean | `false` | Open the pull request as a draft |
| `cleanup` | boolean | `false` | Remove the worktree, or delete the local branch, after the merge |
| `worktree_id` | string | | Act on this worktree instead of the project folder |
| `session_id` | string | | Act on this session's checkout |

```json
{
  "operation_id": "3f9c2a...",
  "status": "running",
  "steps": [
    { "id": "commit", "label": "Commit changes", "status": "running" },
    { "id": "push", "label": "Push commits", "status": "pending" },
    { "id": "create_pr", "label": "Create pull request", "status": "pending" },
    { "id": "merge", "label": "Merge pull request", "status": "pending" },
    { "id": "cleanup", "label": "Clean up branch", "status": "pending" }
  ],
  "result": null
}
```

Returns `400` when the project has no GitHub remote or the current branch is the base branch, `409` while another Ship It or Finish runs for the project, and `502` when GitHub cannot be reached.

### POST /api/v1/projects/{project_id}/git/finish

Finish: push and merge the open pull request for the current branch as it is, then optionally clean up. The working tree must be clean and the branch must have an open pull request (otherwise `400`). Steps: `guard`, `push`, `merge`, `cleanup`.

**Auth:** Required

Body fields: `merge_method` (default `squash`), `cleanup` (default `false`), `worktree_id`, `session_id`, as for Ship It. Same response and errors as Ship It.

### GET /api/v1/projects/{project_id}/git/operations/{operation_id}

Poll a Ship It or Finish operation until `status` is `done` or `error`.

**Auth:** Required. Optional query `session_id`.

Returns the snapshot shown above. Each step's `status` is `pending`, `running`, `done`, `error`, `warning` or `skipped`, with optional `error` and `detail`. When done, `result` can hold `pr_number`, `pr_url` and `worktree_deleted`. Finished operations are kept for 10 minutes, then return `404`.

### GET /api/v1/projects/{project_id}/git/pr-state

The pull request for the current branch.

**Auth:** Required. Optional query `worktree_id` or `session_id`.

```json
{
  "current_branch": "fix/login-timeout",
  "has_github": true,
  "open_pr": { "number": 42, "title": "Fix login timeout", "url": "https://github.com/acme/my-app/pull/42", "draft": false, "state": "OPEN", "mergeable": true },
  "merged_head_sha": null,
  "pr_lookup_failed": false,
  "stale": false
}
```

`has_github` is `false` without a GitHub remote or a signed-in GitHub CLI. `mergeable` is `null` while GitHub is still computing it. With no open pull request, `merged_head_sha` is the last merged pull request's head, so already-merged work can be recognized. When GitHub cannot be reached, `pr_lookup_failed` is `true`; `open_pr` is then the last known answer with `stale: true`, or `null` when none is known, which means unknown rather than "no pull request".

### Worktrees

Worktrees give a session its own branch and folder. Create one with [`POST /api/v1/sessions/with-worktree`](/docs/api-reference/sessions#worktree-sessions).

**Auth:** Required for every worktree endpoint.

| Method | Path | Query | Notes |
|--------|------|-------|-------|
| `GET` | `/git/worktrees` | `include_status` (default `true`) | Returns `worktrees` and `total`. With `include_status`, each existing worktree has `git_status` (`is_clean`, `current_branch`, `modified_count`, `staged_count`, `untracked_count`) |
| `GET` | `/git/worktrees/{worktree_id}` | | One worktree |
| `DELETE` | `/git/worktrees/{worktree_id}` | `keep_branch` (default `true`) | Remove the worktree folder; `keep_branch=false` also deletes its branch. Returns `success` and `message` |
| `POST` | `/git/worktrees/{worktree_id}/reopen` | | Recreate a removed worktree on the same branch (a new branch from the default branch if it was deleted) so its sessions can continue. `400` if it cannot be recreated |
| `GET` | `/git/worktrees/{worktree_id}/files` | `path` (default root), `show_hidden` (default `false`) | List a folder in the worktree. Returns `files` (each with `name`, `type`, `size`, `path`, `modifiedAt`), `path`, `worktreeId`, `worktreePath` |

Paths in the table are relative to `/api/v1/projects/{project_id}`. A worktree has `id`, `branch_name`, `worktree_path`, `base_branch`, `status`, `created_at`, `exists` (whether the folder is on disk), `sessions`, `session_count` and `active_session`.

```bash
curl -X DELETE "http://localhost:8000/api/v1/projects/proj_abc123/git/worktrees/wt_123?keep_branch=false" \
  -H "Authorization: Bearer aih_your_key"
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

### Media tool models

Choose the fal.ai model each media tool uses. Any model id is accepted; it is not checked against a catalog. Surrounding whitespace is trimmed, and an empty `model` returns `400`.

**Auth:** Owner only

| Method | Path | Body | Notes |
|--------|------|------|-------|
| `PATCH` | `/api/v1/settings/integrations/image/model` | `model` (string, required) | Text to image. Options key `image_model`. |
| `PATCH` | `/api/v1/settings/integrations/image/edit-model` | `model` (string, required) | Image editing. Options key `image_edit_model`. |
| `PATCH` | `/api/v1/settings/integrations/video/model` | `model` (string, required) | Text to video. Options key `video_model`. |
| `PATCH` | `/api/v1/settings/integrations/video/i2v-model` | `model` (string, required) | Image to video. Options key `i2v_model`. |
| `PATCH` | `/api/v1/settings/integrations/3d/model` | `model` (string, required) | Text to 3D. Options key `model3d_model`. |
| `PATCH` | `/api/v1/settings/integrations/3d/i2-3d-model` | `model` (string, required) | Image to 3D. Options key `image_to_3d_model`. |
| `PATCH` | `/api/v1/settings/integrations/audio/tts` | `model` (string, required) | Text to speech. Options key `tts_model`. |
| `PATCH` | `/api/v1/settings/integrations/audio/stt` | `model` (string, required) | Speech to text for the transcription tool. Microphone voice input keeps its own model and is not changed. |
| `PATCH` | `/api/v1/settings/integrations/audio/generation-model` | `model` (string, required) | Text to audio (music and sound effects). Options key `audio_model`. |
| `PATCH` | `/api/v1/settings/integrations/audio/audio-to-audio-model` | `model` (string, required) | Audio to audio (remix, style transfer, restoration). Options key `audio_to_audio_model`. |

```bash
curl -X PATCH http://localhost:8000/api/v1/settings/integrations/image/model \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model": "fal-ai/flux-pro/v1.1-ultra"}'
```

Returns `{"success": true, "model": "fal-ai/flux-pro/v1.1-ultra"}`. [Model options](#get-apiv1settingsintegrationsmodel-params) saved under the tool's options key are kept when the model changes; the app clears them (`"params": {}`) after every model change so the new model starts from its own defaults.

### Media tool defaults

Default generation settings for tools that have them. All fields are optional and only the fields you send are saved. Values are not checked against the selected model.

**Auth:** Owner only

| Method | Path | Body | Notes |
|--------|------|------|-------|
| `PATCH` | `/api/v1/settings/integrations/image/settings` | `aspect_ratio` (string), `resolution` (string) | Text to image. An empty string clears a value. |
| `PATCH` | `/api/v1/settings/integrations/video/settings` | `aspect_ratio` (string), `duration` (integer), `resolution` (string) | Text to video. An empty string clears `aspect_ratio` or `resolution`. |
| `PATCH` | `/api/v1/settings/integrations/tts` | `model` (string), `voice` (string), `speed` (number), `output_format` (string) | Text to speech. `speed` must be 0.1 to 5.0 (`400` otherwise; `model` and `voice` in the same request are still saved). `output_format` is `wav`, `mp3`, `ogg` or `flac`; other values are ignored. Empty `model` or `voice` is ignored. |

```bash
curl -X PATCH http://localhost:8000/api/v1/settings/integrations/tts \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"voice": "af_heart", "speed": 1.1, "output_format": "mp3"}'
```

Each returns `{"success": true}`.

### GET /api/v1/settings/integrations/model-params

Extra options saved for one media tool's model, taken from that model's own input schema. They are added to every request the tool sends to fal.ai.

**Auth:** Owner only

**Query:** `key` (required): the tool's options key from [Media tool models](#media-tool-models), for example `image_model`. An unknown key returns empty `params`.

```bash
curl "http://localhost:8000/api/v1/settings/integrations/model-params?key=image_model" \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

```json
{"key": "image_model", "params": {"guidance_scale": 3.5}}
```

`params` is `{}` when nothing is saved.

### PATCH /api/v1/settings/integrations/model-params

Replace the saved options for one key.

**Auth:** Owner only

**Body (JSON):** `key` (string, required; blank returns `400`), `params` (object, default `{}`). Entries whose value is `null` or an empty string are dropped so the model's own default applies. Send `"params": {}` to clear everything.

```bash
curl -X PATCH http://localhost:8000/api/v1/settings/integrations/model-params \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key": "image_model", "params": {"guidance_scale": 3.5, "seed": ""}}'
```

Returns `{"success": true, "params": {"guidance_scale": 3.5}}` with the options as saved.

### GET /api/v1/settings/security-instructions

The base instructions included in the system prompt of every chat.

**Auth:** Owner only

```json
{
  "current": "…",
  "default": "…",
  "is_customized": false
}
```

`current` is the text in use: your custom text, or the built-in default when none is saved. `default` is always the built-in text, so you can compare or start from it.

### PUT /api/v1/settings/security-instructions

Replace the instructions with your own text.

**Auth:** Owner only

**Body (JSON):** `instructions` (string, required). Surrounding whitespace is trimmed; empty text returns `400` (use `DELETE` to go back to the default).

```bash
curl -X PUT http://localhost:8000/api/v1/settings/security-instructions \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"instructions": "Never run destructive commands without asking first."}'
```

Returns `{"success": true, "message": "Security instructions updated successfully"}`.

### DELETE /api/v1/settings/security-instructions

Remove your custom text and go back to the built-in default.

**Auth:** Owner only

Returns `{"success": true, "message": "Security instructions reverted to default"}`.

### GET /api/v1/settings/application-updates

The update policy for your account and the updater status of this worker.

**Auth:** Required

```json
{
  "allow_updates": true,
  "update_all_workers": false,
  "supported": true,
  "phase": "available",
  "version": "…",
  "running_version": "…",
  "error": null
}
```

`allow_updates` is `true` until you turn it off. `update_all_workers` sends update actions to every worker on the account instead of only this one. `supported` is `false` when this install has no updater; `phase` is then `idle`. Otherwise `phase` is the updater's state, such as `checking`, `available`, `downloading`, `ready` (downloaded, waiting to install) or `error`. `version` is the update on offer and `running_version` the build now running. When present, `message`, `release` and `notes` describe the release.

### PUT /api/v1/settings/application-updates

Change the update policy.

**Auth:** Owner only. Refused with `403` when the request comes from an agent's in-app browser preview.

**Body (JSON):** `allow_updates` (boolean), `update_all_workers` (boolean). Send at least one. Values must be real JSON booleans, and unknown fields are rejected; any of these returns `422`.

```bash
curl -X PUT http://localhost:8000/api/v1/settings/application-updates \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"update_all_workers": true}'
```

Returns the same body as `GET`. Turning `allow_updates` off also cancels an update request that has not started yet.

### POST /api/v1/settings/application-updates

Ask the updater to check for, download or install an update.

**Auth:** Owner only. Refused with `403` when the request comes from an agent's in-app browser preview.

**Body (JSON):** `action` (required): `check`, `download` or `install`.

```bash
curl -X POST http://localhost:8000/api/v1/settings/application-updates \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "download"}'
```

```json
{
  "scope": "current",
  "results": [
    {"device_id": "…", "device_name": "Current worker", "accepted": true, "reason": "Request queued", "phase": "available", "version": "…"}
  ]
}
```

With `update_all_workers` on, `scope` is `all` and the action also goes to every other worker on the account; this worker is always queued last. Each worker decides for itself and says why in `reason` when it declines: updates disabled, an update already in progress, nothing to download, nothing downloaded to install, or sessions still running. Offline workers are reported as not queued, and `accepted` is `null` for a worker that could not be reached. Returns `409` when updates are turned off or a previous install is still waiting for workers to finish starting.

### GET /api/v1/settings/application-updates/workers

Update status of this worker and, when `update_all_workers` is on or an install is being tracked, every other worker on the account.

**Auth:** Owner only

```json
{
  "allow_updates": true,
  "update_all_workers": true,
  "operation": null,
  "workers": [
    {"device_id": "…", "device_name": "Current worker", "is_self": true, "online": true, "supported": true, "allow_updates": true, "phase": "ready", "version": "…", "running_version": "…", "percent": null, "message": null, "error": null, "reason": null}
  ]
}
```

`operation` tracks the most recent install across workers (`null` when there is none). Its `phase` is `waiting`, `complete` or `error`, and each entry in its `workers` has a `status` and `message`.

### GET /api/v1/settings/claude-auth

Which credential Claude Code agents run on: your Claude subscription (the signed-in CLI) or an Anthropic API key.

**Auth:** Required. Add `?refresh=true` to ask the CLI again instead of using its last answer.

```json
{
  "mode": "subscription",
  "mode_label": "Claude subscription",
  "explicit": true,
  "modes": [{"value": "subscription", "label": "Claude subscription"}, {"value": "api_key", "label": "Anthropic API key"}],
  "cli": {"installed": true, "signed_in": true, "email": "me@example.com", "org_name": null, "plan": "max", "auth_method_label": "Claude account", "error": null},
  "api_key": {"set": false, "last4": null},
  "gate": {"ready": true, "reason": null, "mode": "subscription", "claude_credential_ready": true, "license_allows_runs": true, "startup_state": "ready"}
}
```

`gate.ready` says whether new agent runs can start; `gate.reason` says why not. The CLI account fields are filled only when signed in.

### PUT /api/v1/settings/claude-auth

Choose the mode. Takes effect on the next agent run.

**Auth:** Owner only

```bash
curl -X PUT http://localhost:8000/api/v1/settings/claude-auth \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode": "api_key"}'
```

`mode` is `subscription` or `api_key` (other values return `400`). Store the key with `POST /api/v1/settings/integrations/anthropic`. Returns the same body as `GET`.

### GET /api/v1/settings/loop-guard

The loop guard limits how many unattended turns (automations, scheduled wake-ups, background workers and connectors) run on a Claude subscription each hour. Past the budget, the next unattended turn is held until you confirm or dismiss it. It applies only in `subscription` mode, never to turns a person starts.

**Auth:** Required

```json
{
  "enabled": true,
  "session_turns_per_hour": 12,
  "install_turns_per_hour": 60,
  "worker_max_minutes": 60,
  "defaults": {"enabled": true, "session_turns_per_hour": 12, "install_turns_per_hour": 60, "worker_max_minutes": 60},
  "applies": true,
  "unattended_sources": ["automation", "wakeup", "provider_agent", "connector"],
  "usage": {"install_last_hour": 3},
  "held": [{"id": "b41c...", "session_id": "...", "source": "automation", "prompt": "...", "reason": "...", "held_at": 1758790000.0}]
}
```

`worker_max_minutes` is the default time limit for a background worker that has none of its own. `applies` is `true` when the guard is enabled and the subscription mode is in use. Counts reset when the worker restarts.

### PUT /api/v1/settings/loop-guard

Change any of `enabled`, `session_turns_per_hour` (1 to 1000), `install_turns_per_hour` (1 to 10000) and `worker_max_minutes` (0 to 1440). Fields you leave out keep their value. Out-of-range values return `400`. Returns the same body as `GET`.

**Auth:** Owner only

```bash
curl -X PUT http://localhost:8000/api/v1/settings/loop-guard \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"session_turns_per_hour": 20}'
```

### POST /api/v1/settings/loop-guard/held/{held_id}/confirm

Run a held turn now, past the budget. Returns `{"success": true, "turn_id": "..."}`, or `404` when the held turn is gone.

**Auth:** Owner only

### POST /api/v1/settings/loop-guard/held/{held_id}/dismiss

Drop a held turn without running it. Returns `{"success": true}`, or `404`.

**Auth:** Owner only

## Media Model Catalog

The image, video, audio and 3D models available through your fal.ai key (see `POST /api/v1/settings/integrations/fal-ai`). The list is cached for an hour.

**Auth:** Required

### GET /api/v1/catalog/models

| Query | Description |
|-------|-------------|
| `q` | Search name, description or model id |
| `category` | fal.ai category, such as `text-to-image` |
| `tool_type` | Tool type, such as `image_generation` |
| `output_type` | `image`, `video`, `3d` or `audio` |

```bash
curl "http://localhost:8000/api/v1/catalog/models?output_type=video&q=kling" \
  -H "Authorization: Bearer aih_your_key"
```

Returns `models`, `total` (matches) and `catalog_size`. Each model has `endpoint_id`, `display_name`, `category`, `description`, `status`, `thumbnail_url`, `tags`, `group_key`, `group_label`, `updated_at`, `tool_type` and `output_type`.

### GET /api/v1/catalog/categories

Returns `categories` (each `category`, `count`, `tool_type`) and `total_models`.

### GET /api/v1/catalog/schema

The input parameters of one model. Query: `endpoint_id` (required) and `refresh` (default `false`, bypasses the cache). Returns `{"found": true, "schema": {"endpoint_id", "primary_text_field", "fields": [...]}}`, where each field has `name`, `type`, `title`, `description`, `required`, `default`, `enum`, `minimum`, `maximum`, `format`, `is_file` and `is_primary_text`. Returns `{"found": false, "endpoint_id": "..."}` when the model publishes no schema.

### POST /api/v1/catalog/refresh

Reload the catalog from fal.ai now. Returns `success`, `models_fetched` and `categories`.

## Computer Use

Lets agents see and control this computer's screen. Available only where AI Shuffle runs on a desktop with a display, not in Docker.

### GET /api/v1/computer-use/status

**Auth:** Required

```json
{
  "available": true,
  "reason": "ok",
  "enabled": true,
  "active": false,
  "display": null,
  "display_label": null,
  "input_mode": "shared_desktop"
}
```

`available` and `reason` say whether this host supports it (`reason` explains why not). `active` is `true` while an agent is controlling the screen; `display_label` then names the display. The response also has live details (`action`, `busy`, `cursor`, `geometry`, `seconds_since`, `stop_reason`, `escape_available`, `overlay_available`).

### GET /api/v1/computer-use/enabled

Returns `{"enabled": false}`, the global switch.

**Auth:** Required

### PUT /api/v1/computer-use/enabled

Turn computer use on or off for all agents. Turning it off also stops any agent that is controlling the screen.

**Auth:** Owner only

```bash
curl -X PUT http://localhost:8000/api/v1/computer-use/enabled \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

### POST /api/v1/computer-use/stop

Take the screen back from the agent now. It does not turn computer use off. Returns `{"stopped": 1}`, the number of sessions stopped.

**Auth:** Required

## License

License status and seats for this worker. See [License](/docs/getting-started/license) for how licenses work.

### GET /api/v1/license

**Auth:** Required

```json
{
  "mode": "licensed",
  "state": "active",
  "allowed": true,
  "reason": null,
  "banner": null,
  "seats": 3,
  "seats_used": 1,
  "install_id": "...",
  "expires_at": "2026-10-02T00:00:00+00:00",
  "grace": false,
  "last_refresh_at": "2026-09-25T08:00:00+00:00",
  "next_refresh_at": "2026-09-26T08:00:00+00:00",
  "last_error": null
}
```

The response also has `plan`, `key_hint`, `issued_at` and `grace_until`. `state` is `beta`, `unlicensed`, `invalid`, `active`, `trialing`, `expiring`, `grace`, `ending` or `expired`. `allowed` says whether new agent runs can start; when `false`, `reason` says why. `banner` (`severity`, `message`) is the notice the app shows, or `null`. During the beta, `mode` and `state` are `beta`.

### POST /api/v1/license/activate

Activate this worker with a license key.

**Auth:** Owner only. Refused (`403`) for requests from an agent preview.

```bash
curl -X POST http://localhost:8000/api/v1/license/activate \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key": "ASH-XXXXX-XXXXX-XXXXX-XXXXX"}'
```

`key` is 1 to 64 characters; no other fields are accepted. On success returns `activated: true` with the status fields. When every seat is taken it returns `200` with `activated: false`, `detail`, `code: "seats_full"`, `seats` and `installs` (the workers holding seats), so you can free one with `release`.

### POST /api/v1/license/release

Free the seat another worker holds. Body: `{"key": "ASH-...", "install_id": "..."}` (`install_id` from the `installs` list, 8 to 128 characters). Returns `{"ok": true, "seats_used": 1}`.

**Auth:** Owner only. Refused for agent previews.

### POST /api/v1/license/deactivate

Deactivate this worker and free its seat. No body. Returns the status fields plus `seat_released`.

**Auth:** Owner only. Refused for agent previews.

### POST /api/v1/license/refresh

Renew the lease with the license server now. No body. Returns the status fields.

**Auth:** Owner only. Refused for agent previews.

## Notifications

The notification history behind the bell. Each caller (the owner, or each API key) has its own.

**Auth:** Required

### GET /api/v1/notifications

Query: `include_dismissed` (default `false`) and `limit` (default 100, at most 200). Returns `notifications` (newest first) and `unread` (count). Each has `id`, `severity`, `surface`, `category`, `source` (`server` or `client`), `title`, `message`, `session_id`, `read`, `dismissed`, `created_at` (milliseconds since the epoch) and `action` (`kind` and `payload`, or `null`).

### POST /api/v1/notifications

Add a notification to the history.

```bash
curl -X POST http://localhost:8000/api/v1/notifications \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"severity": "info", "title": "Export ready", "message": "The report is in your workspace."}'
```

All fields are optional: `id` (sending the same `id` again does not duplicate it), `severity` (default `info`), `surface` (`notification`, `banner`, `toast` or `inline`; default `notification`), `category`, `title`, `message`, `session_id` and `action` (`{"kind": "...", "payload": {...}}`). Returns the stored notification.

### POST /api/v1/notifications/{notification_id}/read

Mark one read. Returns `{"ok": true}`, or `false` when it was not found.

### POST /api/v1/notifications/read-all

Mark all read. Returns `{"updated": 3}`.

### POST /api/v1/notifications/{notification_id}/dismiss

Hide one from the default list. Returns `{"ok": true}`.

### DELETE /api/v1/notifications

Clear the whole history. Returns `{"deleted": 12}`.

## Preferences

User preferences are stored and accessed per key. The owner and each API user have their own preferences.

### GET /api/v1/preferences/all

Get all preferences for the current user.

### GET /api/v1/preferences/{key}

Get a single preference value.

### PUT /api/v1/preferences/{key}

Set a preference value. `POST /api/v1/preferences/{key}` does the same, for `navigator.sendBeacon()` saves on page unload.

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

### GET /api/generated-audio/

List AI-generated audio. The list endpoints return `images`, `videos` or `audio`: each item has `filename`, `url`, `size_bytes` and `created_at`.

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

### POST /api/v1/workspace/upload

Upload a file into the workspace folder, for example to attach it to a chat message.

**Auth:** Required. A project-scoped API key can only upload into its project's folders.

Multipart form fields: `file` (required) and `path` (optional folder, relative to the workspace; default the workspace root). The limit is 50 MB (`413` above it). File names starting with `.` are refused.

```bash
curl -X POST http://localhost:8000/api/v1/workspace/upload \
  -H "Authorization: Bearer aih_your_key" \
  -F "file=@notes.txt" \
  -F "path=my-project/docs"
```

Returns `filename`, `path` (relative to the workspace), `full_path`, `size` and `sizeFormatted`.

### GET /api/v1/artifacts/{hash}

Download a stored artifact, such as a screenshot an agent took, by its SHA-256 hash (64 lowercase hex characters). Chat events link to these as `/api/v1/artifacts/{hash}`.

**Auth:** Required, or a signed link. The caller must be able to open the session that created the artifact; pass `?session_id=` to open it through another session that references it.

Returns the file with its content type. When another worker in your network holds the file, it is fetched from there or the response redirects (`307`) to a signed link. Unknown or malformed hashes return `404`.

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

## Logs

### GET /api/v1/logs

Recent server log lines, newest first, from an in-memory buffer of the last 5000.

**Auth:** Owner only

| Query | Default | Description |
|-------|---------|-------------|
| `limit` | 500 | 1 to 5000 |
| `level` | | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` |
| `search` | | Case-insensitive text match |

Returns `lines` (each `timestamp`, `level`, `logger_name`, `message`), `total` (buffered lines), `returned` and `levels` (levels present).

### GET /api/v1/logs/export

The whole buffer as a downloadable text file.

**Auth:** Owner only

### POST /api/v1/logs/client-error

Record an error seen by a client in the error log.

**Auth:** Required

```json
{ "message": "Failed to load session", "source": "frontend", "level": "ERROR", "url": "/chat/sess_abc123", "status_code": 500 }
```

`message` is required. `source` is `frontend`, `ws` or `api` (other values count as `frontend`); `level` defaults to `ERROR`. Optional: `detail`, `url`, `status_code`, `session_id`, `context` (object). Returns `{"ok": true, "id": ...}`.

### GET /api/v1/logs/errors

The stored error log, most recent first.

**Auth:** Owner only

Query: `limit` (default 100, 1 to 500), `source` (`frontend`, `backend`, `ws` or `api`), `level` and `session_id`. Returns `errors` and `returned`.

## Documentation

### GET /api/v1/documentation

List all documentation pages (this docs system).

### GET /api/v1/documentation/{category}/{slug}

Get a specific documentation page with full markdown content.

### GET /api/v1/documentation/search?q={query}

Search across all documentation.
