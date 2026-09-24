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
- Install file-based agents that work alongside your subagents
- Extend the system with custom commands and skills
- Share and reuse configurations across teams via marketplace repositories

Key concepts:

- **Marketplace-based distribution:** Plugins live in git repositories called marketplaces
- **User-global install path:** Installed plugins and enablement live in `~/.fluid/plugins` and `~/.fluid/settings.json`
- **Project scopes:** LOCAL installs go to `<project>/.fluid/plugins` (enablement in `.fluid/settings.local.json`). PROJECT installs go to `<project>/plugins` (enablement in `.fluid/settings.json`)
- **Per-profile enablement:** Plugins are enabled through the `enabled_plugins` list in profile configuration
- **Admin-only management:** Installing, uninstalling, and enabling/disabling plugins requires admin privileges
- **Read-only hooks:** Plugin-contributed hooks are discoverable but not editable through the hooks UI

## Official connector runtimes

Official apps such as Webhook, Discord Bot and Gmail appear in the
`shuffle-connectors` marketplace. Their source snapshots ship with AI Shuffle
and its application updates; **no GitHub account or private-repository access
is needed** to install them. The normal application access/licensing rules
still apply. This does not change access to third-party git marketplaces.

Installation checks each source against the shipped catalog's pinned SHA-256
and a per-file size limit, then creates a separate Python environment. It needs
access to PyPI for dependencies, even though it does not download connector
source. These runtimes have the host's OS permissions; process isolation is
not a security sandbox.

Update AI Shuffle to receive a newer connector catalog, then update the runtime
in Plugins. Pause dependent automations and disable connections first. Their
IDs/settings are preserved. Missing, corrupt or oversized shipped files fail
closed without replacing an existing runtime; repair/update AI Shuffle rather
than signing into GitHub. A previous installed runtime can be rolled back.

## Plugin Capabilities

A plugin can contribute any combination of:

| Capability   | Description                                              |
|--------------|----------------------------------------------------------|
| **Hooks**    | Event-driven logic that runs at key points in AI workflow |
| **Commands** | Custom commands available to the system                   |
| **Agents**   | File-based subagents with their own prompts and tools     |
| **Skills**   | Specialized skills that extend Claude's abilities          |

Each plugin in the API response includes boolean flags indicating which capabilities it provides: `has_hooks`, `has_commands`, `has_agents`, `has_skills`.

## Plugin Structure

A plugin is a directory containing a `.claude-plugin/plugin.json` manifest file. The manifest defines the plugin's metadata and declares its contributions.

```
my-plugin/
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── hooks/                 # Hook definitions
│   └── pre-tool-use.sh
├── agents/                # File-based agent definitions
│   └── reviewer.md
├── commands/              # Custom commands
│   └── lint.sh
└── README.md              # Plugin documentation
```

The `plugin.json` manifest includes:

- **name** -- Human-readable plugin name
- **description** -- What the plugin does
- **version** -- Semantic version string
- **scope** -- Installation scope

## Marketplaces

Plugins are distributed through marketplaces, which are git repositories containing one or more plugins.

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
    "name": "My Team Plugins"
  }'
```

The repository is cloned locally. If `name` is omitted, it is derived from the URL.

### Sync a Marketplace

Pull the latest changes from the remote repository:

```bash
curl -X POST http://localhost:8000/api/v1/plugins/marketplaces/{marketplace_id}/sync \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Remove a Marketplace

Removes the marketplace and all its installed plugins:

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

The `plugin_id` is in the format `name@marketplace`. The `scope` can be `"user"` (global) or `"project"` (project-specific).

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

## Per-Profile Plugin Enablement

Beyond the global enable/disable state, plugins are also controlled through profile configuration. The `enabled_plugins` field in a profile's config lists the plugin paths to load for sessions using that profile.

```json
{
  "config": {
    "enabled_plugins": [
      "/path/to/plugin-a",
      "/path/to/plugin-b"
    ]
  }
}
```

This means a plugin must be both globally enabled and listed in the profile's `enabled_plugins` to be active during a session.

## Plugin Hooks

Hooks contributed by plugins are read-only. They appear in the hooks UI with their source plugin identified, but cannot be edited or deleted through the hooks API. They are managed by the plugin itself.

To list all plugin-contributed hooks:

```bash
curl http://localhost:8000/api/v1/hooks/plugins/all \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## File-Based Agents

Plugins can contribute file-based agents that work alongside your subagents. These agents are automatically discovered from enabled plugins.

```bash
curl http://localhost:8000/api/v1/plugins/agents/file-based \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Each file-based agent includes:

- `id` -- Unique identifier
- `name` -- Agent name
- `description` -- What it does
- `model` -- Optional model override
- `tools` -- Optional tool restrictions
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
