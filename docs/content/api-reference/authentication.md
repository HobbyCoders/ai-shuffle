<!--
title: Authentication
description: Owner access, Claude CLI, and GitHub endpoints
order: 1
-->

# Authentication API

Endpoints for owner sign-in, local installation setup and development-tool authentication. Claude and GitHub tool credentials are separate from the Shuffle account a worker may be linked to for phone access.

## Local setup and owner access

Requests from the same machine (loopback) on the local listener get owner access without signing in. Other devices on the LAN sign in with the owner password, unless the owner turns on **Trusted LAN**. The authenticated remote listener accepts only existing paired devices. API users keep their scoped integration roles on every network.

Passwordless owner requests that change something (anything but `GET`) must be same-origin: send an `Origin` header that matches the host, as a browser does. Scripts can instead sign in and send the owner token as a Bearer (see [Owner password](#owner-password)).

## Owner password

The owner password is created in the first-run wizard. Sign-in is only available on the local listener. Password checks are rate limited per client address and in total.

### GET /api/v1/owner/status

What the web UI needs to choose between sign-in, first-run setup and the app. No auth required.

**Response:**
```json
{
  "password_set": true,
  "setup_allowed": false,
  "required": false,
  "trusted_lan": false,
  "login_available": true,
  "desktop_handoff": false,
  "min_length": 12
}
```

### POST /api/v1/owner/password

Create the first owner password, or change it. Creating it needs a browser on the local network (or owner access). Changing it needs owner access and `current_password`. Both need a same-origin request. Changing the password ends every owner session, including owner Bearer tokens and the one that made the request. Only a same-origin browser on the local listener gets a new `session` cookie back; a script that used a Bearer token must sign in again.

```bash
curl -X POST http://localhost:8000/api/v1/owner/password \
  -H "Origin: http://localhost:8000" \
  -H "Content-Type: application/json" \
  -d '{"password": "new-password", "current_password": "old-password"}'
```

### POST /api/v1/owner/login

Sign in with the owner password. By default it sets the `session` cookie (same-origin browser requests only). Send `"bearer": true` to get the token in the response instead, for scripts.

```bash
curl -X POST http://localhost:8000/api/v1/owner/login \
  -H "Content-Type: application/json" \
  -d '{"password": "your-owner-password", "bearer": true}'
```

**Response:**
```json
{
  "status": "ok",
  "token": "...",
  "expires_at": "2025-01-15T10:30:00+00:00"
}
```

Send the token as `Authorization: Bearer <token>` on owner-only endpoints. Returns `409` if no owner password is set yet, and `403` on the remote listener.

### PUT /api/v1/owner/trusted-lan

Turn **Trusted LAN** on or off. When it is on, every device on the LAN gets owner access without signing in.

**Auth:** Owner only. A request that signs in with the `session` cookie, or relies on passwordless owner access, must be same-origin. A request that sends an owner token as a Bearer (and no cookie) does not need an `Origin` header.

```json
{
  "enabled": true
}
```

### POST /api/v1/auth/setup

Mark the local installation as configured after choosing its workspace. Requires owner access and a same-origin browser request.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/auth/setup \
  -H "Origin: http://localhost:8000" \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response:**
```json
{
  "status": "ok"
}
```

### POST /api/v1/auth/logout

Sign out: clear the browser's `session` cookie and end the current paired-browser session. Browser requests must be same-origin. Signing out does not end passwordless owner access from loopback or a Trusted LAN.

```bash
curl -X POST http://localhost:8000/api/v1/auth/logout \
  -H "Origin: http://localhost:8000" \
  -b cookies.txt
```

### GET /api/v1/auth/status

Check authentication status. No auth required.

```bash
curl http://localhost:8000/api/v1/auth/status
```

**Example local response (additional status fields are returned):**
```json
{
  "authenticated": true,
  "is_admin": true,
  "setup_required": false,
  "local_access": true,
  "auth_mode": "local",
  "owner_linked": false,
  "credential_storage": { "ready": true, "state": "ready" }
}
```

### Removed login endpoints

`POST /api/v1/auth/login`, `/change-password`, `/account/start`, `/account/poll`, and `/recovery` no longer exist and return 404. Owner sign-in lives under `/api/v1/owner` (see [Owner password](#owner-password)). Browser account sign-in and console recovery codes are gone. Stored credentials use persistent server-managed keys; restoring unavailable keys is a separate operator procedure.

## Claude Code CLI

Run `claude auth login` in the AI Shuffle terminal (or `docker exec -it -u appuser <container> claude auth login`). AI Shuffle never reads your Claude credentials. The stock Claude Code CLI owns its login; these endpoints report what `claude auth status` says and run the CLI's own `claude auth login` and `claude auth logout`.

The old in-app sign-in endpoints (`/api/v1/auth/claude/login`, `/login/poll`, `/complete`, `/validate` and `/api/v1/auth/claude/accounts*`) no longer exist and return 404.

### GET /api/v1/auth/claude/cli

What the CLI reports about its own login. Add `?refresh=true` to ask the CLI again instead of using the answer cached for up to a minute.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/auth/claude/cli?refresh=true \
  -b cookies.txt
```

**Response:**
```json
{
  "installed": true,
  "signed_in": true,
  "auth_method": "claude.ai",
  "auth_method_label": "Claude account",
  "email": "user@example.com",
  "org_name": null,
  "plan": "max",
  "cli_version": "2.1.280",
  "binary_path": "/home/appuser/.local/bin/claude",
  "error": null,
  "login": {
    "surface": "docker",
    "terminal": true,
    "command": "claude auth login",
    "docker_command": "docker exec -it -u appuser <container> claude auth login"
  },
  "relogin_notice": null
}
```

`signed_in` is `null` when the CLI's answer could not be read; `error` then says why. `GET /api/v1/auth/claude/status` returns the same body plus `authenticated`.

### POST /api/v1/auth/claude/cli-login/start

Run `claude auth login` in the in-app terminal (Docker and local runs; not the desktop app or Windows). Poll `GET /api/v1/auth/claude/cli-login?offset=N` for its output and the sign-in link, send the code Anthropic's page shows with `POST /api/v1/auth/claude/cli-login/input` (`{"text": "..."}`), and stop it with `POST /api/v1/auth/claude/cli-login/cancel`.

**Auth:** Owner only

### POST /api/v1/auth/claude/logout

Run `claude auth logout`. The CLI's files are never edited or deleted by AI Shuffle.

**Auth:** Owner only

### POST /api/v1/auth/claude/relogin-notice/dismiss

Hide the one-time notice, shown after upgrading from a version that ran its own Claude sign-in, asking to run `claude auth logout` and then `claude auth login`.

**Auth:** Owner only

## GitHub Integration

### GET /api/v1/auth/github/status

Get GitHub CLI authentication status.

**Auth:** Required

### POST /api/v1/auth/github/login

Login with a GitHub Personal Access Token.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/auth/github/login \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"token": "ghp_your_personal_access_token"}'
```

### POST /api/v1/auth/github/login/web

Start web device flow for GitHub authentication.

**Auth:** Owner only

### POST /api/v1/auth/github/logout

Logout from GitHub.

**Auth:** Owner only

## Onboarding

### POST /api/v1/auth/onboarding/complete

Mark the setup onboarding as complete.

**Auth:** Owner only

### GET /api/v1/auth/diagnostics

Run authentication diagnostic checks.

**Auth:** Owner only
