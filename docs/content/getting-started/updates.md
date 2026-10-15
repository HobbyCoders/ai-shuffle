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

### Runtime overlays

An overlay-aware Docker supervisor can install a release's required Python, Node, Chromium and relocatable tools/libraries on the persistent data volume. Nothing changes for an ordinary release: the image's runtimes remain the default. Overlays activate only when a signed release requires a capability the image lacks and includes an archive for this architecture with a pinned SHA-256 and size limits. Downloads and installed contents are verified before use; there are no privileged package-manager operations.

A release without a Python overlay always builds its virtual environment with the immutable image interpreter, not the supervisor’s interpreter. Launch and cold handover check the environment’s base interpreter against the signed runtime selection; cleanup also protects inherited interpreter dependencies of retained environments.

Archive limits apply before tar parsing: decompressed bytes may exceed the signed expanded bound by at most 64 KiB for headers/padding, individual PAX/GNU metadata records are limited to 16 KiB (64 KiB total), and at most 50,000 headers are accepted. Large archive descriptors must include structural overhead in their expanded bound. The raw-stream guard accepts only PAX path/link, timestamp, owner/group, comment and character-set metadata. PAX size or offset overrides, sparse extensions, unknown keys and nonzero PAX padding are rejected; they cannot redirect tarfile into payload bytes as hidden headers. Sparse runtime archives are unsupported.

The application revision selects its runtimes. If its first start fails and the database snapshot is restored, the previous revision and its runtime selection return together. Active, staged, previous, retained failed-build and currently running supervisor runtimes are retained; unreferenced overlays are cleaned up. Downloads are limited to 512 MiB each, expanded payloads to 2 GiB each, and the overlay store to 8 GiB, with 256 MiB of free disk space reserved. Insufficient space leaves the current version running.

This is delivery support, not a claim that every installed image can already update forever. Releases do not request overlays by default. Older images whose baked startup code rejects missing capabilities still need an overlay-aware bootstrap image before they can cold-start such a release. Runtime artifacts need acceptance on both supported architectures before publication. PostgreSQL stays on the image's existing major version; PostgreSQL major upgrades and incompatible operating-system/ABI changes are not attempted through overlays.

## Updating the Docker image

When no compatible runtime overlay can satisfy a release, the in-app update says **Needs a newer Docker image. This version keeps working.** It names the missing capabilities and does not stop the running version. Update the image instead:

- **Docker Compose:** set `AI_SHUFFLE_VERSION` in `.env` to the new version and run `docker compose up -d`.
- **docker run:** pull the new version, remove the container and start it again with the new version. See [Install with Docker](/docs/getting-started/install-docker-compose#update).

Your data stays in its volumes. Give the container up to 90 seconds to stop cleanly.

Updating the image always works, even when an in-app update would have, and it is the way to jump several versions at once.

## The desktop app

The desktop app checks for new releases when it starts and once a day, and offers to install them.

Before a source update first starts, a current desktop launcher stops the worker and managed PostgreSQL and saves one validated database restore point. If the copy fails (for example, the disk is full), the update does not start and the previous version is kept. Leave room for a database copy and temporary restore files.

If the first start crashes, times out or is interrupted, the next launch restores that database copy before running the previous version. A notice names the failed update and the version restored; the failed update is not offered again automatically. The restore point is deleted once the new build is healthy. This protects the update's first start, not later downgrades or switching from nightly back to stable.

If restoration fails, no older worker starts on the changed database. The recovery dialog offers **Go back to <previous version>** to retry the restoration, or **Quit**. Keep the restore point and logs. Older launchers and earlier failures without a verified restore point retain their migration fence: retry the affected build or install a verified newer release; never delete the fence or point older code at the database.

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
