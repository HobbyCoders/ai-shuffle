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

### GET /api/v1/analytics/providers

Get turns, tokens and estimated cost per provider (Claude, ChatGPT/Codex, OpenAI, xAI/Grok, OpenRouter, custom endpoints) for `start_date`..`end_date`. Each item has a `billing` (`subscription`, `api`, `mixed` or `unknown`; a subscription's cost is what the same usage would cost at API prices) and a `cost_status` (`estimated`, `partial`, or `unknown` when the model has no known price, in which case the cost is missing rather than zero). `inferred_query_count` counts turns recorded before per-provider tracking, which are attributed to their session's current provider.

**Auth:** Required. API users see only their own usage.

### GET /api/v1/analytics/export

Download the per-provider usage for `start_date`..`end_date` as CSV: one `day` row per provider per day, then one `range_total` row per provider. `estimated_cost_usd` is blank when the cost is unknown.

**Auth:** Required. API users see only their own usage.

## Usage Limits

### GET /api/v1/usage/limits

Get the latest usage readings: the Claude windows the Claude CLI reported while it ran, and the last saved reading for each other provider that has one. A plain read never waits on a provider; rows that are due are refreshed in the background. Read again while `provider_refreshing` is `true` to get the new rows as they arrive.

Which providers get a row:

- **ChatGPT (Codex) sign-in:** the subscription windows, read from the provider's own replies. Nothing extra is requested.
- **OpenRouter:** the model key's spend and limit. With an [OpenRouter usage key](/docs/api-reference/model-providers#openrouter-usage-key), a second row (`<provider name> account`) shows the account credit balance.
- **SuperGrok sign-in:** the subscription allowance.
- **OpenAI API:** the month's spend, only with an [OpenAI usage key](/docs/api-reference/model-providers#openai-usage-key). Without one, the row reports `last_error_kind` `missing_credentials`.
- **fal.ai:** the account credit balance, read with the [fal.ai API key](#post-apiv1settingsintegrationsfal-ai) the media tools use. No extra billing key is needed.

xAI API keys, other OpenAI-compatible hosts and the Anthropic API key get no row: they report only per-minute rate limits. Rows of a deleted provider are dropped.

**Auth:** Required. Every reading is the owner's subscription and provider billing, so an API key gets an empty reading (`"available": false`, no `windows`, `"providers": []`) and its request never starts a refresh. The agent tool `GetUsage` likewise returns nothing in an API key's session.

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
| `windows[].observed_at` | Epoch ms when the Claude CLI last reported this window. Each window has its own, so an old window still shows its age after a newer event updates another one. A repeated identical reading moves it too; the shared copy other devices read is updated at most once a minute per window. A window the CLI reports as rejected counts as fully used even when the report has no utilization. The same field appears in `snapshots[].windows[]` |
| `providers` | The provider rows described above. `null` when the cached rows could not be read; keep the rows you already show instead of clearing them |
| `providers[].status` | `fresh` (read within the provider's interval), `stale` (older, or the last attempt failed), `unavailable` (failed with no reading yet) or `never_fetched` |
| `providers[].last_error_kind` | Why the last attempt failed: `billing_access_denied`, `missing_credentials`, `timeout`, `unavailable` or `unsupported`; `null` after a success. After `billing_access_denied` or `unsupported` the provider is not asked again until its key changes |
| `providers[].last_success_at` | Epoch ms of the last successful reading (same as `fetched_at`) |
| `providers[].last_attempt_at` | Epoch ms of the last attempt. A failed attempt moves only this field |
| `providers[].cooldown_until` | Epoch ms before which a refresh, forced or not, serves this cached row instead of asking the provider again. `null` when a refresh may ask now, and while a refused capability is paused (see `last_error_kind`) |
| `provider_refreshing` | `true` while a refresh is running |

When a Codex or SuperGrok banked reset is used (`POST /api/v1/model-providers/{provider_id}/codex/rate-limit-reset-credits/consume`, or `.../grok/...`) and the outcome is `reset`, the provider's windows that were 100% used are removed from its row. The other windows keep their last reading until the provider next reports usage.

## Claude subscription resets

A Claude subscription can have two kinds of reset: weekly resets, banked as grants that each hold a number of resets and a use-by date, and the 5-hour reset, which clears the current 5-hour window a set number of times a week. These endpoints read and use them for the Claude Code CLI signed in on this worker.

Each call reads the CLI's current sign-in for that one request. AI Shuffle never refreshes, stores or returns it. When the sign-in has expired, the endpoint answers `409` and asks you to run a Claude turn, which lets the CLI refresh it. On macOS the CLI keeps its sign-in in the Keychain, which AI Shuffle does not read, so these endpoints answer `409` there.

**Auth:** Owner only for every endpoint in this section.

| Method | Path | Body | Notes |
|--------|------|------|-------|
| `GET` | `/api/v1/usage/claude/resets` | | Both kinds of reset |
| `POST` | `/api/v1/usage/claude/resets/weekly` | `grant_id`, `idempotency_key` | Spend one weekly reset |
| `POST` | `/api/v1/usage/claude/resets/session` | `{}` | Use the 5-hour reset |

The status response. `weekly` or `session` is `null` when the account has no such reset:

```json
{
  "weekly": {
    "eligible": true,
    "ineligible_reason": null,
    "at_limit": true,
    "exhausted": ["seven_day"],
    "grants": [
      { "id": "launch", "label": "Launch bonus", "resets_total": 3, "resets_left": 2, "starts_at": null, "ends_at": "2026-10-30T12:00:00Z", "clears": ["seven_day"], "paused": false, "usable_now": true, "use_requires_limit": true, "blocking": [] }
    ],
    "next_grant_id": "launch",
    "weekly_resets_at": null,
    "cooldown_until": null
  },
  "session": {
    "eligible": true,
    "ineligible_reason": null,
    "available": true,
    "next_available_at": null,
    "weekly_resets_at": null,
    "resets_per_week": 1
  }
}
```

A grant with `use_requires_limit` opens only while a weekly limit is hit (`at_limit`). The 5-hour reset's `ineligible_reason` is `not_at_wall` until the 5-hour limit is hit.

The weekly body is JSON with two required strings: `grant_id` (lowercase letters, digits, `-` or `_`, at most 40) and `idempotency_key` (letters, digits, `-` or `_`, at most 64). Any other field is `422`. Reuse the same `idempotency_key` when retrying, so a retry cannot spend a second reset.

```bash
curl -X POST http://localhost:8000/api/v1/usage/claude/resets/weekly \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"grant_id": "launch", "idempotency_key": "b7e1c9d2-reset-1"}'
```

**Weekly response:** `{"result": "reset", "reason": null, "resets_left": 1, "cleared": ["seven_day"], "weekly_resets_at": null, "cooldown_until": null}`. `result` is `reset`, `already_used`, `not_limited`, `cooldown`, `ineligible` or `unavailable`.

**5-hour response:** `{"result": "reset", "next_available_at": null}`. `result` is `reset`, `already_used`, `not_limited`, `ineligible` or `unavailable`.

After a `reset`, the cleared windows in [usage limits](#get-apiv1usagelimits) show as unused until the CLI next reports them.

**Errors:** `409` the CLI is not signed in, its sign-in has expired, it cannot be read on this machine, or it lacks the profile scope. `429` too many reset requests. `502` Claude's reset service is unavailable.

## Git Integration

A git route that takes a `worktree_id` query parameter accepts only a worktree of the project in the path; a worktree of another project returns `404`, the same answer as an unknown id.

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
| `POST` | `/git/push` | Body: `{"remote": "origin", "branch": null, "set_upstream": false, "force": false}` | `branch` defaults to the current branch and must be a plain branch name: a refspec (`:main`, `+main`, `a:b`) or a name starting with `-` returns `400`. `remote` is letters, digits, `.`, `_` and `-`, and must not start with `-` |
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
| `default_branch` | string | `main` | Name of the first branch. Letters, digits, `.`, `_`, `/` and `-`, starting with a letter, digit or `_` (`422` otherwise) |

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

Which sources a caller may clone (`400` otherwise, before anything is cloned):

- **API keys** may clone only HTTP(S) or SSH remotes (`ssh://...` or `git@host:path`). A local path or `file://` URL answers `400` with `API keys can clone only HTTPS or SSH remotes`.
- **The owner** may also clone a local path or a `file:///absolute/path` URL, but only when it resolves inside the workspace or a project folder (`400`: `A local clone source must be inside the workspace or a project folder`). A relative path is read from the clone's target folder.
- **Everyone** is refused a `git://` URL or any other scheme (`Unsupported clone URL: use an HTTPS or SSH URL`), any `<transport>::` remote helper such as `ext::` or `fd::` (`Refusing unsafe clone URL scheme ...`), a value starting with `-`, and a `file://` URL with a host or `%`-encoding.

Credentials in an HTTPS URL (`https://user:token@host/...`) are redacted from the message, from error text and from the logs. No `remote_url` the server stores or returns includes them, neither the repository record nor `git/status`. Only the clone's own git remote in `.git/config` keeps them, so fetch and push go on working. The user in an SSH URL (`git@host:...` or `ssh://git@host/...`) is kept.

### POST /api/v1/projects/{project_id}/git/generate-commit-message

Write a commit message for the current changes with AI.

**Auth:** Required. Optional query `worktree_id` or `session_id`.

Body: `{"include_unstaged": true}` (default `true`; `false` uses only staged changes). Optional `model` selects a native alias or `prov:<providerId>:<modelId>` for this request; omitted uses the Settings text-generation binding. Returns `{"message": "..."}`.

### POST /api/v1/projects/{project_id}/git/generate-pr-description

Write a pull-request title and description from the branch's commits with AI.

**Auth:** Required. Optional query `worktree_id` or `session_id`.

Body: `{"base_branch": "main"}` (default `main`). Optional `model` selects a native alias or `prov:<providerId>:<modelId>` for this request; omitted uses the Settings text-generation binding. Returns `{"title": "...", "body": "..."}`.

### POST /api/v1/projects/{project_id}/git/generate-branch-name

Suggest a branch name for a task.

**Auth:** Required

```json
{ "message": "The login page times out after 5 seconds", "prefix": "fix" }
```

`message` (required) is 1 to 2000 characters. `prefix` is optional (for example `feat`, `fix`, `chore`): a letter or digit followed by up to 31 letters, digits, `_` or `-`. Without it, or when it is not in that form, one is chosen from the message. Returns `{"branch_name": "fix/login-page-timeout", "display_name": "Login Page Timeout"}`. If AI is unavailable, the name is made from the message's words.

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
| `generation_model` | string | Settings default | Native alias or `prov:<providerId>:<modelId>` used for both commit and PR generation |
| `merge_method` | string | `squash` | `merge`, `squash` or `rebase` |
| `base_branch` | string | repository default | Base of the pull request |
| `draft` | boolean | `false` | Open the pull request as a draft |
| `cleanup` | boolean | `false` | Remove the worktree, or delete the local branch, after the merge. Sessions still running in a worktree are stopped first; in the project folder, cleanup is skipped while another session is busy |
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
| `GET` | `/git/worktrees` | `include_status` (default `true`), `focus_worktree_id` | Returns `worktrees` and `total`. With `include_status`, each existing worktree has `git_status` (`is_clean`, `current_branch`, `modified_count`, `staged_count`, `untracked_count`), `status_checked_at` and `status_error`. Statuses come from a server-side cache: only worktrees with recent session activity, or the one named in `focus_worktree_id`, are re-read, so an idle worktree's status can be older than the response. `status_error` is set when the last read failed; that worktree is retried on a back-off of up to 30 minutes |
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

Get integration settings (API key status for various services). Keys are never returned: `fal_api_key_set` and `anthropic_api_key_set` say whether each is stored, and `fal_api_key_masked` and `anthropic_api_key_masked` show the key's first 7 and last 4 characters (`""` for a key under 22 characters). The response also carries `claude_auth_mode`, the media tool models and the media tool defaults.

**Auth:** Required

### POST /api/v1/settings/integrations/fal-ai

Save the fal.ai API key (used for all AI media tools: image, video, 3D, TTS, STT, and to read the fal.ai credit balance in [usage limits](#usage-limits)). Saving the key (or deleting it) also clears the saved fal.ai balance; the next usage refresh reads it again with the new key.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/settings/integrations/fal-ai \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"api_key": "your_fal_key"}'
```

The key is checked with fal.ai before it is saved. A key fal.ai rejects returns `400` and is not saved. Otherwise the key is saved and the response reports the check:

```json
{"success": true, "masked_key": "fal-012...cdef", "valid": true, "reason": null}
```

`valid` is `true` when fal.ai accepted the key and `null` when fal.ai could not be asked (timeout, network error, rate limit or a fal.ai error); `reason` then says why. `masked_key` is the key's first 7 and last 4 characters, or `""` for a key under 22 characters. When credential storage is locked the key is not saved and the response is `409` with recovery guidance. The same `409` applies to `POST /api/v1/settings/integrations/anthropic`.

### Speech and transcription

`POST /api/v1/settings/tts/stream` (JSON `text`, optional `model`, `voice`, `speed`, `output_format`, `params`) returns synthesized audio, and `POST /api/v1/settings/transcribe` (multipart `file`, up to 25 MB) returns `{"text": "...", "duration_seconds": null}`. Speech uses the request's `model`, else the text-to-speech model (`tts_model`). Transcription uses the voice input model (`voice_input_model` in [voice settings](/docs/api-reference/voice#get-apiv1settingsvoice)), not the transcription tool's `stt_model`.

`GET /api/v1/settings/integrations/audio/models` lists the speech and transcription models (`tts_models`, `stt_models`, each with `id`, `name`, `description`, `price_display`, `provider`, `available` and `is_current`) plus `current_tts`, `current_stt`, `fal_configured` and `openai_configured`. For an API key, `available` and `fal_configured` reflect the fal.ai key that key may use under the policy below.

**Auth:** Required

For fal.ai models, the owner's requests use the owner's fal.ai key. A request made with an API key follows that key's fal.ai credential policy, the same one its agent sessions use:

| Policy | Key used |
|--------|----------|
| `user_provided` (default) | The API user's own stored key. Without one the request returns `403`. |
| `optional` | The API user's own key, else the owner's. |
| `admin_provided` | The owner's key. |

Models served by OpenAI use the owner's OpenAI provider key.

Errors from both routes: `400` for invalid input (empty text or text over 4096 characters; an empty, unreadable or over-25 MB file), a missing key or a key the provider rejects; `403` when the credential policy above leaves the API key without one; `429` when the provider rate-limits; `502` when fal.ai or OpenAI returns an error or cannot be reached; `504` when it does not answer in time.

### DELETE /api/v1/settings/integrations/fal-ai

Remove the fal.ai API key.

**Auth:** Owner only

### POST /api/v1/settings/integrations/anthropic

Save the Anthropic API key used by Claude Code. Body: `api_key` and optional `use_for_claude_code` (default `false`); `true` also switches Claude Code to API-key mode (see [claude-auth](#get-apiv1settingsclaude-auth)). Saving a key never changes the mode otherwise.

The key is checked with a one-token request to Anthropic first. A key Anthropic rejects returns `400` and is not saved; a rate-limited check returns `429`. If Anthropic cannot be reached, the key is saved anyway. Returns `{"success": true, "masked_key": "sk-ant-...wxyz"}`.

**Auth:** Owner only

### DELETE /api/v1/settings/integrations/anthropic

Remove the Anthropic API key.

**Auth:** Owner only

### GET /api/v1/settings/text-generation

The default model and effort for internal one-shot AI calls: session titles, commit messages, PR descriptions, branch names, and generated agents. Generation runs outside any session. Agent/sub-agent builders and Git commit, PR and Ship It options offer a **Generation model** picker to override this binding for one request without changing Settings. An explicit selection uses the chosen model and provider with their default effort; automatic background helpers continue to use this Settings default.

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

`allow_updates` is `true` until you turn it off. `update_all_workers` sends update actions to every worker on the account instead of only this one. `supported` is `false` when this install has no updater; `phase` is then `idle`. Otherwise `phase` is the updater's state, such as `checking`, `available`, `downloading`, `ready` (downloaded, waiting to install) or `error`. `version` is the update on offer and `running_version` the build now running. When present, `message`, `release` and `notes` describe the release. `request_pending` is `true` while a queued action has not been taken by the updater yet; a desktop takes one on its next report, within about 30 seconds.

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

With `update_all_workers` on, `scope` is `all` and the action also goes to every other worker on the account; this worker is always queued last. A `download` sent to a worker that has not found the update yet (phase `idle`) is still queued: that worker checks first and downloads only if the check finds an update, and its `reason` says so. Each worker decides for itself and says why in `reason` when it declines: updates disabled, an update already in progress, an update already downloaded, a newer runtime required, nothing downloaded to install, or sessions still running. Offline workers are reported as not queued, and `accepted` is `null` for a worker that could not be reached. Returns `409` when updates are turned off or a previous install is still waiting for workers to finish starting.

### GET /api/v1/settings/application-updates/workers

Update status of this worker and, when `update_all_workers` is on or an install is being tracked, every other worker on the account.

**Auth:** Owner only

```json
{
  "allow_updates": true,
  "update_all_workers": true,
  "operation": null,
  "workers": [
    {"device_id": "…", "device_name": "Current worker", "is_self": true, "online": true, "supported": true, "allow_updates": true, "phase": "ready", "version": "…", "running_version": "…", "percent": null, "message": null, "error": null, "reason": null, "request_pending": false}
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

### Import Claude and Codex settings

Copy instructions, skills, agents, hooks, enabled Claude Code plugins, MCP server definitions and configured projects from a Claude Code or Codex home folder into AI Shuffle. This is **Import from Claude Code and Codex** in **Settings → System → Workspace**. The source folder is only read, and credentials and session history are never read. An existing destination file is never overwritten (it shows as a conflict instead), except that imported hooks are merged into an existing hooks file. Secret-looking MCP values are replaced by `${VAR}` placeholders. Anything that cannot be imported, such as a plugin name that is not a plain `name@marketplace`, is listed with a diagnostic and skipped. In the **Settings and plugins** category, hooks, enabled Claude Code plugins and Codex's model and approval settings are copied; the other settings it lists are shown for reference only.

**Auth:** Owner only

| Method | Path | Body |
|--------|------|------|
| `POST` | `/api/settings/import/preview` | `source_home` (optional), `providers` (`claude`, `codex`; default both) |
| `POST` | `/api/settings/import/apply` | `token` from the preview, `selected_item_ids`, and the same `source_home` and `providers` |

These paths are outside `/api/v1`. The preview returns a `token`, the source and destination folders, `categories` of items (each with `id`, `label`, `status` and `selectable`), `conflicts` and `diagnostics`. Only items with `status` `ready` can be imported. Apply returns `imported`, `skipped`, `conflicts`, `errors` and `imported_projects`; a selected project folder is registered as an AI Shuffle project.

Without `source_home` the server's own home folder is read. In Docker, `source_home` must be that folder (`/home/appuser`) or `/imports/provider-settings`; to import from another machine's home, bind-mount it read-only at `/imports/provider-settings`.

**Errors:** `400` for a missing source folder, a source Docker does not allow, an unknown provider, or a selection with items that were not `ready` in the preview. `409` when the source or destination changed after the preview; preview again.

## Media Model Catalog

The image, video, audio and 3D models available through your fal.ai key (see `POST /api/v1/settings/integrations/fal-ai`). The list is cached for an hour.

**Auth:** Required

### GET /api/v1/catalog/models

| Query | Description |
|-------|-------------|
| `q` | Search name, description, model id or tags |
| `category` | fal.ai category, such as `text-to-image` |
| `tool_type` | Tool type, such as `image_generation` |
| `output_type` | `image`, `video`, `3d` or `audio` |

```bash
curl "http://localhost:8000/api/v1/catalog/models?output_type=video&q=kling" \
  -H "Authorization: Bearer aih_your_key"
```

Returns `models`, `total` (matches) and `catalog_size`. Each model has `endpoint_id`, `display_name`, `category`, `description`, `status`, `thumbnail_url`, `tags`, `group_key`, `group_label`, `updated_at`, `tool_type` and `output_type`.

`tool_type` is the media tool the model can be chosen for: `image_generation`, `image_editing`, `video_generation`, `image_to_video`, `text_to_3d`, `image_to_3d`, `tts`, `stt`, `audio_generation` or `audio_to_audio`. A model whose fal.ai category maps to none of them has `tool_type` `generic`; no media tool setting uses it, so the model browser in settings shows it greyed out with "No tool uses this model". `output_type` is `null` when the category does not say what the model produces.

### GET /api/v1/catalog/categories

Returns `categories` (each `category`, `count`, `tool_type`, which can be `generic`) and `total_models`.

### GET /api/v1/catalog/schema

The input parameters of one model. Query: `endpoint_id` (required) and `refresh` (default `false`, bypasses the cache). Returns `{"found": true, "schema": {"endpoint_id", "primary_text_field", "fields": [...]}}`, where each field has `name`, `type`, `title`, `description`, `required`, `default`, `enum`, `minimum`, `maximum`, `format`, `is_file` and `is_primary_text`. Returns `{"found": false, "endpoint_id": "..."}` when the model publishes no schema.

### POST /api/v1/catalog/refresh

Reload the catalog from fal.ai now. Returns `success`, `models_fetched` and `categories`. If fal.ai cannot be read, the cached catalog is kept and the response is `502` with `success: false`, `error`, `cached_models` (the number of cached models still served), `categories` and a `detail` message.

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

The bell is the channel that reaches you: notifications arrive live in the open web, desktop and mobile app. Phone push notifications are not delivered; no current app version receives them. An agent's `NotifyDevices` tool sends push only to devices whose app declared it can receive push, so it normally reports `"delivered": false` with a `reason` (`no device can receive push notifications`, push in mock mode without Firebase credentials, or every device rejecting the message) and a note to tell you in chat instead. `delivered` is `true` only when such a device acknowledged the message.

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

`message` is required. `source` is `frontend`, `ws` or `api` (other values count as `frontend`); `level` defaults to `ERROR`. Optional: `detail`, `url`, `status_code`, `session_id`, `context` (object). Long values are cut to fit: `url` to 2048 characters, `level` to 16, `session_id` to 200, and a `context` over 16 KB is stored as `{"truncated": true, "original_bytes": ...}`. Returns `{"ok": true, "id": ...}`.

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
