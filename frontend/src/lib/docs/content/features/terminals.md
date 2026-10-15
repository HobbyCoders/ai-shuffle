<!--
title: Terminals
description: Open a real shell on your worker, in a chat's folder, without leaving AI Shuffle
order: 12
-->

# Terminals

A terminal is a real command line (bash or sh) on your worker. It opens in the folder you are working in, so you can run a build, check `git status` or read a log yourself while the agent works.

## Open a terminal

- **On a computer:** click the terminal button under the message box. The Terminal panel opens on the right. Drag its edge to resize it.
- **On a phone:** open the composer controls and tap **Terminal** under More actions. It fills the screen and adds the keys your keyboard lacks, such as Ctrl, Esc, Tab and the arrows.
- **From an agent's reply:** shell code blocks show **Open terminal** and **Open and paste**. See [Commands from a reply](#commands-from-a-reply).
- **From Files:** choose **Open terminal here** for the current folder on this worker.

Each chat has its own tabs. Use **+** to open another terminal and choose its folder and shell. Closing the panel does not stop anything: the shells keep running and come back when you open the panel again.

The panel has **Search**, **Copy output**, **Clear** and **Stop** (which sends Ctrl+C).

## Commands from a reply

When an agent writes a shell command in a code block:

- **Open terminal** opens a terminal in the right folder.
- **Open and paste** also types the command at the prompt. **It never presses Enter.** Read the command, then press Enter to run it or Esc to clear it.
- Risky commands, such as `rm -rf`, `sudo` or `git push --force`, get a warning.
- A command with several lines asks first.
- The line above the buttons shows where the command will go: the worker, the folder and the shell.
- A folder name with unusual or look-alike characters gets a warning. A folder name that could hide its real location is refused.

Nothing an agent writes runs unless you press Enter.

## Who can use terminals

Only you, the owner, signed in with your owner password, from your own paired phone, or from the desktop app. API keys, other people's devices and shared links cannot open a terminal. Trusted home network access without a password is not enough either.

Agents cannot open or type into a terminal through the terminal API. This is an API restriction, not a security boundary against an agent with its own shell: by default its commands run as the app's OS user and can read the install's database connection details and credential keys. Owner-only checks and filtering secrets from shell environments do not remove that file access. See [What a shell can reach](/docs/features/profiles#permission-modes) and [Docker agent isolation](/docs/getting-started/install-docker-compose#keep-agents-away-from-ai-shuffles-own-keys-optional).

Signing out disconnects every terminal. The shells keep running, and you can reconnect after signing in again.

## Where a terminal opens

A terminal opened from a chat starts in the chat's own working copy, its project folder, or your workspace folder, in that order. You can also choose an existing folder within the Files workspace. Folders outside that root, including symlinks that lead outside it, are refused. If the folder does not exist, you get a clear message and no terminal opens.

### Choose another target

Use **+ → Target** to explicitly choose an approved writable SSH machine grant
for this chat. Approve SSH access through the existing private-access card
first; saved connections still require a fresh grant. SSH starts in that
account's home and login shell. Credentials stay in the private-access broker
and host keys are verified there.

Revoking or expiring an SSH grant ends its SSH connection. After an SSH shell
exits, choose its target again to open a new one. SSH terminals accept
single-line paste only, without pressing Enter.

Peer-worker terminals are not available: they need owner authorization verified
independently by the receiving worker. The `NETWORK_PEER_TERMINALS` setting is
off by default; turning it on does not enable this unfinished feature. Enrolled
workers alone cannot authorize terminals, and peer targets are not offered.

## Pasting

Pasting puts the text at the prompt **without running it**. Press Enter when you are ready.

- A paste with several lines asks first. In sh, paste one line at a time.
- If a command is still running, wait until the prompt is back before pasting.

## Watch an agent's background command

Choose **Watch command** in the Terminal panel to view Monitor output for this chat. This view is read-only: it cannot send input to the command. Choose **Back to shells** to return to your interactive terminals.

## Reloads and other devices

Terminals keep running when you reload the page or close the tab. When you come back, they reconnect and show their recent output. You can have the same terminal open on your computer and your phone at the same time.

## Close and restart

- **Close** ends the shell and everything started from it.
- When a shell exits by itself, its tab stays with its output. **Restart** starts a fresh one in the same folder.

## Limits

- Up to 8 terminals per worker. Close one to open another.
- Stopping or updating AI Shuffle ends every terminal.
- A terminal runs as the worker's workspace user, with that user's files and tools. Known app secrets are filtered from its environment, but by default the install's database and key files remain readable by that user. Docker agent isolation is optional and off by default; when active, it restricts terminal shells as well as agent shells. Treat a terminal like signing in to the worker yourself.

## Not available yet

- Terminal buttons for PowerShell and cmd code blocks.
