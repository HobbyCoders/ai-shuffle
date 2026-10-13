<!--
title: Overview
description: What AI Shuffle is and what it can do
order: 1
-->

# AI Shuffle

AI Shuffle is a self-hosted workspace for AI agents, made by HobbyCoders. It runs on your own machine and uses your own AI account: Claude, OpenAI, OpenRouter, xAI Grok or a custom gateway, with a subscription sign-in or an API key. AI Shuffle includes no AI usage.

New here? Start with [Requirements](/docs/getting-started/requirements) and [First run](/docs/getting-started/first-run).

## What you can do

### Chat with agents

- Watch replies and tool use stream in as the agent works.
- Pick the model, reasoning effort and permission mode for each chat.
- Search, tag and fork chats. Chats you are working in stay in **Active** until you settle them. Settling dismisses a pending prompt.
- Share files, generate images and media, and work in separate git worktrees.

### Agents

Agents are saved setups for how the AI behaves: their instructions, the tools they may use and the other agents they can call. Agents keep their own memory across chats. See [Agents](/docs/features/profiles).

### Projects

Projects point at folders on your machine. Browse and upload files, use git and branches, and give each project its own agents, hooks, MCP servers and memory. See [Projects](/docs/features/projects).

### Automations

Run agents on a schedule, at an interval, from a webhook or when a connector event arrives. Add budgets, approval steps and places to send the output. See [Automations](/docs/features/automations).

### Hooks

Run your own script or call a webhook at key moments, for example before a tool runs (and block it if you want). See [Hooks](/docs/features/hooks).

### MCP servers and plugins

Give agents new tools by adding MCP servers, or install plugins that bundle skills, commands, agents and servers. See [MCP servers](/docs/features/mcp-servers) and [Plugins](/docs/features/plugins).

### Connectors

Let an agent answer messages on Telegram, Discord, WhatsApp, Slack, Signal and iMessage, or give it tools for Gmail, Spotify, Philips Hue, Home Assistant and more. See [Connectors](/docs/connectors).

### Voice

Talk to a chat in a live voice call. See [Voice mode](/docs/features/voice).

### Several machines, one app

Connect several workers so they share settings, keys and chats. See [Worker network](/docs/features/worker-network).

### API

Use the REST API with API keys for your own scripts and integrations. See the [API reference](/docs/api-reference).
