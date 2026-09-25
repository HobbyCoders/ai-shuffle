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

## Choosing a Task worker model

Task shows each eligible agent's default model. When the target runs on a
configured model provider, Task also offers an optional `model` argument drawn
from that provider's user-maintained model list. Omit it to keep the agent's
default; select another listed model to use it for that worker only. Swarm tasks
accept the same optional `model` and pass it through Task's validation.

A model choice does not change providers. If an agent is configured to run on a
different provider, the choice must belong to that target provider's list, not
the parent's. Dispatch rechecks the list before launching, so removed models,
disabled providers, and agents outside the caller's allowed roster are refused.
File/plugin agents use the caller's provider. Native Claude targets without a
configured provider list retain their assigned model and do not accept this
override. This does not change the parent chat's model or save a new default.

## Independent top-level orchestration

Agents can coordinate relevant work without a parent/subagent relationship:

- **ListSessions** discovers active (open, including idle) top-level chats owned
  by the same user on this worker. It returns titles, projects, models and status,
  not conversation contents. The default scope is the current project; `all`
  permits cross-project discovery. Follow `next_offset` to paginate.
- **MessageSession** queues an attributed peer message using an exact discovered
  session ID. A peer can reply with the same tool. Delivery is asynchronous:
  `queued` is not a delivery or completion acknowledgement.
- **StartSession** creates a visible, independent chat with a self-contained peer
  brief. It inherits the caller's agent, model/provider, permissions, project and
  workspace, but not its transcript, private access grants or subagent lifecycle.
  Coordinate file ownership or select a separate branch before concurrent edits.

These tools do not require an explicit request to use subagents. Models should
judge relevance, prefer existing peers when appropriate, and avoid courtesy
acknowledgements or message loops. They remain subject to the agent's tool policy
and normal write permission gates. Peer text is not a user instruction and never
permits bypassing a denial or delegating prohibited work.

Stopped, archived, hidden, voice, subagent, other-owner and other-worker sessions
are not eligible. Starting independent sessions is available only from eligible
top-level chats. No automatic parent completion notification is registered: ask a
peer to report back with MessageSession when needed.

Use a stable `request_id` for each start/message and reuse it on retries. Durable
limits bound each autonomous chain to **4 new sessions, 32 total actions and 8
hops**; only new user input starts a fresh budget. Existing `Task`, `Swarm`,
`ListAgents` and `SendMessage` retain their parent/subagent semantics.

## Where to look now

- [Profiles](/docs/features/profiles) — the unified configuration model (model, tools, prompt, memory, permissions), referred to as "Agents" in the API and UI.
- [Agents API reference](/docs/api-reference/agents) — full endpoint list, including the nested per-agent delegate endpoints and a field-by-field migration table for anyone still calling the old `/api/v1/subagents` paths.
