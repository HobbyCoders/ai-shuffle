<!--
title: Installation
description: How to install and run AI Shuffle locally or on a server
order: 2
-->

# Installation

## Prerequisites

- **Python 3.11+** — Backend runtime
- **Node.js 18+** — Frontend build tooling
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
```

### 3. Install Frontend Dependencies

```bash
cd frontend
npm install
cd ..
```

### 4. Build the Frontend

```bash
cd frontend
npm run build
cd ..
```

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
| `CORS_ORIGINS` | `*` | Allowed CORS origins |
| `DATABASE_URL` | `sqlite:///data/app.db` | Database path |

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
python -m app.main
```

The database migrations run automatically on startup.
