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
    image: ${AI_SHUFFLE_REGISTRY:-registry.hobbycoders.com}/${AI_SHUFFLE_PULL_TOKEN:?Set AI_SHUFFLE_PULL_TOKEN in .env (see env.example)}/ai-shuffle:${AI_SHUFFLE_VERSION:-0.8.19}
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

    # Optional: lets the code-mode sandbox around agents' Python code start. The
    # trade: Docker's default seccomp and AppArmor filters and its /proc masking
    # are off for the whole container. See "The code-mode sandbox" in the
    # install guide.
    # security_opt:
    #   - seccomp=unconfined
    #   # Lets the sandbox mount its own /proc:
    #   - systempaths=unconfined
    #   # Only on hosts that use AppArmor, such as Ubuntu and Debian:
    #   - apparmor=unconfined

    environment:
      PUID: ${PUID:-1000}
      PGID: ${PGID:-1000}
      # Plain http:// on a LAN address needs this off; keep it on behind HTTPS.
      COOKIE_SECURE: ${COOKIE_SECURE:-false}
      # Seeds the owner password on the first start only (see env.example).
      OWNER_PASSWORD: ${OWNER_PASSWORD:-}
      # Activates your license on the first start (see env.example).
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
AI_SHUFFLE_VERSION=0.8.19

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

# --- License key ---------------------------------------------------------------

# Optional. Your license key (ASH-XXXXX-XXXXX-XXXXX-XXXXX) from your license
# email. On the first start AI Shuffle activates it for this install, then
# ignores this line: the license lives in AI Shuffle from then on, so you can
# clear it. Leave it empty to paste the key in Settings > License instead.
# While the beta runs, no key is needed and this line is not used.
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

## 2. Set your pull token

Paste your pull token after `AI_SHUFFLE_PULL_TOKEN=`. It is part of the image name, so Docker downloads the image without `docker login`, and pulls keep working after a reboot.

The pull token only downloads the image; it is not your license key and cannot sign in to anything. It shows in `docker ps` and in logs, so if you share it by accident, get a new one from your account on hobbycoders.com, put it in `.env` and run `docker compose up -d`. The old token stops working within a minute; the running container is not affected.

Optionally, paste your **license key** (`ASH-XXXXX-XXXXX-XXXXX-XXXXX`) after `LICENSE_KEY=` too. AI Shuffle activates it on the first start and ignores the line after that, so you can clear it once the license shows in **Settings → License**. Leave it empty to paste the key there instead. While the beta runs, no key is needed. See [License](/docs/getting-started/license).

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

## The built-in browser's network access (optional)

Agents can open public websites in the built-in browser. By default it cannot reach:

- cloud metadata addresses such as `169.254.169.254` (this cannot be changed);
- your private network: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10` (which includes Tailscale) and IPv6 `fc00::/7`;
- AI Shuffle's own port and the browser's DevTools port, on any internal address (this cannot be changed either).

`localhost` inside the container is allowed, so an agent can view a dev server it started. A page the agent visits can then reach other services listening inside the container too.

To change this, add a line under `environment:` in `compose.yaml` (a line in `.env` alone does not reach the container), then run `docker compose up -d`:

```yaml
    environment:
      # (keep the lines already there)
      # Let the browser open one LAN service. This list replaces the localhost
      # default, so keep the first two ranges to still allow localhost.
      BROWSER_SSRF_ALLOW: "127.0.0.0/8,::1/128,192.168.1.20/32"
```

- `BROWSER_SSRF_ALLOW`: address ranges the browser may open, separated by commas.
- `BROWSER_SSRF_ALLOW_LOOPBACK: "false"`: refuse `localhost` as well. Use it when people you do not trust can prompt agents on this install. It has no effect when `BROWSER_SSRF_ALLOW` is set.
- `BROWSER_SSRF_BLOCK`: ranges to refuse even if `BROWSER_SSRF_ALLOW` lists them.

## The code-mode sandbox (optional)

Agents on the Fluid harness (every model except the Claude Code CLI, outside chat sessions) run their work as Python code in a separate process, the code-mode kernel. The tools that code calls still run in AI Shuffle with their usual permissions; the sandbox limits the Python code itself.

On Linux, AI Shuffle can run that process inside bubblewrap: no network, none of the host's files except the Python runtime, a private `/tmp` and its own process list. The image includes bubblewrap, but Docker's default security profile does not let the container create the user namespaces it needs. The kernel then runs without the sandbox, and the log says why once: `code-mode OS sandbox unavailable, kernel child runs without it`.

To use the sandbox, allow the container to create user namespaces and to mount its own `/proc`, then run `docker compose up -d`:

```yaml
services:
  ai-shuffle:
    # (keep the lines already there)
    security_opt:
      - seccomp=unconfined
      # Lets the sandbox mount its own /proc:
      - systempaths=unconfined
      # Only on hosts that use AppArmor, such as Ubuntu and Debian:
      - apparmor=unconfined
```

These lines turn off Docker's default seccomp and AppArmor filters for the whole container, so weigh that against what the sandbox adds. `systempaths=unconfined` also removes Docker's masking of the container's `/proc` (it normally hides entries such as `/proc/kcore` and makes `/proc/sys` read-only). The sandbox needs it because the kernel refuses a fresh `/proc` mount while those masks are in place; without it the log line ends with `Can't mount proc on /newroot/proc` and names the option. A custom seccomp profile that allows `unshare`, `clone` with namespace flags and `mount` can replace `seccomp=unconfined`, but not `systempaths=unconfined`. The host must also allow unprivileged user namespaces; Ubuntu 24.04 and later restrict them through AppArmor.

`CODE_MODE_OS_SANDBOX` chooses how the sandbox is used. Set it under `environment:` in `compose.yaml` (a line in `.env` alone does not reach the container):

- `auto` (the default): use the sandbox when its self-test (run when the first code-mode kernel starts) passes, otherwise run without it after the one log line above.
- `required`: never run code without the sandbox. When it is unavailable, every Python cell fails with the reason.
- `off`: never use it.

While the kernel runs without the sandbox, Ask mode asks before each Python cell runs; with the sandbox active, cells run without a prompt.

The kernel's own checks inside the process stay on in every mode, as a second layer; the sandbox is the boundary that holds.

## Behind a reverse proxy (optional)

AI Shuffle takes a visitor's address from the `X-Forwarded-For` or `X-Real-IP` header only when the connection comes from loopback or from an address listed in `TRUSTED_PROXIES`. The security audit log records that address, and the attempt limits for pairing a device and for joining a worker network count by it. The owner sign-in limit always counts by the direct connection, so this setting does not change it.

A reverse proxy in another container, or one on the host (Tailscale Serve included) that reaches AI Shuffle through the published port, connects from a Docker network address instead of loopback. Without `TRUSTED_PROXIES`, every visitor then counts as the proxy: the audit log shows only the proxy's address, and one visitor's failed pairing or network-join attempts can block everyone's. Add the proxy's address as the container sees it under `environment:` in `compose.yaml`, then run `docker compose up -d`:

```yaml
    environment:
      # (keep the lines already there)
      # The reverse proxy's address, as AI Shuffle sees it.
      TRUSTED_PROXIES: "172.18.0.1"
```

It takes IP addresses or ranges (`172.18.0.0/16`), separated by commas. For a proxy on the host, use the gateway of the `ai-shuffle_default` network (`docker network inspect ai-shuffle_default` shows it). Never list your whole LAN: any host in a trusted range can choose the address AI Shuffle records for it. On Docker Desktop and rootless Docker, every connection to the published port arrives from that gateway address, so when `BIND_HOST` is not `127.0.0.1`, trusting the gateway lets any LAN device choose its own address again.

## Back up

Stop the container (`docker compose stop`), then back up the Docker volumes whose names start with `ai-shuffle_` and your `WORKSPACE_DIR` folder. The `data` volume holds the database and the key that encrypts your saved credentials; always keep them together. Start again with `docker compose start`.

## Update

1. Read the release notes for the new version (see [Changelog](/docs/getting-started/changelog)).
2. Set `AI_SHUFFLE_VERSION` in `.env` to the new version.
3. Run `docker compose up -d`.

Your data stays in the volumes and in `WORKSPACE_DIR`. See [Updates](/docs/getting-started/updates) for in-app updates and what the update check sends.

## Uninstall

`docker compose down` removes the container and keeps your data.

`docker compose down -v` also **deletes every volume**, including the database and your saved credentials. Use it only when you mean to start over.
