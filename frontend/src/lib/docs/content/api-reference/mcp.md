<!--
title: MCP Servers
description: MCP server management, tool discovery, and project-scoped configuration
order: 10
-->

# MCP Servers API

Endpoints for managing Model Context Protocol (MCP) servers — extending your agents with external tools.

## Global MCP Servers

### GET /api/v1/mcp/servers

List global (user-scope) MCP servers, with the built-in servers (such as `browser`, `"builtin": true`) first.

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
    "scope": "user",
    "enabled": true,
    "description": "File system access",
    "builtin": false
  }
]
```

### POST /api/v1/mcp/servers

Add a global MCP server.

**Auth:** Owner only

```bash
# stdio server
curl -X POST http://localhost:8000/api/v1/mcp/servers \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "filesystem",
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user"],
    "env": {},
    "description": "File system access"
  }'

# SSE server
curl -X POST http://localhost:8000/api/v1/mcp/servers \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-sse-server",
    "type": "sse",
    "url": "http://localhost:3001/sse",
    "headers": { "Authorization": "Bearer token123" }
  }'
```

**Server Types:**

| Type | Required Fields | Description |
|------|----------------|-------------|
| `stdio` | `command` (`args` optional) | Subprocess that communicates via stdin/stdout |
| `sse` | `url` | Server-Sent Events endpoint |
| `http` | `url` | HTTP API endpoint |

`type` defaults to `stdio`. `name` is 1 to 64 characters: letters, digits, `-` and `_`. Returns `201` with the server, or `400` when a required field is missing, the name is invalid or a server with that name already exists.

New servers start enabled. Use the toggle endpoint below to turn one off.

### PUT /api/v1/mcp/servers/{name}

Update a global MCP server. Send only the fields to change. Returns `404` if the server does not exist.

**Auth:** Owner only

### DELETE /api/v1/mcp/servers/{name}

Remove a global MCP server.

**Auth:** Owner only

### PATCH /api/v1/mcp/servers/{name}/toggle

Enable or disable a global MCP server.

**Auth:** Owner only

```json
{ "enabled": false }
```

## Project MCP Servers

### GET /api/v1/projects/{project_id}/mcp/servers

List all MCP servers available to a project, grouped by scope.

**Auth:** Required

**Response:**
```json
{
  "builtin": [],
  "user": [],
  "installed": [],
  "local": [],
  "project": [],
  "total_count": 0
}
```

### POST /api/v1/projects/{project_id}/mcp/servers

Add a project-scoped MCP server.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj123/mcp/servers \
  -H "Authorization: Bearer $OWNER_TOKEN" \
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
| `user` | Global servers. Managed through `/api/v1/mcp/servers`, not here |
| `local` | This project only, kept out of git (`<project>/.claude/.mcp.json`). The default |
| `project` | Shared with the repository (`<project>/.mcp.json`) |
| `installed` | Servers Claude Code installed for this project directory |

### PUT /api/v1/projects/{project_id}/mcp/servers/{name}

Update a project MCP server. Pass `?scope=` (`local`, `project` or `installed`; default `local`). `scope=user` returns `400`; update global servers through `/api/v1/mcp/servers`.

**Auth:** Owner only

### DELETE /api/v1/projects/{project_id}/mcp/servers/{name}

Remove a project MCP server. Pass `?scope=` (`local`, `project` or `installed`; default `local`). `scope=user` removes the global server of that name.

**Auth:** Owner only

### PATCH /api/v1/projects/{project_id}/mcp/servers/{name}/toggle

Enable or disable a server for this project. Body: `{"enabled": true}` or `{"enabled": false}`. A built-in server has no per-project setting, so toggling one here enables or disables it everywhere.

**Auth:** Owner only

## Tool Discovery

### GET /api/v1/mcp/tools

Discover tools from all configured MCP servers. Tools are listed live from each server and cached per server for 10 minutes.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `refresh` | boolean | Bypass the per-server tool-list cache (10 minutes) |

```bash
curl "http://localhost:8000/api/v1/mcp/tools?refresh=true" \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "name": "read_file",
    "server": "filesystem",
    "description": "Read a file from the filesystem"
  }
]
```

`name` is the tool's own name and `server` is the server it comes from. In an agent's tool lists and in permission rules the same tool is written `mcp__<server>__<name>` (here `mcp__filesystem__read_file`).

### GET /api/v1/mcp/stats

Get the number of global (user-scope) MCP servers. Built-in servers are not counted.

```bash
curl http://localhost:8000/api/v1/mcp/stats \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{ "global_server_count": 2 }
```
