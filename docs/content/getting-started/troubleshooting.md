<!--
title: Troubleshooting
description: Fixes for install, access, sign-in, license, update and runtime problems
order: 12
-->

# Troubleshooting

Most problems show up in the container log. Read it first:

- Docker Compose: `docker compose logs --tail 200 ai-shuffle` (add `-f` to follow it).
- Unraid: click the container icon, then **Logs**.

In the commands below, the container is called `ai-shuffle` with Docker Compose and `AI-Shuffle` on Unraid.

## Installing and pulling the image

**`Set AI_SHUFFLE_PULL_TOKEN in .env`**: the `.env` file is missing, is not next to `compose.yaml`, or has no pull token. Copy the settings from [Install with Docker Compose](/docs/getting-started/install-docker-compose) into `.env` and paste your token.

**`denied: This pull token is not valid`**: the token is mistyped, was replaced by a newer one, or its license was revoked. Copy it again from your license email or your account page on hobbycoders.com. A token you rotated stops working within a minute.

**`denied: Your AI Shuffle license has ended`**: after a license ends, the pull token still downloads exact versions (`0.9.1`), but not `latest` or short versions (`0.9`). Use an exact version, or renew.

**`manifest unknown`**: only release versions are published; check the version number. Development tags are never available.

**A temporary error or `503` from `registry.hobbycoders.com`**: the license service could not be reached. Try again in a few minutes. Containers already running are not affected.

**`exec format error` or `no matching manifest for linux/arm64`**: the host is ARM. AI Shuffle runs on 64-bit Intel or AMD (amd64) only for now.

## The container does not become healthy

- **First start:** installing the Claude Code CLI takes a few minutes. Wait for the log to settle.
- **Check readiness from inside:** `docker exec ai-shuffle curl -fsS http://localhost:8000/health/ready`. An answer means the app is up and the problem is the address or network (next section).
- **It restarts in a loop:** look for the first error in the log. `Killed`, `out of memory` or a sudden stop usually means the host ran out of memory; add swap, run fewer agents at once, or give the host more memory.
- **`Permission denied` on `/workspace` or `/data`:** set `PUID` and `PGID` to the owner of those folders (Unraid: 99 and 100).

## I cannot open it from another device

- **Docker Compose:** by default AI Shuffle listens on `127.0.0.1` only. Set `BIND_HOST` in `.env` to the host's LAN or Tailscale address and run `docker compose up -d`.
- **Unraid:** **WebUI address** must be the server's address with the port, for example `192.168.1.20:8000`.
- Check the host firewall allows port 8000 from your network.
- **The page opens but you cannot sign in or stay signed in:** set `COOKIE_SECURE=false` (Unraid: **Secure cookies** `false`) when you use plain `http://`.
- Do not "fix" access by binding to `0.0.0.0` or forwarding the port from your router. See [Remote access](/docs/getting-started/remote-access).

## Owner password

**Forgotten password:** set a new one from a terminal on the server. This needs access to the server itself, which is what keeps it safe:

```bash
# Docker Compose
docker exec -it -u appuser ai-shuffle python -m app.owner_password set

# Unraid (>_ Terminal at the top of the Unraid web interface)
docker exec -it -u appuser AI-Shuffle python -m app.owner_password set
```

It asks for the new password twice and signs every browser out. `python -m app.owner_password reset` removes the password instead, so the next browser that opens AI Shuffle creates a new one; do that only when you can open it yourself straight away. Paired phones and devices linked through your account keep working either way.

Changing **Owner password** in the Unraid template (or `OWNER_PASSWORD` in `.env`) does **not** reset it: that setting is only read on the very first start.

- **A browser keeps asking for the password:** its sign-in expired (after 30 days) or the password was changed, which signs every browser out. Sign in again.
- **"Too many wrong passwords":** after five wrong passwords from one device, sign-in pauses for up to 15 minutes. Wait, then try again.
- **A phone or linked device stopped working:** it does not use the owner password; check **Settings → Devices** for the device.
- **Desktop app:** the desktop app never asks for a password. If a browser you opened some other way shows "Desktop listener token required", use **Open in Browser** from the app's menu instead.

## Agents do not start

The message in the app says which of these it is.

**No credential is set up.** AI Shuffle never falls back to another account. Sign in to the Claude Code CLI or add an API key in **Settings → Providers** (the setup wizard does the same; see [First run](/docs/getting-started/first-run)).

**API key mode, but no key is stored.** The mode you chose has no credential, so AI Shuffle waits instead of using the CLI's sign-in. Add your key in **Settings → Providers**, or switch **Claude Code mode** to subscription.

**The Claude Code CLI is not signed in** (subscription mode):

- Sign in again from the in-app terminal (**Settings → Providers → Claude subscription → Sign in**), or from the host: `docker exec -it -u appuser ai-shuffle claude auth login`.
- Check whose account it uses: `docker exec -u appuser ai-shuffle claude auth status`.
- The login is kept in the Claude Code folder (`/home/appuser/.claude`). If that volume or appdata folder was removed or not mapped, the login is lost on every update; check the mapping.

**The Claude Code CLI is missing.** The installer runs on first start and needs `claude.ai` and Anthropic's download servers. Check the host can reach them, then restart the container. The CLI is kept in its own folder (`/home/appuser/.local`); make sure it is mapped.

**Your plan's usage limit was reached** (subscription mode): agents pause until the limit resets, and nothing is retried before then. Wait, or switch to API key mode for heavy or unattended work. To have the chat continue by itself after the reset, turn on **Settings → General → Usage limits → Resume when the limit resets**. See [Modes and costs](/docs/getting-started/modes-and-costs).

**An automation, wake-up, connector or background agent waits for confirmation** (subscription mode): the loop guard held it because unattended turns reached their hourly budget. Confirm or dismiss it under **Settings → Providers → Claude subscription → Policy**, raise the budget there, or switch to API key mode for unattended work. See [Modes and costs](/docs/getting-started/modes-and-costs).

**A background agent stopped after an hour** (subscription mode): an automation, wake-up or connector started it, and the loop guard's default time limit ended it. Change the limit (**Unattended worker limit**) under **Settings → Providers → Claude subscription → Policy**, or use API key mode for long unattended work. Agents started from your own chats have no such limit.

**The API key is refused:** the key was deleted or the Console account has no credit or reached its spend limit. Check the Anthropic Console, then paste a new key in Settings.

**The license banner says new agent runs are paused:** see [License](/docs/getting-started/license). Your data is not affected.

## Tool calls are denied

**"Permission request timed out":** in Ask mode a tool prompt that nobody answers within 5 minutes is denied, and the agent carries on without it. Answer prompts sooner, or pick another permission mode in the composer. See [Permission modes](/docs/features/profiles#permission-modes).

**"Permission denied: this session has no one to approve tool calls":** one-shot and streaming API queries, `POST /api/v1/conversation/start` and automation **Send prompt** steps cannot show a prompt, so on Claude models Ask mode denies every call that would ask. Run them with a permission mode that allows the tools they need.

## Connectors

**A chat connector refuses to start:** Telegram, Discord, WhatsApp and the other chat connectors need a **sender allowlist**, the accounts allowed to talk to your agent. Add your own account ID to the allowlist in the connector's settings, then start it again. Tool approvals for connector turns appear in the app, not in the chat.

## Computer use is unavailable

**Settings > Security > Computer use** is greyed out with the reason next to it; the help button says what to do. In Docker (including Unraid) it is always unavailable: a container has no desktop to control. It needs a native install on a computer with a desktop. On Linux that must be an X11 session: in a Wayland session, log out and choose an X11 session such as "GNOME on Xorg" or "Plasma (X11)". If it names missing packages or libraries, install them and the setting becomes available within a minute.

## The built-in browser crashes

Pages crash or the browser closes when the container has too little shared memory. The template and compose file set `--shm-size=1g` / `shm_size: 1gb`; if you created the container another way, add it.

## Updates

**`This source update needs a newer container release`**: this release changes the container itself. Change the version in `.env` (Compose) or **Repository** (Unraid) and recreate the container. See [Updates](/docs/getting-started/updates).

**The container stops at `The installed application update … was built for a different container image`**: the data folder or volume still holds an in-app update that is newer than the image you changed to, and the two need different container builds. The message names both. Change the version to a release at least as new as the installed update and recreate the container. To run this image's code over the existing data anyway, stop the container, delete `application-updates/state.json` from the data folder or volume (delete the whole `application-updates` folder to reclaim the space of stored builds), and start it again.

**No update is offered:** check **Settings → System → Updates → Allow updates**. The worker checks about 15 seconds after it starts and once a day, so restarting the container runs a check. Unraid's "update ready" notice only covers the tag you already run; a new release always means changing the version.

## Stopping and the database

- **The container takes a while to stop:** agents stop first, then the database writes a clean checkpoint, which can take up to 90 seconds. Do not kill it. The compose file and Unraid template allow 90 seconds; if you run it another way, use `--stop-timeout 90`.
- **The database does not start after a crash or a full disk:** stop the container, copy the whole data folder or volume somewhere safe before changing anything, and email support. Restore from your last backup only with the data folder and its encryption key together.

## Sending logs to support

**From the app:** when an error notification appears, press **Send report to support**. The dialog shows the exact text that will be sent, with recognised secrets replaced by `***`, and nothing leaves the worker until you press **Send**. It also checks whether HobbyCoders already knows about the error or has fixed it. **Add AI diagnosis** is optional: it runs one request on a model you pick and counts against your own AI usage or subscription. Sending needs the worker signed in to a Shuffle account (**Settings → Devices → Shuffle account**).

When the app does not start or you cannot open it, send the container log by email instead:

```bash
docker compose logs --tail 500 ai-shuffle > ai-shuffle.log
```

On Unraid, use `docker logs --tail 500 AI-Shuffle > /mnt/user/appdata/ai-shuffle/support.log` in the terminal. Read the file before sending it and remove anything private. Email it to support@hobbycoders.com with your AI Shuffle version (shown in Settings) and what you were doing. Never send passwords, API keys, license keys, pull tokens or sign-in codes.
