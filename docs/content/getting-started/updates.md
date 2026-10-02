<!--
title: Updates
description: Release channels, updating the image, in-app updates and what the update check sends
order: 10
-->

# Updates

AI Shuffle never changes under you. Your install runs the exact version named in your compose file or Unraid template until you change it or accept an in-app update.

## Release channels

Customer installs follow the **stable** channel only.

- A stable release is a numbered version (`X.Y.Z`). It is published only after its image passed the full container checks and a clean install from the buyer compose file on a fresh machine.
- In-app updates are signed. Your worker accepts only an update signed for the stable channel, checks the signature, size and checksum, and re-checks every source file against the signed list before each start.
- Unreleased development builds are never offered to customer installs.

Read what changed in the release notes shown next to the update button.

## Two kinds of update

### In-app updates

Most releases can be installed from inside AI Shuffle:

1. When a new release is available, the update button appears in the sidebar with its release notes.
2. Choose **Download update**, then **Update and restart**. Agents stop, the worker installs the release and starts again.

**Settings → System → Updates → Allow updates** turns in-app updates on or off for every worker on your account. With it off, nothing is checked, downloaded or installed.

### New image

Some releases change the container itself (a new browser, database or system packages). The in-app update then says **This source update needs a newer container release**. Update the image instead:

- **Docker Compose:** set `AI_SHUFFLE_VERSION` in `.env` to the new version and run `docker compose up -d`.
- **Unraid:** edit the container, change the version at the end of **Repository**, and **Apply**.

Your data stays in its volumes or appdata folders. The container needs up to 90 seconds to stop cleanly; let it.

Updating the image is always safe even when an in-app update would have worked, and it is the way to move several versions at once.

## The Claude Code CLI updates itself

The Claude Code CLI inside the container is installed with Anthropic's installer on first start and keeps itself up to date, as it does on any computer. It is stored in its own volume or appdata folder, so image updates do not reinstall it, and AI Shuffle never modifies it.

## Upgrading from a version that signed in to Claude for you

Earlier versions ran their own Claude sign-in. That is gone: the Claude Code CLI now owns its login, and the upgrade deletes every Claude account record AI Shuffle kept. The CLI's existing login keeps working, but once after the upgrade the owner sees a notice asking you to sign out and in again so the login comes from the CLI's own sign-in: run `claude auth logout`, then `claude auth login`, in the AI Shuffle terminal (or `docker exec -it -u appuser <container> claude auth login`). AI Shuffle never reads your Claude credentials. The Claude usage bars now show only what the CLI reports while an agent runs; the full picture is on [claude.ai/settings/usage](https://claude.ai/settings/usage).

## What the update check sends

- **What:** a plain `GET https://hobbycoders.com/v1/desktop/manifest`. No cookies, no account token, no license key, no device or install ID, and nothing about your projects, sessions or settings. Like any web request, it reveals your IP address and a generic HTTP client name.
- **When:** about 15 seconds after the app starts, then once every 24 hours.
- **What comes back:** a small signed file naming the newest release and its notes. Nothing is downloaded or installed until you choose to.
- **Where it goes:** a Cloudflare Worker on hobbycoders.com, which keeps no record of update checks. Cloudflare's own request logging applies. Release files are stored with GitHub and delivered through hobbycoders.com, so your worker does not contact GitHub for an update. Installing an update downloads the pinned Python and JavaScript packages it lists from their public registries.
- **Turning it off:** **Settings → System → Updates → Allow updates**. New images still arrive only when you change the version yourself.

Once licensing is live, update requests will also carry the worker's signed license lease, because updates are part of what a license pays for. The lease names your license and this install, and nothing about your work. See [License](/docs/getting-started/license) and the [privacy policy](https://hobbycoders.com/privacy#updates).

## If your license ends

Your install keeps running the version it has, and the exact version in your compose file or template can still be downloaded again, for example on a new host. New versions need an active license.
