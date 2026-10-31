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
3. Choose where it comes from:
   - **Folder on** this worker: pick the folder, or type its path.
   - **Clone from GitHub**: pick one of your repositories, or paste a clone URL. It clones into your workspace.
4. Give it a display name and a project ID (lowercase letters, numbers and hyphens).
5. If you have other workers, tick them under **Set it up on** to set the project up there as well. Offline workers are set up when they come back.
6. Choose **Add Project** (or **Clone project**, or **Create on N workers**).

You can also ask an agent to add a project in an owner conversation with app configuration tools enabled. `listProjects` lists projects on the current worker. `createProject` takes a `name` and optional `id`, `path` and `description`. Without a path it creates a folder in the workspace; a supplied path attaches that folder, creating it if needed. Docker paths must stay in the workspace. These tools do not clone Git URLs; use Add Project or the Projects page for that.

Creation follows the same approval rules as other app configuration writes: Ask prompts before it runs. An existing project with the same name or resolved folder path on this worker is refused rather than added twice.

## Projects across workers

A project is shared by every worker in your network, but each worker needs its own copy of the folder before a chat can run there. The Projects page shows one chip per worker on every project: ready, missing (dashed), setting up, failed, or offline. Select a missing chip to set the project up on that worker.

A project's **Workers** tab lists each worker's folder and branch. From there you can:

- **Set up** the project on a worker that doesn't have it, or **Set up on all missing**.
- Choose an existing folder on that worker instead of a new one. A folder that already holds the same repository is used as it is.
- Cancel a setup that is still running.
- **Remove from** a worker. This drops that worker's record of the project; its files stay.

Setting up a Git project clones its remote on that worker, with that worker's own GitHub sign-in. A project that isn't a Git repository gets an empty folder. Setup never overwrites anything: a folder that already holds something else is refused, and you can choose another. If a worker is offline, the setup waits and runs when it is back.

When you start a new chat on a worker that doesn't have the project yet, the new chat says so, and the send button becomes **Set up & send**. It sets the project up there first, then sends your message.

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
