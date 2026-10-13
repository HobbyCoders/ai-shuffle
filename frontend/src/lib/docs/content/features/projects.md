<!--
title: Projects
description: Organize your work into project folders with Git built in
order: 7
-->

# Projects

A project is a folder on your worker, usually a code repository. Chats started in a project can read and change its files, and the project's Git tools are one click away.

## Add a project

1. Open the workspace picker at the top of the app.
2. Choose **Add Project**.
3. Pick the folder, or type its path.
4. Give it a display name and a project ID (lowercase letters, numbers and hyphens).
5. Choose **Add Project**.

## Files

Browse, upload, download, rename and delete a project's files from the file browser. See [File Management](/docs/features/file-management).

## Git

When the project folder is a Git repository, you can:

- see changed, staged and new files;
- view, create and switch branches;
- view history and make commits;
- pull and push.

When you sign in to GitHub, you can also create pull requests, follow their checks and merge them.

## Separate branches for each chat

A chat can work on its own branch in its own copy of the project (a Git worktree). The agent works there without touching your main folder, so several chats can work on different changes at once.

Choose the branch in the new chat's branch picker, or pick **New branch…**. The choice applies to that one chat. The next new chat starts on **Auto** again.

## Per-project settings

Each project has its own tabs for:

- **MCP servers** only for this project;
- **Agents** stored in the project;
- **Hooks** only for this project;
- **Memory** for this project.

## Bring in Claude Code chats

You can import the Claude Code CLI chats you already had for a project's folder, so they show up in AI Shuffle.

## Past chats

Type `@` in the message box to point the agent at an earlier chat in the same project.

To manage projects from your own code, see the [Projects API](/docs/api-reference/projects).
