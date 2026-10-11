<!--
title: Agents
description: Agents decide how the AI behaves, which tools it can use and which other agents it can call
order: 6
-->

# Agents

An agent is a saved setup for the AI: its instructions, the tools it may use, the other agents it can call, its plugins and its memory. Switch agents to get different behavior from the same install. A coding assistant, a code reviewer and a plain chatbot can each be their own agent.

Manage agents on the **Agents** page in the sidebar. Click an agent to edit it.

An agent does not fix the model. You pick the model, reasoning effort and permission mode in the composer for each chat, and you can change them mid-conversation. The same agent can run on a cheap model in one chat and a powerful one in the next.

## Optional starter team

AI Shuffle ships no built-in subagents. Create your own, install agents from plugins, or choose **Install starter team** on the **Agents** page to add:

- **General Purpose** for most coding and research work.
- **Frontend Designer** for visual and UI work.

The starter team is opt-in. Its agents are ordinary owner-owned agents: you can rename, edit or delete them, and later updates do not reset your changes. Installing again adds only missing members; existing team members (including renamed ones) and ordinary agents with the same names are left alone. The header offers the team when neither member exists; the empty state offers it too.

Choose a subagent default model or a model assignment before delegating. With no available agents, Task and Swarm refuse delegation instead of inventing a built-in fallback.

On upgrade, unchanged built-in General Purpose and Frontend Designer agents are retired. **Edited legacy built-ins are kept as ordinary owner agents**, with the same IDs, names, prompts, models, tools and settings. Their chats, automations, API-key scopes and saved agent selections stay attached; even renamed roles are preserved. If the app cannot prove a row matches a known shipped default, it keeps it rather than risking owner data. Installing the starter team recognizes these preserved roles by ID and adds only missing roles without overwriting them.

For unchanged agents that are retired, historical chats remain with the agent reference cleared. Affected automations are preserved but disabled and unlinked: choose an agent before enabling them again. Existing API-key restrictions and explicit nested-agent selections are not transferred to fresh starter IDs; reassign them where needed. Explore and Plan retain their earlier retirement behavior. Upgrade older workers too. Incoming customized built-in updates are treated as ordinary owner-agent edits, in their original sync order, so edits made before a peer upgrades are not lost. Incoming untouched shipped defaults remain blocked; an upgrade itself never gets a fresh edit clock. Permission-only customizations catch up through a signed state copy, so the preserved agent arrives before its rules without replacing newer owner edits.

## Create an agent

1. Open **Agents** and choose **New agent**.
2. Give it a name and a short description.
3. Write its instructions in the **Prompt** section.
4. Choose its tools, the agents it may call, and any plugins.
5. Save. It now appears in the composer's agent picker.

**Generate** writes an agent for you from a short description. You can then edit the result.

## What you can set

- **Prompt:** the agent's instructions, for example "You are a careful code reviewer. Point out bugs and security problems."
- **Tools:** which tools the agent can use. See [Tool configuration](#tool-configuration).
- **Subagents:** which other agents it can call: all of them, only the ones you select, or none.
- **Plugins:** extra plugins loaded only for this agent, on top of the ones turned on for everyone.
- **Models:** the model this agent runs on when another agent calls it, per provider.
- **Memory:** notes the agent keeps for itself across chats.
- **Working folder and extra folders:** where its chats start, and other folders it may use.
- **Environment variables** for its chats.
- **Turn limit:** an optional cap on how many steps one reply can take.

## Tool configuration

The **Tools** section lists every tool the agent could use, including the tools from your MCP servers. Pick one of four modes:

| Mode | The agent gets |
| --- | --- |
| All | Every tool |
| None | No tools. Good for a plain chatbot |
| Allow | Only the tools you tick |
| Disallow | Every tool except the ones you tick |

Ticking a whole MCP server also covers tools that server adds later.

A read-only reviewer, for example, uses **Allow** with only the read and search tools ticked.

## Permission modes

The permission mode decides when the agent asks you before using a tool. Pick it in the composer under **Tool access**. You can change it while the agent is working, and it applies at once.

| Mode | What happens |
| --- | --- |
| Ask | Tools that only read run straight away. Tools that change something ask you first |
| Auto | Your provider checks each tool call and approves safe ones. Only on Claude models |
| Accept Edits | File edits run without asking. Everything else asks, as in Ask |
| Bypass | Nothing asks. Use with care |

Chat sessions always use Ask. Code and voice sessions show the picker.

**What a shell can reach.** An agent's shell commands run as the same user as AI Shuffle on that worker. A command you approve, or any command in Bypass, can read what AI Shuffle itself can read there, including its database and the key for your saved credentials. Owner-only features such as terminals do not limit that. On a Docker worker you can turn on [agent isolation](/docs/getting-started/install-docker-compose#keep-agents-away-from-ai-shuffles-own-keys-optional): shells then still run as that user, but AI Shuffle's database and keys belong to a separate account they cannot read. Use Bypass only with work and sources you trust, and keep Ask when an agent reads untrusted web pages or repositories.

**Answering a prompt:**

- Choose **Approve** or **Deny**. On a keyboard, Enter approves and Escape denies.
- A prompt nobody answers within 5 minutes is denied, and the agent carries on.
- Tick **Always do this for** *tool* to save your answer for that agent in every chat. Only do this for tools you always want handled the same way.

**Runs with nobody to ask**, such as API queries and automation **Send prompt** steps, follow the mode alone. Anything that would ask is denied. Give them a mode that allows the tools they need.

### Screen control

Computer use (screen control) always asks the first time in each chat, in every mode, including Bypass. After you allow it, later requests in that chat follow the mode. Stopping the chat or restarting the worker resets it.

On a computer nobody works at, such as a virtual machine set aside for agents, you can turn on **Settings → Security → Only agents use this computer**. Screen control then follows the mode from the first request. Leave it off on a computer you use yourself.

## Reasoning effort

On models that support it, pick how hard the model thinks in the composer: **low**, **medium**, **high**, and on some models **xhigh** and **max**. Higher effort is slower and uses more tokens.

## Text the app writes for you

Session titles, commit messages, pull request descriptions, branch names and generated agents use their own model. Set it once under **Settings → Default models → Text tasks**.

## Export and import

- **Export** downloads an agent as a JSON file to share or keep. Paths that only make sense on your machine are left out.
- **Import** creates a new agent from such a file. You can rename it as you import.

To manage agents from your own scripts, see the [Agents API](/docs/api-reference/agents).
