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

Agents cannot open or type into a terminal.

Signing out disconnects every terminal. The shells keep running, and you can reconnect after signing in again.

## Where a terminal opens

A terminal opened from a chat starts in the chat's own working copy, its project folder, or your workspace folder, in that order. You can also choose any folder on the worker. If the folder does not exist, you get a clear message and no terminal opens.

## Pasting

Pasting puts the text at the prompt **without running it**. Press Enter when you are ready.

- A paste with several lines asks first. In sh, paste one line at a time.
- If a command is still running, wait until the prompt is back before pasting.

## Reloads and other devices

Terminals keep running when you reload the page or close the tab. When you come back, they reconnect and show their recent output. You can have the same terminal open on your computer and your phone at the same time.

## Close and restart

- **Close** ends the shell and everything started from it.
- When a shell exits by itself, its tab stays with its output. **Restart** starts a fresh one in the same folder.

## Limits

- Up to 8 terminals per worker. Close one to open another.
- Stopping or updating AI Shuffle ends every terminal.
- A terminal runs as the same user as AI Shuffle, with that user's files and tools. AI Shuffle's own secrets are kept out of it. Treat a terminal like signing in to the worker yourself.

## Not available yet

- Terminals on Windows workers.
- Terminals on another of your workers, or directly on the machine that runs Docker.
- Watching a command an agent started in the background.
- Terminal buttons for PowerShell and cmd code blocks.
