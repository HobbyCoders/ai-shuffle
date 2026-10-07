<!--
title: Terminals
description: Open a real shell on your worker, in a chat's folder, without leaving AI Shuffle
order: 12
-->

# Terminals

A terminal is a real command line (bash, or sh) running on your worker. It opens in the folder you are working in, so you can run a build, check `git status` or look at a log yourself while the agent works.

## Opening a terminal

- **On a computer:** click the terminal button next to the globe under the message box. The Terminal panel opens on the right, next to Changes and Browser. Drag its edge to resize it, expand it, or press Esc (outside the terminal) to close it.
- **On a phone:** open the composer controls, then tap **Terminal** under More actions. The terminal fills the screen, with a row of keys your keyboard lacks: Ctrl, Esc, Tab, Ctrl+C, the arrows and a few symbols.
- **From an agent's reply:** shell code blocks (`bash`, `sh`, `zsh`, `shell`, `console`) show **Open terminal** and **Open and paste** next to Copy. See [Commands from a reply](#commands-from-a-reply).

Each chat has its own tabs. Use **+** to open another terminal and pick its folder and shell. A tab shows its shell and folder; the line at the bottom shows whether it is live, running a command, reconnecting or exited.

Closing the panel does not stop anything: the shells keep running and their tabs come back when you open the panel again.

The buttons only appear when you can use terminals on this worker. If the worker can't run one (for example a Windows worker), the button says why.

### Panel tools

- **Search** finds text in the output (Enter for the next match, Shift+Enter for the previous one).
- **Copy output** copies what you selected, or everything when nothing is selected.
- **Clear** clears the screen in this window. The shell keeps running.
- **Stop** sends Ctrl+C to the running command.

## Commands from a reply

When an agent writes a shell command in a code block:

- **Open terminal** opens a terminal for this chat in the right folder.
- **Open and paste** opens one and types the command at the prompt. **It never presses Enter.** Read the command, then press Enter to run it or Esc to clear it. A command that could delete data or change the machine (for example `rm -rf`, `sudo` or `git push --force`) gets a warning to check it first.
- A command with more than one line asks first ("Paste 6 lines?"). In an sh terminal it is refused, because sh can take one line at a time.
- The command goes into a new tab unless an idle tab's prompt is known to be empty, so it never joins text already waiting at a prompt.
- The terminal takes the keyboard only if you are not typing somewhere else, including the terminal's own search box. If you went back to the message box while it opened, your typing stays there.
- If the terminal can't open within about 15 seconds, or you close the panel first, nothing is pasted and the panel says so. An old click never pastes later.

Nothing an agent writes runs unless you press Enter.

The line above the buttons shows where the command goes: the worker, the folder and the shell.

A code block can name its folder or shell after the language, for example ` ```bash cwd="/workspace/app" `. A folder named this way is shown in full under the block's header, wrapped onto more lines when long, with **Outside this chat's folder** when it is somewhere else on the worker. A folder with any character other than plain English letters, digits, punctuation and single spaces between them (for example an accented letter, a letter from another alphabet, or a space at the end of a name) still opens, but gets **Unusual characters in this folder name**, and each such character is shown as its code, like `U+0441`, so a look-alike can't pass for an ordinary letter. A folder with `.` or `..` parts, or with characters that don't show at all (control, zero-width, text-direction and other formatting characters, and line separators), is refused: the buttons are greyed out and the reason is shown. A block meant for another worker or the Unraid host shows the buttons greyed out, with the reason.

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

Pasting puts the text at the prompt **without running it**. Press Enter when you are ready. This applies to your own clipboard too: pasting into the terminal goes through the same path, and a multi-line paste asks first.

- Line breaks at the end of what you copied are removed, so nothing runs by accident.
- In bash, a multi-line paste stays in the prompt as one block; nothing runs until you press Enter.
- In sh, paste one line at a time. A multi-line paste is refused with a message explaining why.
- If a command is still running, the paste waits for you: "A command is running in this terminal. Paste when it's back at the prompt."

## Reloading and switching devices

Terminals keep running when you reload the page or close the tab. When you come back, the terminal reconnects and shows its recent output (about the last 5,000 lines). If the connection drops, it tries again with growing pauses, starting at half a second (one second when the screen fell too far behind), and stops after 8 tries with "Can't reach". The count starts over only after the terminal has stayed connected for a few seconds. You can have the same terminal open in two places at once, for example on your desktop and phone; both see the same screen.

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
- Terminals on another worker of yours, or on the Unraid host itself. A chat that runs on another worker shows the terminal button greyed out.
- Watching a command an agent started in the background.
- "Open terminal here" from the Files page.
- PowerShell and cmd code blocks don't get terminal buttons.
