<!--
title: Quick Start
description: Get up and running with AI Shuffle in minutes
order: 8
-->

# Quick Start

This guide walks you through the essential workflows once [First run](/docs/getting-started/first-run) is done.

## Start a Chat

1. Click **New session** at the top of the sidebar
2. Pick an **agent** and a **model** in the composer
3. Type your message and press Enter
4. The agent responds with streaming text, and you can see tool usage in real time

## Create an Agent

Agents control how the AI behaves. To create one:

1. Open **Build > Agents** in the sidebar
2. Click **New agent**
3. Configure:
   - **Name**: a descriptive name
   - **Prompt**: instructions for the agent
   - **Tools**: which tools the agent can use
4. Save the agent

The model, reasoning effort and permission mode are picked per session in the composer, not stored on the agent. You can also choose **Generate** to create an agent from a description.

## Set Up a Project

Projects link to directories on your system:

1. Navigate to **Projects** in the sidebar
2. Click **New project**
3. Set the **Path** to your code directory
4. Use the project's tabs (MCP Servers, Agents, Hooks, Memory) for project-specific settings
5. New sessions created with this project will have access to those files

## Connect a Platform

To receive messages from Telegram, Discord, etc.:

1. Navigate to the connector's setup guide in these docs
2. Create the bot/app on the platform
3. Open **Build > Plugins > Apps** in AI Shuffle and choose the platform
4. Install it if asked, click **Connect account**, enter credentials, choose the **Agent** that answers its messages and click **Create Connector**
5. Chat connectors need at least one allowed sender. Add yours, then turn on **Activate** to start receiving messages

## Create an API Key

For programmatic access:

1. Go to **Settings > API**
2. Click **Add API User**
3. Set a name and optionally restrict it to a project and to specific agents
4. Copy the generated API key (shown only once)
5. Use it in the `Authorization: Bearer aih_...` header

## Example API Call

Use the id of one of your agents. API fields still say `profile`.

```bash
# One-shot query
curl -X POST http://localhost:8000/api/v1/query \
  -H "Authorization: Bearer aih_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain the difference between let and const in JavaScript",
    "profile": "your-agent-id"
  }'
```

```bash
# Start a conversation
curl -X POST http://localhost:8000/api/v1/conversation/start \
  -H "Authorization: Bearer aih_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Help me refactor this function",
    "profile": "your-agent-id",
    "project": "your-project-id"
  }'
```

## Next Steps

- **[Connectors](/docs/connectors)**: set up Telegram, Discord, and more
- **[Automations](/docs/features/automations)**: schedule AI workflows
- **[API Reference](/docs/api-reference)**: full endpoint documentation
- **[Hooks](/docs/features/hooks)**: custom event handlers
