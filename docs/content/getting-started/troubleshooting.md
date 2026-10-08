<!--
title: Troubleshooting
description: Fixes for install, access, sign-in, license, update and everyday problems
order: 12
-->

# Troubleshooting

With Docker, most problems show up in the container log. Read it first:

- Docker Compose: `docker compose logs --tail 200 ai-shuffle` (add `-f` to follow it).
- docker run: `docker logs --tail 200 ai-shuffle`.

## Installing and pulling the image

**`Set AI_SHUFFLE_PULL_TOKEN in .env`**: the `.env` file is missing, is not next to `compose.yaml`, or has no pull token. See [Install with Docker](/docs/getting-started/install-docker-compose).

**`denied: This pull token is not valid`**: the token is mistyped or was replaced by a newer one. Copy it again from your license email or your account page on hobbycoders.com.

**`denied: Your AI Shuffle license has ended`**: after a license ends, you can still download exact versions like `0.9.1`, but not `latest`. Use an exact version, or renew.

**`manifest unknown`**: that version does not exist. Check the version number.

**A temporary error or `503` from `registry.hobbycoders.com`**: try again in a few minutes. Running containers are not affected.

**`exec format error` or `no matching manifest for linux/arm64`**: the machine is ARM. AI Shuffle needs a 64-bit Intel or AMD machine for now.

## The container does not become healthy

- **First start:** setting up takes a few minutes. Wait for the log to settle.
- **Check from inside:** `docker exec ai-shuffle curl -fsS http://localhost:8000/health/ready`. If that answers, AI Shuffle is running and the problem is the address or network (next section).
- **It keeps restarting:** look for the first error in the log. `Killed` or `out of memory` means the machine ran out of memory. Add swap, run fewer agents at once, or add memory.
- **`Permission denied` on `/workspace` or `/data`:** set `PUID` and `PGID` to the owner of your workspace folder.

## I cannot open it from another device

- By default AI Shuffle only answers on the machine itself. Set `BIND_HOST` in `.env` to the machine's home network or Tailscale address and run `docker compose up -d`.
- Check that the machine's firewall allows port 8000 from your network.
- **The page opens but you cannot sign in or stay signed in:** set `COOKIE_SECURE=false` when you use plain `http://`.
- Do not "fix" access with `0.0.0.0` or by forwarding the port from your router. See [Remote access](/docs/getting-started/remote-access).

## Owner password

**Forgotten password:** set a new one from a terminal on the machine running AI Shuffle:

```bash
docker exec -it -u appuser ai-shuffle python -m app.owner_password set
```

It asks for the new password twice and signs every browser out. `python -m app.owner_password reset` removes the password instead, so the next browser that opens AI Shuffle creates a new one. Only do that when you can open it yourself straight away. Paired phones keep working either way.

Changing `OWNER_PASSWORD` in `.env` does **not** reset the password. It is only read on the very first start.

- **A browser keeps asking for the password:** its sign-in expired after 30 days, or the password was changed. Sign in again.
- **"Too many wrong passwords":** after five wrong tries, sign-in pauses for up to 15 minutes.
- **A phone stopped working:** phones do not use the owner password. Check **Settings → Devices**.
- **Desktop app:** it never asks for a password. If a browser shows "Desktop listener token required", use **Open in Browser** from the app's menu.

## Agents do not start

The message in the app says which of these it is.

- **No AI provider is connected.** Connect one in **Settings → Providers**.
- **A subscription sign-in stopped working.** Sign in again in **Settings → Providers**.
- **Your plan's usage limit was reached.** Agents pause until it resets. To continue by itself after the reset, turn on **Settings → General → Usage limits → Resume when the limit resets**. See [Modes and costs](/docs/getting-started/modes-and-costs).
- **The API key is refused.** The key was deleted, or the account is out of credit or at its spend limit. Check your provider's console, then paste a new key.
- **The license banner says new agent runs are paused.** See [License](/docs/getting-started/license). Your data is not affected.

### Claude

- **API key mode, but no key is stored.** Add your Anthropic key in **Settings → Providers → Claude**, or switch to subscription mode.
- **The Claude Code CLI is not signed in.** Sign in again from **Settings → Providers → Claude subscription → Sign in**, or run `docker exec -it -u appuser ai-shuffle claude auth login`. `claude auth status` shows which account is signed in.
- **The Claude Code CLI is missing.** Install it in **Settings → Providers → Claude**.
- **An automation or background agent waits for confirmation.** The loop guard held it. Confirm it under **Settings → Providers → Claude subscription → Policy**, raise the budget there, or use an API key for unattended work.
- **A background agent stopped after an hour.** The loop guard's time limit ended it. Change **Unattended worker limit** on the same page, or use an API key.

## Tool calls are denied

**"Permission request timed out":** in Ask mode, a tool prompt nobody answers within 5 minutes is denied. Answer sooner, or pick another permission mode in the composer. See [Permission modes](/docs/features/profiles#permission-modes).

**"Permission denied: this session has no one to approve tool calls":** API queries and automation **Send prompt** steps cannot show a prompt. Run them with a permission mode that allows the tools they need.

## Connectors

**A chat connector refuses to start:** chat connectors need a **sender allowlist**, the accounts allowed to talk to your agent. Add your own account ID in the connector's settings, then start it again.

## Computer use is unavailable

**Settings → Security → Computer use** is greyed out with the reason next to it. With Docker it is never available, because a container has no screen to control. It needs the desktop app. On Linux, it needs an X11 session rather than Wayland.

## The built-in browser crashes

The container has too little shared memory. Keep `shm_size: 1gb` in the compose file, or `--shm-size 1g` with `docker run`.

## Updates

**`This source update needs a newer container release`**: update the Docker image instead. See [Updates](/docs/getting-started/updates).

**The container stops with `The installed application update … was built for a different container image`**: change the image version to a release at least as new as the in-app update you installed, then re-create the container.

**No update is offered:** check **Settings → System → Updates → Allow updates**. Restarting AI Shuffle runs a new check.

## Stopping and the database

- **The container takes a while to stop:** AI Shuffle needs up to 90 seconds to shut down cleanly. Do not kill it. With `docker run`, use `--stop-timeout 90`.
- **The database does not start after a crash or a full disk:** stop the container, copy the whole `data` volume somewhere safe before changing anything, and email support.

## Sending logs to support

**From the app:** open **Settings → System → Help → Report a problem**, describe what went wrong and press **Continue**. Error notifications also have **Send report to support**. The dialog shows exactly what will be sent, with secrets hidden, and nothing is sent until you press **Send**.

When the app does not start, save the container log and email it instead:

```bash
docker logs --tail 500 ai-shuffle > ai-shuffle.log
```

Read the file and remove anything private first. Email it to support@hobbycoders.com with your AI Shuffle version and what you were doing. Never send passwords, API keys, license keys, pull tokens or sign-in codes.
