<!--
title: Subagents (Folded into Agents)
description: Subagents were unified into the Agents model in schema v71
order: 4
-->

# Subagents (Folded into Agents)

As of schema v71 ("agents unification"), subagents are no longer a separate top-level concept. There is no more standalone `/api/v1/subagents` API and no dedicated "Subagents" section of the app — everything here now lives under [Profiles](/docs/features/profiles) (the config-container concept, referred to as "Agents" in the API and UI).

## What changed

- A former subagent's `prompt` is now an agent's `config.system_prompt.content` (with `config.system_prompt.type` set to `"custom"`).
- A former subagent's `tools` is now `config.allowed_tools`, read together with `config.tool_selection_mode` (`all`, `none`, `allow`, `disallow`). The names that may go in that list are not a fixed set: `GET /api/v1/tools/surface` discovers them live from the running registries, and the agent editor's Tools tab shows exactly what it returns.
- A former subagent's `model` is now `config.model`.
- A former subagent's `memory` is now `config.memory`.
- Delegation from one agent to a specialist during a session still works the same way, but the specialist is now managed through the owning agent's nested-agent endpoints (`/api/v1/agents/{agent_id}/agents`) rather than a standalone router.
- Orchestration and multi-step delegation during a session go through the app's Task tooling, not a separate subagent-dispatch mechanism.

## Where to look now

- [Profiles](/docs/features/profiles) — the unified configuration model (model, tools, prompt, memory, permissions), referred to as "Agents" in the API and UI.
- [Agents API reference](/docs/api-reference/agents) — full endpoint list, including the nested per-agent delegate endpoints and a field-by-field migration table for anyone still calling the old `/api/v1/subagents` paths.
