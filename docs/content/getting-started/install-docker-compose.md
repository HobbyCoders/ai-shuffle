<!--
title: Install with Docker
description: Install AI Shuffle with Docker Compose or docker run on Linux, a NAS, a home server or any machine with Docker
order: 5
-->

# Install with Docker

Use this guide for a Linux server, a NAS or home server with Docker, or a PC running Docker. Docker Compose is the easiest way. A plain `docker run` command works too.

You need:

- a 64-bit Intel or AMD machine (see [Requirements](/docs/getting-started/requirements));
- Docker Engine 24 or later with Docker Compose 2.24 or later (`docker compose version` shows yours);
No account or Docker login is needed to download the public image.

Many NAS and home server systems have a Docker or "Container" app with a Compose or "Project" screen. Paste the two files below there, or use a terminal on the machine.

> **Before enabling agent shells:** by default an agent can read this install's own database password and credential keys. Consider the [optional agent/service account split](#keep-agents-away-from-ai-shuffles-own-keys-optional) on a disposable install first. It is separate from the code-mode sandbox below and is not fail-closed; setting it to `on` alone does not prove that it is active.

## Install with Docker Compose

### 1. Create the two files

Make an empty folder, for example `~/ai-shuffle`, and put two files in it.

`compose.yaml`: copy it exactly as it is.

```yaml
# AI Shuffle with Docker Compose.
#
# 1. Put this file and env.example in an empty folder.
# 2. Copy env.example to .env and adjust the settings if needed.
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
    # A pinned public release. No account or Docker login is needed.
    image: ghcr.io/hobbycoders/ai-shuffle-server:${AI_SHUFFLE_VERSION:-1.0.0}
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

    # The code-mode sandbox around agents' Python code, on by default. These
    # relax Docker's default seccomp and AppArmor filters and its /proc masking
    # for the whole container; remove them to opt out. See "The code-mode
    # sandbox" in the install guide.
    security_opt:
      - seccomp=unconfined
      # Lets the sandbox mount its own /proc:
      - systempaths=unconfined
      # Docker ignores this on hosts without AppArmor:
      - apparmor=unconfined

    environment:
      PUID: ${PUID:-1000}
      PGID: ${PGID:-1000}
      # Plain http:// on a LAN address needs this off; keep it on behind HTTPS.
      COOKIE_SECURE: ${COOKIE_SECURE:-false}
      # Seeds the owner password on the first start only (see env.example).
      OWNER_PASSWORD: ${OWNER_PASSWORD:-}
      # Optional: links a paid Cloud plan on the first start (see env.example).
      LICENSE_KEY: ${LICENSE_KEY:-}
      # Optional: each applies only once uncommented here (a line in .env alone
      # does not reach the container). The install guide explains each one.
      # The reverse proxy's address, as AI Shuffle sees it:
      # TRUSTED_PROXIES: "172.18.0.1"
      # The code-mode sandbox: auto (the default), required or off:
      # CODE_MODE_OS_SANDBOX: "auto"
      # Ranges the built-in browser may open. This list replaces the localhost
      # default, so keep the first two ranges to still allow localhost:
      # BROWSER_SSRF_ALLOW: "127.0.0.0/8,::1/128,192.168.1.20/32"
      # Refuse localhost too (no effect when BROWSER_SSRF_ALLOW is set):
      # BROWSER_SSRF_ALLOW_LOOPBACK: "false"
      # Ranges to refuse even if BROWSER_SSRF_ALLOW lists them:
      # BROWSER_SSRF_BLOCK: "192.168.1.1/32"

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
      # Provider CLI sign-in (the Claude Code CLI, for a Claude subscription),
      # kept when the container is replaced.
      - claude-home:/home/appuser/.claude
      # Provider CLI programs (Claude Code installed on demand from Settings
      # or setup with Anthropic's installer; it updates itself). Reinstalled
      # at start only while Keep Claude Code installed is on.
      - local-home:/home/appuser/.local
      # GitHub CLI sign-in.
      - gh-auth:/home/appuser/.config/gh
      # The built-in browser's profile, so sites you signed in to stay signed in.
      - browser-profile:/home/appuser/chrome-profile

    # Ready once the database answers. First start can take a few minutes
    # while it sets up, including installing the Claude Code CLI.
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

`.env`: your settings. Copy this and adjust the values if needed.

```bash
# AI Shuffle settings for Docker Compose.
# Copy this file to .env in the same folder as compose.yaml, then edit it.
# These are the only settings a normal install needs; everything else is set
# in the app.

# --- Version ----------------------------------------------------------------

# The release to run. Pinned on purpose: nothing changes until you change it.
# To update: set the new version here, then run `docker compose up -d`.
AI_SHUFFLE_VERSION=1.0.0

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
# line, so change the password in Settings > Security > Owner password (and clear
# this line if you like). Leave it empty to create the password in your
# browser on first visit instead. At least 12 characters.
OWNER_PASSWORD=

# --- Optional Cloud plan -------------------------------------------------------

# Optional. Your paid Cloud plan's license key (ASH-XXXXX-XXXXX-XXXXX-XXXXX).
# On the first start AI Shuffle links it for this install. After a definitive
# answer it will not retry the same key; Settings > Cloud shows the result.
# You can clear this line after linking, or leave it empty and link in Settings.
# Local use and updates are free and never need a key.
LICENSE_KEY=

# --- Files and permissions ---------------------------------------------------

# The folder on this host where your projects live. Created on first start.
WORKSPACE_DIR=./workspace

# The user and group that own WORKSPACE_DIR. On Linux, `id -u` and `id -g`
# print yours. Leave 1000 on a single-user machine.
PUID=1000
PGID=1000

# --- Advanced: set in compose.yaml, not here ---------------------------------

# Optional: leave them unset for the defaults. A line in this file alone does
# not reach the container: uncomment the matching example under `environment:` in
# compose.yaml instead, then run `docker compose up -d`. The install guide
# explains each one.

# The address of a reverse proxy in another container or on this host (Tailscale
# Serve included), as AI Shuffle sees it. IPs or ranges, comma-separated.
# Never list your whole LAN.
#TRUSTED_PROXIES=172.18.0.1

# The sandbox around agents' Python code: auto (the default), required or off.
# It needs the security_opt lines in compose.yaml; without them, auto runs
# the code unsandboxed and says so once in the log.
#CODE_MODE_OS_SANDBOX=auto

# Address ranges the built-in browser may open. Replaces the localhost default,
# so keep the first two ranges to still allow localhost.
#BROWSER_SSRF_ALLOW=127.0.0.0/8,::1/128,192.168.1.20/32

# false refuses localhost too. No effect when BROWSER_SSRF_ALLOW is set.
#BROWSER_SSRF_ALLOW_LOOPBACK=false

# Ranges the browser refuses even if BROWSER_SSRF_ALLOW lists them.
#BROWSER_SSRF_BLOCK=192.168.1.1/32
```

### 2. Fill in your settings


- **`LICENSE_KEY`** (optional): links a paid Cloud plan on the first start. You can enter it in Settings later instead. Local use and updates never need a key.
- **`OWNER_PASSWORD`** (optional): set the owner password now, or leave it empty and create it in your browser on the first visit.
- **`WORKSPACE_DIR`**: the folder where your projects live. The default is `./workspace` next to `compose.yaml`.
- **`PUID` and `PGID`**: the user and group that should own your project files. On Linux, `id -u` and `id -g` show yours. Keep 1000 on a single-user machine.

### 3. Choose who can reach it

By default, AI Shuffle only answers on the machine itself, at <http://localhost:8000>.

To use it from a laptop or phone on your network, set `BIND_HOST` to **one** address of the machine:

- its home network address, for example `192.168.1.20`;
- or its Tailscale address (`100.x.y.z`), to reach it only over your tailnet.

Do not use `0.0.0.0`, and never forward the port from your router. Keep `COOKIE_SECURE=false` for a plain `http://` address. Set it to `true` only when you reach AI Shuffle through HTTPS. For access away from home, see [Remote access](/docs/getting-started/remote-access).

### 4. Start it

In the folder with the two files, run:

```bash
docker compose up -d
```

The first start downloads the image and sets everything up, which can take a few minutes. `docker compose ps` shows `healthy` when AI Shuffle is ready. `docker compose logs -f` shows progress.

### 5. Open it

Open the address you chose, for example <http://localhost:8000>, and follow [First run](/docs/getting-started/first-run).

## Install with docker run

If you prefer one command, this starts the same container as the compose file. Use the current version number.

```bash
mkdir -p ~/ai-shuffle/workspace

docker run -d \
  --name ai-shuffle \
  --restart unless-stopped \
  --stop-timeout 90 \
  --shm-size 1g \
  --pids-limit 8192 \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  --security-opt apparmor=unconfined \
  -e PUID=1000 \
  -e PGID=1000 \
  -e COOKIE_SECURE=false \
  -p 127.0.0.1:8000:8000 \
  -v ai-shuffle_data:/data \
  -v ~/ai-shuffle/workspace:/workspace \
  -v ai-shuffle_fluid-settings:/home/appuser/.fluid \
  -v ai-shuffle_claude-home:/home/appuser/.claude \
  -v ai-shuffle_local-home:/home/appuser/.local \
  -v ai-shuffle_gh-auth:/home/appuser/.config/gh \
  -v ai-shuffle_browser-profile:/home/appuser/chrome-profile \
  ghcr.io/hobbycoders/ai-shuffle-server:1.0.0
```

- Change `127.0.0.1` in `-p` to one address of the machine to reach it from other devices, as in [step 3](#3-choose-who-can-reach-it).
- Add `-e LICENSE_KEY=...` or `-e OWNER_PASSWORD=...` to set them on the first start.
- Keep `--stop-timeout 90`. AI Shuffle needs up to 90 seconds to shut down cleanly.
- Set `PUID` and `PGID` to the owner of your workspace folder.
- Keep `--restart unless-stopped`. When AI Shuffle stops answering, the container restarts it on its own. If that keeps failing, the container exits so that Docker can start it again. Without a restart policy it stays stopped until you start it.

### Unraid, Portainer and other container managers

If you create the container in a web interface instead of a terminal, set the same options as the `docker run` command above. Most important is the restart policy: many of these tools create containers with no restart policy, and then AI Shuffle stays down after a failure.

- **Unraid:** in **Add Container**, switch to **Advanced View** and put the options without their own field into **Extra Parameters**: `--restart unless-stopped --stop-timeout 90 --shm-size 1g --pids-limit 8192 --security-opt seccomp=unconfined --security-opt systempaths=unconfined --security-opt apparmor=unconfined`. Add the ports, volumes and variables from the command with **Add another Path, Port, Variable, Label or Device**.
- **Portainer:** under **Restart policy**, choose **Unless stopped**.

For an existing hand-created install, change the policy on the **Docker host**, not inside the container: `docker update --restart=unless-stopped <container-name>`. This does not restart it. Verify with `docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' <container-name>`; it must print `unless-stopped`. Save the same policy in Unraid's template or Portainer's configuration so recreating the container preserves it. In-app source updates restart the application, not the Docker container, and cannot change its host restart policy.

## Where your data lives

Everything you keep is in Docker volumes and in your workspace folder, not in the container. You can delete and re-create the container, or update it, without losing anything.

| Where | What it holds |
| --- | --- |
| `data` volume | Your database, settings, chats and saved credentials |
| Workspace folder | Your projects, as ordinary files you can open directly |
| `fluid-settings` volume | App settings, skills and plugins |
| `claude-home` and `local-home` volumes | The Claude Code CLI and its sign-in, if you use it |
| `gh-auth` volume | Your GitHub CLI sign-in |
| `browser-profile` volume | The built-in browser's profile, so sites stay signed in |

With Docker Compose, the volumes are named `ai-shuffle_data`, `ai-shuffle_fluid-settings` and so on. `docker volume ls` lists them.

## Update

With Docker Compose:

1. Read the release notes for the new version in AI Shuffle.
2. Set `AI_SHUFFLE_VERSION` in `.env` to the new version.
3. Run `docker compose up -d`.

With `docker run`: pull the new version, then stop and remove the container and run the same command again with the new version number. Your volumes and workspace stay as they are.

```bash
docker pull ghcr.io/hobbycoders/ai-shuffle-server:NEW_VERSION
docker stop ai-shuffle
docker rm ai-shuffle
```

Many releases can also be installed from inside the app. See [Updates](/docs/getting-started/updates).

## Back up

Stop AI Shuffle first: `docker compose stop` (or `docker stop ai-shuffle`). Never copy the volumes while it runs; a copy of a running database may not start.

Then copy every volume to a file. This writes one file per volume to `~/ai-shuffle-backup`:

```bash
mkdir -p ~/ai-shuffle-backup
for v in data fluid-settings claude-home local-home gh-auth browser-profile; do
  docker run --rm -v ai-shuffle_$v:/from:ro -v ~/ai-shuffle-backup:/to ubuntu \
    tar --numeric-owner -czf /to/$v.tar.gz -C /from .
done
```

Copy your workspace folder as well, with any backup tool you like. Then start AI Shuffle again: `docker compose start` (or `docker start ai-shuffle`).

Always back up the whole `data` volume. It holds your database and the key that decrypts your saved credentials, and neither works without the other. Keep the backup files private.

### Restore

Restore into new, empty volumes, for example on another machine. Use the same `compose.yaml` and `.env` as before, with the same `PUID`, `PGID` and version, and put your workspace folder back where `WORKSPACE_DIR` points.

```bash
docker compose create
for v in data fluid-settings claude-home local-home gh-auth browser-profile; do
  docker run --rm -v ai-shuffle_$v:/to -v ~/ai-shuffle-backup:/from:ro ubuntu \
    tar --numeric-owner -xzpf /from/$v.tar.gz -C /to
done
docker compose start
```

With `docker run`, leave out the two `docker compose` lines: run the loop first (it creates the volumes), then your `docker run` command.

## Optional settings

These are only needed in special cases. Uncomment the matching line under `environment:` in `compose.yaml` (or add `-e NAME=value` to `docker run`), then run `docker compose up -d`. A line in `.env` alone does not reach the container.

- **`BROWSER_SSRF_ALLOW`**: by default the built-in browser can open public websites and the container itself, but not your home network. List the address ranges it may also open, for example one service on your network.
- **`BROWSER_SSRF_ALLOW_LOOPBACK`**: set `false` to stop the built-in browser from opening services inside the container too.
- **`BROWSER_SSRF_BLOCK`**: ranges the browser must never open.

## Behind a reverse proxy (optional)

If a reverse proxy (in another container, or on the host, Tailscale Serve included) sits in front of AI Shuffle, set **`TRUSTED_PROXIES`** to the proxy's address as the container sees it. Then AI Shuffle sees each visitor's real address instead of the proxy's. For a proxy on the host, use the gateway address of the `ai-shuffle_default` network (`docker network inspect ai-shuffle_default` shows it). Never list your whole network. Set `COOKIE_SECURE=true` when you only reach AI Shuffle through HTTPS.

## The code-mode sandbox

On Linux, agents' Python code runs in a sandbox with no network access and no access to your files. The `security_opt` lines in the compose file (and the `--security-opt` flags in `docker run`) turn it on. **Settings → Security → Code sandbox** shows whether it is active.

- To turn it off, remove those lines, or set `CODE_MODE_OS_SANDBOX` to `off`. To refuse to run code without it, set `required`.
- **Ubuntu 24.04 and later** block the sandbox by default. Allow it with `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, and put the same setting in a file under `/etc/sysctl.d/` to keep it after a reboot. Or run without the sandbox.

## Keep agents away from AI Shuffle's own keys (optional)

Agents' shell commands run as the `PUID` user. By default that user can also read AI Shuffle's own database password and the key that protects your saved credentials. Set **`AGENT_ISOLATION=on`** to change that. AI Shuffle then gives those files and its database to a separate service account inside the container (user and group 64000). Agents' shells, background commands, hooks, MCP servers and terminals still run as `PUID`, and opening those files gives them "Permission denied".

**Turn it on**

1. Back up (see [Back up](#back-up)).
2. Under `environment:` in `compose.yaml`, add `AGENT_ISOLATION: "on"` (or add `-e AGENT_ISOLATION=on` to `docker run`).
3. Run `docker compose up -d`. The log says `agent isolation is on`.

AI Shuffle notes who owned each file before changing anything. Your database, keys and saved credentials stay as they are; nothing is created again. The container must start as root, which is the default; with `--user` the setting is ignored and the log says so. If the setting cannot be applied, AI Shuffle undoes the change, starts without it and says why in the log. Your projects keep their owner.

**Turn it off**

Remove the line (or set `AGENT_ISOLATION=off`) and run `docker compose up -d`. On that start AI Shuffle gives every file back to `PUID` exactly as it was.

**Good to know**

- Agents still start only in the workspace, their home folder, the data volume and `/tmp`. To allow another folder, set `AGENT_ISOLATION_EXTRA_DIRS` to absolute paths separated by `:`.
- If user or group 64000 is already used on your host, set `AGENT_ISOLATION_SERVICE_UID` and `AGENT_ISOLATION_SERVICE_GID` to free numbers. They must differ from `PUID` and `PGID`.
- While it is on, the data volume's top folder and those files show owner 64000 on the host. Back up with `docker compose exec -T ai-shuffle bash /app/scripts/backup.sh`, or copy the stopped volumes as root.
- Anything an agent could read before you turned it on may have been copied. If that matters, change the owner password and replace your provider keys afterwards.
- An agent can still stop AI Shuffle's own processes, for example by ending them. It cannot read their secrets.
- It needs this release's image. On an older image the setting has no effect.

## Uninstall

- `docker compose down` (or `docker rm -f ai-shuffle`) removes the container and keeps your data.
- `docker compose down -v` also **deletes every volume**, including your database and saved credentials. Only use it when you mean to start over.
