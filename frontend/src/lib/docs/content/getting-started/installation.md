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
| `DATABASE_URL` | _(required)_ | PostgreSQL connection string, e.g. `postgresql://REDACTED@localhost:5432/aishuffle`. No default — the server refuses to start without it. |

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
