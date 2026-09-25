<!--
title: Projects
description: Project CRUD, files, project agents, memory files, plugins, and links
order: 6
-->

# Projects API

Endpoints for managing projects — directory references with file browsing, project agents, memory files and plugins. Git and GitHub endpoints are under [Miscellaneous](/docs/api-reference/miscellaneous#git-integration).

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

## Project Agents

Agents defined as files inside the project, in `.fluid/agents/<name>.md` (a Markdown prompt with a YAML header). Sessions in the project can use them as nested agents. These are separate from the agents under `/api/v1/agents`.

**Auth:** Required for every endpoint in this section.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/projects/{project_id}/agents` | List the project's agents (no prompt text) |
| `GET` | `/api/v1/projects/{project_id}/agents/{agent_name}` | One agent, including `prompt` and `raw_content` |
| `POST` | `/api/v1/projects/{project_id}/agents` | Create an agent (`201`) |
| `PUT` | `/api/v1/projects/{project_id}/agents/{agent_name}` | Update an agent; only the fields you send change |
| `POST` | `/api/v1/projects/{project_id}/agents/{agent_name}/rename` | Rename an agent: `{"new_name": "..."}` |
| `DELETE` | `/api/v1/projects/{project_id}/agents/{agent_name}` | Delete an agent (`204`) |

```bash
curl -X POST http://localhost:8000/api/v1/projects/my-web-app/agents \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test-writer",
    "description": "Writes unit tests for changed code",
    "prompt": "You write focused unit tests...",
    "tools": ["Read", "Write", "Bash"]
  }'
```

`name` starts with a letter and uses only letters, digits, `-` and `_`. `description` and `prompt` are required; `model` and `tools` are optional. List and create return `name`, `description`, `model`, `tools`, `file_path`, `created_at` and `updated_at`.

## Project Memory

A project has two memory files that agents read as instructions: `project` (`AGENTS.md` in the project root, meant to be committed) and `local` (`.agents/AGENTS.md`, kept private). If a location has no `AGENTS.md`, its older file is used instead when it exists: `CLAUDE.md` in the project root for `project`, `.claude/CLAUDE.md` for `local`. Reads and deletes act on that older file; saving always writes `AGENTS.md`. `file_path` is the path of the file in use, relative to the project root.

### GET /api/v1/projects/{project_id}/memory

List both locations, including ones that do not exist yet.

**Auth:** Required

**Response:**
```json
[
  {
    "location": "project",
    "exists": true,
    "content": "# Conventions\n...",
    "file_path": "AGENTS.md",
    "size": 512,
    "updated_at": "2025-01-15T10:30:00Z"
  },
  { "location": "local", "exists": false, "content": "", "file_path": ".agents/AGENTS.md", "size": 0, "updated_at": null }
]
```

### GET /api/v1/projects/{project_id}/memory/{location}

Read one file. `location` is `project` or `local`.

**Auth:** Required

### PUT /api/v1/projects/{project_id}/memory/{location}

Create or replace one file. Returns the saved file.

**Auth:** Required

```bash
curl -X PUT http://localhost:8000/api/v1/projects/my-web-app/memory/project \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"content": "# Conventions\n\nUse pnpm, not npm."}'
```

### DELETE /api/v1/projects/{project_id}/memory/{location}

Delete one file: `AGENTS.md`, or the older `CLAUDE.md` file when that is the one in use (never both). Returns `204`, or `404` if neither exists.

**Auth:** Required

### POST /api/v1/projects/{project_id}/memory-import/preview

Show which memory notes an import would copy from a provider's own memory folder for this project into AI Shuffle's. `provider` is `claude` or `codex`.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/projects/my-web-app/memory-import/preview \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"provider": "claude"}'
```

Returns `provider`, `source_dir`, `destination_dir`, `items` (each with `relative_path`, `size` and `status`: `ready`, or `conflict` when a file already exists at the destination), `ready_count` and `conflict_count`. A missing source folder previews as empty.

### POST /api/v1/projects/{project_id}/memory-import/apply

Copy the files. Send `{"provider": "claude", "relative_paths": ["notes.md"]}`, or omit `relative_paths` to copy every `ready` file. Existing files are never overwritten and the source folder is not changed.

**Auth:** Owner only

Returns `imported` (paths copied), `skipped` (each with `relative_path` and `reason`), `source_dir` and `destination_dir`.

## Project Plugins

Plugins installed into one project instead of the whole workspace. Scope `local` installs into `.fluid/plugins/` (private); scope `project` installs into `plugins/` (meant to be committed).

**Auth:** Required for every endpoint in this section.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/projects/{project_id}/plugins/installed` | Plugins installed in this project, from both scopes |
| `GET` | `/api/v1/projects/{project_id}/plugins/available` | Marketplace plugins, with `installed` and `enabled` for this project. Optional `?marketplace=` filter |
| `POST` | `/api/v1/projects/{project_id}/plugins/install` | Install a plugin (`201`) |
| `DELETE` | `/api/v1/projects/{project_id}/plugins/{plugin_id}` | Uninstall (`204`) |
| `POST` | `/api/v1/projects/{project_id}/plugins/{plugin_id}/enable` | Enable (`204`) |
| `POST` | `/api/v1/projects/{project_id}/plugins/{plugin_id}/disable` | Disable (`204`) |
| `GET` | `/api/v1/projects/{project_id}/plugins/hooks` | Hooks contributed by this project's plugins |

```bash
curl -X POST http://localhost:8000/api/v1/projects/my-web-app/plugins/install \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"plugin_id": "my-plugin@my-marketplace", "scope": "local"}'
```

`plugin_id` for install is `name@marketplace` from the available list. For uninstall, enable and disable, use the `id` from the installed list. Installed plugins return `id`, `name`, `marketplace`, `description`, `version`, `scope`, `install_path`, `installed_at`, `enabled` and `has_commands`, `has_agents`, `has_skills`, `has_hooks`, `has_mcp`.

## Linking Projects

Two project entries that are really the same codebase (for example, clones with different remotes) can be linked so the sessions board groups their sessions together. Projects that share a git remote are grouped automatically.

### POST /api/v1/projects/{project_id}/link

Link this project with another. If either is already in a group, both join that group. Returns the updated project.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/projects/my-web-app/link \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target_project_id": "my-web-app-fork"}'
```

### DELETE /api/v1/projects/{project_id}/link

Remove this project's manual link. It then regroups automatically by its git remote, if it has one. Returns the updated project.

**Auth:** Owner only
