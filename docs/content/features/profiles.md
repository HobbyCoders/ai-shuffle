<!--
title: Profiles
description: Configuration containers that control Claude's behavior and capabilities
order: 6
-->

# Profiles

Profiles are configuration containers that control how Claude behaves during a session. Each profile defines the system prompt, available tools, enabled subagents, plugins, memory, and other settings. Switch between profiles to get different behaviors from the same AI Shuffle instance.

A profile does **not** pin a model. The model, reasoning effort, and permission mode belong to the session: you pick them in the composer when you start a chat, and they stay with that chat (changeable mid-conversation) no matter which profile it runs. That way the same profile can run cheap on one conversation and expensive on the next.

## Overview

Every session uses a profile. Profiles let you create specialized configurations for different use cases -- a coding assistant, a code reviewer, a chatbot, a documentation writer -- each with its own personality, permissions, and toolset.

Key concepts:

- **A starter profile** is created when an install has none, so there is always
  something to chat with. It is an ordinary profile — rename, edit or delete it
- **Every profile is yours**: nothing ships as a protected built-in
- **AI-powered generation** to create profiles from a description
- **Export/import** profiles as JSON for sharing across instances
- **Per-profile subagents and plugins** for fine-grained control

## Profile Configuration

A profile contains a `config` object with the following settings:

### Core Settings

| Field              | Type   | Default    | Description                                    |
|--------------------|--------|------------|------------------------------------------------|
| `thinking_mode`    | string | `"default"`| Extended thinking behavior (Sonnet/Haiku)       |
| `max_turns`        | int    | null       | Optional model-call cap; null means unlimited    |

`model`, `permission_mode` and `effort` are session settings, not profile
settings. Profiles saved by older versions may still carry them; they are used
only as a fallback for sessions created before the move.

Text the app generates for you outside a chat — session titles, commit
messages, PR descriptions, branch names, generated agents — runs on its own
model and effort, set once under **Settings → Providers → Generated text**.

The vocabularies below describe what you can pick per session.

#### Models

| Value    | Description                                    |
|----------|------------------------------------------------|
| `haiku`  | Claude Haiku (fast, cheap)                     |
| `sonnet` | Claude Sonnet (balanced, default)              |
| `opus`   | Claude Opus (powerful)                         |
| `fable`  | Claude Fable (most capable)                    |

Any other string is treated as a custom model ID and passed through unchanged
(e.g. `claude-sonnet-4-5`). Older stored full model IDs (`claude-sonnet-4-6`,
`claude-opus-4-8`, etc.) are still recognized: the app displays them as the
matching shorthand and rewrites them to it the next time the profile is saved
from the UI.

#### Permission Modes

| Mode                | Description                                                 |
|---------------------|-------------------------------------------------------------|
| `default`           | Ask for permission on potentially destructive operations     |
| `auto`              | Auto-approve operations without prompting                    |
| `acceptEdits`       | Automatically accept file edits, ask for other operations    |
| `plan`              | Plan-only mode -- Claude explains what it would do           |
| `bypassPermissions` | Skip all permission prompts (use with caution)               |

#### Thinking Mode (Sonnet / Haiku)

Controls Claude's extended thinking behavior. More thinking produces better results for complex tasks but uses more tokens.

| Mode           | Description                                          |
|----------------|------------------------------------------------------|
| `default`      | Standard thinking behavior                            |
| `disabled`     | Turn off extended thinking                            |
| `adaptive`     | Let the model decide how much to think               |
| `think`        | Enable extended thinking                              |
| `think_harder` | More thorough extended thinking                       |
| `ultra_think`  | Maximum extended thinking (highest token usage)        |

#### Effort (Opus 4.6+)

For Opus 4.6 and later models, effort-based reasoning replaces thinking modes.
Pick it per session from the composer:

| Level    | Description                                   |
|----------|-----------------------------------------------|
| `low`    | Quick responses, minimal reasoning             |
| `medium` | Balanced reasoning and speed                   |
| `high`   | Thorough reasoning                             |
| `max`    | Maximum reasoning depth                        |
| `xhigh`  | Highest reasoning depth (Opus 4.7+ only)       |

### Tool Configuration

| Field              | Type          | Description                                      |
|--------------------|---------------|--------------------------------------------------|
| `allowed_tools`    | list[string]  | Only allow these tools (whitelist)                |
| `disallowed_tools` | list[string]  | Block these tools (blacklist)                     |
| `tools`            | list[string]  | Explicit tool list (SDK 0.1.12+)                  |
| `tools_preset`     | string        | Use `"claude_code"` preset for standard tools      |

### System Prompt

The `system_prompt` field configures Claude's instructions:

```json
{
  "system_prompt": {
    "type": "custom",
    "content": "You are a senior code reviewer. Focus on security, performance, and readability.",
    "inject_env_details": false
  }
}
```

Or use a preset with an appended instruction:

```json
{
  "system_prompt": {
    "type": "preset",
    "preset": "claude_code",
    "append": "Always write tests for any code you create."
  }
}
```

### Session Behavior

| Field                     | Type | Default | Description                                    |
|---------------------------|------|---------|------------------------------------------------|
| `include_partial_messages`| bool | false   | Stream partial messages during generation       |
| `continue_conversation`   | bool | false   | Continue the most recent conversation           |
| `fork_session`            | bool | false   | Fork instead of continuing when resuming        |

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

### Environment and Arguments

| Field        | Type              | Description                             |
|--------------|-------------------|-----------------------------------------|
| `env`        | dict[string, string] | Environment variables for the session |
| `extra_args` | dict              | Additional CLI arguments                 |
| `user`       | string            | User identifier                          |

### Subagents and Plugins

| Field              | Type          | Description                                     |
|--------------------|---------------|-------------------------------------------------|
| `enabled_agents`   | list[string]  | Agent IDs declared into this profile's context (name + description in the prompt's agents index). Never a dispatch restriction — read with `agent_invoke_mode` |
| `agent_invoke_mode`| string        | `all` (declare every agent), `custom` (declare exactly `enabled_agents`; empty = declare none), or null (legacy: a non-empty `enabled_agents` declares those ids, empty/absent declares all). The Task tool can dispatch any agent that resolves by id or name, declared or not |
| `enabled_plugins`  | list[string]  | Plugin paths to load for this profile            |

### Advanced

| Field                       | Type | Description                                   |
|-----------------------------|------|-----------------------------------------------|
| `max_buffer_size`           | int  | Maximum bytes when buffering CLI stdout        |
| `enable_file_checkpointing` | bool | Enable file snapshots for rewind (SDK 0.1.15+) |
| `app_config_tools`          | bool | Let the agent manage hooks, automations, MCP servers, and profiles through conversation (admin sessions only) |
| `strict_mcp_config`         | bool | Lock the MCP server set for the session; reject mid-session changes (SDK 0.1.74+) |

## API Reference

### List All Profiles

```bash
curl http://localhost:8000/api/v1/agents \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get a Profile

```bash
curl http://localhost:8000/api/v1/agents/{profile_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Create a Profile

```bash
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Code Reviewer",
    "description": "Reviews code for quality, security, and best practices",
    "config": {
      "model": "sonnet",
      "permission_mode": "plan",
      "system_prompt": {
        "type": "custom",
        "content": "You are a senior code reviewer. Analyze code for bugs, security issues, performance problems, and style violations. Provide specific, actionable feedback."
      },
      "allowed_tools": ["Read", "Grep", "Glob"],
      "enabled_agents": ["code-reviewer"]
    }
  }'
```

If `id` is omitted, it is automatically derived from the name (e.g., "Code Reviewer" becomes `code-reviewer`).

### Update a Profile

```bash
curl -X PUT http://localhost:8000/api/v1/agents/{profile_id} \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Updated description",
    "config": {
      "model": "opus",
      "thinking_mode": "think"
    }
  }'
```

### Delete a Profile

```bash
curl -X DELETE http://localhost:8000/api/v1/agents/{profile_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Export and Import

### Export a Profile

Download a profile as a JSON file for sharing:

```bash
curl http://localhost:8000/api/v1/agents/{profile_id}/export \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -o my-profile.json
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
    "model": "sonnet",
    "permission_mode": "plan",
    "system_prompt_type": "custom",
    "system_prompt_content": "You are a senior code reviewer...",
    "allowed_tools": ["Read", "Grep", "Glob"],
    "enabled_agents": ["code-reviewer"]
  }
}
```

### Import a Profile

Create a new profile from exported JSON:

```bash
curl -X POST http://localhost:8000/api/v1/agents/import \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "version": "1.0",
    "type": "ai-shuffle-agent",
    "agent": {
      "name": "Imported Reviewer",
      "description": "Imported code reviewer profile",
      "model": "sonnet",
      "permission_mode": "plan",
      "system_prompt_type": "custom",
      "system_prompt_content": "You are a senior code reviewer..."
    },
    "new_name": "My Code Reviewer",
    "new_id": "my-code-reviewer"
  }'
```

Both `new_name` and `new_id` are optional. If omitted, the original name and a generated ID are used. If the ID conflicts with an existing profile, a numeric suffix is appended automatically.

## Practical Examples

### Coding Assistant

A general-purpose coding profile with full tool access:

```json
{
  "name": "Coding Assistant",
  "description": "Full-featured coding assistant with all tools",
  "config": {
    "model": "sonnet",
    "permission_mode": "acceptEdits",
    "thinking_mode": "think",
    "system_prompt": {
      "type": "preset",
      "preset": "claude_code",
      "append": "Prefer writing clean, well-documented code. Always run tests after making changes."
    },
    "enabled_agents": ["chat-history-summarizer"]
  }
}
```

### Code Reviewer (Read-Only)

A review-focused profile that cannot modify files:

```json
{
  "name": "Code Reviewer",
  "description": "Reviews code without making changes",
  "config": {
    "model": "opus",
    "effort": "high",
    "permission_mode": "plan",
    "system_prompt": {
      "type": "custom",
      "content": "You are a thorough code reviewer. Analyze code for correctness, security, performance, and style. Never make changes directly -- only provide feedback and suggestions."
    },
    "allowed_tools": ["Read", "Grep", "Glob"],
    "enabled_agents": ["code-reviewer"]
  }
}
```

### Chatbot (No Tools)

A conversational profile with no tool access:

```json
{
  "name": "Chatbot",
  "description": "Conversational assistant without code tools",
  "config": {
    "model": "haiku",
    "permission_mode": "default",
    "system_prompt": {
      "type": "custom",
      "content": "You are a helpful conversational assistant. Answer questions clearly and concisely."
    },
    "allowed_tools": []
  }
}
```
