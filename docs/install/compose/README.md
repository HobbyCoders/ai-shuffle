# Install AI Shuffle with Docker Compose

For any Docker host: a Linux server, a NAS or a desktop running Docker.

## What you need

- A 64-bit Intel or AMD computer (amd64). ARM (Raspberry Pi, Apple silicon
  servers) is not supported yet.
- Docker Engine 24 or later with Docker Compose 2.24 or later
  (`docker compose version`).
- 4 GB of free memory for light use; 8 GB or more if you run several agents at
  once. Swap on the host is strongly recommended: without it a burst of agents
  can run the host out of memory.
No account, license key or Docker login is needed. Compose downloads the
public `ghcr.io/hobbycoders/ai-shuffle-server` image at the version pinned by
`AI_SHUFFLE_VERSION`.

## Install

1. Make an empty folder and put two files in it:
   [`compose.yaml`](compose.yaml) and [`env.example`](env.example).
2. Copy `env.example` to `.env` and adjust the version and local settings if needed.
3. Start it:

   ```bash
   docker compose up -d
   ```

   The first start downloads the image and sets it up, so it can take a few minutes.
   If you choose Claude, install Claude Code from setup or Settings > Providers > Claude.
   Anthropic's own installer downloads it. It is reinstalled at start only while
   Keep Claude Code installed is on. `docker compose ps` shows `healthy` when it is ready.
4. Open <http://localhost:8000> on the same computer and follow the setup
   wizard.

## Open it from another device

By default AI Shuffle only listens on this computer (`127.0.0.1`). To use it
from a laptop or phone, set `BIND_HOST` in `.env` to **one** address of this
host, such as its LAN address or its Tailscale address, then run
`docker compose up -d` again. Do not use `0.0.0.0`, and never forward the port
from your router to the internet.

## Update

1. Read the release notes for the new version.
2. Set `AI_SHUFFLE_VERSION` in `.env` to the new version.
3. Run `docker compose up -d`.

Your data stays in the named volumes and in `WORKSPACE_DIR`. Updates that do
not need a new image can also arrive inside the app, through the update button
in the sidebar. They are signed releases only, never unreleased code, and
**Settings → General → Allow updates** turns them off. See
[update checks](../README.md#update-checks) for what the check sends.

## Optional Cloud account

Local use and updates are free, with or without a Cloud account. You can
always download the pinned version or update to a new release anonymously.

An optional free Cloud account serves one remote worker. A paid plan serves
three; you can link its key in Settings → Cloud or supply `LICENSE_KEY` at
install time. If a plan ends, nothing local stops working and updates stay
free. See [AI Shuffle Cloud](../../content/getting-started/cloud.md).

## Back up

Stop the container (`docker compose stop`), then back up the Docker volumes
whose names start with `ai-shuffle_` and your `WORKSPACE_DIR` folder. The `data`
volume holds the database and the key that encrypts your saved credentials;
keep them together. The "Back up" section of the install guide has the
commands, and how to restore.

## Remove

`docker compose down` removes the container and keeps your data.
`docker compose down -v` also **deletes every volume**, including the database.
