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
- Your **pull token** from your license email. It lets Docker download the
  image and nothing else.

## Install

1. Make an empty folder and put two files in it:
   [`compose.yaml`](compose.yaml) and [`env.example`](env.example).
2. Copy `env.example` to `.env` and set `AI_SHUFFLE_PULL_TOKEN`.
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

## Your pull token

The pull token only downloads the image; there is nothing to log in to, and
it keeps working after a reboot. It appears in `docker ps` and in logs, so if
you have shared it by accident (a screenshot, a forum post), get a new one
from your account on hobbycoders.com, put it in `.env` and run
`docker compose up -d`. The old token stops working within a minute; your
running container is not affected.

If your license ends, AI Shuffle keeps running and your data stays yours. The
exact version in `.env` can still be downloaded again (for example on a new
host), but new versions need an active license.

## Back up

Stop the container (`docker compose stop`), then back up the Docker volumes
whose names start with `ai-shuffle_` and your `WORKSPACE_DIR` folder. The `data`
volume holds the database and the key that encrypts your saved credentials;
keep them together. The "Back up" section of the install guide has the
commands, and how to restore.

## Remove

`docker compose down` removes the container and keeps your data.
`docker compose down -v` also **deletes every volume**, including the database.
