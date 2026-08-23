<!--
title: Installation
description: How to install and run AI Shuffle locally or on a server
order: 2
-->

# Installation

## Prerequisites

- **Python 3.11+** — Backend runtime
- **Node.js 18+** — Frontend build tooling
- **PostgreSQL** — Application database (required). The provided `docker-compose.yml` runs one for you; for a manual install you'll need a reachable PostgreSQL instance.
- **GitHub CLI** — [Download](https://cli.github.com/) — Used for PR reviews, issue management, etc.
- **Git** — For project management and worktree features

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/your-org/ai-shuffle.git
cd ai-shuffle
```

### 2. Install Backend Dependencies

```bash
pip install -r requirements.txt
# Optional: computer-use (host desktop control), native runs only
pip install -r requirements-native.txt
```

### 3. Install Frontend Dependencies

```bash
cd frontend
npm install
cd ..
```

### 4. Build the Frontend

Build the frontend and copy the output into `app/static`, which is where the
backend serves it from:

```bash
cd frontend
npm run build
cd ..
rm -rf app/static && cp -r frontend/build app/static
```

> **Tip:** `python run_local.py --setup` performs the dependency install, build,
> and copy steps for you in one command.

### 5. Start the Server

```bash
python -m app.main
```

The application starts on `http://localhost:8000` by default.

## First Launch

On first launch, you'll be directed to the setup page where you:

1. **Create an admin account** — Set a username and password
2. **Authenticate Claude** — Connect your Anthropic account via OAuth
3. **Configure workspace** — Set the default working directory for projects

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8000` | Server port |
| `CORS_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000` | Comma-separated allowed CORS origins (use `*` for development only) |
| `POSTGRES_PASSWORD` | _(required)_ | PostgreSQL password. No default — the server refuses to start without it. Collected by `python run_local.py --setup` on local installs. |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_USER` / `POSTGRES_DB` | `localhost`¹ / `5432` / `ai_shuffle` / `ai_shuffle` | Remaining PostgreSQL connection pieces — the app assembles the connection URL itself. ¹`postgres` (the compose service) in Docker. |
| `DATABASE_URL` | _(legacy)_ | Full connection string from installs that predate the `POSTGRES_*` split — auto-imported into the `POSTGRES_*` values at startup, so a URL-only `.env` keeps working without re-running setup; takes precedence when explicit pieces are also set. |

### Running in Production

For production deployments, consider:

- Running behind a reverse proxy (nginx, Caddy) for HTTPS
- Setting specific CORS origins instead of `*`
- Using a process manager (systemd, PM2) for auto-restart
- Reviewing security settings (rate limiting, audit logging)

## Updating

```bash
git pull
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
rm -rf app/static && cp -r frontend/build app/static
python -m app.main
```

The database migrations run automatically on startup.

> **Always rebuild the frontend when you update the backend.** The backend
> serves the SPA from `app/static`, so a `git pull` that changes API routes but
> leaves a stale `app/static` in place will ship a frontend that calls endpoints
> the new backend no longer exposes. Symptoms of a skipped rebuild include
> repeated `404`s in the browser network tab and a stale service worker (`sw.js`)
> that keeps requesting cached assets such as `/offline.html`. The `git pull`
> block above already rebuilds and re-copies `app/static`; do not omit those
> steps.

## Multi-install rollout (shared database)

AI Shuffle supports running more than one install (for example a desktop
**primary** plus an always-on **worker**) against a **single shared PostgreSQL
database**. The installs coordinate entirely through that database — durable
session state, turn leases, and pending interactions all live in shared tables.

**Upgrade every install that shares a database together.** Mixed-version
clusters are **not supported**. The V2 re-architecture replaced the old
peer-to-peer HTTP relay with shared-table coordination and **deleted** the
legacy relay endpoints (the cross-install `message` / `live` / `intake` routes
and the `/sessions/cloud/*` offload routes) in one phase. An install still
running pre-upgrade code alongside an upgraded peer will:

- get `404`s when it polls relay endpoints the upgraded peer has removed, and
  classify that peer as **offline** (a "device offline" toast, with sends to it
  blocked); and
- serve a stale frontend if its `app/static` was not rebuilt — for example a
  worker still shipping a pre-upgrade SPA produced repeated `404`s on the deleted
  `/sessions/cloud/*` endpoints plus a stale service worker requesting
  `/offline.html`.

Recommended rollout for a shared-database cluster:

1. Stop **all** installs sharing the database.
2. On **each** install: `git pull`, reinstall backend deps, **rebuild the
   frontend and re-copy `app/static`** (see [Updating](#updating) above).
3. Bring the installs back up. Database migrations run automatically on the
   first process to start; the schema is forward-only, so bringing every install
   to the same commit avoids one install writing state a peer cannot read.

Do not upgrade one install and leave a peer on an older commit "for now" — the
two will not interoperate across a V2 phase boundary. For the engineering
rationale and phase history, see the internal
[V2 Core Re-architecture](../../V2_CORE_REARCHITECTURE.md) spec.
