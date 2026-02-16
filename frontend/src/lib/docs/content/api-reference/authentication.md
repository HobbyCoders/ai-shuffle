<!--
title: Authentication
description: Auth endpoints for admin login, 2FA, Claude CLI, and GitHub
order: 1
-->

# Authentication API

Endpoints for admin authentication, two-factor authentication, Claude CLI integration, and GitHub authentication.

## Admin Setup & Login

### POST /api/v1/auth/setup

First-launch admin account creation. Only works when no admin exists.

```bash
curl -X POST http://localhost:8000/api/v1/auth/setup \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "your_secure_password"
  }'
```

**Response:**
```json
{
  "token": "session_token_here",
  "requires_2fa": false,
  "user": { "username": "admin" }
}
```

### POST /api/v1/auth/login

Authenticate with admin credentials.

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "your_password"
  }'
```

**Response:**
```json
{
  "token": "session_token_here",
  "requires_2fa": true
}
```

If `requires_2fa` is true, you must call `/auth/verify-2fa` next.

### POST /api/v1/auth/verify-2fa

Complete login with TOTP code.

```bash
curl -X POST http://localhost:8000/api/v1/auth/verify-2fa \
  -H "Content-Type: application/json" \
  -d '{
    "token": "session_token_from_login",
    "code": "123456"
  }'
```

### POST /api/v1/auth/logout

Invalidate the current session.

```bash
curl -X POST http://localhost:8000/api/v1/auth/logout \
  -b cookies.txt
```

### GET /api/v1/auth/status

Check authentication status. No auth required.

```bash
curl http://localhost:8000/api/v1/auth/status
```

**Response:**
```json
{
  "authenticated": true,
  "is_admin": true,
  "setup_complete": true,
  "has_2fa": false
}
```

### POST /api/v1/auth/change-password

Change admin password. Re-encrypts all stored secrets.

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/auth/change-password \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{
    "current_password": "old_password",
    "new_password": "new_secure_password"
  }'
```

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
