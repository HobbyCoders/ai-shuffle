<!--
title: Changelog
description: What changed in each AI Shuffle release
order: 14
-->

# Changelog

Newest release first. Each release lists what changes for you, grouped by area. The same notes appear next to the in-app update button. How to install a release: [Updates](/docs/getting-started/updates).

## Next release (changes since 0.8.18)

Not yet released. This entry collects everything merged since 0.8.18 and becomes the release notes when the version is tagged.

### Install and packaging

- The Docker image is now private. You download it from `registry.hobbycoders.com` with the pull token from your license email; the token is part of the image name, so there is no `docker login` and pulls keep working after a reboot.
- New buyer compose file with a pinned version, the right shared-memory size for the built-in browser, a 90-second clean shutdown, and a volume for the built-in browser's profile. It listens on this computer only until you choose an address.
- New Unraid template (v2): pinned version, everything under `appdata/ai-shuffle`, the web interface bound to one address, a browser-profile path, and the shared-memory and shutdown settings the image needs.
- The Claude Code CLI is now installed on first start with Anthropic's own installer and kept in its own volume, so it updates itself and survives image updates. AI Shuffle runs only that standard CLI and no longer carries a copy of its own.
- The app shows its real version everywhere, taken from the release.
- Third-party license notices now ship with the app.

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

### Sessions and interface

- Long sessions have a redesigned timeline for moving through earlier turns.
- Saving a remembered fact works when the agent also sends a revision.

### Desktop app (beta)

- The desktop app uses your normal Claude Code login instead of a separate one.
- It tells you on first launch that agents run as your user account.
- Each launch uses its own token between the window and its worker.
- macOS and Linux builds get their own update feed.

### Android app

- You can delete your HobbyCoders account from inside the app.

### Remote access and accounts

- Remote access keeps working through short outages of the connection service.
- The connection service no longer stores synced data. Sync between your workers stays end to end encrypted.
