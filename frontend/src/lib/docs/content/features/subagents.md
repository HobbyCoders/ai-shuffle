<!--
title: Subagents
description: Specialized AI agents for specific tasks within sessions
order: 4
-->

# Subagents

Subagents are specialized AI agents that Claude can delegate tasks to during a session. Each subagent has its own system prompt, tool set, and optionally a different model -- making it an expert at a specific type of work.

## Overview

While Claude handles general-purpose tasks, subagents let you define focused specialists. When Claude encounters a task that matches a subagent's description, it can delegate the work to that subagent. This pattern is useful for tasks that benefit from a dedicated system prompt, a restricted tool set, or a more cost-effective model.

Key concepts:

- **Global definition:** Subagents are defined globally and enabled per-profile
- **Built-in and custom:** AI Shuffle ships with built-in subagents; you can create your own
- **Configurable model:** Each subagent can use a different model (haiku for speed, opus for quality)
- **Tool control:** Restrict which tools a subagent can use, or inherit all available tools
- **AI-powered generation:** Describe what you want and let AI generate the subagent config

## Configuration

Each subagent has the following fields:

| Field         | Type          | Required | Description                                                    |
|---------------|---------------|----------|----------------------------------------------------------------|
| `id`          | string        | Yes      | Unique identifier (lowercase, alphanumeric, hyphens only)       |
| `name`        | string        | Yes      | Human-readable name                                             |
| `description` | string        | Yes      | When Claude should use this subagent (used for routing)         |
| `prompt`      | string        | Yes      | System prompt -- the subagent's instructions                    |
| `tools`       | list[string]  | No       | Allowed tools (null = inherit all tools from parent session)    |
| `model`       | string        | No       | Model override: `haiku`, `sonnet`, `opus`, or null (inherit)    |

The `description` field is important -- it tells Claude when to delegate to this subagent. Write it as a clear statement of the subagent's specialty.

The `tools` field controls which tools the subagent can access:

- **null (default):** The subagent inherits all tools available in the parent session
- **Empty list `[]`:** The subagent has no tools (text-only responses)
- **Explicit list:** Only the listed tools are available (e.g., `["Read", "Grep", "Glob"]`)

## Built-in Subagents

AI Shuffle ships with five built-in subagents:

### General Purpose

- **ID:** `general-purpose`
- **Purpose:** Researches complex questions, searches for code, and executes multi-step tasks
- **Tools:** Inherits all tools from the session
- **Model:** Inherits from the session
- **When used:** When Claude needs to search for a keyword or file and isn't confident it will find the match in the first few tries

### Explore

- **ID:** `Explore`
- **Purpose:** Fast, read-only agent specialized for exploring codebases -- finding files by pattern, searching code, and answering questions about the codebase
- **Tools:** `Read`, `Grep`, `Glob`, `Bash`, `Skill`
- **Model:** `claude-haiku-4-5`
- **When used:** When Claude needs a quick, thorough search across the codebase

### Plan

- **ID:** `Plan`
- **Purpose:** Software architect agent that explores the codebase (read-only) and designs step-by-step implementation plans
- **Tools:** `Read`, `Grep`, `Glob`, `Bash`, `Skill`
- **Model:** Inherits from the session
- **When used:** When Claude needs to plan an implementation strategy before writing code

### Frontend Designer

- **ID:** `frontend-designer`
- **Purpose:** Handles all frontend, UI, and UX work -- creating and modifying components, layouts, responsive designs, animations, and design systems
- **Tools:** Inherits all tools from the session
- **When used:** When the task involves anything visual or design-related

### Chat History Summarizer

- **ID:** `chat-history-summarizer`
- **Purpose:** Reads `.jsonl` session history files and produces concise summaries
- **Tools:** `Read`, `Grep`, `Glob`
- **Model:** `claude-haiku-4-5` for fast, cost-effective summarization
- **When used:** When Claude needs to understand the context of a previous conversation

Built-in subagents can be customized (name, description, prompt, tools, model) through the UI or API. If you modify a built-in subagent, you can revert it to its original defaults at any time.

## Custom Subagents

Create your own subagents for any specialized task. Custom subagents are created through the UI, the API, or via a profile.

### Creating via API

```bash
curl -X POST http://localhost:8000/api/v1/subagents \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "code-reviewer",
    "name": "Code Reviewer",
    "description": "Reviews code for bugs, security issues, and best practices. Use when the user asks for a code review or wants feedback on code quality.",
    "prompt": "You are an expert code reviewer. Analyze the provided code thoroughly and provide feedback on:\n1. Bugs and logic errors\n2. Security vulnerabilities\n3. Performance issues\n4. Code style and readability\n5. Best practices\n\nBe specific with line references. Suggest concrete fixes, not just descriptions of problems.",
    "tools": ["Read", "Grep", "Glob"],
    "model": "sonnet"
  }'
```

### Creating via Profile

You can also create a subagent and automatically enable it for a specific profile:

```bash
curl -X POST http://localhost:8000/api/v1/profiles/{profile_id}/agents \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Writer",
    "description": "Writes comprehensive tests for code. Use when the user asks for tests or test coverage.",
    "prompt": "You are an expert test engineer. Write thorough tests that cover edge cases, error conditions, and happy paths. Use the testing framework already in use in the project.",
    "tools": null,
    "model": null
  }'
```

This creates the subagent globally and adds it to the profile's `enabled_agents` list in one step.

## Per-Profile Enablement

Subagents are enabled on a per-profile basis. A profile's `enabled_agents` list contains the IDs of subagents available during sessions using that profile.

### View Enabled Agents

```bash
curl http://localhost:8000/api/v1/profiles/{profile_id}/enabled-agents \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Enable a Subagent

```bash
curl -X POST http://localhost:8000/api/v1/profiles/{profile_id}/enabled-agents/{subagent_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Disable a Subagent

```bash
curl -X DELETE http://localhost:8000/api/v1/profiles/{profile_id}/enabled-agents/{subagent_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Set the Full List

```bash
curl -X PUT http://localhost:8000/api/v1/profiles/{profile_id}/enabled-agents \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "enabled_agents": ["chat-history-summarizer", "code-reviewer", "test-writer"]
  }'
```

## Customizing Built-in Subagents

Built-in subagents can be edited like any other subagent. When a built-in subagent has been modified, it is flagged with `is_modified: true` in the API response.

### Revert to Defaults

To restore a built-in subagent to its original configuration:

```bash
curl -X POST http://localhost:8000/api/v1/subagents/{subagent_id}/revert \
  -H "Authorization: Bearer YOUR_TOKEN"
```

This only works for built-in subagents. Custom subagents cannot be reverted (they have no defaults to revert to).

## Unified Subagent Discovery

When building the autocomplete or agent picker in the UI, AI Shuffle aggregates subagents from four sources with deduplication:

1. **Profile-enabled subagents** (highest priority) -- from the database
2. **Project agents** -- defined in the project's `.claude/agents/` directory
3. **User-global agents** -- defined in `~/.claude/agents/`, auto-loaded by the SDK
4. **Plugin file-based agents** (lowest priority) -- from enabled plugins

```bash
curl "http://localhost:8000/api/v1/profiles/{profile_id}/subagents/all?project_id={project_id}&search=review&limit=30" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Each result includes a `source` field (`profile`, `project`, `user`, or `plugin`) and `source_name` to identify where it came from. Deduplication priority is `profile > project > user > plugin`.

## API Reference

### List All Subagents

```bash
curl http://localhost:8000/api/v1/subagents \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Get a Subagent

```bash
curl http://localhost:8000/api/v1/subagents/{subagent_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### Update a Subagent

```bash
curl -X PUT http://localhost:8000/api/v1/subagents/{subagent_id} \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Updated system prompt for this subagent."
  }'
```

### Delete a Subagent

```bash
curl -X DELETE http://localhost:8000/api/v1/subagents/{subagent_id} \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Built-in subagents cannot be deleted. You can disable them in profile settings instead.

## Practical Examples

### Code Reviewer

An expert reviewer that only has read access:

```json
{
  "id": "code-reviewer",
  "name": "Code Reviewer",
  "description": "Reviews code for bugs, security issues, and best practices. Delegate to this agent when the user asks for a code review.",
  "prompt": "You are a senior code reviewer. Analyze code for:\n- Correctness and logic errors\n- Security vulnerabilities (injection, XSS, CSRF, etc.)\n- Performance bottlenecks\n- Readability and maintainability\n\nProvide specific, actionable feedback with code suggestions.",
  "tools": ["Read", "Grep", "Glob"],
  "model": "sonnet"
}
```

### Test Writer

A fast agent for generating tests:

```json
{
  "id": "test-writer",
  "name": "Test Writer",
  "description": "Writes unit tests, integration tests, and end-to-end tests. Use when the user needs tests or wants to improve test coverage.",
  "prompt": "You are a test engineering specialist. Write comprehensive tests following the testing conventions already used in the project. Cover happy paths, edge cases, error conditions, and boundary values. Aim for high coverage without redundancy.",
  "tools": null,
  "model": null
}
```

### Documentation Generator

A lightweight agent for writing docs:

```json
{
  "id": "doc-generator",
  "name": "Documentation Generator",
  "description": "Generates documentation from code. Use when the user asks for API docs, README updates, or code documentation.",
  "prompt": "You are a technical writer. Generate clear, accurate documentation from the codebase. Include:\n- Function/class descriptions\n- Parameter documentation\n- Usage examples\n- Return value descriptions\n\nMatch the existing documentation style of the project.",
  "tools": ["Read", "Grep", "Glob"],
  "model": "haiku"
}
```
