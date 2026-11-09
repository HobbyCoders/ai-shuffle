<!--
title: Terminals
description: Open a real shell on your worker, in a chat's folder, without leaving AI Shuffle
order: 12
-->

# Terminals

A terminal is a real command line (bash or sh) on your worker. It opens in the folder you are working in, so you can run a build, check `git status` or read a log yourself while the agent works.

## Open a terminal

- **On a computer:** click the terminal button in the row of controls at the bottom of the message box. The Terminal panel opens on the right. Drag its edge to resize it.
- **On a phone:** open the composer controls and tap **Terminal** under More actions. It fills the screen and adds the keys your keyboard lacks, such as Ctrl, Esc, Tab and the arrows.
- **From an agent's reply:** shell code blocks show **Open terminal** and **Open and paste**. See [Commands from a reply](#commands-from-a-reply).
- **From Files:** choose **Open terminal here** for the current folder on this worker.

Each chat has its own tabs. Use **+** to open another terminal and choose its folder and shell. Closing the panel does not stop anything: the shells keep running and come back when you open the panel again.

The panel has **Search**, **Copy output**, **Clear** and **Stop** (which sends Ctrl+C). On a computer, a chat's terminal also has **Send selection to chat**: select some output and press it to add that text to your message as a code block. It is not sent, so you can add a question first.

Web links in the output, starting with `http://` or `https://`, can be clicked and open in a new browser tab. The tab opens on the device you are using, so a `localhost` link from a worker on another machine points at your own device, not the worker.

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

### A terminal on another worker

For a chat running on another worker in your network, **Open terminal** and
**Open and paste** open the shell on that worker, in the chat's folder, and show
it in the app you are using. You can also pick that worker under **+ → Target**.

**Turn it on.** Go to **Settings → Security → Terminals on other workers**. It is
off by default, and both workers need it on: the one you are using and the one
that runs the shell.

**Link this device, once per worker.** The first time you open a shell on a
worker, this device shows a twelve-character code. On the other worker, go to
**Settings → Security → Terminals on other workers** and enter the code, either
on that worker itself or from a device already signed in to it as the owner.
That links the device. From then on, opening a shell on that worker takes one
press. A code that isn't entered within ten minutes expires.

The device holds a private key that can't be copied out of it, and signs every
shell it opens. The worker you are using only passes the request along.

**Where a device can link.** The desktop app, the Android app, HTTPS addresses
and `localhost` can hold the key. A plain `http://` LAN address can't, because
browsers only offer this on secure connections. There the terminal button is
disabled and says why.

**Linked devices.** The other worker lists its linked devices in the same place
in Settings. **Revoke** takes effect at once: the device's next request is
refused and any shell it has open on that worker closes immediately.

A shell on another worker lasts at most ten minutes and closes after 30 seconds
without traffic. Closing the tab, revoking the device, signing out on the worker
you are using, turning the setting off, removing a worker from the network or
restarting either worker ends the shell. Restart is not offered for it; open a
new one instead, without linking again.

As with any paste, a command you open and paste waits at the prompt until you
press Enter. Both workers write each link, open and close to their app log. The
log never includes what you type or what the shell prints.

**What is refused.**

- Anyone who cannot open a local terminal (see [Who can use terminals](#who-can-use-terminals)),
  including API keys and agent preview links. An agent on the worker you are
  using cannot sign for you, and being a member of the network is not enough.
- Linking from anywhere except the owner on the receiving worker. The worker you
  are using cannot approve on its behalf.
- A worker that is offline, runs an older version, has no shell, or has no
  HTTPS route. The picker shows the reason. Plain HTTP is never used.
- Every request while the setting is off on either worker. Listing and revoking
  linked devices still work.
- A signature that is old, already used, or made for a different worker, chat,
  folder or action.

One limit: if the worker you are using were fully compromised, it serves the page
you are looking at and could trick an open browser into signing a request. It
still could not take the key. Revoke the device on the other worker if you ever
suspect this.

## Pasting

At a shell prompt, pasting puts the text there **without running it**. Press Enter when you are ready.

- A paste with several lines asks first. In sh, paste one line at a time.
- You can paste a single line into a running program, including a password prompt or a REPL. Pasting strips trailing newlines; press Enter yourself to submit the input.
- Multi-line pastes into a running program are refused, even if it advertises bracketed paste. This prevents extra lines from being submitted unexpectedly.

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
