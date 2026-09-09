<!--
title: Installation
description: How to install and run AI Shuffle locally or on a server
order: 2
-->

# Installation

For an ordinary desktop installation, use the packaged desktop app. It includes
its database and starts with a clean local store; no database credentials or
import are needed. The requirements below are for building from source.

## Prerequisites

- **Python 3.12+** — Backend runtime
- **Node.js 22.12+** — Frontend build tooling
- **Managed runtime** — The desktop app and Docker image bundle and supervise their own private PostgreSQL. Do not install or configure an external database.
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
pip install -r requirements.txt ./fluid
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

### 5. Start Through the Managed Runtime

Use the desktop app, or build and start the managed Docker runtime:

```bash
docker compose up -d --build
```

Docker starts the application on `http://localhost:8000` by default. Source
workers require a managed supervisor and its private `DATA_DIR/managed-postgres.json`
descriptor; standalone `app.main` startup is not a database setup path. Never
construct, edit or publish that descriptor yourself.

## First Launch

On first launch, you'll be directed to the setup page where you:

1. **Choose a workspace** — Set the working directory for projects.
2. **Connect development tools** — Connect Claude or add another supported model provider. GitHub setup is optional.
3. **Configure providers** — Review the models and credentials available to your worker.

Local setup and trusted local-network use require no username, password, or Shuffle account. For optional remote access, open **Devices → Remote access** and link the worker to your account in the browser or Android app. Shuffle creates the managed tunnel automatically. Remote browsers sign in with the account that owns that worker.

## Configuration

The managed runtime generates and retains database credentials privately. There
are no database connection or tuning environment options. Preserve the complete
data directory and protected backups when updating.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8000` | Server port |
| `SHUFFLE_LOCAL_ACCESS` | `true` | Allow owner access from direct trusted local/LAN requests on the local listener. Use `false` during an existing deployment's protected-ingress upgrade. |
| `CORS_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000` | Comma-separated allowed CORS origins (use `*` for development only) |

### Running in Production

For production deployments, consider:

- Using Shuffle's managed remote connection, or directing a custom HTTPS reverse proxy to the protected loopback listener on port 8001 in the same network namespace. Never expose the local owner listener through a public proxy or tunnel.
- Setting specific CORS origins instead of `*`
- Using the desktop or Docker supervisor for ordered worker/database restarts
- Reviewing security settings (rate limiting, audit logging)

## Updating

Keep a non-managed installation on its existing release and preserve its data,
configuration and backup until a separately tested migration is approved. This
release does not automatically attach to or import an existing external database.
For packaged desktop installs, update the package and restart through the desktop
app. For a managed source/Docker install, rebuild through its supervisor:

```bash
git pull
docker compose up -d --build
```

Database schema migrations run automatically on startup. Before upgrading a password-encrypted installation, stop all writers and back up both PostgreSQL and the persistent data directory. Adopt its existing encryption key using the legacy startup password or the private `python -m scripts.credential_keys adopt` prompt, then verify `python -m scripts.credential_keys status` reports ready. Browser sign-in is not an encryption-unlock step.

For an existing managed tunnel, start the new build with `SHUFFLE_LOCAL_ACCESS=false`, verify its protected port 8001 and public remote authorization, then enable local access. Preserve persistent credential keys and remove the legacy password from configuration after successful adoption and restart verification. The repository's [local-access rollout](https://github.com/HobbyCoders/ai-shuffle/blob/main/docs/LOCAL_ACCESS.md) and [credential recovery guide](https://github.com/HobbyCoders/ai-shuffle/blob/main/docs/credential-key-recovery.md) describe the full migration and backup requirements.

> **Always rebuild the frontend when you update the backend.** The backend
> serves the SPA from `app/static`, so a `git pull` that changes API routes but
> leaves a stale `app/static` in place will ship a frontend that calls endpoints
> the new backend no longer exposes. Symptoms of a skipped rebuild include
> repeated `404`s in the browser network tab and a stale service worker (`sw.js`)
> that keeps requesting cached assets such as `/offline.html`. The `git pull`
> Docker rebuild above includes the frontend build and deploy; do not skip it
> when updating source. A source-only `--update` also rebuilds its assets.

## Multiple Workers

Each worker has its own private managed PostgreSQL database. Join workers through
**Settings → Devices**; never point multiple workers at a shared database.
Historical shared-database installations need a coordinated, separately rehearsed
migration. Preserve the original deployment and backup until recovery is verified;
see the [network migration guide](https://github.com/HobbyCoders/ai-shuffle/blob/main/docs/network/migration.md).

### Refreshing a native checkout

After `git pull`, run `python run_local.py --update` (or the platform launcher with
`--update`). This installs the checked-in Fluid harness and locked Node packages,
then rebuilds the frontend, browser MCP and AI tools. It does not reconfigure the
database or restart an already running server. Restart through the managed desktop
runtime after it succeeds; do not launch the worker directly.
Native release archives already contain the frontend; replace the archive to update
that frontend, then run `--update` to install/build its runtime tools.
