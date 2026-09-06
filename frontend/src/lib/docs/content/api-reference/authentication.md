<!--
title: Authentication
description: Local access, owner account sign-in, Claude CLI, and GitHub endpoints
order: 1
-->

# Authentication API

Endpoints for local installation setup, optional owner account sign-in, and development-tool authentication. Claude and GitHub tool credentials are separate from the Shuffle account that owns a remote worker.

## Local setup and owner access

The normal local listener grants owner access to direct trusted localhost/LAN requests. First setup does not require a username, password, or Shuffle account. The authenticated remote listener requires an existing paired device or proof of the worker owner's Clerk account. API users retain their scoped integration roles, including when used on the local network.

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

### POST /api/v1/auth/account/start

Start optional remote browser sign-in for an already linked worker. The request must originate from that worker's browser page. It sets a short-lived, HttpOnly browser-proof cookie and returns `request_id`, `authorization_url`, and `expires_at`.

The frontend opens the account service, where Clerk verifies the identity that owns this worker. Completion is bound to the initiating tab using an exact-origin popup message and a one-use completion code. If the browser blocks the popup handoff, paste that code into the original worker tab. Do not put it in a URL or share it.

### POST /api/v1/auth/account/poll

Poll from the same worker browser, retaining its proof cookie. Send `{}` while waiting, then `{"completion_secret":"..."}` after the verified popup message or manual code entry. Without completion proof, polling cannot create a session. On successful one-use authorization, the worker returns `{"status":"authenticated"}` and sets its HttpOnly session cookie. Pending requests return `{"status":"pending"}`; expired or rejected authorization returns 401 or 403.

This flow works with localhost and LAN workers without a redirect back to a caller-selected address. Before first remote use, link the worker through **Devices → Remote access** using the browser or Android app.

### POST /api/v1/auth/logout

Invalidate the current browser session and clear its cookie. Browser requests must be same-origin. Signing out does not disable automatic owner access on a trusted local listener.

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
  "account_sign_in_available": false,
  "credential_storage": { "ready": true, "state": "ready" }
}
```

### POST /api/v1/auth/recovery

If local access is deliberately disabled, generate a one-use code in the worker's local console with `python -m app.core.owner_auth recovery`. Open `/login` through a direct localhost connection, expand **Local recovery**, and enter the code. The endpoint accepts `{"code":"..."}` with the same-origin browser request.

The code expires after ten minutes and grants fifteen minutes of local access. Recovery is unavailable through the public tunnel or LAN. Restoring unavailable credential keys is a separate operator procedure; account sign-in or a local recovery session does not replace those keys.

### Retired worker-password endpoints

`POST /api/v1/auth/login` and `POST /api/v1/auth/change-password` return 410. Worker username/password login and password changes are retired. Stored credentials use persistent server-managed keys. Older account-hub password APIs, where still supported for legacy account recovery, are separate from these worker endpoints.

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
