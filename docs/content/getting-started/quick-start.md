<!--
title: Quick Start
description: The everyday basics once setup is done
order: 8
-->

# Quick start

These are the basics once [First run](/docs/getting-started/first-run) is done.

## Start a chat

1. Click **New session** at the top of the sidebar.
2. Pick an **agent** and a **model** in the composer.
3. Type your message and press Enter.
4. Watch the reply and the agent's tool use stream in.

You can change the reasoning effort and turn **Fast mode** on or off (where the model supports it) while a chat is running. The composer remembers your picks for each model.

## Create an agent

Agents decide how the AI behaves.

1. Open **Agents** in the sidebar.
2. Click **New agent**.
3. Give it a **name**, write its **prompt** (its instructions), and choose its **tools**.
4. Changes save as you type. Click **Done** to close the editor.

You can also choose **Generate** to create an agent from a short description. The model, effort and permission mode are picked per chat, not per agent. See [Agents](/docs/features/profiles).

## Set up a project

1. Open **Projects** in the sidebar.
2. Click **New project**.
3. Choose the folder with your code.
4. Use the project's tabs to add project-only MCP servers, agents, hooks and memory.

New chats in this project can work with its files. See [Projects](/docs/features/projects).

## Connect a messaging app

1. Follow the setup guide for your platform under [Connectors](/docs/connectors) to create a bot.
2. In AI Shuffle, open **Settings → Plugins**, filter **Apps** and choose the platform.
3. Install it if asked, click **Connect account**, enter the details, choose the **Agent** that answers and click **Create Connector**.
4. Add at least one allowed sender, then turn on **Activate**.

## Create an API key

1. Open **Settings → Integrations**.
2. Click **Add API User**.
3. Give it a name. Optionally limit it to a project and to certain agents.
4. Copy the key. It is shown only once.
5. Send it as `Authorization: Bearer aih_...` with each request.

A key only sees its own chats and notes. See the [API reference](/docs/api-reference).

## Example API call

Use the id of one of your agents (the API calls it `profile`).

```bash
curl -X POST http://localhost:8000/api/v1/query \
  -H "Authorization: Bearer aih_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain the difference between let and const in JavaScript",
    "profile": "your-agent-id"
  }'
```

## Next steps

- **[Connectors](/docs/connectors)**: Telegram, Discord and more.
- **[Automations](/docs/features/automations)**: run agents on a schedule.
- **[Hooks](/docs/features/hooks)**: run your own scripts at key moments.
- **[API reference](/docs/api-reference)**: every endpoint.
