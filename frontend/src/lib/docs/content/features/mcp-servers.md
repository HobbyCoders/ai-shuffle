<!--
title: MCP Servers
description: Give your agents more tools with Model Context Protocol servers
order: 3
-->

# MCP Servers

MCP (Model Context Protocol) servers give your agents more tools: a database, a file system, GitHub, a search service and many more. Add a server, and its tools show up next to the agent's own tools.

Only the owner can add, change or remove servers.

## Add a server

1. Open **Settings → MCP servers** for a server every project can use. For a server only one project uses, open the project's **MCP Servers** tab instead.
2. Choose **Add server**.
3. Give it a name and pick its type:
   - **Command (stdio):** AI Shuffle runs a program on your worker, for example `npx -y @modelcontextprotocol/server-filesystem /workspace/docs`. Add any environment variables it needs, such as an access token.
   - **HTTP:** a server at a URL, with any headers it needs, such as an API key.
   - **SSE:** an older kind of URL server. It only works with Claude models, so use HTTP when the server offers it.
4. Save. Use the switch next to a server to turn it off and on.

Values you enter, such as tokens and keys, are only shown to you as the owner. Anyone else sees `********`.

## Where a server applies

- **Everywhere:** servers added under **Settings → MCP servers**.
- **One project, just for you:** kept with the project but out of Git.
- **One project, shared:** saved in the project's `.mcp.json`, so everyone who clones the repository gets it.
- **Installed with the Claude Code CLI:** servers you added to a project with the CLI also show up, and you can turn them off here.

## Built-in servers

AI Shuffle comes with a built-in browser server, so agents can open and use web pages. You can turn it off, but not remove it.

## Choosing which tools an agent gets

An MCP server's tools are named `mcp__<server>__<tool>`, for example `mcp__github__create_issue`. In an agent's **Tools** section you can give it a whole server or only some of its tools. See [Tool configuration](/docs/features/profiles#tool-configuration).

## Examples

**A folder (command):**

```json
{
  "name": "filesystem",
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace/docs"]
}
```

**GitHub (command, with a token):**

```json
{
  "name": "github",
  "type": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-github"],
  "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxxxxxxxxxxx" }
}
```

**A remote server (HTTP):**

```json
{
  "name": "api-tools",
  "type": "http",
  "url": "https://mcp.example.com/mcp",
  "headers": { "X-API-Key": "your-api-key" }
}
```

To manage MCP servers from your own code, see the [MCP API](/docs/api-reference/mcp).
