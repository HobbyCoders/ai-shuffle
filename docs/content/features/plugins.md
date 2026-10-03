<!--
title: Plugins
description: Extend AI Shuffle with installable plugin packages
order: 5
-->

# Plugins

Plugins extend AI Shuffle with additional hooks, commands, agents, and prompts. They are distributed through plugin marketplaces (git repositories) and can be installed, enabled, and managed through the UI or API.

## Overview

The plugin system allows you to:

- Add new capabilities to Claude sessions through plugin-contributed hooks
- Install file-based agents that work alongside your own agents
- Extend the system with custom commands and skills
- Share and reuse configurations across teams via marketplace repositories

Key concepts:

- **Marketplace-based distribution:** Plugins live in git repositories called marketplaces
- **User scope:** User-scope plugins are installed for the whole worker
- **Project scopes:** LOCAL installs stay in the project and out of git. PROJECT installs go to `<project>/plugins` and are committed
- **Global enablement:** Enabled plugins load in every session; an agent can add more through its `enabled_plugins` list
- **Owner-only management:** Installing, uninstalling, updating and enabling/disabling plugins requires the owner
- **Read-only hooks:** Plugin-contributed hooks are discoverable but not editable through the hooks UI

In the app, plugins are managed in **Settings > Plugins**. Older "Manage plugins" links open that section.

## Plugins found or made in a chat

Ask an agent in a code session for a plugin ("make me a plugin that reads my Cloudflare DNS records") and it follows the built-in `build-plugin` skill, using three tools that call the same code as **Settings > Plugins**:

- `FindPlugins` searches the plugins you have installed and the ones your plugin sources offer.
- `CheckPlugin` reports, for a plugin from your sources or a folder the agent wrote, what would load, what is adapted or left out for AI Shuffle and why, which hooks and MCP servers run code, and which outside services it asks for. For a folder it also runs the format check and lists every problem. Literal MCP `env` values are never shown.
- `InstallPlugin` installs one, always switched off. A folder the agent wrote becomes an [authored plugin](#ai-authored-plugins). A plugin from your sources is marked **Needs your review** in Settings, with the session and agent that asked recorded on this device; it stays off, and its hooks and MCP servers do not run, until you enable it there. An update while it waits keeps it waiting.

No tool enables a plugin. `InstallPlugin` asks for permission in Ask mode and is refused in sessions that are not yours (API keys). It only installs from the plugin sources you added, and only entries whose files come from inside the source or over HTTPS; an entry fetched over SSH or plain HTTP, an official connector runtime, or anything from an unknown source is refused. Agents never add sources. An install waiting for your review is not copied to your other devices until you enable it. Chat-mode sessions and background research do not have these tools.

## AI-authored plugins

A plugin folder an agent installs with `InstallPlugin`, or saves with the `save_plugin` tool in a code-mode session, is an authored plugin. Like `save_skill`, these tools ask for permission in Ask mode and are not available in chat sessions or background research.

- The plugin is written in the Agent Plugins layout to `~/.fluid/plugins/cache/authored/<name>/<version>/` and appears at once in **Settings > Plugins** as `<name>@authored`, marked **Needs review**.
- It stays off until you open it, check what it adds (each skill, command, agent and MCP server, with the server's command line or URL), and choose **Approve and enable**. That approval covers only the version you reviewed, identified by a hash of its files.
- Any later edit or new version stops it loading until you review it again. Disabling it withdraws the approval.
- If the session ran on another device of your network, the draft reaches every device's Settings, the same way saved skills do. Approving, disabling or uninstalling it on one device applies on all of them. A device turns the plugin on only when its own copy has the hash you approved.
- Authored plugins can contain skills, slash commands, subagents, MCP servers and service declarations. They cannot contain hooks, MCP server `env` values must be `${VAR}` references, and text that looks like a credential (a private key, an API key, a bearer token) is refused, so no secret is written into a plugin.

Through the API, enabling an authored plugin needs the reviewed hash: `POST /api/v1/plugins/{plugin_id}/enable` with `{"content_hash": "<content_hash from the plugin's details>"}`. A plugin that changed since returns 409.

## Official connector runtimes

Official apps such as Webhook, Discord Bot and Gmail appear in the `shuffle-connectors` marketplace. Their source snapshots ship with AI Shuffle and its application updates; **no GitHub account or private-repository access is needed** to install them. The normal application access/licensing rules still apply. This does not change access to third-party git marketplaces.

Installation checks each source against the shipped catalog's pinned SHA-256 and a per-file size limit, then creates a separate Python environment. It needs access to PyPI for dependencies, even though it does not download connector source. These runtimes have the host's OS permissions; process isolation is not a security sandbox.

Update AI Shuffle to receive a newer connector catalog, then update the runtime in Plugins. Pause dependent automations and disable connections first. Their IDs/settings are preserved. Missing, corrupt or oversized shipped files fail closed without replacing an existing runtime; repair/update AI Shuffle rather than signing into GitHub. A previous installed runtime can be rolled back.

## Plugin Capabilities

A plugin can contribute any combination of:

| Capability   | Description                                              |
|--------------|----------------------------------------------------------|
| **Hooks**    | Event-driven logic that runs at key points in AI workflow |
| **Commands** | Custom commands available to the system                   |
| **Agents**   | File-based subagents with their own prompts and tools     |
| **Skills**   | Specialized skills that extend Claude's abilities          |
| **MCP servers** | Tool servers bundled with the plugin                  |

Each plugin in the API response includes boolean flags indicating which capabilities it provides: `has_hooks`, `has_commands`, `has_agents`, `has_skills`, `has_mcp`.

Responses also carry:

- `format`: `claude` (Claude Code), `agent-plugins` (Agent Plugins / OpenAI) or `shuffle` (official connector runtimes).
- `trust`: `official`, `marketplace` or `authored`.
- `components`: every component the plugin declares, each with `kind`, `name`, `support` (`loaded`, `unsupported` or `needs_config`) and a `reason`: why it does not load, or, for a loaded component, what was adapted for this app (see [Plugins from Claude Code and Codex](#plugins-from-claude-code-and-codex)). LSP servers, output styles, ChatGPT-hosted apps (`.app.json`), plugin dependencies and SSE MCP servers are listed as unsupported; a `userConfig` block is listed as `needs_config`. Each declared [service](#services) is a `service` component; one with an `auth` sign-in has a `detail` line saying where it signs in, its scopes, its shipped `client_id` and its `registration_url`.

These fields are absent from workers that predate them.

## Plugin format

This is the format for plugins made for AI Shuffle. Check a folder with `python -m fluid.context.plugin_check <folder>`, which lists every problem it finds. It follows the Agent Plugins layout (a root `plugin.json`), with the parts only AI Shuffle reads kept under `extensions["ai.shuffle"]`.

Agents learn this format from the built-in `build-plugin` skill, which ships with AI Shuffle and is listed with your other skills. It covers when a plugin is worth making instead of a plain skill, the layout, declaring services, running the validator and saving the result for your review. A skill named `build-plugin` in `~/.fluid/skills` or in a project replaces it.

```
my-plugin/
├── plugin.json               # required
├── skills/<name>/SKILL.md    # frontmatter: name (= folder name), description
├── commands/<name>.md        # frontmatter: description (optional)
├── agents/<name>.md          # frontmatter: name (= file name), description, tools (optional)
├── mcp.json                  # {"mcpServers": {...}} (optional)
└── README.md                 # optional, not installed
```

```json
{
  "name": "cloudflare-dns",
  "version": "0.1.0",
  "description": "Read and explain DNS records for the owner's Cloudflare zones",
  "skills": "./skills/",
  "mcpServers": "./mcp.json",
  "extensions": {
    "ai.shuffle": {
      "commands": "./commands/",
      "agents": "./agents/",
      "services": [
        {"name": "cloudflare", "provider": "cloudflare", "access": "read",
         "purpose": "Read DNS records"}
      ]
    }
  }
}
```

Rules the validator enforces:

- `name`, skill, command and agent names: lowercase letters, digits and hyphens. `version` looks like `1.2.3`. `description` is required.
- No other files: no hooks, scripts, binaries or another tool's manifest folder. A plugin installed from a chat carries only what you can review in Settings.
- Skill, command and agent frontmatter holds only the fields listed above. `model`, `effort`, `permissionMode` and `hooks` are refused: the model is chosen per chat (defaults are under **Settings > Default models**) and permissions come from the chat.
- An agent's `tools` uses this app's tool names (Read, Write, Edit, Bash, Grep, Glob, WebFetch, WebSearch, Task, Monitor, Skill, AskUser, or `mcp__<server>__<tool>`). Leaving it out gives the agent the calling chat's tools.
- MCP servers are a `command` with `args` and `env`, or an `https://` `url`. `env` values must be `${VAR}` references.
- No credentials anywhere in the folder.

### Services

A plugin that talks to an outside API declares it under `services`: `name`, `provider` (`cloudflare` or `custom`), `url` (the public `https://` API address; required for `custom`), `access` (`read` or `write`), `purpose`, and optionally `auth`. No other keys are accepted. The declaration shows a reviewer what the plugin will ask for. It holds no credential.

`auth` declares an OAuth 2.0 sign-in: `{"type": "oauth2", "authorize_url": ..., "token_url": ..., "scopes": [...], "pkce": true}`, with an optional `revoke_url`. Every address must be plain `https://` with no query, fragment or credentials, on the same domain as the service `url` (`dash.example.com` and `api.example.com` match; `example.net` does not). Scopes are single words, at most 20. A client secret is never accepted in a plugin, under any key name.

Two optional keys let the owner sign in without registering an OAuth app first:

- `client_id`: a public OAuth client the plugin author registered with the provider. It needs `"pkce": true`, is never paired with a secret, and is refused if it looks like a key or token. The provider only accepts it from the return addresses the author registered for it (see [Service sign-in](/docs/api-reference/plugins#service-sign-in-oauth)).
- `registration_url`: the provider's dynamic client registration endpoint (RFC 7591), under the same address rules as the other endpoints. On the first sign-in from a return address, the worker registers its own public client there for that address and keeps it encrypted on the worker.

The client a sign-in uses is, in order: one the owner entered, else the plugin's `client_id`, else a registered one. With none of these, the owner enters a client before signing in.

The plugin list and `CheckPlugin` show where the plugin signs in, which scopes it asks for, its shipped `client_id` and its `registration_url`, so they are part of what the owner approves. A plugin update that asks for more scopes, changes an endpoint, the `registration_url` or the `client_id` needs a new sign-in.

The plugin's skill tells the agent to call `ChatResources`, and, when there is no grant yet, `RequestChatAccess`. For a service with a key, that names the provider and purpose; you enter the key in the private access card. For a service with `auth`, it names the plugin and the service, and you sign in. The agent then calls `ChatHttpRequest`, and the worker attaches the key or token. No model sees it.

## Plugins from Claude Code and Codex

AI Shuffle reads Claude Code plugins (`.claude-plugin/plugin.json`) and Codex / Agent Plugins (`.codex-plugin/plugin.json` or a root `plugin.json`) as they are, and keeps their files unchanged on disk. Every time a plugin is read, from any source (a marketplace install or update, a settings import, a project folder), fields that only mean something in the tool it came from are adapted:

| Part | What happens here |
|------|-------------------|
| Subagent `model`, `effort`, provider pins | Removed. The model is chosen per chat. |
| Subagent `permissionMode`, `hooks`, `mcpServers` | Removed. A subagent runs with the permissions of the chat that starts it. |
| Subagent `disallowedTools` | Mapped like `tools` and enforced: a dispatched plugin agent can never call a tool on this list, whatever it would otherwise inherit. |
| Subagent `tools` | Mapped to this app's names (`MultiEdit` and `apply_patch` become `Edit`, `shell` becomes `Bash`, `LS` becomes `Glob`, `mcp__plugin_<plugin>_<server>__<tool>` becomes `mcp__<server>__<tool>`). Tools with no equivalent (`TodoWrite`, `BashOutput`, `update_plan`) are dropped. If none of an agent's tools exist here, the agent is not offered, because an empty list would give it every tool. |
| Other subagent fields (`maxTurns`, `memory`, `color`, `isolation`, ...) | Removed as unused. |
| Skill and command `model` | Ignored, and not reported as the command's model. |
| `${CLAUDE_PLUGIN_ROOT}` / `${PLUGIN_ROOT}` in skills, commands and agents | Replaced with the plugin's folder when the skill, command or agent loads. |
| `${user_config.*}` | This app does not collect plugin settings. In skills, commands and agents each reference is replaced with a note that the setting is not configured. An MCP server that uses one does not start and is marked as needing settings. |
| Inline shell in commands (`` !`cmd` ``) | Never run for plugin commands, whatever `allowed-tools` says. |
| Codex skill metadata (`agents/openai.yaml`) | Not used. |
| LSP servers, output styles, monitors, themes, workflows, channels, `bin/`, `settings.json`, dependencies, MCP bundles (`.mcpb`), inline command maps, Codex agent files (`agents/*.toml`), ChatGPT apps (`.app.json`) | Skipped and listed with the reason. |

Each adaptation and each skipped part appears on the plugin's component list (`components[].reason`). Hooks and MCP servers from a marketplace plugin run once the plugin is enabled, the same as before.

## Plugin Structure

AI Shuffle reads two package formats and keeps each on disk as it was published.

**Claude Code.** A directory with an optional `.claude-plugin/plugin.json` manifest (only `name` is required). The manifest defines the plugin's metadata and declares its contributions.

```
my-plugin/
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── hooks/
│   └── hooks.json         # Hook definitions
├── agents/                # File-based agent definitions
│   └── reviewer.md
├── commands/              # Slash commands (markdown)
│   └── lint.md
├── skills/
│   └── <name>/SKILL.md    # One folder per skill
└── README.md              # Plugin documentation
```

The `plugin.json` manifest includes:

- **name** -- Human-readable plugin name
- **description** -- What the plugin does
- **version** -- Semantic version string
- **scope** -- Installation scope

**Agent Plugins / OpenAI (ChatGPT and Codex).** A root `plugin.json` (or the older `.codex-plugin/plugin.json`) with `skills/` and `mcp.json`. Display metadata under `extensions["com.openai"].interface` (`displayName`, `shortDescription`, `logo`) is shown in the plugin list. Commands and agents can be declared under `extensions["ai.shuffle"]`.

```
my-plugin/
├── plugin.json            # Agent Plugins manifest
├── skills/
│   └── <name>/SKILL.md
├── mcp.json               # MCP servers
└── .app.json              # ChatGPT-hosted apps (listed, not run)
```

`${CLAUDE_PLUGIN_ROOT}` and `${PLUGIN_ROOT}` both expand to the plugin's folder. `${CLAUDE_PLUGIN_DATA}` and `${PLUGIN_DATA}` both expand to `~/.fluid/plugins/data/<name>@<marketplace>`, which is kept across plugin updates.

## Marketplaces

Plugins are distributed through marketplaces, which are git repositories containing one or more plugins. A marketplace lists its plugins in `.claude-plugin/marketplace.json` (Claude Code) or `.agents/plugins/marketplace.json` (Agent Plugins / OpenAI). In the second format, `source.path` and `source.url` entries are supported; npm (`source.package`) entries are skipped for now. `GET /api/v1/plugins/marketplaces` lists each skipped entry under `skipped` with its `name` and `reason`, and Settings shows them in Explore.

### List Marketplaces

```bash
curl http://localhost:8000/api/v1/plugins/marketplaces \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Add a Marketplace

```bash
curl -X POST http://localhost:8000/api/v1/plugins/marketplaces \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://github.com/your-org/ai-shuffle-plugins.git",
    "name": "my-team-plugins"
  }'
```

`url` can be an `http://` or `https://` git URL, a `git@host:path` SSH URL, or GitHub shorthand `owner/repo`. The repository is cloned locally. If `name` is omitted, it is derived from the URL; either way it may contain only letters, digits, `-` and `_`.

### Sync a Marketplace

Pull the latest changes from the remote repository:

```bash
curl -X POST http://localhost:8000/api/v1/plugins/marketplaces/{marketplace_id}/sync \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Remove a Marketplace

Removes the marketplace, all its installed plugins and their cached files:

```bash
curl -X DELETE http://localhost:8000/api/v1/plugins/marketplaces/{marketplace_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Installing Plugins

### Browse Available Plugins

```bash
# List all available plugins across all marketplaces
curl http://localhost:8000/api/v1/plugins/available \
  -H "Authorization: Bearer YOUR_TOKEN"

# Filter by marketplace
curl "http://localhost:8000/api/v1/plugins/available?marketplace=my-marketplace" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Each result includes an `installed` flag and an `enabled` flag so you can see the current state.

### Install a Plugin

```bash
curl -X POST http://localhost:8000/api/v1/plugins/install \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "plugin_id": "my-plugin@my-marketplace",
    "scope": "user"
  }'
```

The `plugin_id` is in the format `name@marketplace`. `scope` defaults to `"user"`; the plugin is installed for the whole worker and enabled. Pass `"enable": false` to install it switched off; Settings does this, and shows the hooks and MCP servers a plugin runs before you turn it on. To install a plugin into one project, use the [Project Plugins](/docs/api-reference/projects#project-plugins) endpoints.

A marketplace entry's `source` can be:

- a relative path starting with `./`, inside the marketplace repository;
- `{"source": "github", "repo": "owner/repo"}`;
- `{"source": "url", "url": "..."}`;
- `{"source": "git-subdir", "url": "...", "path": "plugins/foo"}`, which takes one folder of the repository (`url` may be GitHub shorthand `owner/repo`).

Git sources may add `ref` (a branch or tag) and `sha`. Other source types are rejected.

Installation only takes files from inside the plugin's source:

- A relative path that resolves outside the marketplace repository is not found, and a `git-subdir` path must stay inside the cloned repository.
- Symlinks that point outside the marketplace repository (or outside a cloned plugin) are skipped.
- A plugin or marketplace name or version that is not a single plain folder name (one containing `/` or `\`, or `.` or `..`) is rejected.
- A `url` source must start with `https://`, `http://`, `ssh://` or `git@`. Anything else, such as `file://`, a local path or a string starting with `-`, is rejected. A `git-subdir` URL without one of these prefixes is read as GitHub `owner/repo`.
- An entry that pins a `sha` (7 to 64 hex characters) installs exactly that commit, and the install fails if that commit cannot be fetched.

Uninstalling deletes the plugin's files only inside the plugin cache.

### Batch Install

Install multiple plugins at once:

```bash
curl -X POST http://localhost:8000/api/v1/plugins/install/batch \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "plugin_ids": ["plugin-a@marketplace", "plugin-b@marketplace"],
    "scope": "user"
  }'
```

### Uninstall a Plugin

```bash
curl -X DELETE http://localhost:8000/api/v1/plugins/{plugin_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Enabling and Disabling

Plugins can be enabled or disabled globally without uninstalling them.

### Enable a Plugin

```bash
curl -X POST http://localhost:8000/api/v1/plugins/{plugin_id}/enable \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Disable a Plugin

```bash
curl -X POST http://localhost:8000/api/v1/plugins/{plugin_id}/disable \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Check Enable States

```bash
curl http://localhost:8000/api/v1/plugins/state/enabled \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Per-Agent Plugins

Globally enabled plugins load in every session. An agent's `enabled_plugins` list (API only) can add further plugins, by `name@marketplace` or by path, for that agent's sessions.

```json
{
  "config": {
    "enabled_plugins": [
      "plugin-a@my-marketplace",
      "/path/to/plugin-b"
    ]
  }
}
```

## Updating Plugins

```bash
# Plugins with a newer version in their marketplace
curl http://localhost:8000/api/v1/plugins/updates \
  -H "Authorization: Bearer YOUR_TOKEN"

# Update one plugin
curl -X POST http://localhost:8000/api/v1/plugins/update \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"plugin_id": "my-plugin@my-marketplace"}'

# Update every plugin that has an update
curl -X POST http://localhost:8000/api/v1/plugins/update/all \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Release versions such as `1.4.0` are compared numerically. A marketplace that lists an older or equal version is not offered as an update, so automatic updates never downgrade a plugin. Versions that cannot be ordered, such as commit hashes, count as an update whenever they differ.

Automatic updates are configured with `GET` and `PUT /api/v1/plugins/auto-update/settings` (`enabled`, `interval_hours`, `auto_sync_marketplaces`, `auto_update_plugins`). `POST /api/v1/plugins/auto-update/run` runs one check now.

## Plugin Hooks

Hooks contributed by plugins are read-only and managed by the plugin itself. See [Plugin-Contributed Hooks](/docs/features/hooks#plugin-contributed-hooks).

## File-Based Agents

Plugins can contribute file-based agents that work alongside your own agents. These agents are automatically discovered from enabled plugins.

```bash
curl http://localhost:8000/api/v1/plugins/agents/file-based \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Each file-based agent includes:

- `id` -- Unique identifier
- `name` -- Agent name
- `description` -- What it does
- `model` -- Always empty: a plugin agent runs on the chat's model
- `tools` -- Optional tool restrictions, mapped to this app's tool names
- `prompt` -- The agent's system prompt
- `source_plugin` -- Which plugin provides it
- `file_path` -- Path to the agent definition file

## Plugin Details

Get detailed information about a specific plugin, including its README and full capability lists:

```bash
curl http://localhost:8000/api/v1/plugins/{plugin_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Returns extended information including `commands`, `agents`, `skills` lists, and the plugin's `readme` content (if available).
