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

A project folder inside another repository remains usable for chat. Its Git indicator says **Inside repository** and identifies the enclosing root. Open that root as a project to use Git controls: the subfolder does not display the parent's changes, initialize a nested repository, or offer parent-wide Git actions. A folder with its own nested repository uses that repository normally. Plain folders still work without Git, and a missing or unreadable checkout still reports an error.

## Separate branches for each chat

A chat can work on its own branch in its own copy of the project (a Git worktree). The agent works there without touching your main folder, so several chats can work on different changes at once.

The composer remembers worktrees on or off separately for each project, including across devices. A project without a saved choice starts with worktrees off. Chats without a project and folders that are not Git repositories always use the main folder. Existing chats keep their own checkout.

A new chat says where it will run in one sentence under the agent's name, for example "Runs on office-pc in my-app on a new branch from main in a worktree." Each underlined part opens its picker: the worker, the project (and any extra folders), the branch, and the worktree, which switches between **a worktree** and **the main checkout**. Without a project the sentence reads "with no project", and that part adds one. After the first message these choices move to the row of chips above the message box.

Choose the branch in the new chat's branch picker, or pick **New branch…**. Branch and base-branch choices stay with that project while you switch projects. The choice applies to that one chat; after it is created, the next new chat starts on **Auto** again.

When upgrading, an old global worktree **On** is remembered only for the last selected project recorded on this device. An old branch choice stays with the project already recorded in that choice. Other projects still start **Off**; existing chats keep their own checkout.

A chat's branch belongs to that chat. When an agent in a chat on its own branch starts another chat, the new chat gets its own worktree on a new `chore/chat-…` branch, starting from the first chat's last commit. Uncommitted changes are not copied. An agent cannot switch, rename or delete the branch of a worktree that belongs to another chat, or move or remove that worktree: AI Shuffle refuses the command and tells the agent to use its own checkout. To remove such a worktree, archive its chat first. Chats in the project's main folder still share it.

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
