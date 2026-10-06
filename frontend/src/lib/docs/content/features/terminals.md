<!--
title: Terminals
description: Open a real shell on your worker, in a chat's folder, without leaving AI Shuffle
order: 12
-->

# Terminals

A terminal is a real command line (bash, or sh) running on your worker. It opens in the folder you are working in, so you can run a build, check `git status` or look at a log yourself while the agent works.

> The terminal panel is arriving in stages. This release adds terminals to the worker; the panel that shows them in a chat comes next. Everything below describes how terminals behave.

## Who can use terminals

Only you, the owner. A terminal is a full shell on your worker, so AI Shuffle admits:

- your owner password session on this worker,
- your own paired phone or desktop app (when your account owns this install),
- the desktop app on the same computer.

Everything else is turned away: API keys, other people's devices, other workers, share links and agent browser previews. Opening the app from the same computer or your trusted home network without signing in is **not** enough for terminals; sign in with the owner password.

Agents cannot open a terminal or type into one. There is no tool, hook or automation for it.

Signing out disconnects every open terminal within about 15 seconds; the shells keep running and you can reconnect after signing in again.

## Where a terminal opens

When you open a terminal from a chat, it starts in:

1. the chat's worktree, if the chat has one, otherwise
2. the chat's project folder, otherwise
3. your workspace folder.

You can also pick a folder yourself. It must be a full path to a folder that exists on this worker. If it doesn't, you get a clear message ("The folder … doesn't exist on this worker.") and no terminal opens; AI Shuffle never quietly opens somewhere else.

## Shells

bash is the default. sh is offered too. Only shells that are actually installed on the worker are listed.

## Pasting

Pasting puts the text at the prompt **without running it**. Press Enter when you are ready.

- Line breaks at the end of what you copied are removed, so nothing runs by accident.
- In bash, a multi-line paste stays in the prompt as one block; nothing runs until you press Enter.
- In sh, paste one line at a time. A multi-line paste is refused with a message explaining why.
- If a command is still running, the paste waits for you: "A command is running in this terminal. Paste when it's back at the prompt."

## Reloading and switching devices

Terminals keep running when you reload the page or close the tab. When you come back, the terminal reconnects and shows its recent output (about the last 5,000 lines). You can have the same terminal open in two places at once, for example on your desktop and phone; both see the same screen.

## Closing and restarting

- **Close** ends the shell and everything started from it, including background jobs.
- When the shell exits by itself (for example you typed `exit`), the terminal stays in the list with its exit code and output. **Restart** starts a fresh shell in the same folder.

## Limits

- Up to 8 terminals per worker. When all 8 are open you'll see "8 terminals are open on <worker>. Close one to open another." Exited terminals count until you close them.
- Very fast or very large input is refused to keep the worker responsive.

## When the app restarts

Terminals live only while AI Shuffle is running. Stopping or updating the app ends every terminal and everything running in it. After a restart the terminal list is empty.

## What a terminal can see

The shell runs as the same user as AI Shuffle on your worker, with that user's files and tools. It does **not** get AI Shuffle's secrets: database passwords, encryption and signing keys, provider tokens and webhook secrets are removed from its environment. Files that user can read are still readable from the terminal, including AI Shuffle's own data folder, so treat a terminal like signing in to the worker yourself.

## Not available yet

- Windows workers: "Terminals are not supported on this worker yet."
- Terminals on another worker of yours, or on the Unraid host itself.
- Watching a command an agent started in the background.
- "Open terminal here" from the Files page.
