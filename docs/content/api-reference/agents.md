<!--
title: Agents
description: Agent configuration CRUD and nested-agent (formerly subagent) management endpoints
order: 5
-->

# Agents API

> **Renamed from `/profiles`.** As of schema v71 ("agents unification"), this router moved from `/api/v1/profiles` to `/api/v1/agents`. Handlers and response shapes are unchanged — only the mount path moved. There is no alias or redirect: `/api/v1/profiles` paths return `404` as of this release. The standalone `/api/v1/subagents` router was removed in the same release; see [Former `/subagents` API](#former-subagents-api) below.

Endpoints for managing agents — configuration containers that control Claude's behavior, prompt, and tool access. Every session runs under an agent.

An agent does not pin a model: the model, reasoning effort, and permission mode are chosen when a session is created and stored on the session, not on the agent.

## Agent CRUD

### GET /api/v1/agents

List all agents. API users only see their assigned agents.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/agents \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "id": "default",
    "name": "Default",
    "description": "Default agent",
    "is_builtin": true,
    "is_modified": false,
    "session_count": 3,
    "config": {
      "model": "sonnet",
      "system_prompt": {
        "type": "preset",
        "preset": "claude_code"
      },
      "permission_mode": "default",
      "thinking_mode": "default",
      "allowed_tools": [],
      "enabled_plugins": [],
      "enabled_agents": [],
      "memory": null
    },
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:00:00Z"
  }
]
```

### GET /api/v1/agents/{agent_id}

Get a specific agent.

### POST /api/v1/agents

Create a new agent.

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Code Reviewer",
    "description": "Reviews code for quality and bugs",
    "config": {
      "model": "opus",
      "system_prompt": {
        "type": "custom",
        "content": "You are an expert code reviewer. Focus on bugs, security issues, and code quality."
      },
      "permission_mode": "plan",
      "thinking_mode": "think_harder",
      "allowed_tools": ["Read", "Grep", "Glob"]
    }
  }'
```

If `id` is omitted, it is derived from the name.

### PUT /api/v1/agents/{agent_id}

Update an existing agent. All agents (including built-ins) are editable.

**Auth:** Admin only

```bash
curl -X PUT http://localhost:8000/api/v1/agents/agent_abc123 \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Updated Name",
    "config": {
      "system_prompt": {
        "type": "custom",
        "content": "Updated instructions..."
      }
    }
  }'
```

### DELETE /api/v1/agents/{agent_id}

Delete an agent. Built-in agents cannot be deleted.

**Auth:** Admin only

### POST /api/v1/agents/{agent_id}/restore

Restore a built-in agent to its original seed defaults (name, description, and config). Only works for built-in agents with known defaults; custom agents have no defaults to restore to. No agent ships as built-in today, so this currently applies to nothing.

**Auth:** Admin only

## Agent Config Fields

The top-level object has `id`, `name`, `description`, `is_builtin`, `is_modified`, `session_count`, `created_at`, `updated_at`, and a `config` object. The fields below live under `config`:

| Field | Type | Description |
|-------|------|-------------|
| `system_prompt` | object | `{type: "preset"\|"custom", preset, content, append, inject_env_details}` |
| `thinking_mode` | string | `default`, `think`, `think_harder`, `ultra_think` |
| `provider_models` | object | Model to run this agent on when another agent dispatches it as a subagent, keyed by provider (`__native__` for Claude). The one model binding an agent still owns |
| `allowed_tools` | string[] | Tool whitelist (empty/null = all tools allowed) |
| `disallowed_tools` | string[] | Tool blacklist |
| `enabled_plugins` | string[] | Plugin paths to enable |
| `enabled_agents` | string[] | IDs of the agents declared into this agent's context — they appear in the prompt's agents index as name + description. This is not an allowlist: the Task tool dispatches any agent that resolves. Read together with `agent_invoke_mode` |
| `agent_invoke_mode` | string | Context-declaration mode: `all` (declare every agent), `custom` (declare exactly `enabled_agents` — an empty list declares none), or omitted/null (legacy: a non-empty `enabled_agents` declares those ids, an empty/absent one declares all) |
| `memory` | string | Durable per-agent memory text, injected into the compiled system prompt on every run |
| `setting_sources` | string[] | Config sources: `user`, `project`, `local` |
| `cwd` | string | Override working directory |
| `add_dirs` | string[] | Additional accessible directories |

`model`, `permission_mode` and `effort` are no longer agent fields. They are
accepted on write and returned on read for backward compatibility, and an agent
that still carries them uses them only as a fallback for sessions created before
those settings moved onto the session row (a top-level `model` also still seeds
the agent's native subagent binding). The model behind internal one-shot
generation is a separate setting — see `GET/PUT /api/v1/settings/text-generation`.


## Nested Agents (formerly "subagents")

Each agent can delegate to its own nested agents during a session — specialists with their own prompt, tool restrictions, and model. These are a distinct, flatter object shape (not a `config` sub-object); the fields below are exactly what the API returns and accepts today.

### GET /api/v1/agents/{agent_id}/agents

Get the full nested-agent objects enabled for this agent.

**Response:**
```json
[
  {
    "id": "chat-history-summarizer",
    "name": "Chat History Summarizer",
    "description": "Reads and summarizes .jsonl chat history files",
    "prompt": "You are a chat history summarizer...",
    "tools": ["Read", "Grep", "Glob"],
    "model": "haiku",
    "skills": null,
    "memory": null,
    "mcp_servers": null,
    "is_builtin": true,
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:00:00Z"
  }
]
```

### GET /api/v1/agents/{agent_id}/subagents/all

Get all available nested agents for autocomplete, aggregated from multiple sources: this agent's enabled agents, project agents, user-global agents (`~/.claude/agents/`), and plugin file-based agents. Deduplication priority: agent > project > user > plugin.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | string | Include project-specific agents |
| `worktree_id` | string | Include worktree-specific agents (overrides project path) |
| `search` | string | Filter by name/description |
| `limit` | integer | Max results (1-50, default 30) |

Each result includes `source` (`profile`, `project`, `user`, or `plugin` — the `profile` value is legacy and still returned as-is) and `source_name`.

### POST /api/v1/agents/{agent_id}/agents

Create a new nested agent and enable it for this agent.

**Auth:** Admin only

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Nested agent display name (also used to derive its id) |
| `description` | string | Yes | When to use this nested agent (shown to Claude) |
| `prompt` | string | Yes | System prompt for the nested agent |
| `tools` | string[] | No | Allowed tools (null = inherit all from parent) |
| `model` | string | No | Model override, or null to inherit |

```bash
curl -X POST http://localhost:8000/api/v1/agents/agent_abc123/agents \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Writer",
    "description": "Writes tests. Use when the user asks for tests or coverage.",
    "prompt": "You are an expert test engineer...",
    "tools": null,
    "model": null
  }'
```

### PUT /api/v1/agents/{agent_id}/agents/{agent_name}

Update a nested agent. `agent_name` may be the nested agent's id or name.

**Auth:** Admin only

### DELETE /api/v1/agents/{agent_id}/agents/{agent_name}

Delete a nested agent and remove it from the owning agent's enabled list.

**Auth:** Admin only

## Enabled Agents Management

### GET /api/v1/agents/{agent_id}/enabled-agents

Get the list of enabled nested-agent IDs.

### PUT /api/v1/agents/{agent_id}/enabled-agents

Replace the entire enabled agents list.

**Auth:** Admin only

```bash
curl -X PUT http://localhost:8000/api/v1/agents/default/enabled-agents \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"enabled_agents": ["chat-history-summarizer", "my-custom-agent"]}'
```

### POST /api/v1/agents/{agent_id}/enabled-agents/{subagent_id}

Enable a single nested agent.

**Auth:** Admin only

### DELETE /api/v1/agents/{agent_id}/enabled-agents/{subagent_id}

Disable a single nested agent.

**Auth:** Admin only

## Export & Import

### GET /api/v1/agents/{agent_id}/export

Export an agent as JSON.

```bash
curl http://localhost:8000/api/v1/agents/agent_abc123/export \
  -H "Authorization: Bearer aih_your_key" \
  -o agent.json
```

### POST /api/v1/agents/import

Import an agent from JSON. Creates a new agent; a unique id is generated if `new_id` is omitted or conflicts.

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/agents/import \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d @agent.json
```

## Former `/subagents` API

Before v71, global subagents also had their own standalone router: `GET/POST /api/v1/subagents`, `GET/PUT/DELETE /api/v1/subagents/{subagent_id}`, and `POST /api/v1/subagents/{subagent_id}/revert`. That router was deleted with no alias — all of those paths now return `404`.

What replaced it:

- **Managing a nested delegate the way you always did** (create/update/delete a specialist prompt+tools+model bundle that a parent agent can hand off to): unchanged in shape, just addressed through the owning agent now — use `POST`/`PUT`/`DELETE /api/v1/agents/{agent_id}/agents[/{agent_name}]` above instead of the old top-level `/api/v1/subagents` endpoints.
- **Listing or fetching a subagent outside of any agent's context**: no longer exposed directly. List via `GET /api/v1/agents/{agent_id}/agents` (or the aggregated `/subagents/all`) for a specific agent instead.
- **Reverting a built-in subagent's customizations**: no direct equivalent. `POST /api/v1/agents/{agent_id}/restore` resets the owning agent's entire config (including its `enabled_agents` list) to seed defaults; there is no per-nested-agent revert. To reset a single nested agent, delete and recreate it.
- **Promoting a former subagent into its own independent, top-level agent**: create it via `POST /api/v1/agents` with the old fields mapped into `config`:

  | Old subagent field | New location |
  |---------------------|--------------|
  | `prompt` | `config.system_prompt.content` (with `config.system_prompt.type = "custom"`) |
  | `tools` | `config.allowed_tools` |
  | `model` | `config.model` |
  | `memory` | `config.memory` |
