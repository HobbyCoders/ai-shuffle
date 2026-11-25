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
   - **Clone from GitHub**: pick one of your repositories, or paste a clone URL. The dialog shows where the clone goes (your workspace, in a folder named after the project ID). Repositories that are already a project are marked **Already added**; open that project instead of adding it twice. If this worker (or another worker that shares its database) is already cloning or setting up the same repository, the dialog names that worker; wait for it to finish, or cancel it there. Paired workers that keep their own database can each add the same repository, even while one of them is offline, and once they sync it's one project with a copy on each worker. When a worker sets up its copy, it tries each of those two forms the repository has been added with, in turn, so a worker that can only sign in over SSH (or only over HTTPS) still gets one. A repository's HTTPS URL (`https://host/owner/repo`) and its SSH form `git@host:owner/repo` count as the same repository for all of this, with any case in the host and path and with or without `.git`; the SSH login must be `git` in lowercase. Any other spelling, such as an `ssh://` URL, another SSH login, an absolute path (`git@host:/srv/repo`) or a `~` path, only matches when it's written the same way. A repository that is already two projects, because an earlier version added it over both SSH and HTTPS, stays two projects: remove the one you don't use. While a paired worker still runs an earlier version, it can't set up a project that an updated worker cloned over SSH (it shows "The remote does not match this project") until it updates; nothing is lost.
4. Give it a display name (up to 100 characters). The project ID is made from the name; to change it, open **Advanced** (lowercase letters, numbers and hyphens, up to 50).
5. If you have other workers, tick them under **Set it up on** to set the project up there as well. Offline workers are set up when they come back. Until you change a tick, a repository starts with every online worker ticked and a plain folder with none.
6. Choose **Add Project** (or **Clone project**, or **Create on N workers**).

A clone shows its progress in the dialog, and the project is added only once the clone succeeds. If it fails, the dialog says why (for example the repository wasn't found, this worker's GitHub sign-in can't reach it, the folder already has other files, or the network dropped), nothing is added, and **Retry** tries again. You can close the dialog while a clone runs; it keeps going, and a notification tells you when it's done. If a chosen worker can't start its setup, a notification names it; the project's **Workers** tab shows setup progress.

You can also ask an agent to add a project in an owner conversation with app configuration tools enabled. `listProjects` lists projects on the current worker. `createProject` takes a `name` and optional `id`, `path` and `description`. Without a path it creates a folder in the workspace; a supplied path attaches that folder, creating it if needed. Docker paths must stay in the workspace. These tools do not clone Git URLs; use Add Project or the Projects page for that.

Creation follows the same approval rules as other app configuration writes: Ask prompts before it runs. An existing project with the same name or resolved folder path on this worker is refused rather than added twice.

## Projects across workers

A new project appears in connected workers' project catalogs as soon as it is added; you do not need to start a chat first. Each worker still needs its own copy of the folder before a chat can run there. The Projects page shows one chip per worker on every project: ready, missing (dashed), setting up, failed, or offline. Select a chip to open that worker in the project's worker table.

A project's **Workers** tab is a table with one row per worker: its status, folder, branch and when it was last seen. Select a row to open that worker's details beside the table (on a phone, as a sheet). From there you can:

- **Set up** the project on a worker that doesn't have it, or **Set up on all missing** (**Set up here** when only this worker lacks it).
- Choose an existing folder on that worker instead of a new one. A folder that already holds the same repository is used as it is.
- **Change folder** on a ready, online worker. Enter or browse to an existing directory on that worker. This changes only its project registration; no files are moved or copied. New chats and future turns in chats without worktrees use the new folder. Existing worktrees stay in place and still need their original checkout. After a folder change, AI Shuffle can reopen a removed app-managed worktree only in its original Git checkout, on the branch tip saved by **Keep branch**. For a removed external worktree, use the original Git checkout to check out the kept branch manually; AI Shuffle will not recreate it or reopen the old chat automatically. Leave external worktrees in place if you still need to use their bound chats. If an older worktree’s Git registration cannot be verified, restore it or forget the stale registration in Worktree management before changing folders. Finish any queued or running chats on that worker before changing the folder.
- Cancel a setup that is still running.
- **Remove project and chats from** a worker. This drops that worker's record of the project; its files stay.

The project's repository and group sit beside its name. **Change repository** points every worker's copy at another repository at once. The group menu moves the project to another group; **No group** takes only this project out of its group, and **Delete group** is a separate, confirmed action. **New chat** starts a chat on this worker, or on any worker that has the project ready. **Project settings** (the gear) holds the name, version control and the project's one danger zone.

The **Resources** tab holds the project's MCP servers, agents, hooks and memory on this worker. A project that isn't on this worker yet offers to set it up here instead.

Setting up a Git project clones its remote on that worker, with that worker's own GitHub sign-in. A project that isn't a Git repository uses the worker's folder of that name as it is, or gets an empty folder when there is none. Setup never overwrites anything: for a Git project, a folder that already holds something else is refused, and you can choose another. If a worker is offline, the setup waits and runs when it is back.

When you start a new chat on a worker that doesn't have the project yet, the new chat says so, and the send button becomes **Set up & send**. It sets the project up there first, then sends your message.

When an existing local-only project gains a Git remote, its next Git status refresh links it to the shared project identity (the repository cache can take up to a minute to expire).

**Older retained branches:** branches created under a local-only identity before first-link history was recorded may not appear in the linked project's worktree list, and project-level Git actions on them are refused. Chats already bound to those checkouts still work; linking does not delete their files. AI Shuffle does not guess ownership from a matching name, path, or reused project ID. There is not yet an owner-approved reattach action; keep using the bound chat until that action is available.

## Files

Browse, upload, download, rename and delete a project's files from the file browser. See [File Management](/docs/features/file-management).

## Git

When the project folder is a Git repository, you can:

- see changed, staged and new files;
- view, create and switch branches;
- view history and make commits;
- pull and push.

When you sign in to GitHub, you can also create pull requests, follow their checks and merge them.

### Watching a pull request

Click **Watch** on a pull request in the Pull requests view, or turn on **Watch PR** in a chat's Git menu, to have an agent keep an eye on it. AI Shuffle checks the pull request on GitHub every few minutes without using a model. When a CI check fails, a reviewer with write access comments or requests changes, or the branch has a merge conflict, it waits a few minutes for related events to arrive, then wakes the chat that opened the pull request once with everything that happened. The agent fixes what it can and pushes. It never merges, closes or force-pushes.

Comments from people without write access, and from bots you have not listed, only put a note in the notification bell, because anyone can comment on a public repository. GitHub tells only people who can push to a repository who else can, so on a pull request to a repository you cannot push to (from your fork, for example), review comments also only notify you. A watch ends by itself when the pull request is merged or closed, or after a number of quiet days. When no chat opened the pull request, or that chat runs on another worker, the watch only notifies you.

You decide what a watch may cost in **Settings > Usage > PR watch**:

- **When a watched PR needs attention**: **Fix it** (push fixes and answer reviews), **Triage** (assess and suggest only) or **Notify only** (no model at all).
- **Wakes per day** and **Spending per day**: the most one watched pull request may wake its agent, and spend, in 24 hours. After that, events go to the bell. Your daily spending limit still applies.
- **Ask me after the same check fails**: after this many failed fixes of the same check, the watch stops waking the agent and asks you instead.
- **End a quiet watch after**: the number of days without new commits, comments or CI results.
- **Bots that may wake the agent**: GitHub logins of review bots whose comments should wake it.

A project folder inside another repository remains usable for chat. Its Git indicator says **Inside repository** and identifies the enclosing root. Open that root as a project to use Git controls: the subfolder does not display the parent's changes, initialize a nested repository, or offer parent-wide Git actions. A folder with its own nested repository uses that repository normally. Plain folders still work without Git, and a missing or unreadable checkout still reports an error.

## Separate branches for each chat

A chat can work on its own branch in its own copy of the project (a Git worktree). The agent works there without touching your main folder, so several chats can work on different changes at once.

If Git LFS is missing or broken on a worker, AI Shuffle keeps a completed worktree when only its LFS checkout hook failed, or retries a failed checkout once with LFS filters and checkout hooks disabled for that command only. It never deletes a registered checkout during this recovery and reports the Git error if that checkout is incomplete. Large LFS files may remain pointer-text placeholders. The warning is logged and included in the agent's workspace facts; the repository's configuration and hooks are unchanged. Install or repair `git-lfs`, then run `git lfs pull` in the worktree to fetch the files. Other Git checkout failures report the actual Git error instead of guessing that the branch is missing.

The composer remembers worktrees on or off separately for each project, including across devices. A project without a saved choice starts with worktrees off. Chats without a project and folders that are not Git repositories always use the main folder. Existing chats keep their own checkout.

A new chat says where it will run in one sentence under the agent's name, for example "Runs on office-pc in my-app on a new branch from main in a worktree." Each underlined part opens its picker: the worker, the project (and any extra folders), the branch, and the worktree, which switches between **a worktree** and **the main checkout**. Every new chat starts with no project, so the sentence reads "with no project" and that part adds one. A draft you already typed in a project keeps it when you come back to it. To pick a project from the keyboard, press **Ctrl+P** (**⌘P** on a Mac) while the new chat is open, even with the cursor in the message box: type part of a project's name and press Enter, or press 1–9 to pick one of the projects listed first, which are the ones you used most recently (a digit that starts a project's name types into the search instead). Esc closes the list and puts you back in the message box where you left off. With a project already picked, the same keys switch it, and **No project** removes it. After the first message these choices move to the row of chips above the message box.

Choose the branch in the new chat's branch picker, or pick **New branch…**. Branch and base-branch choices stay with that project while you switch projects. The choice applies to that one chat; after it is created, the next new chat starts on **Auto** again.

When upgrading, an old global worktree **On** is remembered only for the last selected project recorded on this device. An old branch choice stays with the project already recorded in that choice. Other projects still start **Off**; existing chats keep their own checkout.

A chat's branch belongs to that chat. When an agent in a chat on its own branch starts another chat, the new chat gets its own worktree on a new `chore/chat-…` branch, starting from the first chat's last commit. Uncommitted changes are not copied. An agent cannot switch, rename or delete the branch of a worktree that belongs to another chat, or move or remove that worktree: AI Shuffle refuses the command and tells the agent to use its own checkout. To release a finished chat's checkout, settle the chat. Running or queued work, unfinished child work and Monitor jobs still reserve it; settling alone never makes their checkout safe to remove. A later explicit send must acquire the checkout again. Chats in the project's main folder still share it.

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
