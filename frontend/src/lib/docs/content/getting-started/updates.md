<!--
title: Updates
description: Updating AI Shuffle, in-app updates and what the update check sends
order: 10
-->

# Updates

AI Shuffle never changes under you. Your install runs the version you chose until you update it.

## Release channel

Customer installs get **stable** releases only. Every release is tested before it is published, and in-app updates are signed and verified before they install. Read what changed in the release notes shown next to the update button.

## In-app updates

Most releases install from inside AI Shuffle:

1. When a new release is out, the update button appears in the sidebar with its release notes.
2. Choose **Download update**, then **Update and restart**. Agents stop, the update installs, and AI Shuffle starts again.

**Settings → System → Updates → Allow updates** turns in-app updates on or off for every worker on your account.

## Updating the Docker image

Some releases change the container itself. The in-app update then says **This source update needs a newer container release**. Update the image instead:

- **Docker Compose:** set `AI_SHUFFLE_VERSION` in `.env` to the new version and run `docker compose up -d`.
- **docker run:** pull the new version, remove the container and start it again with the new version. See [Install with Docker](/docs/getting-started/install-docker-compose#update).

Your data stays in its volumes. Give the container up to 90 seconds to stop cleanly.

Updating the image always works, even when an in-app update would have, and it is the way to jump several versions at once.

## The desktop app

The desktop app checks for new releases when it starts and once a day, and offers to install them.

## The Claude Code CLI

If you use Claude, the Claude Code CLI keeps itself up to date, as on any computer. Updating AI Shuffle does not reinstall it.

## What the update check sends

- **What:** a plain request for the latest release information. No account, license key, device ID or anything about your projects, chats or settings. Like any web request, it reveals your IP address.
- **When:** shortly after the app starts, then once a day.
- **What comes back:** a small signed file naming the newest release and its notes. Nothing is downloaded until you choose to.
- **Turning it off:** **Settings → System → Updates → Allow updates**.

Updates are free with or without a Cloud account. A linked worker may send its signed Cloud lease to the hub; that lease controls remote capacity, not access to updates. See [AI Shuffle Cloud](/docs/getting-started/cloud).

## If your Cloud plan ends

Your install keeps running, including agent runs, and you can still download and install new versions for free. Only paid Cloud services change when a plan ends.
