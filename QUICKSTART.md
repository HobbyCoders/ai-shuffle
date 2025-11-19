# Quick Start Guide - Claude Code SDK

Get your Claude Code AI agent running on Unraid in 5 minutes!

## Step 1: Deploy to Unraid

SSH into your Unraid server and run:

```bash
cd /mnt/user/appdata/
git clone <your-repo-url> claude-sdk
cd claude-sdk
docker-compose up -d
```

## Step 2: Login to Claude (REQUIRED)

```bash
docker exec -it claude-sdk-agent claude login
```

Follow the OAuth flow in your browser. This only needs to be done once - your login persists!

## Step 3: Verify It's Working

```bash
curl http://YOUR-UNRAID-IP:8000/auth/status
```

Expected response:
```json
{
  "authenticated": true,
  "message": "Authenticated with Claude Code"
}
```

## Step 4: Chat with Claude

```bash
curl -X POST http://YOUR-UNRAID-IP:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello Claude, tell me a joke!"}'
```

## That's It!

Your Claude AI agent is now running 24/7 on your Unraid server.

### Web UI

Visit these URLs for interactive API testing:
- **Swagger UI**: `http://YOUR-UNRAID-IP:8000/docs`
- **ReDoc**: `http://YOUR-UNRAID-IP:8000/redoc`

### Common Commands

```bash
# Check health
curl http://YOUR-UNRAID-IP:8000/health

# View logs
docker logs claude-sdk-agent -f

# Restart
docker-compose restart

# Update
git pull && docker-compose down && docker-compose up -d --build
```

### Integration Examples

**Python:**
```python
import requests
response = requests.post("http://YOUR-UNRAID-IP:8000/chat",
    json={"prompt": "What's the weather like?"})
print(response.json()["response"])
```

**JavaScript:**
```javascript
const response = await fetch('http://YOUR-UNRAID-IP:8000/chat', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({prompt: 'Hello!'})
});
const data = await response.json();
console.log(data.response);
```

### Troubleshooting

**Not authenticated error?**
```bash
docker exec -it claude-sdk-agent claude login
```

**Container not starting?**
```bash
docker-compose logs
docker-compose build --no-cache
```

**Need help?**
- Full documentation: See [README.md](README.md)
- Check container logs: `docker logs claude-sdk-agent`
- Verify health: `curl http://YOUR-UNRAID-IP:8000/health`

---

For full documentation, see [README.md](README.md)
