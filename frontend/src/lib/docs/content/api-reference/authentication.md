<!--
title: Authentication
description: Local access, Claude CLI, and GitHub endpoints
order: 1
-->

# Authentication API

Endpoints for local installation setup and development-tool authentication. Shuffle is not hosted on the internet and never asks its owner to sign in. Claude and GitHub tool credentials are separate from the Shuffle account a worker may be linked to for phone access.

## Local setup and owner access

The normal local listener grants owner access to direct trusted localhost/LAN requests. There is no login page: first setup does not require a username, password, or Shuffle account. The authenticated remote listener accepts only existing paired devices. API users retain their scoped integration roles, including when used on the local network.

### POST /api/v1/auth/setup

Mark the local installation as configured after choosing its workspace. Requires owner access and a same-origin browser request. A direct localhost command can also use the enabled local listener.

```bash
curl -X POST http://localhost:8000/api/v1/auth/setup \
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

Invalidate the current paired browser session and clear its cookie. Browser requests must be same-origin. Signing out does not disable automatic owner access on a trusted local listener.

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

`POST /api/v1/auth/login`, `/change-password`, `/account/start`, `/account/poll`, and `/recovery` no longer exist and return 404. Worker passwords, browser account sign-in, and console recovery codes are gone: a browser is the owner on the trusted local listener or it is not authenticated at all. Stored credentials use persistent server-managed keys; restoring unavailable keys is a separate operator procedure.

## Claude Code CLI

Run `claude auth login` in the AI Shuffle terminal (or `docker exec -it -u appuser <container> claude auth login`). AI Shuffle never reads your Claude credentials. The stock Claude Code CLI owns its login; these endpoints report what `claude auth status` says and run the CLI's own `claude auth login` and `claude auth logout`.

The old in-app sign-in endpoints (`/api/v1/auth/claude/login`, `/login/poll`, `/complete`, `/validate` and `/api/v1/auth/claude/accounts*`) no longer exist and return 404.

### GET /api/v1/auth/claude/cli

What the CLI reports about its own login. Add `?refresh=true` to ask the CLI again instead of using the answer cached for up to a minute.

**Auth:** Any signed-in caller

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

**Auth:** Admin only

### POST /api/v1/auth/claude/logout

Run `claude auth logout`. The CLI's files are never edited or deleted by AI Shuffle.

**Auth:** Admin only

### POST /api/v1/auth/claude/relogin-notice/dismiss

Hide the one-time notice, shown after upgrading from a version that ran its own Claude sign-in, asking to run `claude auth logout` and then `claude auth login`.

**Auth:** Admin only

## GitHub Integration

### GET /api/v1/auth/github/status

Get GitHub CLI authentication status.

**Auth:** Admin only

### POST /api/v1/auth/github/login

Login with a GitHub Personal Access Token.

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/auth/github/login \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"token": "ghp_your_personal_access_token"}'
```

### POST /api/v1/auth/github/login/web

Start web device flow for GitHub authentication.

**Auth:** Admin only

### POST /api/v1/auth/github/logout

Logout from GitHub.

**Auth:** Admin only

## Onboarding

### POST /api/v1/auth/onboarding/complete

Mark the setup onboarding as complete.

**Auth:** Admin only

### GET /api/v1/auth/diagnostics

Run authentication diagnostic checks.

**Auth:** Admin only
