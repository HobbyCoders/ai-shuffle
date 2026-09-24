<!--
title: Install with Docker Compose
description: Install AI Shuffle on Linux, a NAS or any Docker host with the buyer compose file
order: 5
-->

# Install with Docker Compose

For a Linux server, a NAS or a desktop running Docker. On Unraid, use [Install on Unraid](/docs/getting-started/install-unraid) instead.

You need a 64-bit Intel or AMD host, Docker Engine 24 or later with Docker Compose 2.24 or later (`docker compose version` prints it), and your **pull token** from your license email. See [Requirements](/docs/getting-started/requirements).

## 1. Create the two files

Make an empty folder, for example `~/ai-shuffle`, and put two files in it.

`compose.yaml`: copy it exactly as it is.

```yaml
# AI Shuffle with Docker Compose.
#
# 1. Put this file and env.example in an empty folder.
# 2. Copy env.example to .env and set AI_SHUFFLE_PULL_TOKEN from your license email.
# 3. Run: docker compose up -d
# 4. Open http://localhost:8000 on this computer.
#
# Full guide: "Install with Docker Compose" in the AI Shuffle docs. Needs Docker Engine 24 or later
# with Compose 2.24 or later, on a 64-bit Intel or AMD (amd64) host.
#
# Everything you keep (database, settings, sign-ins, projects) lives in the
# named volumes below and in WORKSPACE_DIR. Updating is: change
# AI_SHUFFLE_VERSION in .env, then `docker compose up -d`. Never run
# `docker compose down -v`; -v deletes the volumes.

name: ai-shuffle

services:
  ai-shuffle:
    # A pinned release, pulled through your licensed registry path. The pull
    # token is read-only and is not your license key.
    image: ${AI_SHUFFLE_REGISTRY:-registry.hobbycoders.com}/${AI_SHUFFLE_PULL_TOKEN:?Set AI_SHUFFLE_PULL_TOKEN in .env (see env.example)}/ai-shuffle:${AI_SHUFFLE_VERSION:-0.8.18}
    container_name: ai-shuffle
    restart: unless-stopped

    # The app stops its agents first, then its private PostgreSQL database
    # writes a clean checkpoint. That can take up to 90 seconds; Docker's
    # default of 10 seconds would cut it off.
    stop_grace_period: 90s

    # The built-in browser (Chromium) needs more shared memory than Docker's
    # 64 MB default.
    shm_size: 1gb

    # A burst of short-lived agent processes must not exhaust the process table.
    pids_limit: 8192

    environment:
      PUID: ${PUID:-1000}
      PGID: ${PGID:-1000}
      # Plain http:// on a LAN address needs this off; keep it on behind HTTPS.
      COOKIE_SECURE: ${COOKIE_SECURE:-false}
      # Seeds the owner password on the first start only (see env.example).
      OWNER_PASSWORD: ${OWNER_PASSWORD:-}

    # Loopback only by default: the web UI is reachable from this computer.
    # To use it from another device, set BIND_HOST in .env to one address of
    # this host (for example its LAN or Tailscale IP). Never 0.0.0.0, and
    # never forward this port from your router.
    ports:
      - "${BIND_HOST:-127.0.0.1}:${PORT:-8000}:8000"

    volumes:
      # Database, encryption key, sessions and logs.
      - data:/data
      # Your projects. A folder on this host so you can open the files directly.
      - ${WORKSPACE_DIR:-./workspace}:/workspace
      # AI Shuffle settings.
      - fluid-settings:/home/appuser/.fluid
      # The Claude Code CLI login, kept when the container is replaced.
      - claude-home:/home/appuser/.claude
      # The Claude Code CLI itself (installed on first start, updates itself).
      - local-home:/home/appuser/.local
      # GitHub CLI sign-in.
      - gh-auth:/home/appuser/.config/gh
      # The built-in browser's profile, so sites you signed in to stay signed in.
      - browser-profile:/home/appuser/chrome-profile

    # Ready once the database answers. First start can take a few minutes
    # while the Claude Code CLI installs.
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/health/ready"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 120s

    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  data:
  fluid-settings:
  claude-home:
  local-home:
  gh-auth:
  browser-profile:
```

`.env`: your settings. Copy this, then fill in `AI_SHUFFLE_PULL_TOKEN`.

```bash
# AI Shuffle settings for Docker Compose.
# Copy this file to .env in the same folder as compose.yaml, then edit it.
# These are the only settings a normal install needs; everything else is set
# in the app.

# --- Required ---------------------------------------------------------------

# Your pull token, from your license email or your account page on
# hobbycoders.com. It only lets Docker download AI Shuffle; it is not your
# license key and cannot sign in to anything.
AI_SHUFFLE_PULL_TOKEN=

# The release to run. Pinned on purpose: nothing changes until you change it.
# To update: set the new version here, then run `docker compose up -d`.
AI_SHUFFLE_VERSION=0.8.18

# --- Where and how it is reachable -------------------------------------------

# The host address the web UI listens on. 127.0.0.1 means only this computer.
# To open it from another device, use ONE address of this host, for example
# its LAN address (192.168.1.20) or Tailscale address (100.x.y.z).
# Do not use 0.0.0.0 and do not forward this port from your router.
BIND_HOST=127.0.0.1

# The port on that address.
PORT=8000

# Plain http:// on a LAN address needs false. Set true when you reach AI
# Shuffle only through HTTPS (a reverse proxy or Tailscale Serve).
COOKIE_SECURE=false

# --- Owner password -----------------------------------------------------------

# Optional. The password every browser signs in with. It is read on the FIRST
# start only; after that AI Shuffle keeps its own hashed copy and ignores this
# line, so change the password in Settings > Account > Owner access (and clear
# this line if you like). Leave it empty to create the password in your
# browser on first visit instead. At least 12 characters.
OWNER_PASSWORD=

# --- Files and permissions ---------------------------------------------------

# The folder on this host where your projects live. Created on first start.
WORKSPACE_DIR=./workspace

# The user and group that own WORKSPACE_DIR. On Linux, `id -u` and `id -g`
# print yours. Leave 1000 on a single-user machine.
PUID=1000
PGID=1000
```

## 2. Set your pull token

Paste your pull token after `AI_SHUFFLE_PULL_TOKEN=`. It is part of the image name, so Docker downloads the image without `docker login`, and pulls keep working after a reboot.

The pull token only downloads the image; it is not your license key and cannot sign in to anything. It shows in `docker ps` and in logs, so if you share it by accident, get a new one from your account on hobbycoders.com, put it in `.env` and run `docker compose up -d`. The old token stops working within a minute; the running container is not affected.

## 3. Choose who can reach it

By default AI Shuffle listens only on this computer (`BIND_HOST=127.0.0.1`), so you open it at <http://localhost:8000>.

To use it from a laptop or phone on your network, set `BIND_HOST` to **one** address of this host:

- its LAN address, for example `192.168.1.20`, to reach it from your home network;
- its Tailscale address (`100.x.y.z`), to reach it only over your tailnet.

Do not use `0.0.0.0`: that publishes AI Shuffle on every network the host is on, including VPN and Docker networks, and Docker-published ports can bypass the host firewall. Never forward the port from your router to the internet. For access away from home, see [Remote access](/docs/getting-started/remote-access).

Keep `COOKIE_SECURE=false` for plain `http://` on your LAN. Set it to `true` only when you reach AI Shuffle exclusively through HTTPS, for example with Tailscale Serve.

## 4. Choose the workspace folder

`WORKSPACE_DIR` is the folder on this host where your projects live (`./workspace` next to `compose.yaml` by default). Inside the container it is always `/workspace`. Set `PUID` and `PGID` to the user and group that should own the files; on Linux, `id -u` and `id -g` print yours.

## 5. Start it

```bash
docker compose up -d
```

The first start downloads the image and installs the Claude Code CLI with Anthropic's installer, so it can take a few minutes. `docker compose ps` shows `healthy` when AI Shuffle is ready; `docker compose logs -f` shows progress.

## 6. First launch

Open the address you chose (<http://localhost:8000> on the host itself) and follow [First run](/docs/getting-started/first-run). The welcome screen and the owner password come first; the rest of the wizard follows once the password is set.

To sign in to the Claude Code CLI from the host's terminal instead of the in-app one:

```bash
docker exec -it -u appuser ai-shuffle claude auth login
```

## Back up

Stop the container (`docker compose stop`), then back up the Docker volumes whose names start with `ai-shuffle_` and your `WORKSPACE_DIR` folder. The `data` volume holds the database and the key that encrypts your saved credentials; always keep them together. Start again with `docker compose start`.

## Update

1. Read the release notes for the new version (see [Changelog](/docs/getting-started/changelog)).
2. Set `AI_SHUFFLE_VERSION` in `.env` to the new version.
3. Run `docker compose up -d`.

Your data stays in the volumes and in `WORKSPACE_DIR`. See [Updates](/docs/getting-started/updates) for in-app updates and what the update check sends.

## Remove

`docker compose down` removes the container and keeps your data.

`docker compose down -v` also **deletes every volume**, including the database and your saved credentials. Use it only when you mean to start over.
