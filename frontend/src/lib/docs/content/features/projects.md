<!--
title: Projects
description: Organize work into directory-based projects with git integration
order: 7
-->

# Projects

Projects link AI Shuffle to directories on your system, providing file access, git integration, and organized session management.

## Overview

A project is a reference to a directory (e.g., your code repository). When you create a session with a project, Claude has access to that directory's files and can browse, read, and modify them.

## Creating a Project

### Via the UI

1. Open the workspace picker dropdown at the top of the app
2. Click **Add Project** to open the Add Project modal
3. Choose a **Location** (browse a directory) or type a **Path** to your code directory
4. Set a **Display Name** and a **Project ID** (required, lowercase letters, numbers, and hyphens)
5. Click **Add Project**

### Via the API

Creating a project needs owner access; API-user keys (`aih_...`) get `403`. Send an owner token from `POST /api/v1/owner/login` (see [Owner password](/docs/api-reference/authentication#owner-password)):

```bash
curl -X POST http://localhost:8000/api/v1/projects \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "my-web-app",
    "name": "My Web App",
    "path": "/home/user/projects/my-web-app",
    "settings": {
      "default_profile_id": "YOUR_AGENT_ID"
    }
  }'
```

Use the id of one of your agents (`GET /api/v1/agents`). API fields still say `profile`.

## File Management

Browse, upload, download, rename and delete a project's files from the file browser. See [File Management](/docs/features/file-management). Files are accessible to the agent during sessions.

## Git Integration

If the project directory is a git repository, you get:

- **Status** — See modified, staged, and untracked files
- **Branches** — View, create, and switch branches
- **Commits** — View commit history, create commits
- **Pull/Push** — Sync with remote repositories

### GitHub Integration

With GitHub authenticated, you can also:
- Create pull requests
- View PR status and checks
- Merge pull requests

## Worktrees

Git worktrees allow parallel development on multiple branches without switching:

1. Create a session with a worktree (specify branch name)
2. AI Shuffle creates a git worktree for that branch
3. Claude works in the isolated worktree directory
4. Changes stay separate from the main branch

```bash
curl -X POST http://localhost:8000/api/v1/sessions/with-worktree \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "proj_abc",
    "branch_name": "feature/new-feature",
    "create_new_branch": true,
    "base_branch": "main"
  }'
```

## Per-Project Configuration

Each project has tabs for its own:
- **MCP servers** — Project-scoped tool servers
- **Agents** — Project-scoped agent files
- **Hooks** — Project-specific event handlers
- **Memory** — The project's memory

Plugins can be installed to a project scope through the API.

## Session Import

Import sessions from the Claude Code CLI:

```bash
curl -X POST "http://localhost:8000/api/v1/projects/proj_abc/sync-sessions?profile_id=YOUR_AGENT_ID" \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

This finds Claude Code CLI sessions recorded for this project's directory (in the CLI's own session store) and imports any not yet listed. `profile_id` is the agent the imported sessions use; without it, the project's default agent is used. Imported sessions belong to the owner, so an API key cannot open them afterwards.

## Chat History

Previous sessions in a project are available for @ mention references, allowing Claude to reference context from past conversations.
