<!--
title: Changelog
description: What changed in each AI Shuffle release
order: 14
-->

# Changelog

Newest release first. Each release lists what changes for you, grouped by area. The same notes appear next to the in-app update button. How to install a release: [Updates](/docs/getting-started/updates).

## 0.8.22

- Add the persistent assistant: one long-running assistant that stays available across your sessions.
- Long conversations compact less often and keep more of their prompt cache, which lowers cost on Codex models.
- Delegated Claude Code sessions behave more like Claude Code itself, and search tools are no longer missing from them.
- Move local sessions to another worker again; project sessions carry their Git state and context with them.
- Change permissions, effort and Fast mode while a session is running. Fast mode and context controls now work for newer models that support them.
- The chat header's Git controls are folded into one Changes button and panel. Mobile action rows are reorganized, and busy Git worktrees are protected from cleanup.
- Sign in to other workers with your account, without approving each one on your phone. Android account sign-in links no longer reach a 404 page.
- See remaining provider usage without setting up an extra billing key. Claude subscription usage refreshes correctly again.
- Stop stays visible while a session runs, also when you have a draft, and works for sessions on another worker.
- Suggested replies fill in only on Tab or a double-click or double-tap.
- Pending messages from other sessions show who sent them.
- Rewind is disabled for mid-turn messages that have no checkpoint.
- Fix sidebar status drift, missing PR status, slow history replay and slow cross-worker sync. Large history syncs no longer block new sessions.
- Your first message is saved before another worker is contacted, so a timeout no longer leaves an empty session.
- Imported Claude Code sessions keep their original dates.
- Fix the Android multi-file picker, the mobile swipe gesture and mobile Git controls.
- Fix live preview reloading while you scroll or type, and lower the built-in browser's memory use.
- Desktop update checks are more reliable, the bell respects each worker's update scope, and the UI no longer shows stale cached pages after an update.
- Installer updates are verified before activation, and the app explains when a stored update needs a newer container image.
- Improve oversized-image recovery, rewind matching, permission persistence and private-access confirmations.

## 0.8.21

- Desktop update downloads now wait for an active update check to finish instead of silently losing the download request.

## 0.8.20

- Resume project sessions on another worker through Git remotes, without transferring file bundles. Chats without a project move by transferring their history.
- See ChatGPT subscription usage refresh without having to send another message.
- Desktop first-start updates stay visible and no longer lock the app.
- Choose provider-specific models for delegated tasks.
- Worktree status refreshes in the background, and the update button avoids polling peers every two seconds.
- Ship It cleanup stops sessions still running in its worktree before removing it. Shared project checkouts keep their existing behavior.

## 0.8.19

### Action needed

- After upgrading, sign out of Claude Code and in again with `claude auth login` (see [Updates](/docs/getting-started/updates)).
- Check automation schedules that use weekday numbers. They now follow standard cron: 0 and 7 are Sunday and `1-5` is Monday to Friday. Before, they ran one day late (`1-5` ran Tuesday to Saturday). Schedules that use day names, such as `mon-fri`, are unchanged. See [Automations](/docs/features/automations#cron).
- If you use API users limited to certain agents, make sure every call uses one of those agents. Calls for any other agent are now refused, and an API user with several agents must name the one it wants.
- If a chat connection you created in the app never answered, give its API user an agent in **Settings → API**.
- If an integration uses an API key to read, continue or stop sessions that key did not start (for example sessions you started in the app, or ones started with another key), it is now refused. Start those sessions with the same key, or use an owner token.
- If a reverse proxy in another container or on another host sits in front of AI Shuffle, add its address to the new `TRUSTED_PROXIES` setting. Forwarding headers are now read only from this computer and those addresses; without it, the audit log and the pairing limits see every visitor as the proxy. See [Behind a reverse proxy](/docs/getting-started/install-docker-compose#behind-a-reverse-proxy-optional).
- If an integration connects to the CLI bridge WebSocket (`/api/v1/ws/cli/{session_id}`), move it off: that route is removed. Nothing in the app used it.

### Install and packaging

- The code-mode sandbox around agents' Python code is now on by default: the compose file and the Unraid template pass the Docker security options it needs (`seccomp=unconfined`, `systempaths=unconfined` and `apparmor=unconfined`, which relax Docker's default filters for the whole container). Remove those lines or set `CODE_MODE_OS_SANDBOX=off` to opt out. **Settings → System** shows whether the sandbox is active and, when it is not, why. See [the code-mode sandbox](/docs/getting-started/install-docker-compose#the-code-mode-sandbox).
- The Docker image is now private. You download it from `registry.hobbycoders.com` with the pull token from your license email; the token is part of the image name, so there is no `docker login` and pulls keep working after a reboot.
- New buyer compose file with a pinned version, the right shared-memory size for the built-in browser, a 90-second clean shutdown, and a volume for the built-in browser's profile. It listens on this computer only until you choose an address.
- New Unraid template (v2): pinned version, everything under `appdata/ai-shuffle`, the web interface bound to one address, a browser-profile path, and the shared-memory and shutdown settings the image needs.
- The Claude Code CLI is now installed on first start with Anthropic's own installer and kept in its own volume, so it updates itself and survives image updates. AI Shuffle runs only that standard CLI and no longer carries a copy of its own.
- The app shows its real version everywhere, taken from the release.
- Third-party license notices now ship with the app.
- A new setup wizard: welcome, owner password, license, Claude Code mode, sign-in or API key, workspace and a first agent run. See [First run](/docs/getting-started/first-run).
- The compose file and the Unraid template list the optional `TRUSTED_PROXIES`, `CODE_MODE_OS_SANDBOX` and built-in browser network settings, with a commented option for the code-mode sandbox. Defaults are unchanged.
- An empty on/off or number setting in the compose file or the Unraid template (for example `BROWSER_SSRF_ALLOW_LOOPBACK=""`) now means the default instead of stopping start-up with a parsing error. Text settings keep an empty value.
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
- On Linux, code mode now runs agents' Python inside a bubblewrap sandbox with no network and none of the server's files when the system allows it. The new `CODE_MODE_OS_SANDBOX` setting chooses `auto` (the default: sandbox when available), `required` (refuse to run code without it) or `off`. The Docker image includes bubblewrap; the sandbox needs the Docker security options `seccomp=unconfined` and `systempaths=unconfined` (plus `apparmor=unconfined` on AppArmor hosts), which the error message and the log name. See [the code-mode sandbox](/docs/getting-started/install-docker-compose#the-code-mode-sandbox).
- The permission prompt's Enter and Space shortcuts approve only once the prompt has been on screen for half a second, so a key meant for the message box or a held key no longer approves a request. Enter on a focused Deny button or in a text field no longer approves either.
- Each permission request gets its own prompt: a ticked "Always do this" no longer carries over to the next request, a double-click answers only one request, and clicks right as a new request appears are ignored.
- API keys of 10 or 11 characters are now fully masked instead of shown whole.
- Code mode: when `required` finds no sandbox, saved skills are no longer marked broken; they are checked again once the sandbox works. A skill saved while its self-test could not run stays unused until the test passes. A sandbox that refuses a later start falls back to running without it in `auto` and fails the cell with the reason in `required`.
- Code mode's checks inside the process also block a denied function started directly as a new thread.
- On Windows, drive-letter and network (`\\server\share`) paths count as local folders when cloning, so an API key can no longer clone a local repository through them.
- Passwords that contain `@`, and `ssh://user:password@` addresses, are now fully removed from clone addresses in logs and messages.
- Forwarded addresses written with a port or in brackets, and IPv4 addresses written as IPv6, are matched against `TRUSTED_PROXIES` and counted per visitor.
- Plugin names in a repository's own settings can no longer point outside the plugin folders.
- Code mode no longer treats a Python cell as safe while the kernel runs without the sandbox, as it does with the stock compose file. Note: in Ask mode, each cell then asks first. With the sandbox active, cells run without a prompt once its self-test has passed. The self-test now runs when the app starts; a cell sent while it is still running asks first.
- On Linux, the app now hides its own process memory and start-up settings from the programs agents start, which run as the same user. Note: this also blocks core dumps and debuggers on the app process; `ALLOW_PROCESS_INTROSPECTION=true` allows them again (see [the code-mode sandbox](/docs/getting-started/install-docker-compose#the-code-mode-sandbox)).
- In Docker, the database and the built-in browser no longer start with the app's secrets (such as `SECRET_KEY`, the owner password and provider keys) in their environment, where other programs running as the same user could read them.
- A team name written by an agent can no longer make the app read files outside the teams folder or signal an unrelated process.
- Unpairing a device or signing it out closes its open connections at once; before, they stayed open until it next used a session. A device whose session token was replaced loses them within about 30 seconds. An outage of the connection service never drops a live connection.

### API keys

- Subagents and voice workers started from an API key's session now run as that key, with its tools, credentials and agents, instead of as the owner.
- Every turn on an API key's session runs as that key, including scheduled wake-ups, voice follow-ups, chat connector turns and turns sent through the REST conversation route. The one exception is a message the owner types or queues in that session's chat: it runs as the owner.
- App logs and errors, the schedule tools and remote triggers are owner-only. Session search and session views return only the key's own sessions, and voice workers give the app's settings tools to the owner only.
- Notes belong to the key that wrote them: a key sees and references only its own notes, on every synced worker too. Notes written before stay the owner's.
- MCP server settings shown to API keys and non-owner devices mask environment values, headers, arguments and credentials in addresses, including secrets inside argument values such as database connection strings.
- Importing a session with an API key refuses agents and projects outside the key's scope and files the session under the key's project. A project-limited key's new chat sessions are filed under its project.
- Usage is the owner's account data. Note: API keys now get an empty usage reading, and the usage tool refuses key sessions.
- Disabling, rotating or deleting an API key closes all its open connections at once, even idle ones: chat, session streams, the browser panel, voice calls and speech.
- Transcription, read-aloud and voice calls use the fal.ai key that the API key's policy allows instead of the owner's. A key set to bring its own fal.ai key without one is refused before anything is sent.
- A session an API key starts on another worker keeps its owner.
- Team views are limited to sessions the caller may open.
- Preview links belong to the owner: a key's sessions no longer get a preview address, and a preview token made for a key's session is refused.
- Live usage updates go only to the owner's own connections and paired devices.
- A key's sessions no longer get the shared notes folder in their workspace; the notes a key sees stay its own.

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
- `Notification` and `SessionEnd` hooks now fire on Claude subscription sessions too.
- The `jq` that hook commands get now behaves like the real `jq` in many more cases, stops runaway programs, and hands anything it does not support to a real `jq` if one is installed.
- Messages you send while an agent is working are no longer lost or delivered twice when the turn ends, fails or waits for background work at that moment, and removing a queued message works with every provider.
- Claude subscription sessions: Stop ends the turn as Interrupted and keeps its cost instead of showing an error, works while Claude Code is still starting, and no longer makes a resumed session send the stopped message again.
- Claude subscription sessions survive a Claude Code crash between turns and unusual output lines instead of failing every later turn.
- Claude subscription sessions show Claude's real rate-limit status instead of always "allowed", and a rejected status marks the window as used up.
- A model or permission-mode switch now counts only once Claude Code confirms it; a refused switch keeps the old setting and is tried again next turn. Switching back and forth (for example Ask, then Bypass, then Ask) now lands on the last choice with every provider. A Stop or timeout while a switch waits for confirmation keeps the old value and the next turn sends the switch again; Claude Code is restarted on the recorded values if it still does not confirm. That Stop also stops the turn the switch was preparing when it is sent within 15 seconds.
- Claude subscription sessions: Stop in the pause between turns ends the turn at once as Interrupted instead of after up to 30 seconds, and the next turn no longer takes the answer of Claude Code's own background turn as its reply.
- Changing the effort mid-session now takes effect. Note: it restarts the session's engine; the conversation is kept.
- Other providers: a reply cut off mid-stream is reported and retried instead of saved as a finished answer, and text already received is kept and marked as interrupted.
- Other providers: errors show the provider's HTTP status and message. A 429 shows the usage-limit notice (wait for the window to reset) only on a subscription login (Claude, ChatGPT/Codex, SuperGrok); on an API key it shows a plain rate-limit notice. HTTP 504 and timeout errors are retried like other brief failures.
- Other providers: a provider that sends nothing for 60 seconds is retried only when not even the response headers arrived; once it has answered, the turn ends as a reply cut off mid-stream instead of sending the request again. This now covers OpenRouter, OpenAI (Responses) and OpenAI-format `custom` providers too, which before waited up to 600 seconds on a silent server. Note: a turn on one of these that used to wait longer than 60 seconds for its first byte now fails; raise `PROVIDER_FIRST_BYTE_TIMEOUT` if a hosted provider needs longer.
- OpenAI (Responses): a provider that answers and then sends only keep-alive heartbeats, with no event, now ends the turn as a reply cut off mid-stream after `PROVIDER_READ_TIMEOUT` (600 seconds) instead of waiting for the 900-second call limit. This applies only before the first event: after it, heartbeats keep a long reasoning step alive up to `PROVIDER_STREAM_DEADLINE`, and with that set to `0`, without limit.
- New settings for sessions on other providers: `PROVIDER_READ_TIMEOUT` (600 seconds between two reads of a reply), `PROVIDER_STREAM_DEADLINE` (900 seconds for one whole call), `PROVIDER_FIRST_BYTE_TIMEOUT` (60 seconds for the first byte, every provider) and `PROVIDER_FIRST_BYTE_TIMEOUT_LOCAL` (the first-byte wait for a provider on this computer or the local network, such as Ollama or vLLM; off by default, since a local server can hold its reply while it loads a model). 0 turns off all but the first.
- Other providers: an out-of-credit refusal (such as OpenAI's `insufficient_quota`, which arrives as a 429) is reported as HTTP 402, is not retried and no longer shows the usage-limit notice. Any other 402 shows the provider's own message, for example OpenRouter asking for fewer `max_tokens`.
- Other providers: an OpenRouter reply that carries an error mid-stream is reported as an error, with its partial text kept and marked, instead of saved as a finished answer. **Resume** works after a reply interrupted by a provider error and sends the failed request again.
- Other providers: tool calls sent without an id each get their own, a tool call cut off by an error shows the error, and replies without usage figures are accepted and estimated instead of dropped or counted as free.
- Usage figures AI Shuffle had to estimate (a provider that reported no token counts, or a reply you stopped before its counts arrived) are now marked: **≈** next to the cost under the reply and **est.** on the Stats tiles and rows that include them, with the reason on hover. The amounts still count toward budgets. Local models are never given a dollar price, even when the model's name matches a hosted one; before, such a model could be billed at the hosted rate. See [Modes and costs](/docs/getting-started/modes-and-costs#when-usage-numbers-are-estimates).
- OpenRouter-only request fields are sent only to OpenRouter.
- Sessions on other providers still set to the retired Auto mode now run as Ask everywhere; before, their prompts were approved automatically.
- Runs nobody watches (automations, connectors, API turns) follow the permission mode as a watched session would. Note: on other providers, Accept Edits and Bypass now run the calls the mode allows instead of waiting 15 minutes and denying them; Ask still waits. Screen control still asks, and is denied when nobody can answer.
- Accept Edits now means what it says: file edits (Edit, Write, MultiEdit, NotebookEdit) run without asking, and every other tool asks as in Ask. Note: before, it ran every tool except screen control on other providers and approved every prompt the Claude Code CLI raised; an agent that relied on that for shell commands, Python cells or web fetches now gets a prompt (and, where nobody can answer, the call is denied or waits as in Ask). Choose Bypass for an agent that should run everything.

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
- Built-in browser: reading a page's response gives up after 5 seconds instead of holding the browser forever on a stream, a long value typed into a field is typed in full instead of stopping part way, and a redirect to a blocked address names that address.
- The Ask prompt for a Python cell is titled "Run Python code", says it appears because the kernel runs without the sandbox, and shows the cell's first 20 lines as a code block instead of escaped text. Every other prompt keeps showing the command, file or address it asks about.
- After you switch project, a slower answer for the project you left no longer replaces the hooks, MCP servers, agents, memory files or GitHub details of the one you are on, and its failure no longer shows as an error there. These panels show the server's reason when a load fails.
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
- **Allow All** and **Deny All** in the permission queue wait for the same short delay as the single-request buttons, so they no longer answer a request that just appeared, and focus moves to the next prompt or back to the message box.
- On a phone-sized screen, opening a session from a notification in the menu drawer closes the drawer. Closing the drawer from a menu item or a notification returns focus to the menu button unless the opened page takes it.

### Accessibility

- Dialogs and the command palette keep keyboard focus inside while open, close on Escape and return focus where it was. Enter on a focused Cancel or Close button no longer confirms.
- The message box, icon-only buttons, menus, fields and each API user's Edit, Rotate and Delete buttons have names screen readers announce, and states shown only by colour now also have text.
- A streaming reply is announced once instead of per word, and the permission prompt says when its shortcuts are ready.
- Row buttons in the connector, plugin, file and API-user lists name their item, the project switcher says what it does, branch menus are announced as menus, and the git dialogs announce errors and the selected mode.

### Connectors

- Creating a chat or incoming-webhook connection now asks which agent answers it. Before, new connections refused every message until an agent was assigned elsewhere.
- Changing part of a connection's settings through the API keeps the rest (such as the bot token) instead of replacing them all, and a running connection restarts with the result.
- Turning a connection on or off through the API now also starts or stops it.
- Starting a connection that is already running no longer answers every message twice.
- Incoming hooks whose name is `test`, `activate`, `deactivate` or `restart` are reachable again.
- Connections stop on a worker that a network membership change removed them from, including ones still connecting, and a connection stuck on connecting is replaced.
- Connector fixes, offered as a connector update in **Plugins** after this release: Philips Hue prefers a light whose name matches exactly over a longer name that contains it, reports a change the bridge refused (for example brightness on a light that is off) instead of claiming success, and no longer fails on an unexpected bridge answer; Home Assistant keeps the case of values (`set house mode to Away`) and `set <device> to off` turns the device off instead of on; Spotify searches with `&` or `#` in the title work; Gmail keeps a blank line in a message body out of the subject, and a reply whose original message could not be read no longer fails to send; Discord refuses to delete the bound channel however its ID is spelled; Hue, Home Assistant, Spotify and Gmail connections leave the error state on their own once the device or service answers again.

### Desktop app (beta)

- The desktop app uses your normal Claude Code login instead of a separate one.
- It tells you on first launch that agents run as your user account.
- Each launch uses its own token between the window and its worker.
- `Start AI Shuffle.cmd` on Windows now starts the desktop app; a broken path stopped it before.
- **Worker → Worker Status…** now offers **Open AI Shuffle** to return to the app.
- An update that fails partway reports the failure and starts the worker and database again instead of leaving them stopped.
- A worker that cannot start shows an error instead of staying on "starting", and quitting no longer tries to stop a process that is not the app's database.
- Only AI Shuffle's own pages get microphone, clipboard and fullscreen access, and sign-in or payment pop-ups get none. The launch token is sent only to the worker.
- A background script left behind by a sign-in or payment pop-up no longer breaks the app until its profile is cleared. The worker's own background scripts get the launch token, pop-ups that open another site run in a separate session with no token or permissions, and leftover background scripts are cleared at start and when a pop-up closes.
- **Quit** from the tray stops the worker cleanly and forces it after 60 seconds; closing the window offers **Quit Anyway** if stopping fails again.
- The unused `aishuffle://` link handler is removed, so web pages can no longer launch the app.
- The desktop app reports update status to the web interface every 30 seconds, and at once when the phase changes (checking, available, downloading, ready), with less load on the worker. Download percentages in the web interface move in 30-second steps. One missed report no longer makes the update button say the updater is unavailable.
- **Restart and update** from the bell asks first while sessions are running, and says so when it cannot tell.
- A worker health reply cut off partway no longer leaves the app on "starting" and makes **Quit** hang, and an old worker exiting late no longer marks the new one failed. Opening the app from the dock or tray during the first-launch notice no longer starts the worker before you accept it, and two quick opens no longer create two windows.
- A worker health check that answers one byte at a time now gives up after 5 seconds and says why, instead of holding start-up.
- `worker.log` also moves to `worker.prev.log` when it passes 20 MB during a run, so a long run no longer grows it without limit. `worker.prev.log` then holds this run's earlier output instead of the previous run's log.
- An update lock left empty or damaged for more than ten minutes is taken over, so updates no longer stay blocked until the file is deleted by hand.

### Android app

- You can delete your HobbyCoders account from inside the app.
- **Settings → About** no longer crashes.
- App and website text points to **Settings → Providers → Devices** and the **Sign in** button that exists.
- Devices still registered by older Android builds with a UnifiedPush address are treated as unable to receive push, so notifications to them are no longer counted as delivered.
- Voice calls in the Android app now use the voice built into the chat page, the same as the website. The app's own call screen, the mic button in the chat toolbar and the **Start voice chat** entry on a worker's menu are gone; start a call from the voice button in the chat instead.

### Remote access and accounts

- Workers signed in to the same Shuffle account now always talk to each other over the encrypted tunnel, even on the same local network and even if they were paired with an invite code; the plain local address is used only between workers that are not signed in. A worker whose tunnel does not answer shows as offline instead of being contacted over plain http. Invite codes are for your home network or a VPN: a plain `http://` invite to a public address is refused, and the Devices panel says so.
- Remote access keeps working through short outages of the connection service.
- The connection service no longer stores synced data. Sync between your workers stays end to end encrypted.
- Account devices that ask for owner access must be approved on each worker (**Settings → Providers → Devices**).
- Two workers with different versions of the same plugin no longer send each other updates forever. Adding a plugin or turning one on or off is shared; each worker keeps the version it has.
- Addresses and capabilities that other workers and your account's device list report are checked before they are kept: credentials and link-local addresses are dropped, lists are capped, and a malformed value no longer breaks a sync.
- A damaged file part from another worker no longer stops the rest of the missing files from arriving, and an artifact download reports it instead of a server error.
- Network recovery and loading an older transcript from another worker answer 409 on a worker that is not in a network, instead of a server error.

### Reliability

- Upgrading from a build before 19 September (database schema 88 or older) no longer fails at start.
- Several workers sharing one PostgreSQL database can start at the same time without failing on setup.
- The app's own log lines appear in `docker logs` again.
- An approved automation run waiting at shutdown is re-queued on the next start and runs once. Runs of disabled or deleted automations are skipped, approvals older than 24 hours expire, and events that arrive during shutdown no longer create runs. Once shutdown has begun, no approved or scheduled run starts by any path, and a run cancelled after its session was created removes that empty session again.
- A catalog refresh started while another one runs waits for it, and a refresh gives up after two minutes instead of hanging.
- Error reports the app sends to the server are size-capped.
- A permission check no longer waits for the code-mode sandbox self-test to finish.

### Removed

- Message hooks. This hook type never delivered anything and only reported failures. It is gone from the API, the hook editor and the agent's configuration tools; on upgrade, any message hooks still in the database are deleted. Bash and webhook hooks are unchanged. See [Hooks](/docs/features/hooks).
- The team status bar above the chat and the `/api/v1/teams` routes behind it. Nothing has recorded agent teams since the streaming rewrite, so the bar was always empty. Team cards in the history of old sessions still show.
- The voice "command" session mode. No app used it; voice sessions are always chat.
- The CLI bridge WebSocket (`/api/v1/ws/cli/{session_id}`). No app used it, and it accepted any API key for any session. See Action needed.
- Standalone outbound webhooks (`/api/v1/webhooks`). No app could create or list one, so none existed in practice; connectors and automations already receive the same session events. Any rows left by an old build are removed on upgrade.
- Endpoints no app calls: `/api/v1/permission-rules` (rules you save from a permission prompt still apply), the credential-policy routes under `/api/v1/settings`, `/api/v1/export-import` (an agent's own **Export** and **Import** remain), and `/api/v1/sessions/{id}/provider-workers` and `/stop`.
- The Android push registration routes (`/api/v1/mobile/capabilities`, `/push/token`, `/push/unifiedpush`) and the `/api/v1/mobile/notifications/stream` feed. No app version registers for push or reads the feed; the bell remains the channel that reaches you. A device's stored UnifiedPush address is dropped on upgrade, and the unused `rate_limits` and `request_log` tables from the removed rate-limit feature are dropped too.
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
- WebFetch stops downloading at its 5 MiB limit instead of reading the whole page first, and a page with an unknown character set is read as UTF-8 instead of failing.
- An Edit with empty old text is refused unless the file is empty; before, it inserted the new text between every character.
- One malformed MCP server address no longer breaks the whole settings import preview.
- Session import: a file over 50 MB gets a "too large" error, and a malformed export (not a JSON object, a message that is not an object, a title that is not text, counters that are not numbers) is refused before anything is saved instead of failing with a server error or leaving an empty session behind. An export naming a project this install does not have is imported without a project, and re-importing the app's own export keeps its whole transcript.
- Session import also checks every message's fields before saving anything, and a transcript that cannot be written returns an error and removes the new session instead of leaving it half imported.
- In Docker, the folder browser, project paths, the default working folder for commands and sessions, the built-in browser, history, plugin paths and settings import all use the workspace chosen during setup. Before, some used the start-up folder, so paths inside the chosen workspace were refused or resolved elsewhere, and settings import skipped projects there as "outside workspace".
- Changing the workspace in **Settings** applies at once; before, some paths kept using the old folder until a restart.
- An MCP or plugin settings file that exists but cannot be read (damaged, or caught while another program was writing it) is no longer replaced by an empty one on the next change. The change is refused with an error naming the file, which is left as it was. Plugin auto-update settings are the exception and are replaced.
- A plugin install, uninstall, update or marketplace change refused for such a file is refused before it deletes, copies or clones anything, so the plugin or marketplace is left as it was and the same request works once the file is fixed. Before, uninstalling had already deleted the plugin's files, so the retry answered "not installed". A bad plugin ID in a batch install now answers 400, like a single install, instead of a server error.
- When a damaged plugin settings file is refused, a copy of it is saved next to it as `<name>.<time>.bak`. A copy never overwrites an earlier one or a `.bak` of your own, the same content is saved only once, and the five newest copies of each file are kept.
- The hooks card, the project agents, hooks and memory tabs and the build page no longer leave an uncaught error behind when a load fails; the tab shows the error, and the build page's badges stay hidden.
- Clearing the sign-in-again flag of a ChatGPT or Grok provider no longer changes how it signs in; a provider without that sign-in returns 404.
- MCP servers, plugins and the project MCP tab show a failed load or action instead of failing silently, and a failed plugin details load stays on the list. Changing project closes an MCP editor still open on the previous project's server, and removing a global MCP server from a project asks first.
- Uninstalling a plugin asks first, and the API user list stays usable while it shows an error.
- Files: a refused new folder or rename keeps what you typed, and pressing Enter twice no longer creates the folder twice.
- Clearing an API user's description or project restriction now saves.
- A Claude settings file that holds `null`, a list or text that is not UTF-8 no longer breaks every MCP server request. MCP server names must use plain ASCII characters.
- One agent file that is not UTF-8 no longer hides all of a plugin's agents, and hooks written in a plugin's `plugin.json` in the standard object form now load.
- A documentation page with a non-numeric order no longer breaks the whole documentation index.
- The phone's open-URL tool reports a malformed address or a stored expiry without a time zone instead of failing.
- Documentation: device unpairing, provider timeout settings and errors, Resume after an interrupted reply, and session import errors are described as the app behaves.
- Documentation: a second pass against the app corrected many pages (permission modes, API users, voice, usage, settings import, hooks, connectors), and new sections cover reverse proxies and the code-mode sandbox.
