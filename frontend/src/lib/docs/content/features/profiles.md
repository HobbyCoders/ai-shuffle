<!--
title: Agents
description: Agents are the configurations that control an AI's behavior and capabilities
order: 6
-->

# Agents

An agent is a configuration that controls how the AI behaves during a session. Each agent defines the system prompt, available tools, the other agents it can call, plugins, memory, and other settings. Switch between agents to get different behaviors from the same AI Shuffle instance. Manage them in **Build → Agents**.

The API and some field names still say "profile": the endpoints are `/api/v1/agents`, but ids are passed as `profile_id` and `{profile_id}`.

An agent does **not** pin a model. The model, reasoning effort, and permission mode belong to the session: you pick them in the composer when you start a chat, and they stay with that chat (changeable mid-conversation) no matter which agent it runs. That way the same agent can run cheap on one conversation and expensive on the next.

## Overview

Every session uses an agent. Agents let you create specialized configurations for different use cases -- a coding assistant, a code reviewer, a chatbot, a documentation writer -- each with its own personality, permissions, and toolset.

Key concepts:

- **A starter agent** ("Assistant") is created by the setup wizard's first run if you have none. It is an ordinary agent: rename, edit or delete it
- **Every agent is yours**: nothing ships as a protected built-in
- **Generate** creates an agent from a description
- **Export/import** agents as JSON for sharing across instances
- **Per-agent roster and plugins** for fine-grained control

## Agent Configuration

An agent contains a `config` object with the following settings:

### Core Settings

| Field              | Type   | Default    | Description                                    |
|--------------------|--------|------------|------------------------------------------------|
| `max_turns`        | int    | null       | Optional model-call cap; null means unlimited    |
| `provider_models`  | dict   | null       | Model this agent runs on when another agent dispatches it, per provider, including the Voice row (the editor's **Agents** tab). A provider left out keeps the agent hidden from callers on that provider |
| `memory`           | string | null       | Durable notes this agent keeps across sessions; the agent writes them with its Memory tool |

`model`, `permission_mode` and `effort` are session settings, not agent settings. Agents saved by older versions may still carry them; they are used only as a fallback for sessions created before the move.

Text the app generates for you outside a chat — session titles, commit messages, PR descriptions, branch names, generated agents — runs on its own model and effort, set once under **Settings → Providers → Generated text**.

The vocabularies below describe what you can pick per session.

#### Models

| Value    | Description                                    |
|----------|------------------------------------------------|
| `haiku`  | Claude Haiku (fast, cheap)                     |
| `sonnet` | Claude Sonnet (balanced, default)              |
| `opus`   | Claude Opus (powerful)                         |
| `fable`  | Claude Fable (most capable)                    |

Any other string is treated as a custom model ID and passed through unchanged (e.g. `claude-sonnet-4-5`). Older stored full model IDs (`claude-sonnet-4-6`, `claude-opus-4-8`, etc.) are still recognized: the app displays them as the matching shorthand and rewrites them to it the next time the agent is saved from the UI.

#### Permission Modes

| Mode                | In the composer | Description                                                 |
|---------------------|-----------------|-------------------------------------------------------------|
| `default`           | Ask             | Ask for permission on potentially destructive operations     |
| `auto`              | Auto            | Claude Code evaluates tool calls automatically (shown only where available) |
| `acceptEdits`       | Accept Edits    | Automatically accept file edits, ask for other operations    |
| `bypassPermissions` | Bypass          | Skip all permission prompts (use with caution)               |

#### Effort

Available on current Sonnet, Opus and Fable models (not Haiku). Pick it per session from the composer, which only lists the levels the selected model supports:

| Level    | Description                                   |
|----------|-----------------------------------------------|
| `low`    | Quick responses, minimal reasoning             |
| `medium` | Balanced reasoning and speed                   |
| `high`   | Thorough reasoning                             |
| `max`    | Maximum reasoning depth                        |
| `xhigh`  | Highest reasoning depth (not on every model)   |

### Tool Configuration

| Field                 | Type          | Description                                                          |
|-----------------------|---------------|----------------------------------------------------------------------|
| `tool_selection_mode` | string        | `all`, `none`, `allow`, or `disallow` — decides what the lists mean   |
| `allowed_tools`       | list[string]  | The surface in `allow` mode; ignored in every other mode              |
| `disallowed_tools`    | list[string]  | The block list in `disallow` mode; ignored in every other mode        |
| `tools`               | list[string]  | LEGACY, no longer written                                              |
| `tools_preset`        | string        | LEGACY, no longer written                                              |

`tool_selection_mode` is the authority, not the shape of the lists:

| Mode       | What the agent gets                                        |
|------------|------------------------------------------------------------|
| `all`      | Every tool. No list is stored.                              |
| `none`     | No tools at all. No list is stored.                         |
| `allow`    | Only `allowed_tools`. An empty list means no tools.         |
| `disallow` | Everything except `disallowed_tools`.                       |

**The tool list is discovered live, never hardcoded.** `GET /api/v1/tools/surface` enumerates every tool a session can be given, straight from the running registries: the built-in tools, the app's bridged servers, the AI tools, the browser, computer use, app configuration, and each configured MCP server. The agent editor's Tools tab is that inventory — a new built-in tool shows up there on the next refresh, with no list to update anywhere.

Entries in either list are exact tool names (`Read`, `mcp__github__create_issue`) or `fnmatch` patterns. Selecting a whole MCP server in the editor saves `mcp__<server>__*`, so tools added to that server later are covered too; unchecking one of its tools expands the pattern back to explicit names.

`ai_tools` and `app_config_tools` are no longer separate switches. The AI tools and app-configuration tools are permitted through the same list as everything else; both fields survive only so older exports still import.

### System Prompt

The `system_prompt` field holds the agent's instructions (the editor's **Prompt** tab):

```json
{
  "system_prompt": {
    "type": "custom",
    "content": "You are a senior code reviewer. Focus on security, performance, and readability."
  }
}
```

### Session Behavior

| Field                     | Type | Default | Description                                    |
|---------------------------|------|---------|------------------------------------------------|
| `include_partial_messages`| bool | false   | Stream partial messages during generation       |

### Working Directory and Paths

| Field      | Type          | Description                                           |
|------------|---------------|-------------------------------------------------------|
| `cwd`      | string        | Override the working directory for sessions             |
| `add_dirs` | list[string]  | Additional directories Claude can access                |

### Settings Loading

| Field            | Type          | Description                                       |
|------------------|---------------|---------------------------------------------------|
| `setting_sources`| list[string]  | Which config sources to load: `user`, `project`, `local` |

Controls which Claude Code configuration files are loaded. By default, all sources are used. Set this to restrict which settings apply.

### Environment

| Field        | Type              | Description                             |
|--------------|-------------------|-----------------------------------------|
| `env`        | dict[string, string] | Environment variables for the session |

### Agent Roster and Plugins

| Field              | Type          | Description                                     |
|--------------------|---------------|-------------------------------------------------|
| `enabled_agents`   | list[string]  | Agent IDs declared into this agent's context (name + description in the prompt's agents index). Never a dispatch restriction — read with `agent_invoke_mode` |
| `agent_invoke_mode`| string        | `all` (declare every agent), `custom` (declare exactly `enabled_agents`; empty = declare none), or null (legacy: a non-empty `enabled_agents` declares those ids, empty/absent declares all). The Task tool can dispatch any agent that resolves by id or name, declared or not |
| `enabled_plugins`  | list[string]  | Extra plugins (`name@marketplace` or a path) to load for this agent's sessions, on top of the globally enabled ones |

### Advanced

| Field                       | Type | Description                                   |
|-----------------------------|------|-----------------------------------------------|
| `app_config_tools`          | bool | LEGACY. The app-configuration tools are now granted through the tool list like any others (owner sessions only); kept so older exports still import |

## API Reference

### List All Agents

```bash
curl http://localhost:8000/api/v1/agents \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get an Agent

```bash
curl http://localhost:8000/api/v1/agents/{profile_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Create an Agent

```bash
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Code Reviewer",
    "description": "Reviews code for quality, security, and best practices",
    "config": {
      "system_prompt": {
        "type": "custom",
        "content": "You are a senior code reviewer. Analyze code for bugs, security issues, performance problems, and style violations. Provide specific, actionable feedback."
      },
      "tool_selection_mode": "allow",
      "allowed_tools": ["Read", "Grep", "Glob"]
    }
  }'
```

If `id` is omitted, it is automatically derived from the name (e.g., "Code Reviewer" becomes `code-reviewer`).

### Update an Agent

```bash
curl -X PUT http://localhost:8000/api/v1/agents/{profile_id} \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Senior Code Reviewer",
    "description": "Updated description"
  }'
```

`config` is a full replacement, not a merge: any field you leave out is removed. To change one setting, fetch the agent (`GET /api/v1/agents/{profile_id}`), edit that field, and send the whole `config` back.

### Delete an Agent

```bash
curl -X DELETE http://localhost:8000/api/v1/agents/{profile_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Export and Import

### Export an Agent

Download an agent as a JSON file for sharing:

```bash
curl http://localhost:8000/api/v1/agents/{profile_id}/export \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -o my-agent.json
```

The export includes all configuration except system-specific paths (cwd, add_dirs, plugin paths). Sensitive data is sanitized.

The exported JSON has this structure:

```json
{
  "version": "1.0",
  "type": "ai-shuffle-agent",
  "exported_at": "2026-02-16T12:00:00Z",
  "agent": {
    "name": "Code Reviewer",
    "description": "Reviews code for quality",
    "system_prompt_content": "You are a senior code reviewer...",
    "tool_selection_mode": "allow",
    "allowed_tools": ["Read", "Grep", "Glob"]
  }
}
```

### Import an Agent

Create a new agent from exported JSON:

```bash
curl -X POST http://localhost:8000/api/v1/agents/import \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "version": "1.0",
    "type": "ai-shuffle-agent",
    "agent": {
      "name": "Imported Reviewer",
      "description": "Imported code reviewer agent",
      "system_prompt_content": "You are a senior code reviewer..."
    },
    "new_name": "My Code Reviewer",
    "new_id": "my-code-reviewer"
  }'
```

Both `new_name` and `new_id` are optional. If omitted, the original name and a generated ID are used. If the ID conflicts with an existing agent, a numeric suffix is appended automatically.

## Practical Examples

Pick the model, effort and permission mode in the composer when you start a chat with each of these.

### Coding Assistant

A general-purpose coding agent with full tool access:

```json
{
  "name": "Coding Assistant",
  "description": "Full-featured coding assistant with all tools",
  "config": {
    "system_prompt": {
      "type": "custom",
      "content": "Prefer writing clean, well-documented code. Always run tests after making changes."
    },
    "tool_selection_mode": "all"
  }
}
```

### Code Reviewer (Read-Only)

A review-focused agent that cannot modify files, because it only has read tools:

```json
{
  "name": "Code Reviewer",
  "description": "Reviews code without making changes",
  "config": {
    "system_prompt": {
      "type": "custom",
      "content": "You are a thorough code reviewer. Analyze code for correctness, security, performance, and style. Never make changes directly -- only provide feedback and suggestions."
    },
    "tool_selection_mode": "allow",
    "allowed_tools": ["Read", "Grep", "Glob"]
  }
}
```

### Chatbot (No Tools)

A conversational agent with no tool access:

```json
{
  "name": "Chatbot",
  "description": "Conversational assistant without code tools",
  "config": {
    "system_prompt": {
      "type": "custom",
      "content": "You are a helpful conversational assistant. Answer questions clearly and concisely."
    },
    "tool_selection_mode": "none"
  }
}
```
