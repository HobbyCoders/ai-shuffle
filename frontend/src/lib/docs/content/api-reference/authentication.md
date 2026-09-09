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

## Claude CLI Integration

### GET /api/v1/auth/claude/status

Get Claude CLI authentication status.

**Auth:** Admin only

```bash
curl http://localhost:8000/api/v1/auth/claude/status \
  -b cookies.txt
```

**Response:**
```json
{
  "authenticated": true,
  "account": "user@example.com"
}
```

### POST /api/v1/auth/claude/login

Start OAuth login flow for Claude.

**Auth:** Admin only

### GET /api/v1/auth/claude/login/poll

Poll for OAuth completion.

**Auth:** Admin only

### POST /api/v1/auth/claude/complete

Complete OAuth with authorization code.

**Auth:** Admin only

### POST /api/v1/auth/claude/logout

Logout from Claude CLI.

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
