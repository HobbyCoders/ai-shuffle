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

1. Navigate to **Projects** in the sidebar
2. Click **Create Project**
3. Enter the path to your code directory
4. Set a name and optional default profile
5. Save

### Via the API

```bash
curl -X POST http://localhost:8000/api/v1/projects \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Web App",
    "path": "/home/user/projects/my-web-app",
    "default_profile_id": "default"
  }'
```

## File Management

Each project provides a file browser:

- **Browse** directories with show/hide hidden files
- **Upload** files directly to the project
- **Download** files from the project
- **Create** new folders
- **Rename** and **delete** files/folders

Files are accessible to Claude during sessions.

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
- Browse issues

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

Projects can have their own:
- **MCP servers** — Project-scoped tool servers
- **Plugins** — Project-specific extensions
- **Hooks** — Project-specific event handlers

## Session Import

Import sessions from the Claude Code CLI:

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj_abc/sync-sessions \
  -H "Authorization: Bearer aih_your_key"
```

This scans the project directory for Claude Code session files and imports them.

## Chat History

Previous sessions in a project are available for @ mention references, allowing Claude to reference context from past conversations.
