# Claude Code Python SDK - Dockerized AI Agent

A dockerized FastAPI service that provides REST API access to Claude Code CLI with OAuth authentication, designed to run on Unraid servers as a cloud AI agent.

## Key Features

- **Claude Code CLI Integration** - Uses official Claude Code CLI with OAuth (not API keys)
- **REST API Wrapper** - FastAPI-based HTTP interface for Claude interactions
- **Persistent Authentication** - OAuth tokens persist across container restarts
- **Docker Containerized** - Easy deployment on Unraid or any Docker host
- **Pre-built Images** - Available on ghcr.io, no build required
- **Interactive Login** - Simple OAuth flow via `claude login`
- **Health Monitoring** - Built-in health checks and status endpoints
- **Command Execution** - Execute any Claude Code CLI command via API

## Docker Image

Pre-built multi-architecture images are automatically published to GitHub Container Registry:

```bash
docker pull ghcr.io/<your-username>/proxy-python-sdk:latest
```

**Available tags:**
- `latest` - Latest build from main branch
- `v*.*.*` - Specific version releases
- `main` - Latest main branch build

**Supported platforms:**
- `linux/amd64` - Standard x86_64 systems, Intel/AMD CPUs
- `linux/arm64` - ARM systems like Raspberry Pi 4/5, Apple Silicon

> **📖 For detailed information about GitHub Container Registry setup, publishing, and troubleshooting, see [GHCR_SETUP.md](GHCR_SETUP.md)**

## Prerequisites

- Docker and Docker Compose installed
- Unraid server (or any Docker-compatible system)
- Anthropic account for Claude OAuth login (free at [claude.ai](https://claude.ai))

## Quick Start

### Option A: Use Pre-built Image (Recommended for Unraid)

The easiest way is to pull the pre-built image from GitHub Container Registry:

```bash
# Create directory and config
mkdir -p /mnt/user/appdata/claude-sdk
cd /mnt/user/appdata/claude-sdk

# Download docker-compose.yml
curl -O https://raw.githubusercontent.com/<your-username>/Proxy-Python-SDK/main/docker-compose.yml

# Optional: Download and customize .env
curl -O https://raw.githubusercontent.com/<your-username>/Proxy-Python-SDK/main/.env.example
mv .env.example .env

# Pull and start (will use pre-built image from ghcr.io)
docker-compose pull
docker-compose up -d
```

### Option B: Build from Source

If you prefer to build the image yourself:

```bash
# 1. Clone and Setup
git clone <your-repo-url>
cd Proxy-Python-SDK

# 2. Optional: Configure Environment
cp .env.example .env
# Edit .env if you want to change defaults (port, logging, etc.)

# 3. Build and Start
docker-compose up -d --build
```

### Authentication (Required for Both Options)

After starting the container (regardless of which method you used), you must login to Claude Code:

```bash
# Access the container
docker exec -it claude-sdk-agent /bin/bash

# Inside the container, run:
claude login

# Follow the OAuth flow in your browser
# After successful login, exit the container:
exit
```

Your authentication will persist in the `claude-auth` volume.

### Verify Authentication

```bash
# Check authentication status
curl http://localhost:8000/auth/status

# Should return: {"authenticated": true, ...}
```

## Authentication

This SDK uses **Claude OAuth** (not API keys). You authenticate once using `claude login`, and the tokens persist.

### Login Methods

#### Method 1: Direct Container Access (Recommended)

```bash
docker exec -it claude-sdk-agent claude login
```

#### Method 2: Via API Endpoint

```bash
# Get login instructions
curl http://localhost:8000/auth/login-instructions

# The API will guide you to exec into the container
```

### Check Authentication Status

```bash
curl http://localhost:8000/auth/status
```

Response:
```json
{
  "authenticated": true,
  "config_dir": "/home/appuser/.config/claude",
  "message": "Authenticated with Claude Code"
}
```

### Logout

```bash
curl -X POST http://localhost:8000/auth/logout
```

## Unraid Deployment

### Method 1: Pre-built Image (Fastest)

1. SSH into your Unraid server
2. Create directory and download config:
   ```bash
   cd /mnt/user/appdata/
   mkdir claude-sdk && cd claude-sdk
   curl -O https://raw.githubusercontent.com/<your-username>/Proxy-Python-SDK/main/docker-compose.yml
   curl -O https://raw.githubusercontent.com/<your-username>/Proxy-Python-SDK/main/.env.example
   mv .env.example .env
   ```
3. Edit `.env` if needed (optional)
4. Pull and start:
   ```bash
   docker-compose pull
   docker-compose up -d
   ```
5. Login to Claude:
   ```bash
   docker exec -it claude-sdk-agent claude login
   ```

### Method 2: Build from Source

1. SSH into your Unraid server
2. Navigate to `/mnt/user/appdata/`
3. Clone this repository:
   ```bash
   cd /mnt/user/appdata/
   git clone <your-repo-url> claude-sdk
   cd claude-sdk
   ```
4. Start the container:
   ```bash
   docker-compose up -d --build
   ```
5. Login to Claude:
   ```bash
   docker exec -it claude-sdk-agent claude login
   ```

### Method 3: Unraid Docker UI

1. Open Unraid Web UI → Docker tab
2. Click "Add Container"
3. Configure:
   - **Name**: `claude-sdk-agent`
   - **Repository**: `ghcr.io/<your-username>/proxy-python-sdk:latest`
   - **Network Type**: `bridge`
   - **Port**: `8000` (Container) → `8000` (Host)
   - **Volume**: Add path mapping for auth persistence:
     - Container Path: `/home/appuser/.config/claude`
     - Host Path: `/mnt/user/appdata/claude-sdk/auth`
     - Access Mode: `Read/Write`
   - **Console shell command**: `Bash`
4. Apply and start
5. Use console to run `claude login`

### Persistent Storage

Authentication tokens are stored in a Docker volume (`claude-auth`) and persist across container restarts and updates.

## API Endpoints

### Authentication Endpoints

#### GET `/auth/status`
Check if authenticated with Claude Code.

```bash
curl http://localhost:8000/auth/status
```

#### GET `/auth/login-instructions`
Get step-by-step login instructions.

```bash
curl http://localhost:8000/auth/login-instructions
```

#### POST `/auth/logout`
Logout from Claude Code.

```bash
curl -X POST http://localhost:8000/auth/logout
```

### Core Endpoints

#### GET `/health`
Health check with authentication status.

```bash
curl http://localhost:8000/health
```

Response:
```json
{
  "status": "healthy",
  "service": "claude-code-sdk",
  "version": "2.0.0",
  "authenticated": true
}
```

#### POST `/chat`
Send a prompt to Claude and get a response.

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "What is the capital of France?",
    "model": "claude-sonnet-4"
  }'
```

Response:
```json
{
  "response": "The capital of France is Paris.",
  "status": "success",
  "metadata": {
    "model": "claude-sonnet-4",
    "returncode": 0
  }
}
```

#### POST `/execute`
Execute any Claude Code CLI command.

```bash
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{
    "command": "chat",
    "args": ["--model", "claude-sonnet-4"],
    "timeout": 300
  }'
```

#### GET `/version`
Get Claude Code version.

```bash
curl http://localhost:8000/version
```

## Usage Examples

### Python Client

```python
import requests

# Check auth status
response = requests.get("http://localhost:8000/auth/status")
print(response.json())

# Chat with Claude
response = requests.post(
    "http://localhost:8000/chat",
    json={
        "prompt": "Explain quantum computing in simple terms",
        "model": "claude-sonnet-4"
    }
)

result = response.json()
print(result["response"])
```

### JavaScript/Node.js

```javascript
// Check authentication
const authStatus = await fetch('http://localhost:8000/auth/status');
const auth = await authStatus.json();
console.log('Authenticated:', auth.authenticated);

// Chat with Claude
const response = await fetch('http://localhost:8000/chat', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    prompt: 'Write a haiku about programming',
    model: 'claude-sonnet-4'
  })
});

const data = await response.json();
console.log(data.response);
```

### cURL Examples

```bash
# Simple chat
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello Claude!"}'

# Execute custom command
curl -X POST http://localhost:8000/execute \
  -H "Content-Type: application/json" \
  -d '{
    "command": "version"
  }'

# Check health
curl http://localhost:8000/health
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HOST` | Server host address | `0.0.0.0` |
| `PORT` | Server port | `8000` |
| `SERVICE_NAME` | Service identifier | `claude-code-sdk` |
| `LOG_LEVEL` | Logging level (debug, info, warning, error) | `info` |
| `COMMAND_TIMEOUT` | Default timeout for Claude commands (seconds) | `300` |
| `WORKSPACE_DIR` | Host directory to mount as workspace | `./workspace` |

### Available Claude Models

Use these model names in your API requests:

- `claude-sonnet-4` - Latest Claude Sonnet (recommended)
- `claude-opus-4` - Most capable model
- `claude-haiku-4` - Fastest model
- `claude-3-5-sonnet` - Previous generation

## Docker Compose Configuration

The `docker-compose.yml` includes:

- **Persistent Auth Volume** - `claude-auth` stores OAuth tokens
- **Workspace Volume** - Optional directory for file operations
- **TTY/STDIN** - Enabled for interactive commands
- **Resource Limits** - 2 CPU cores, 2GB RAM (configurable)
- **Health Checks** - Automatic container health monitoring
- **Logging** - Configured with rotation (10MB, 3 files)

## Monitoring and Maintenance

### View Logs

```bash
# Real-time logs
docker-compose logs -f

# Last 100 lines
docker-compose logs --tail=100

# Specific service
docker logs claude-sdk-agent
```

### Check Container Status

```bash
docker-compose ps
docker ps | grep claude-sdk
```

### Restart Service

```bash
docker-compose restart
```

### Update Service

```bash
git pull
docker-compose down
docker-compose build --no-cache
docker-compose up -d

# Re-authenticate if needed
docker exec -it claude-sdk-agent claude login
```

### Backup Authentication

```bash
# Backup auth volume
docker run --rm -v proxy-python-sdk_claude-auth:/data -v $(pwd):/backup \
  alpine tar czf /backup/claude-auth-backup.tar.gz -C /data .

# Restore auth volume
docker run --rm -v proxy-python-sdk_claude-auth:/data -v $(pwd):/backup \
  alpine tar xzf /backup/claude-auth-backup.tar.gz -C /data
```

## Interactive API Documentation

FastAPI provides automatic interactive documentation:

- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

These UIs allow you to test all endpoints directly from your browser.

## Troubleshooting

### Not Authenticated Error

```
"authenticated": false
```

**Solution**: Login to Claude Code
```bash
docker exec -it claude-sdk-agent claude login
```

### Container Won't Start

```bash
# Check logs
docker-compose logs

# Verify build
docker-compose build --no-cache
```

### Claude Command Timeout

If commands timeout (default 300s), increase in `.env`:
```env
COMMAND_TIMEOUT=600
```

### Authentication Lost After Update

If auth is lost after rebuild:
```bash
# Check if volume exists
docker volume ls | grep claude-auth

# Re-login
docker exec -it claude-sdk-agent claude login
```

### Port Already in Use

Change port in `.env`:
```env
PORT=8001
```

Then restart:
```bash
docker-compose down
docker-compose up -d
```

## Architecture

```
┌─────────────────────────────────────────┐
│           Client Application            │
│     (Python, JavaScript, cURL, etc.)    │
└────────────────┬────────────────────────┘
                 │ HTTP REST API
                 ▼
┌─────────────────────────────────────────┐
│          FastAPI Service                │
│      (main.py + auth_helper.py)         │
└────────────────┬────────────────────────┘
                 │ Subprocess calls
                 ▼
┌─────────────────────────────────────────┐
│         Claude Code CLI                 │
│      (OAuth authenticated)              │
└────────────────┬────────────────────────┘
                 │ OAuth + API
                 ▼
┌─────────────────────────────────────────┐
│       Anthropic Claude AI               │
└─────────────────────────────────────────┘
```

## Security Considerations

1. **OAuth Tokens** - Stored in Docker volume, protected by container isolation
2. **No API Keys** - Uses OAuth flow, more secure than API keys
3. **Non-root Container** - Runs as `appuser` (UID 1000)
4. **Network Isolation** - Uses dedicated Docker network
5. **CORS** - Configure in production for specific origins
6. **Firewall** - Secure port 8000 on Unraid with firewall rules
7. **Reverse Proxy** - Recommended: Use nginx/Traefik with SSL for production

## Development

### Local Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run locally (requires Claude Code installed on host)
python main.py
```

### Testing

```bash
# Build container
docker-compose build

# Run tests (start container first)
docker-compose up -d

# Test health
curl http://localhost:8000/health

# Test auth status
curl http://localhost:8000/auth/status

# Test chat (after login)
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Say hello"}'
```

## Advanced Usage

### Custom Workspace

Mount a workspace directory for file operations:

```yaml
# In docker-compose.yml or .env
WORKSPACE_DIR=/mnt/user/appdata/claude-workspace
```

### Multiple Instances

Run multiple instances on different ports:

```bash
# Instance 1
PORT=8000 docker-compose up -d

# Instance 2 (separate directory)
cd ../claude-sdk-2
PORT=8001 docker-compose up -d
```

### Integration with Other Services

Use the REST API to integrate with:
- Home automation (Home Assistant, Node-RED)
- Chat platforms (Discord, Slack bots)
- CI/CD pipelines
- Custom applications

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## License

This project is open source and available under the MIT License.

## Support

- **Issues**: Open an issue on GitHub
- **Claude Code Docs**: [docs.anthropic.com](https://docs.anthropic.com)
- **Unraid Forums**: [forums.unraid.net](https://forums.unraid.net)

## Acknowledgments

- Built with [FastAPI](https://fastapi.tiangolo.com/)
- Powered by [Anthropic Claude](https://www.anthropic.com/)
- Uses [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)
- Designed for [Unraid](https://unraid.net/)

---

**Note**: This is an unofficial community project and is not affiliated with Anthropic.
