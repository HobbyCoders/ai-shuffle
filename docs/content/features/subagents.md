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
- A Task worker started from an API key's session runs as that key (its agents, tools and credentials) and cannot use an agent outside the key's assigned agents. See [API Users](/docs/api-reference/api-users#own-sessions).

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

## Session organization

When asked to organize chats, agents can use **SettleSession** to move a chat out
of Active, **UnsettleSession** to put a settled chat back in Active, and
**ArchiveSession** to archive it without deleting the conversation. Each takes an
exact `session_id`, or a `session_ids` list of up to 500 chats in one call; the
current chat is also allowed. `ArchiveSession(all_settled=true)` archives every
settled chat in the project (or everywhere with `scope="all"`) without listing
first, never including the current chat. Bulk calls check each chat on its own,
keep going past refusals, and return counts plus the refused or failed chats.
These are write tools and use the same permission gates and sidebar updates as
the app's controls.

`ListSessions(state="settled")` finds settled chats. `state="archived"` finds
archives, and `state="all"` includes both, plus active and stopped chats. These
management views include same-owner chats on other workers; they do not make
those chats eligible for messaging. Project scope and pagination still apply.

None of these actions starts or stops work, clears a user stop, or deletes a
transcript. Settling and unsettling work even when the execution worker is
offline; archiving requires that worker to be reachable. Archived chats must be
restored in the app before they can be unsettled. Hidden, voice and subagent
chats are excluded, and persistent personal assistant chats cannot be settled or
archived. Other users' chats are never accessible.

## Where to look now

- [Agents](/docs/features/profiles) — the unified configuration model (tools, prompt, memory, the agents it can call).
- [Agents API reference](/docs/api-reference/agents) — full endpoint list, including the nested per-agent delegate endpoints and a field-by-field migration table for anyone still calling the old `/api/v1/subagents` paths.
