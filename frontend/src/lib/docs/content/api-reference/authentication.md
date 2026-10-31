<!--
title: Authentication
description: Owner access, Claude CLI, and GitHub endpoints
order: 1
-->

# Authentication API

Endpoints for owner sign-in, local installation setup and development-tool authentication. Claude and GitHub tool credentials are separate from the Shuffle account a worker may be linked to for phone access.

## Local setup and owner access

On the desktop app and a native install, requests from the same machine (loopback) on the local listener get owner access without signing in. A worker started by the desktop app also requires its per-launch listener token, which only its own window sends. Other devices on the LAN sign in with the owner password, unless the owner turns on **Trusted LAN**. The authenticated remote listener accepts only existing paired devices. API users keep their scoped integration roles on every network.

The Docker image never gives passwordless owner access to its own machine. Inside the container, every process is AI Shuffle itself or something it started for an agent (shell commands, background commands, hooks, MCP servers), so requests from loopback (`127.0.0.1`, `::1`) or from the container's own address get no owner access, whatever `Host`, `Origin` or `X-Forwarded-For` they send. Sign in with the owner password, or send an owner token (see [Calling a Docker worker from scripts](#calling-a-docker-worker-from-scripts)). **Trusted LAN** never covers the worker's own addresses either, on any install. On a desktop or native install with Trusted LAN on, that means two cases now need a sign-in: opening AI Shuffle on the same machine by its LAN address or host name (on Debian and Ubuntu the host name often resolves to `127.0.1.1`) instead of `localhost`, and a container run with rootless Podman's default `pasta` networking, where requests can arrive with the container's own address. Use `http://localhost:8000` on the machine itself, or sign in.

Passwordless owner requests that change something (anything but `GET`) must be same-origin: send an `Origin` header that matches the host, as a browser does. Scripts can instead sign in and send the owner token as a Bearer (see [Owner password](#owner-password)).

### Calling a Docker worker from scripts

What changes depends on how the script reaches the container:

- **From the host through the published port** (for example `curl http://localhost:8000` with the default compose file): nothing changes. Docker forwards those requests from its bridge network, so they never counted as the container's own machine and already needed a sign-in.
- **From inside the container** (`docker exec <container> curl http://localhost:8000/...`), or **with host networking** (`network_mode: host`): these used to get owner access without signing in. They now get `401` or `403` on owner endpoints, and `/api/v1/auth/status` reports `"is_admin": false`.

Sign in once and send the token instead:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/owner/login \
  -H "Content-Type: application/json" \
  -d '{"password": "your-owner-password", "bearer": true}' | jq -r .token)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/sessions
```

An owner token is the owner's authority: do not put it where an agent can read it, such as a project folder or an environment variable the container passes on. For an integration that only needs some projects or agents, create an [API user](/docs/api-reference/api-users) instead. `/health` and `/health/ready` need no sign-in, so the container's health check is unaffected.

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

Sign out: clear the browser's `session` cookie and end the current paired-browser session. Browser requests must be same-origin. Signing out does not end passwordless owner access from loopback (desktop and native installs) or a Trusted LAN.

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

**Example local response on a desktop or native install (additional status fields are returned):**
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

Run `claude auth login` in the AI Shuffle terminal (or `docker exec -it -u appuser <container> claude auth login`). AI Shuffle never stores or refreshes your Claude credentials; it reads the CLI's current sign-in only when you check or use a Claude usage reset. The stock Claude Code CLI owns its login; these endpoints report what `claude auth status` says and run the CLI's own `claude auth login` and `claude auth logout`.

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
  "binary_path": "/usr/local/bin/claude",
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

Run `claude auth login` in the in-app terminal (Docker and local runs; not the desktop app or Windows). Poll `GET /api/v1/auth/claude/cli-login?offset=N` for its output and the sign-in link, send the code Anthropic's page shows with `POST /api/v1/auth/claude/cli-login/input` (`{"text": "..."}`), and stop it with `POST /api/v1/auth/claude/cli-login/cancel`. While an add-another subscription sign-in is running, starting or cancelling this one returns `409`: stop that sign-in with `POST /api/v1/settings/claude-auth/accounts/subscription/cancel-login`. Reading and sending input work for it.

These endpoints, `GET /claude/cli` and `POST /claude/logout` act on the active saved subscription sign-in when one is selected (see [Saved Claude accounts](/docs/api-reference/miscellaneous#saved-claude-accounts-apiv1settingsclaude-authaccounts)); otherwise on Claude Code's own folder.

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

### POST /api/v1/auth/github/login/web/cancel

Stop a web sign-in started with `POST /api/v1/auth/github/login/web`. No body. Returns `{"success": true}`, also when no sign-in was running.

**Auth:** Owner only

### POST /api/v1/auth/github/logout

Logout from GitHub.

**Auth:** Owner only

### GET /api/v1/github/repos

List the repositories the signed-in GitHub account can clone (owned, collaborator and organization repositories, most recently pushed first). The project clone dialog uses it.

Query parameters: `q` (matches name, owner or description), `page` (default 1), `per_page` (default 30, max 100) and `refresh=true` to refetch instead of using the list cached for two minutes. Up to 1,000 repositories are fetched; `truncated: true` means older ones were left out.

Returns `connected`, `items` (each with `full_name`, `name`, `owner`, `private`, `fork`, `archived`, `description`, `clone_url`, `html_url`, `default_branch`, `updated_at`), `total`, `page`, `per_page`, `has_more` and `truncated`. When GitHub is not signed in on this server it returns `200` with `connected: false`; when GitHub cannot answer it returns `502`. The token is never part of the response.

**Auth:** Owner only

## Onboarding

### POST /api/v1/auth/onboarding/complete

Mark the setup onboarding as complete.

**Auth:** Owner only

### GET /api/v1/auth/diagnostics

Run authentication diagnostic checks.

**Auth:** Owner only

## Workspace folder

The folder where new projects are created. Setup asks for it on first run.

### GET /api/v1/workspace/config

**Auth:** Required

```json
{
  "workspace_path": "/home/me/ai-shuffle",
  "is_configured": true,
  "is_local_mode": true,
  "default_path": "/home/me/ai-shuffle",
  "exists": true
}
```

`is_configured` is `false` until a folder has been saved for this worker; `workspace_path` then shows the folder in use.

### POST /api/v1/workspace/config

Save the workspace folder. It is created if missing and must be writable.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/workspace/config \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"workspace_path": "~/ai-shuffle"}'
```

Returns the same fields as `GET`. `~` is expanded and the path made absolute. Returns `400` for an empty path or a folder that cannot be written.

### POST /api/v1/workspace/validate

Check a folder without saving or creating anything. Same body as `POST /api/v1/workspace/config`.

**Auth:** Owner only

Returns `{"valid": true, "error": null, "path": "/home/me/ai-shuffle", "exists": false, "writable": true}`. For a folder that does not exist yet, `writable` says whether its parent is writable.

### GET /api/v1/filesystem/browse

List the folders and files under a path, for picking a folder.

**Auth:** Owner only

| Query | Type | Required | Description |
|-------|------|----------|-------------|
| `path` | string | No | Folder to list. Omit it to get the starting points |

```bash
curl "http://localhost:8000/api/v1/filesystem/browse?path=/home/me" \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

Returns `path`, `parent` (`null` at the top), `is_local_mode` and `entries`, each with `name`, `path`, `is_dir` and `has_children` (whether a folder has subfolders). Hidden entries are left out. Without `path`, the entries are the home folder and `/` (drive letters on Windows) when AI Shuffle runs directly on your computer, or the workspace folder in Docker. In Docker, paths outside the workspace folder return `400`. A missing path returns `404`.
