<!--
title: Profiles
description: Profile configuration CRUD and agent management endpoints
order: 5
-->

# Profiles API

Endpoints for managing profiles — configuration containers that control Claude's behavior, tool access, and model selection.

## Profile CRUD

### GET /api/v1/profiles

List all profiles.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/profiles \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "id": "default",
    "name": "Default",
    "description": "Default profile",
    "model": "claude-sonnet-4-5-20250929",
    "system_prompt": "You are a helpful assistant.",
    "permission_mode": "default",
    "thinking_mode": "default",
    "allowed_tools": [],
    "enabled_plugins": [],
    "enabled_agents": [],
    "created_at": "2025-01-01T00:00:00Z"
  }
]
```

### GET /api/v1/profiles/{profile_id}

Get a specific profile.

### POST /api/v1/profiles

Create a new profile.

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/profiles \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Code Reviewer",
    "description": "Reviews code for quality and bugs",
    "model": "claude-opus-4-6",
    "system_prompt": "You are an expert code reviewer. Focus on bugs, security issues, and code quality.",
    "permission_mode": "plan",
    "thinking_mode": "think_harder",
    "allowed_tools": ["Read", "Grep", "Glob"]
  }'
```

### PUT /api/v1/profiles/{profile_id}

Update an existing profile.

**Auth:** Admin only

```bash
curl -X PUT http://localhost:8000/api/v1/profiles/prof_abc123 \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Updated Name",
    "system_prompt": "Updated instructions..."
  }'
```

### DELETE /api/v1/profiles/{profile_id}

Delete a profile.

**Auth:** Admin only

## Profile Configuration Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Display name |
| `description` | string | Description of the profile's purpose |
| `model` | string | Claude model ID (e.g., claude-sonnet-4-5-20250929) |
| `system_prompt` | string | Instructions for Claude |
| `permission_mode` | string | default, acceptEdits, plan, bypassPermissions |
| `thinking_mode` | string | default, think, think_harder, ultra_think |
| `allowed_tools` | string[] | Tool whitelist (empty = all tools allowed) |
| `enabled_plugins` | string[] | Plugin paths to enable |
| `enabled_agents` | string[] | Subagent IDs to enable |
| `setting_sources` | string[] | Config sources: user, project, local |
| `cwd` | string | Override working directory |
| `add_dirs` | string[] | Additional accessible directories |

## Profile Agents

### GET /api/v1/profiles/{profile_id}/agents

Get full subagent objects enabled for this profile.

### GET /api/v1/profiles/{profile_id}/subagents/all

Get all available subagents from multiple sources.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | string | Include project-specific agents |
| `worktree_id` | string | Include worktree-specific agents |
| `search` | string | Filter by name/description |
| `limit` | integer | Max results |

### POST /api/v1/profiles/{profile_id}/agents

Create a new subagent for this profile.

**Auth:** Admin only

### PUT /api/v1/profiles/{profile_id}/agents/{agent_name}

Update a profile's subagent.

**Auth:** Admin only

### DELETE /api/v1/profiles/{profile_id}/agents/{agent_name}

Delete a profile's subagent.

**Auth:** Admin only

## Enabled Agents Management

### GET /api/v1/profiles/{profile_id}/enabled-agents

Get the list of enabled subagent IDs.

### PUT /api/v1/profiles/{profile_id}/enabled-agents

Replace the entire enabled agents list.

**Auth:** Admin only

```bash
curl -X PUT http://localhost:8000/api/v1/profiles/default/enabled-agents \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"agent_ids": ["chat-history-summarizer", "my-custom-agent"]}'
```

### POST /api/v1/profiles/{profile_id}/enabled-agents/{subagent_id}

Enable a single subagent.

**Auth:** Admin only

### DELETE /api/v1/profiles/{profile_id}/enabled-agents/{subagent_id}

Disable a single subagent.

**Auth:** Admin only

## Export & Import

### GET /api/v1/profiles/{profile_id}/export

Export a profile as JSON.

```bash
curl http://localhost:8000/api/v1/profiles/prof_abc123/export \
  -H "Authorization: Bearer aih_your_key" \
  -o profile.json
```

### POST /api/v1/profiles/import

Import a profile from JSON.

**Auth:** Admin only

```bash
curl -X POST http://localhost:8000/api/v1/profiles/import \
  -H "Authorization: Bearer aih_your_key" \
  -F "file=@profile.json"
```
