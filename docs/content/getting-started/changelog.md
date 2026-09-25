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

### Install and packaging

- The Docker image is now private. You download it from `registry.hobbycoders.com` with the pull token from your license email; the token is part of the image name, so there is no `docker login` and pulls keep working after a reboot.
- New buyer compose file with a pinned version, the right shared-memory size for the built-in browser, a 90-second clean shutdown, and a volume for the built-in browser's profile. It listens on this computer only until you choose an address.
- New Unraid template (v2): pinned version, everything under `appdata/ai-shuffle`, the web interface bound to one address, a browser-profile path, and the shared-memory and shutdown settings the image needs.
- The Claude Code CLI is now installed on first start with Anthropic's own installer and kept in its own volume, so it updates itself and survives image updates. AI Shuffle runs only that standard CLI and no longer carries a copy of its own.
- The app shows its real version everywhere, taken from the release.
- Third-party license notices now ship with the app.
- A new setup wizard: welcome, owner password, license, Claude Code mode, sign-in or API key, workspace and a first agent run. See [First run](/docs/getting-started/first-run).

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
- Message hooks are not supported. They now report an error instead of appearing to succeed.

### Sessions and interface

- Long sessions have a redesigned timeline for moving through earlier turns.
- Saving a remembered fact works when the agent also sends a revision.
- The usage panel keeps Claude usage after a restart and on every device. Unchanged readings no longer look stale, a window that has rolled over reads "awaiting next report" instead of "resets in now", and stale sections show when they last updated.
- OpenAI spend keeps updating, background API queries update the rate-limit readings, and the 7- and 30-day analytics ranges cover exactly 7 and 30 days.
- Access requests in a chat update when something changes instead of asking the server every two seconds, and **Saved connections** refresh in every open session.
- Links between documentation pages open inside the Docs panel instead of a missing page. The docs were checked against the app and corrected, including hooks, connector setup, menu paths and the API reference, which now covers the endpoints it left out.
- Menus and messages name the right settings pages (**Settings → Providers → Devices**, **Settings → Account**, **Settings → API**).
- The broken "Manage project knowledge" search action is removed.

### Connectors

- Creating a chat or incoming-webhook connection now asks which agent answers it. Before, new connections refused every message until an agent was assigned elsewhere.

### Desktop app (beta)

- The desktop app uses your normal Claude Code login instead of a separate one.
- It tells you on first launch that agents run as your user account.
- Each launch uses its own token between the window and its worker.
- `Start AI Shuffle.cmd` on Windows now starts the desktop app; a broken path stopped it before.

### Android app

- You can delete your HobbyCoders account from inside the app.
- **Settings → About** no longer crashes.
- App and website text points to **Settings → Providers → Devices** and the **Sign in** button that exists.

### Remote access and accounts

- Remote access keeps working through short outages of the connection service.
- The connection service no longer stores synced data. Sync between your workers stays end to end encrypted.
- Account devices that ask for owner access must be approved on each worker (**Settings → Providers → Devices**).
