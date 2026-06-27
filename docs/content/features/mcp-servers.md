<!--
title: MCP Servers
description: Extend Claude's capabilities with Model Context Protocol servers
order: 3
-->

# MCP Servers

MCP (Model Context Protocol) servers extend Claude's capabilities by providing external tools, data sources, and integrations. AI Shuffle manages MCP server configuration and discovery so Claude can use tools from databases, file systems, APIs, and more.

## Overview

MCP is an open protocol that lets AI models interact with external systems through a standardized interface. When you configure an MCP server in AI Shuffle, Claude gains access to the tools that server provides. Tools from MCP servers appear alongside Claude's built-in tools and are invoked the same way.

Key concepts:

- **Three server types:** stdio (subprocess), SSE (Server-Sent Events), HTTP
- **Multiple scopes:** USER (global), LOCAL (project root config), PROJECT (project-scoped config), and INSTALLED (per-project entries in `~/.claude.json`)
- **Built-in servers:** AI Shuffle ships managed integrations (e.g. `claude-in-chrome`) that appear alongside configured servers
- **Tool discovery:** MCP tools are automatically discovered and namespaced
- **Admin-only management:** Creating, updating, deleting, and toggling servers requires admin privileges

## Server Types

### stdio

Runs the MCP server as a local subprocess. AI Shuffle starts the command and communicates over stdin/stdout using the MCP protocol.

Best for: locally installed tools, CLI-based integrations, development tools.

**Configuration fields:**

| Field     | Type          | Description                         |
|-----------|---------------|-------------------------------------|
| `command` | string        | Command to execute                  |
| `args`    | list[string]  | Command-line arguments              |
| `env`     | dict          | Environment variables for the process |

### SSE (Server-Sent Events)

Connects to a remote MCP server over HTTP using Server-Sent Events for streaming communication.

Best for: remote services, shared team tools, cloud-hosted integrations.

**Configuration fields:**

| Field     | Type   | Description                              |
|-----------|--------|------------------------------------------|
| `url`     | string | Server URL (e.g., `http://host:port/sse`) |
| `headers` | dict   | HTTP headers (e.g., authorization)        |

### HTTP

Connects to a remote MCP server using standard HTTP request/response.

Best for: stateless tools, REST-like integrations, serverless functions.

**Configuration fields:**

| Field     | Type   | Description                                |
|-----------|--------|--------------------------------------------|
| `url`     | string | Server URL (e.g., `http://host:port/mcp`)   |
| `headers` | dict   | HTTP headers (e.g., authorization)          |

## Scopes

MCP servers can be configured at several levels:

### USER (Global)

Available across all projects. Managed through the global MCP settings page. Only admins can add, update, or remove global servers.

Use for: tools you want available everywhere, such as a database explorer or a documentation search server.

### LOCAL

Configured at the project root level. Stored in the project's local configuration. Available when working within that specific project directory.

Use for: project-specific development tools, local database connections, test fixtures.

### PROJECT

Scoped to a specific project in AI Shuffle's database. Similar to LOCAL but managed through the project settings rather than filesystem config files.

Use for: project-level integrations managed through the AI Shuffle UI.

### INSTALLED

Per-project servers stored in `~/.claude.json` under the project's `mcpServers` entry (the same place the Claude Code CLI tracks installed servers). When adding a project server, pass `"scope": "installed"` to write here. Removing or toggling an installed server targets this same location.

Use for: servers installed for a project outside AI Shuffle's project config files.

### Built-in servers

AI Shuffle also ships managed built-in integrations (currently `claude-in-chrome`) that are surfaced through the same API. These are not user-configured and cannot be created or deleted; they are enabled or disabled via the toggle endpoints and the global `settings.json` disable list.

When listing MCP servers for a project, every scope is returned grouped by scope, plus a `total_count` that sums all of them:

```json
{
  "builtin": [...],
  "user": [...],
  "installed": [...],
  "local": [...],
  "project": [...],
  "total_count": 0
}
```

## Tool Discovery

MCP tools are automatically discovered from configured servers. Tools follow a namespaced naming convention:

```
mcp__<server_name>__<tool_name>
```

For example, a server named `filesystem` providing a `read_file` tool appears as `mcp__filesystem__read_file`.

To list all discovered MCP tools:

```bash
curl http://localhost:8000/api/v1/mcp/tools \
  -H "Authorization: Bearer YOUR_TOKEN"
```

To force a refresh from the Claude CLI cache:

```bash
curl "http://localhost:8000/api/v1/mcp/tools?refresh=true" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Each tool in the response includes:

- `name` -- The tool name (without the `mcp__server__` prefix)
- `server` -- The server that provides it
- `description` -- What the tool does (if available)

## API Reference

### Global (USER Scope) Servers

#### List Global Servers

```bash
curl http://localhost:8000/api/v1/mcp/servers \
  -H "Authorization: Bearer YOUR_TOKEN"
```

#### Add a Global Server

```bash
curl -X POST http://localhost:8000/api/v1/mcp/servers \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "filesystem",
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/dir"],
    "description": "Filesystem access for Claude"
  }'
```

#### Update a Global Server

```bash
curl -X PUT http://localhost:8000/api/v1/mcp/servers/{server_name} \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/new/path"]
  }'
```

#### Remove a Global Server

```bash
curl -X DELETE http://localhost:8000/api/v1/mcp/servers/{server_name} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

#### Toggle a Global Server

Enable or disable a global (or built-in) server via `settings.json`:

```bash
curl -X PATCH http://localhost:8000/api/v1/mcp/servers/{server_name}/toggle \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "enabled": false }'
```

### Project-Scoped Servers

#### List Project Servers (All Scopes)

```bash
curl http://localhost:8000/api/v1/projects/{project_id}/mcp/servers \
  -H "Authorization: Bearer YOUR_TOKEN"
```

#### Add a Project Server

```bash
curl -X POST http://localhost:8000/api/v1/projects/{project_id}/mcp/servers \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "project-db",
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-sqlite", "./project.db"],
    "scope": "local",
    "description": "Project SQLite database"
  }'
```

The `scope` field accepts `"local"`, `"project"`, or `"installed"` (not `"user"` -- use the global endpoint for that). It defaults to `"local"`.

#### Update a Project Server

```bash
curl -X PUT "http://localhost:8000/api/v1/projects/{project_id}/mcp/servers/{server_name}?scope=local" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "args": ["-y", "@modelcontextprotocol/server-sqlite", "./new-database.db"]
  }'
```

#### Remove a Project Server

```bash
curl -X DELETE "http://localhost:8000/api/v1/projects/{project_id}/mcp/servers/{server_name}?scope=local" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

The `scope` query parameter accepts `local`, `project`, `installed`, or `user`.

#### Toggle a Project Server

Enable or disable a server for a specific project (written to the project's `disabledMcpServers` list in `~/.claude.json`):

```bash
curl -X PATCH http://localhost:8000/api/v1/projects/{project_id}/mcp/servers/{server_name}/toggle \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "enabled": false }'
```

### Statistics

```bash
curl http://localhost:8000/api/v1/mcp/stats \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Returns the count of global servers configured.

## Example Configurations

### Filesystem Server (stdio)

Give Claude read/write access to a specific directory:

```json
{
  "name": "filesystem",
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/projects"],
  "description": "Filesystem access"
}
```

### SQLite Database Server (stdio)

Let Claude query and modify a SQLite database:

```json
{
  "name": "sqlite",
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-sqlite", "/path/to/database.db"],
  "description": "SQLite database access"
}
```

### GitHub Server (stdio)

Provide GitHub API access:

```json
{
  "name": "github",
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-github"],
  "env": {
    "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxxxxxxxxxxx"
  },
  "description": "GitHub API integration"
}
```

### Remote SSE Server

Connect to a remote MCP server over SSE:

```json
{
  "name": "remote-tools",
  "type": "sse",
  "url": "https://mcp.example.com/sse",
  "headers": {
    "Authorization": "Bearer your-api-key"
  },
  "description": "Remote tool server"
}
```

### HTTP Server

Connect to a stateless HTTP-based MCP server:

```json
{
  "name": "api-tools",
  "type": "http",
  "url": "https://mcp.example.com/mcp",
  "headers": {
    "X-API-Key": "your-api-key"
  },
  "description": "Stateless API tools"
}
```
