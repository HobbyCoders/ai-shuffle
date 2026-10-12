<!--
title: Updates
description: Updating AI Shuffle, in-app updates and what the update check sends
order: 10
-->

# Updates

AI Shuffle never changes under you. Your install runs the version you chose until you update it.

## Release channels

**Stable** is the default: tested releases. **Nightly** follows yesterday's `main`: it may break. A signed nightly is published at 06:00 UTC only when `main` changed and that exact revision has a local full-suite pass. If the pass is missing, the previous nightly stays available. Both channels verify signatures before installing.

**There is no way back from nightly to stable without a reinstall.** Database migrations do not run backwards. Keep a backup before switching; pointing an older stable version at a nightly database is not a downgrade path.

For Docker, set `UPDATE_CHANNEL=nightly` (or `stable`). An explicit environment value overrides the saved local choice and makes it read-only in the channel API. Without that override, the owner API saves `application-updates/update-channel.json` in the data volume; restart the container to apply a change. Desktop stores `{"channel":"nightly"}` in `updates/update-channel.json` under its user-data directory. The choice applies to this install, not every worker on the account.

For one release, existing `edge` settings are accepted as an alias of `nightly` and log a deprecation line. Publishing a nightly does not switch channels, install it or restart a worker. Read the release notes next to the update button before updating.

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
