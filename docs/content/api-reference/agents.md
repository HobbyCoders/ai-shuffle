<!--
title: Agents
description: Agent configuration CRUD and nested-agent (formerly subagent) management endpoints
order: 5
-->

# Agents API

> **Renamed from `/profiles`.** Agent endpoints live at `/api/v1/agents`. Response shapes did not change. The old `/api/v1/profiles` and `/api/v1/subagents` paths return `404`; see [Former `/subagents` API](#former-subagents-api) below.

Endpoints for managing agents — configurations that control an agent's behavior, prompt, and tool access. Sessions may run without a saved agent. Paths and fields that say `profile` hold an agent id.

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
    "id": "assistant",
    "name": "Assistant",
    "description": "General-purpose agent",
    "is_builtin": false,
    "is_modified": false,
    "session_count": 3,
    "config": {
      "system_prompt": {
        "type": "custom",
        "content": "You are a helpful assistant."
      },
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

### GET /api/v1/agents/templates

Legacy system-prompt template endpoint. Returns an empty list now that no built-in agents ship. Use the opt-in team endpoints below instead.

**Auth:** Required

Returns a list of `{"id", "name", "description", "prompt", "tools"}`.

### GET /api/v1/agents/team-templates

List the single available opt-in team and which members already exist.

**Auth:** Owner only

Returns `[{"id": "starter-team", "name": "Starter team", "description": "…", "agents": [{"key": "general-purpose", "name": "General Purpose", "existing_agent_id": null}, {"key": "frontend-designer", "name": "Frontend Designer", "existing_agent_id": null}]}]`. Installed members have their ordinary agent ID in `existing_agent_id`.

### POST /api/v1/agents/templates/starter-team/install

Install General Purpose and Frontend Designer as ordinary editable, deletable owner agents (`is_builtin: false`) with fresh IDs. No request body. Returns the same template object with the installed IDs.

**Auth:** Owner only

Idempotent: creates only missing members. Existing team members are recognized even after renaming, and an ordinary agent with the same name is left untouched. Reinstallation does not reset prompts, model assignments or other owner edits. Removing a member allows a later install to recreate it with a new ID, without inheriting old API-key grants.

### GET /api/v1/agents/{agent_id}

Get a specific agent.

### POST /api/v1/agents

Create a new agent.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Code Reviewer",
    "description": "Reviews code for quality and bugs",
    "config": {
      "system_prompt": {
        "type": "custom",
        "content": "You are an expert code reviewer. Focus on bugs, security issues, and code quality."
      },
      "thinking_mode": "think_harder",
      "tool_selection_mode": "allow",
      "allowed_tools": ["Read", "Grep", "Glob"]
    }
  }'
```

If `id` is omitted, it is derived from the name (lowercase letters, digits and hyphens).

Optional top-level `avatar_color` sets the colour the Agents page shows for the agent. It is one of `primary`, `emerald`, `rose`, `blue` or `warning` (the app's accent token names, so each theme draws its own shade), or `null` to let the page choose. Any other value returns `422`.

### PUT /api/v1/agents/{agent_id}

Update an existing owner agent, including agents installed from the starter team.

**Auth:** Owner only

```bash
curl -X PUT http://localhost:8000/api/v1/agents/agent_abc123 \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Updated Name",
    "description": "Updated description"
  }'
```

`config` replaces the whole stored config. It is not merged: any field you leave out is removed. To change one setting, read the agent with `GET /api/v1/agents/{agent_id}`, change that field, and send the complete `config` object back. `name` and `description` can be sent on their own.

`avatar_color` can be sent on its own too. Send a colour name to set it, `null` to clear it, or leave the key out to keep the current colour.

### DELETE /api/v1/agents/{agent_id}

Delete an owner agent, including an agent installed from the starter team. Historical sessions are preserved.

**Auth:** Owner only

### POST /api/v1/agents/{agent_id}/restore

Legacy built-in restore endpoint. No built-in subagents ship. Ordinary agents, including starter-team agents, have no managed defaults and return `400`.

**Auth:** Owner only

## Agent Config Fields

The top-level object has `id`, `name`, `description`, `avatar_color`, `is_builtin`, `is_modified`, `session_count`, `created_at`, `updated_at`, and a `config` object. `avatar_color` is `null` when no colour was chosen. The fields below live under `config`:

| Field | Type | Description |
|-------|------|-------------|
| `system_prompt` | object | `{type: "preset"\|"custom", preset, content, append, inject_env_details}` |
| `thinking_mode` | string | `default`, `disabled`, `adaptive`, `think`, `think_harder`, `ultra_think` |
| `provider_models` | object | The model this agent is pinned to when another agent dispatches it as a subagent, keyed by the **caller's** provider (`__native__` for Claude, `__voice__` for voice calls). Values are a Claude alias or a `prov:<providerId>:<modelId>` composite and may name any configured provider. A missing key means the agent runs on the [subagent default](/docs/api-reference/miscellaneous#get-apiv1settingssubagent-default); with no default set, the start is refused. Voice calls still dispatch only agents with a `__voice__` entry |
| `tool_selection_mode` | string | `all`, `none`, `allow`, or `disallow` — decides what the two lists below mean. `all` and `none` carry no list |
| `allowed_tools` | string[] | The tool surface in `allow` mode. Exact names or `fnmatch` patterns (a whole MCP server saves as `mcp__<server>__*`) |
| `disallowed_tools` | string[] | The block list in `disallow` mode. Same entry shapes |
| `enabled_plugins` | string[] | Plugin paths to enable |
| `enabled_agents` | string[] | The agents this one may call when `agent_invoke_mode` is `custom` |
| `agent_invoke_mode` | string | Who it can call: `all` (every other agent), `custom` (exactly `enabled_agents`; an empty list calls none of the listed agents), `none` (no subagents: the Task and Swarm tools are not offered and a Task call is refused), or omitted/null (legacy: a non-empty `enabled_agents` means those ids, an empty/absent one means all). Any other value is rejected with 422 |
| `memory` | string | Legacy per-agent memory text from old exports. Accepted on import for compatibility, but no longer injected anywhere — memory is the continual learner plus session recall |
| `setting_sources` | string[] | Config sources: `user`, `project`, `local` |
| `cwd` | string | Override working directory |
| `add_dirs` | string[] | Additional accessible directories |

`model`, `permission_mode` and `effort` are no longer agent fields. They are accepted on write and returned on read for backward compatibility, and an agent that still carries them uses them only as a fallback for sessions created before those settings moved onto the session row (a top-level `model` also still seeds the agent's native subagent binding). The model behind internal one-shot generation is a separate setting — see `GET/PUT /api/v1/settings/text-generation`.

The tool names that may appear in `allowed_tools` / `disallowed_tools` are not a fixed set. `GET /api/v1/tools/surface` discovers them live from the running registries — harness builtins, the app's bridged servers, AI tools, browser, computer use, app configuration, and every configured MCP server — grouped, with a `discovered_at` stamp and an `errors` entry for any server that failed to answer. Add `?refresh=true` to bust the per-server enumeration cache. The agent editor's Tools tab is exactly that response; nothing anywhere hardcodes a list.

`ai_tools` and `app_config_tools` are legacy: those tools are now granted through the same list as everything else. Both fields are still accepted so older exports import cleanly.


## Nested Agents (formerly "subagents")

Each agent can delegate to its own nested agents during a session — specialists with their own prompt, tool restrictions, and model. These are a distinct, flatter object shape (not a `config` sub-object); the fields below are exactly what the API returns and accepts today.

A worker session that a session started with an API key hands work to (with the Task tool, or from a voice call) runs as that key: with the key's tools and credentials, never the owner's. A worker on an agent outside the key's assigned agents is refused with an error the calling agent sees. See [API Users](/docs/api-reference/api-users#own-sessions).

### GET /api/v1/agents/{agent_id}/agents

Get the full nested-agent objects enabled for this agent.

**Response:**
```json
[
  {
    "id": "general-purpose",
    "name": "General Purpose",
    "description": "Coding agent for backend and general implementation work",
    "prompt": "You are a coding subagent for AI Shuffle...",
    "tools": null,
    "model": null,
    "skills": null,
    "memory": null,
    "mcp_servers": null,
    "is_builtin": false,
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:00:00Z"
  }
]
```

### GET /api/v1/agents/{agent_id}/subagents/all

Get all available nested agents for autocomplete, aggregated from multiple sources: this agent's enabled agents, project agents, user-global agents (`~/.fluid/agents/`), and plugin file-based agents. Deduplication priority: agent > project > user > plugin.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | string | Include project-specific agents |
| `worktree_id` | string | Include worktree-specific agents (overrides project path) |
| `search` | string | Filter by name/description |
| `limit` | integer | Max results (1-50, default 30) |

Each result includes `source` (`profile`, `project`, `user`, or `plugin` — the `profile` value is legacy and still returned as-is) and `source_name`.

An API key limited to certain agents gets `403` for any other `agent_id`, and a key limited to one project gets `403` for another `project_id`. A `worktree_id` that is not a worktree of `project_id` is ignored.

### POST /api/v1/agents/{agent_id}/agents

Create a new nested agent and enable it for this agent.

**Auth:** Owner only

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Nested agent display name (also used to derive its id) |
| `description` | string | Yes | When to use this nested agent (shown to the parent agent) |
| `prompt` | string | Yes | System prompt for the nested agent |
| `tools` | string[] | No | Allowed tools (null = inherit all from parent). Names come from `GET /api/v1/tools/surface` |
| `model` | string | No | Model override, or null to inherit |

```bash
curl -X POST http://localhost:8000/api/v1/agents/agent_abc123/agents \
  -H "Authorization: Bearer $OWNER_TOKEN" \
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

**Auth:** Owner only

### DELETE /api/v1/agents/{agent_id}/agents/{agent_name}

Delete a nested agent and remove it from the owning agent's enabled list.

**Auth:** Owner only

## Enabled Agents Management

### GET /api/v1/agents/{agent_id}/enabled-agents

Get the list of enabled nested-agent IDs.

### PUT /api/v1/agents/{agent_id}/enabled-agents

Replace the entire enabled agents list.

**Auth:** Owner only

```bash
curl -X PUT http://localhost:8000/api/v1/agents/agent_abc123/enabled-agents \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled_agents": ["general-purpose", "my-custom-agent"]}'
```

### POST /api/v1/agents/{agent_id}/enabled-agents/{subagent_id}

Enable a single nested agent.

**Auth:** Owner only

### DELETE /api/v1/agents/{agent_id}/enabled-agents/{subagent_id}

Disable a single nested agent.

**Auth:** Owner only

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

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/agents/import \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d @agent.json
```

## Generate with AI

Draft an agent or a nested agent from a plain-language description. Nothing is saved: the result has the export format, which you can pass to `POST /api/v1/agents/import` or edit first.

**Auth:** Required for every generate endpoint.

The **Generation model** picker in the agent and sub-agent builders is a per-request choice. Leave it on **Use Settings default** for the text-generation model and effort configured in Settings. Selecting a model binds both its provider and model for this generation, uses that model's default effort, and does not save anything to Settings or change the generated delegate's model assignment. The same choice is sent when answering clarification questions.

API callers can send a native alias (such as `opus`) or a provider-qualified `prov:<providerId>:<modelId>` in `model`. Omit it (or send an empty value) to use Settings. Send the same binding to `/continue` to retain it through clarification.

### POST /api/v1/generate/profile

Draft an agent. When the description is vague, the answer may be questions instead of a result.

```bash
curl -X POST http://localhost:8000/api/v1/generate/profile \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"description": "Reviews pull requests for security problems and explains each finding"}'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `description` | string | Yes | 10 to 5000 characters |
| `reference_material` | string | No | Up to 15000 characters, such as an existing prompt to build on. When given, no questions are asked |
| `model` | string | No | Native alias or `prov:<providerId>:<modelId>` for this request; omitted uses Settings (see `GET /api/v1/models`) |
| `skip_clarification` | boolean | No | `true` never asks questions. Default `false` |

```json
{
  "status": "needs_clarification",
  "result": null,
  "questions": [{ "id": "scope", "question": "...", "options": ["..."], "context": "..." }],
  "generation_type": "profile"
}
```

With `status: "complete"`, `result` is `{"version": "1.0", "type": "ai-shuffle-agent", "exported_at": "...", "agent": {...}}` and `questions` is `null`.

### POST /api/v1/generate/subagent

Draft a nested agent. Same body and response as `POST /api/v1/generate/profile`. A complete `result` has `type: "ai-shuffle-subagent"` and a `subagent` object with `id`, `name`, `description`, `prompt`, and optional `tools`, `model`, `skills`, `memory` and `mcp_servers`.

### POST /api/v1/generate/continue

Finish a draft after answering the questions. Always returns `status: "complete"`.

```json
{
  "generation_type": "profile",
  "description": "Reviews pull requests for security problems and explains each finding",
  "answers": { "scope": "Only the changed files" }
}
```

`generation_type` (`profile` or `subagent`), `description` and `answers` (question `id` to answer) are required; `reference_material` and `model` are optional as above. Any other `generation_type` returns `400`.

### POST /api/v1/generate/profile/direct

Draft an agent without questions. Same body as `POST /api/v1/generate/profile`; returns the export object itself (not wrapped in `status` and `result`).

### POST /api/v1/generate/subagent/direct

Draft a nested agent without questions. Same body as `POST /api/v1/generate/subagent`; returns the nested-agent export object itself.

## Former `/subagents` API

Previously, global subagents also had their own standalone router: `GET/POST /api/v1/subagents`, `GET/PUT/DELETE /api/v1/subagents/{subagent_id}`, and `POST /api/v1/subagents/{subagent_id}/revert`. That router was deleted with no alias — all of those paths now return `404`.

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
