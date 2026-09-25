<!--
title: Changelog
description: What changed in each AI Shuffle release
order: 14
-->

# Changelog

Newest release first. Each release lists what changes for you, grouped by area. The same notes appear next to the in-app update button. How to install a release: [Updates](/docs/getting-started/updates).

## Next release (changes since 0.8.18)

Not yet released. This entry collects everything merged since 0.8.18 and becomes the release notes when the version is tagged.

### Action needed

- After upgrading, sign out of Claude Code and in again with `claude auth login` (see [Updates](/docs/getting-started/updates)).
- Check automation schedules that use weekday numbers. They now follow standard cron: 0 and 7 are Sunday and `1-5` is Monday to Friday. Before, they ran one day late (`1-5` ran Tuesday to Saturday). Schedules that use day names, such as `mon-fri`, are unchanged. See [Automations](/docs/features/automations#cron).
- If you use API users limited to certain agents, make sure every call uses one of those agents. Calls for any other agent are now refused, and an API user with several agents must name the one it wants.
- If a chat connection you created in the app never answered, give its API user an agent in **Settings → API**.
- If an integration uses an API key to read, continue or stop sessions that key did not start (for example sessions you started in the app, or ones started with another key), it is now refused. Start those sessions with the same key, or use an owner token.
- If a reverse proxy in another container or on another host sits in front of AI Shuffle, add its address to the new `TRUSTED_PROXIES` setting. Forwarding headers are now read only from this computer and those addresses; without it, the audit log and the pairing limits see every visitor as the proxy. See [Behind a reverse proxy](/docs/getting-started/install-docker-compose#behind-a-reverse-proxy-optional).

### Install and packaging

- The Docker image is now private. You download it from `registry.hobbycoders.com` with the pull token from your license email; the token is part of the image name, so there is no `docker login` and pulls keep working after a reboot.
- New buyer compose file with a pinned version, the right shared-memory size for the built-in browser, a 90-second clean shutdown, and a volume for the built-in browser's profile. It listens on this computer only until you choose an address.
- New Unraid template (v2): pinned version, everything under `appdata/ai-shuffle`, the web interface bound to one address, a browser-profile path, and the shared-memory and shutdown settings the image needs.
- The Claude Code CLI is now installed on first start with Anthropic's own installer and kept in its own volume, so it updates itself and survives image updates. AI Shuffle runs only that standard CLI and no longer carries a copy of its own.
- The app shows its real version everywhere, taken from the release.
- Third-party license notices now ship with the app.
- A new setup wizard: welcome, owner password, license, Claude Code mode, sign-in or API key, workspace and a first agent run. See [First run](/docs/getting-started/first-run).
- The compose file and the Unraid template list the optional `TRUSTED_PROXIES`, `CODE_MODE_OS_SANDBOX` and built-in browser network settings, with a commented option for the code-mode sandbox. Defaults are unchanged.
- Every build now starts the real app against a real database, also with several workers starting together, and fails on any error in its logs.

### Updates

- Customer installs follow a **stable** channel: only numbered releases that passed the full container checks and a clean install are offered. Development builds are never offered.
- Every in-app update is signed for its channel and carries a digest of every source file; the worker verifies the files before building and again before every start. Python packages install only from a hash-pinned lock.
- Release notes are shown next to the update button and in the desktop app's update prompt.
- Update checks run when the app starts and once a day, and send nothing about you. See [Updates](/docs/getting-started/updates).

### License

- New **Settings → License** card and an app-wide license banner. During the beta it reads **Beta: no license needed**.
- Licenses cover one account with up to 3 workers. Leases last 14 days offline, with 14 days of grace after a failed payment. An unlicensed worker only stops starting new agent runs; your data is never locked.
- You can free the seat of a worker you no longer use from any worker.

### Security

- Chat connectors (Telegram, Discord, WhatsApp and others) refuse to start without a sender allowlist and check it again on every message and approval. Tool approvals for connector turns go to you in the app, never to the chat. WhatsApp messages must carry a valid signature.
- Agent tools can no longer read the app's own secrets: its encryption key, database settings, the CLI's credentials and the GitHub CLI's login.
- Agents and the built-in browser can no longer reach addresses on your local network unless you allow them.
- Bypass permission mode is never remembered as your default.
- The GitHub status and documentation routes need authentication, and the interactive API documentation is off in production.
- Logs no longer record sensitive values, and push notification endpoints must use HTTPS.
- In Ask mode, sessions on a Claude subscription now ask before app tools that change things (hooks, MCP servers, schedules, memory, computer use, paid generation, background commands), as other sessions already did.
- Project file browsing, upload, rename and delete stay inside the project, and a session or worktree must belong to that project. Deleting or renaming a link acts on the link, not its target.
- API users limited to certain agents are held to them on every query, conversation, WebSocket and voice request.
- Session events, live session sync, stopping a turn and the active-streams list apply the same API-user access checks as other session requests. Importing a session file is owner-only.
- Signing out with an owner token revokes that token.
- Hooks run without the app's secrets in their environment, credentials in URLs are removed from webhook payloads, and a guard hook that cannot start blocks the tool call instead of allowing it.
- An unused route that returned a decrypted provider key is removed.
- API keys act only on sessions they started: session requests, conversations, live sync, the chat connection and voice calls refuse another key's or the owner's sessions. The owner is unrestricted.
- "Always allow" from an API key is remembered for that session only, so a key can no longer save a rule that approves tools in the owner's sessions.
- Voice call routes (secret, call, hang up, state, configuration and the audio connection) now check who owns the session, like other session requests.
- The built-in browser now runs behind a local proxy that checks every connection, including WebSockets and each redirect, against the address it actually connects to. Cloud metadata addresses and the app's own ports are always blocked; other services on this computer stay reachable by default. The browser does not start without the proxy.
- The browser's page snapshots no longer show what is typed in password fields.
- Plugin installs refuse names and versions that are not plain folder names, clone URLs that are not https, http, ssh or `git@`, and links that point outside the plugin, and uninstalling never deletes anything outside the plugin folder.
- Importing settings refuses plugin and marketplace names that would read or write outside the plugin folders.
- Discarding changes in Git can no longer delete the project folder, the repository or a linked folder; only files inside the checkout are discarded.
- Worktree and per-branch checkout paths that would land outside the project are refused.
- On Claude subscription sessions, "Always allow" on a Bash command whose programs cannot be worked out (such as `echo $(whoami)`) now allows that one call instead of every Bash command.
- On Claude subscription sessions too, a tool call is denied when its `PreToolUse` hooks cannot be run or run past their deadline.
- Computer use starts only after an approval was recorded for that call, whether it came from a prompt, a remembered rule, a hook or the permission mode.
- Pushing from the Git panel accepts only plain branch names, so a branch name can no longer run a command, turn the push into a mirror push or delete a remote branch. Base branches and GitHub repository names that look like command options are refused too.
- Cloning: API keys may clone only HTTPS or SSH remotes, and the owner may clone a local folder only from inside the workspace or a project. Note: `git://` sources and `transport::` helper addresses are now refused for everyone.
- Passwords and tokens in clone addresses are removed from Git status, stored remotes, logs and error messages, including plugin install errors. Git itself keeps the working remote.
- Discarding changes also refuses Windows and macOS spellings of `.git` (short names, stream names, trailing dots, case and Unicode variants) and links that lead to it. Upload, rename, new folder and delete refuse every spelling of `.git` as well.
- Git requests refuse a worktree that belongs to another project.
- The built-in browser's network proxy can no longer be switched off with a differently spelled browser switch, and IPv6 addresses that carry an IPv4 address are checked as that IPv4 address. Requests the proxy blocks are reported as blocked instead of retried.
- Sync conflicts no longer keep secrets in plain text, the conflicts list is owner-only, and conflicts stored earlier are cleaned on the next start.
- Forwarding headers such as `X-Forwarded-For` are trusted only from this computer and from addresses in `TRUSTED_PROXIES`, so other machines on your network can no longer dodge attempt limits or fake audit addresses. Note: behind a reverse proxy that is not on this computer, set `TRUSTED_PROXIES` (see Action needed).
- Pending device challenges are counted per requester, and a flood from unknown addresses is refused instead of pushing out a member's pending challenge.
- Prompts, permission answers and Stop never go to a device that was removed from the network; its sessions stay read-only copies you can fork. A browser panel relayed from a removed device closes.
- Agents' Python code (code mode) keeps its safety checks and its connection to the app out of the code's reach, including after a restore from a checkpoint.
- On Linux, code mode now runs agents' Python inside a bubblewrap sandbox with no network and none of the server's files when the system allows it. The new `CODE_MODE_OS_SANDBOX` setting chooses `auto` (the default: sandbox when available), `required` (refuse to run code without it) or `off`. The Docker image includes bubblewrap; see [the code-mode sandbox](/docs/getting-started/install-docker-compose#the-code-mode-sandbox-optional) for the Docker option it needs.
- The permission prompt's Enter and Space shortcuts approve only once the prompt has been on screen for half a second, so a key meant for the message box or a held key no longer approves a request. Enter on a focused Deny button or in a text field no longer approves either.
- Each permission request gets its own prompt: a ticked "Always do this" no longer carries over to the next request, a double-click answers only one request, and clicks right as a new request appears are ignored.
- API keys of 10 or 11 characters are now fully masked instead of shown whole.

### API keys

- Subagents and voice workers started from an API key's session now run as that key, with its tools, credentials and agents, instead of as the owner.
- Every turn on an API key's session runs as that key, including scheduled wake-ups, voice follow-ups, chat connector turns and turns sent through the REST conversation route.
- App logs and errors, the schedule tools and remote triggers are owner-only. Session search and session views return only the key's own sessions, and voice workers give the app's settings tools to the owner only.
- Notes belong to the key that wrote them: a key sees and references only its own notes, on every synced worker too. Notes written before stay the owner's.
- MCP server settings shown to API keys and non-owner devices mask environment values, headers, arguments and credentials in addresses.
- Importing a session with an API key refuses agents and projects outside the key's scope and files the session under the key's project. A project-limited key's new chat sessions are filed under its project.
- Usage is the owner's account data. Note: API keys now get an empty usage reading, and the usage tool refuses key sessions.
- Disabling, rotating or deleting an API key closes its open chat connections at once, even idle ones.
- Transcription, read-aloud and voice calls use the fal.ai key that the API key's policy allows instead of the owner's. A key set to bring its own fal.ai key without one is refused before anything is sent.
- A session an API key starts on another worker keeps its owner.
- Team views are limited to sessions the caller may open.

### Agents and providers

- Claude Code runs in the mode you choose (Claude subscription or API key) and never switches on its own.
- In subscription mode, a loop guard limits unattended turns and background agent run time (**Settings → Providers → Loop guard**).
- AI Shuffle no longer runs its own Claude sign-in.
- Sessions on a Claude subscription use the app's tools reliably again: allowed tools run without extra prompts, background work between turns is kept, and turns no longer hang when nobody can answer a permission prompt.
- A permission prompt nobody answers is denied after 15 minutes, so background, API and automation runs finish instead of waiting forever.
- Agents you create for a project are saved where sessions look for them, so they now appear as subagents. Project agents saved earlier are copied over the first time the project's agents are listed.
- First run creates the starter Assistant agent. Before, it picked the built-in read-only Explore agent.
- Each hook's own timeout now applies; before, every hook stopped at 60 seconds. A hook that times out is stopped together with everything it started.
- PostToolUse, UserPromptSubmit, SessionStart, SessionEnd, SubagentStop, PreCompact and Notification hooks now run. Before, only PreToolUse and Stop hooks did.
- A Stop webhook can block by returning `{"decision": "block"}`, a bad tool pattern no longer skips the event's other hooks, and hook data over 64 KiB is shortened but stays valid JSON.
- Message hooks are not supported. They now report an error instead of appearing to succeed, and the hook editor no longer offers them.
- `Notification` and `SessionEnd` hooks now fire on Claude subscription sessions too.
- The `jq` that hook commands get now behaves like the real `jq` in many more cases, stops runaway programs, and hands anything it does not support to a real `jq` if one is installed.
- Messages you send while an agent is working are no longer lost or delivered twice when the turn ends, fails or waits for background work at that moment, and removing a queued message works with every provider.
- Claude subscription sessions: Stop ends the turn as Interrupted and keeps its cost instead of showing an error, works while Claude Code is still starting, and no longer makes a resumed session send the stopped message again.
- Claude subscription sessions survive a Claude Code crash between turns and unusual output lines instead of failing every later turn.
- Claude subscription sessions show Claude's real rate-limit status instead of always "allowed", and a rejected status marks the window as used up.
- A model or permission-mode switch now counts only once Claude Code confirms it; a refused switch keeps the old setting and is tried again next turn. Switching back and forth (for example Ask, then Bypass, then Ask) now lands on the last choice with every provider.
- Changing the effort mid-session now takes effect. Note: it restarts the session's engine; the conversation is kept.
- Other providers: a reply cut off mid-stream is reported and retried instead of saved as a finished answer, and text already received is kept and marked as interrupted.
- Other providers: errors show the provider's HTTP status and message, a 429 shows the rate-limit notice, and an Anthropic stream that sends nothing for 60 seconds is retried.
- Other providers: tool calls sent without an id each get their own, a tool call cut off by an error shows the error, and replies without usage figures are accepted and estimated instead of dropped or counted as free.
- OpenRouter-only request fields are sent only to OpenRouter.
- Sessions on other providers still set to the retired Auto mode now run as Ask everywhere; before, their prompts were approved automatically.
- Runs nobody watches (automations, connectors, API turns) follow the permission mode as a watched session would. Note: on other providers, Accept Edits and Bypass now run the calls the mode allows instead of waiting 15 minutes and denying them; Ask still waits. Screen control still asks, and is denied when nobody can answer.

### Sessions and interface

- Long sessions have a redesigned timeline for moving through earlier turns.
- Saving a remembered fact works when the agent also sends a revision.
- The usage panel keeps Claude usage after a restart and on every device. Unchanged readings no longer look stale, a window that has rolled over reads "awaiting next report" instead of "resets in now", and stale sections show when they last updated.
- The usage panel re-reads usage at most every 30 seconds however often you open it, keeps its last readings when a read fails, and a stale provider shows when it last updated and when it checks next.
- OpenAI spend keeps updating, background API queries update the rate-limit readings, and the 7- and 30-day analytics ranges cover exactly 7 and 30 days.
- Access requests in a chat update when something changes instead of asking the server every two seconds, and **Saved connections** refresh in every open session.
- Links between documentation pages open inside the Docs panel instead of a missing page. The docs were checked against the app and corrected, including hooks, connector setup, menu paths and the API reference, which now covers the endpoints it left out.
- Menus and messages name the right settings pages (**Settings → Providers → Devices**, **Settings → Account**, **Settings → API**).
- The broken "Manage project knowledge" search action is removed.
- A session file with a damaged or unusual line no longer shows an empty history, token totals are no longer counted several times, and rewinding to an earlier reply keeps the reply you chose.
- The automation edit form shows the webhook URL and a secret you can sign requests with, and run history shows what started each run (Schedule, Event, Connector, Webhook or Manual) instead of always "Manual".
- Plugin details show the Hooks badge for plugins that contribute hooks, and plugin errors show the real reason.
- The chat connection survives a malformed message instead of closing.
- Large file uploads in the built-in browser get more time instead of failing after 30 seconds.
- Usage panel: OpenRouter shows the current period's percentage and reset time (a key with 20% used showed 100%), and Codex keeps its windows and plan when a reply carries only credit figures.
- Usage panel: one unreadable provider no longer blanks every provider, readings restored in the browser and carried over from an older reading go stale on schedule, and very small percentages no longer round to "0% left" or "100% left".
- Usage panel: reset times are read as UTC, an open panel refreshes when a turn ends, every device shows the latest reading, and the usage tool agrees with the panel on how fresh each window is.
- **Browse All** in the model catalog greys out models no tool uses, fal.ai speech-to-text models now appear for the transcription tool, and a failed catalog refresh shows why.
- The owner access screen no longer stays on "Loading..." when the worker does not answer or is older.
- Form errors from the server show as text instead of "[object Object]".
- The settings import dialog describes what it copies (hooks, enabled Claude plugins and Codex settings).
- Rewinding a session whose history is out of step no longer deletes replies you kept.
- Note autocomplete and the chat history list load faster in large projects.
- The notification tool reports a push as delivered only when a device confirmed it, and points to the in-app notification center otherwise.

### Accessibility

- Dialogs and the command palette keep keyboard focus inside while open, close on Escape and return focus where it was. Enter on a focused Cancel or Close button no longer confirms.
- The message box, icon-only buttons, menus, fields and each API user's Edit, Rotate and Delete buttons have names screen readers announce, and states shown only by colour now also have text.
- A streaming reply is announced once instead of per word, and the permission prompt says when its shortcuts are ready.

### Connectors

- Creating a chat or incoming-webhook connection now asks which agent answers it. Before, new connections refused every message until an agent was assigned elsewhere.
- Changing part of a connection's settings through the API keeps the rest (such as the bot token) instead of replacing them all, and a running connection restarts with the result.
- Turning a connection on or off through the API now also starts or stops it.
- Starting a connection that is already running no longer answers every message twice.
- Incoming hooks whose name is `test`, `activate`, `deactivate` or `restart` are reachable again.
- Connections stop on a worker that a network membership change removed them from, including ones still connecting, and a connection stuck on connecting is replaced.

### Desktop app (beta)

- The desktop app uses your normal Claude Code login instead of a separate one.
- It tells you on first launch that agents run as your user account.
- Each launch uses its own token between the window and its worker.
- `Start AI Shuffle.cmd` on Windows now starts the desktop app; a broken path stopped it before.
- **Worker → Worker Status…** now offers **Open AI Shuffle** to return to the app.
- An update that fails partway reports the failure and starts the worker and database again instead of leaving them stopped.
- A worker that cannot start shows an error instead of staying on "starting", and quitting no longer tries to stop a process that is not the app's database.
- Only AI Shuffle's own pages get microphone, clipboard and fullscreen access, and sign-in or payment pop-ups get none. The launch token is sent only to the worker.
- **Quit** from the tray stops the worker cleanly and forces it after 60 seconds; closing the window offers **Quit Anyway** if stopping fails again.
- The unused `aishuffle://` link handler is removed, so web pages can no longer launch the app.
- Update progress refreshes more often.

### Android app

- You can delete your HobbyCoders account from inside the app.
- **Settings → About** no longer crashes.
- App and website text points to **Settings → Providers → Devices** and the **Sign in** button that exists.

### Remote access and accounts

- Remote access keeps working through short outages of the connection service.
- The connection service no longer stores synced data. Sync between your workers stays end to end encrypted.
- Account devices that ask for owner access must be approved on each worker (**Settings → Providers → Devices**).

### Reliability

- Upgrading from a build before 19 September (database schema 88 or older) no longer fails at start.
- Several workers sharing one PostgreSQL database can start at the same time without failing on setup.
- The app's own log lines appear in `docker logs` again.
- An approved automation run waiting at shutdown is re-queued on the next start and runs once. Runs of disabled or deleted automations are skipped, approvals older than 24 hours expire, and events that arrive during shutdown no longer create runs.
- A catalog refresh started while another one runs waits for it, and a refresh gives up after two minutes instead of hanging.

### Removed

- The voice "command" session mode. No app used it; voice sessions are always chat.
- Code that nothing used any more. No change in behaviour.

### Fixed

- Automations: a repeated result is no longer delivered again inside the deduplication window, an approved run no longer runs alongside another run of the same automation, and failed runs show the real error.
- Files: a slow folder listing no longer replaces the folder you moved to, and a failed file in a multi-delete is reported.
- Voice calls: a dropped connection shows its error, and a call that drops after its 50-minute renewal no longer shows as live.
- Two parts of the app sharing a keyboard shortcut no longer remove each other's binding.
- A requeued permission prompt no longer causes an error in the app.
- The REST streaming query ends on its error when a turn crashes, instead of also reporting a clean finish.
- Git: **Ship It** works when a change has only new files and plugin installs from `ssh://` addresses work.
- Speech: fal.ai and OpenAI failures are reported as provider errors instead of server errors, and saving a blank voice keeps the saved one.
- Saving a fal.ai or Anthropic key while credential storage is locked says so instead of failing, and a fal.ai outage saves the key without checking it.
- MCP server names over 64 characters get the same clear error as other invalid names.
- Documentation: a second pass against the app corrected many pages (permission modes, API users, voice, usage, settings import, hooks, connectors), and new sections cover reverse proxies and the code-mode sandbox.
