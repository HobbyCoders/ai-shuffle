<!--
title: MCP Servers
description: MCP server management, tool discovery, and project-scoped configuration
order: 10
-->

# MCP Servers API

Endpoints for managing Model Context Protocol (MCP) servers — extending Claude's capabilities with external tools.

## Global MCP Servers

### GET /api/v1/mcp/servers

List global (user-scope) MCP servers.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/mcp/servers \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "name": "filesystem",
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user"],
    "env": {},
    "enabled": true,
    "description": "File system access"
  }
]
```

### POST /api/v1/mcp/servers

Add a global MCP server.

**Auth:** Admin only

```bash
# stdio server
curl -X POST http://localhost:8000/api/v1/mcp/servers \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "filesystem",
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user"],
    "env": {},
    "enabled": true,
    "description": "File system access"
  }'

# SSE server
curl -X POST http://localhost:8000/api/v1/mcp/servers \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-sse-server",
    "type": "sse",
    "url": "http://localhost:3001/sse",
    "headers": { "Authorization": "Bearer token123" },
    "enabled": true
  }'
```

**Server Types:**

| Type | Required Fields | Description |
|------|----------------|-------------|
| `stdio` | `command`, `args` | Subprocess that communicates via stdin/stdout |
| `sse` | `url` | Server-Sent Events endpoint |
| `http` | `url` | HTTP API endpoint |

### PUT /api/v1/mcp/servers/{name}

Update a global MCP server.

**Auth:** Admin only

### DELETE /api/v1/mcp/servers/{name}

Remove a global MCP server.

**Auth:** Admin only

## Project MCP Servers

### GET /api/v1/projects/{project_id}/mcp/servers

List all MCP servers available to a project (combines user, local, and project scopes).

**Auth:** Required

### POST /api/v1/projects/{project_id}/mcp/servers

Add a project-scoped MCP server.

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj123/mcp/servers \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "project-db",
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-sqlite", "./data/project.db"],
    "scope": "project"
  }'
```

**Scopes:**

| Scope | Description |
|-------|-------------|
| `user` | Global servers (admin-only) |
| `local` | Project-local servers (stored in project root) |
| `project` | Project-scoped servers (in project root) |

### PUT /api/v1/projects/{project_id}/mcp/servers/{name}

Update a project MCP server.

**Auth:** Admin only

### DELETE /api/v1/projects/{project_id}/mcp/servers/{name}

Remove a project MCP server.

**Auth:** Admin only

## Tool Discovery

### GET /api/v1/mcp/tools

Discover tools from all configured MCP servers.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `refresh` | boolean | Force refresh from Claude CLI cache |

```bash
curl "http://localhost:8000/api/v1/mcp/tools?refresh=true" \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "name": "mcp__filesystem__read_file",
    "description": "Read a file from the filesystem",
    "server": "filesystem",
    "input_schema": {...}
  }
]
```

Tool names follow the pattern: `mcp__<server_name>__<tool_name>`

### GET /api/v1/mcp/stats

Get MCP server count and status.

```bash
curl http://localhost:8000/api/v1/mcp/stats \
  -H "Authorization: Bearer aih_your_key"
```
