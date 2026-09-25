<!--
title: Subagents (Folded into Agents)
description: Subagents are now ordinary agents
order: 4
-->

# Subagents (Folded into Agents)

Subagents are no longer a separate concept. There is no more standalone `/api/v1/subagents` API and no dedicated "Subagents" section of the app — everything here now lives under [Agents](/docs/features/profiles).

## What changed

- A former subagent's `prompt` is now an agent's `config.system_prompt.content` (with `config.system_prompt.type` set to `"custom"`).
- A former subagent's `tools` is now `config.allowed_tools`, read together with `config.tool_selection_mode` (`all`, `none`, `allow`, `disallow`). See [Tool Configuration](/docs/features/profiles#tool-configuration).
- A former subagent's `model` is now the agent's `config.provider_models` entry: the model it runs on when another agent dispatches it.
- A former subagent's `memory` is now `config.memory`.
- Delegation from one agent to a specialist during a session still works the same way, but the specialist is now managed through the owning agent's nested-agent endpoints (`/api/v1/agents/{agent_id}/agents`) rather than a standalone router.
- Orchestration and multi-step delegation during a session go through the app's Task tooling, not a separate subagent-dispatch mechanism.

## Where to look now

- [Agents](/docs/features/profiles) — the unified configuration model (tools, prompt, memory, the agents it can call).
- [Agents API reference](/docs/api-reference/agents) — full endpoint list, including the nested per-agent delegate endpoints and a field-by-field migration table for anyone still calling the old `/api/v1/subagents` paths.
