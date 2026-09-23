<!--
title: Quick Start
description: Get up and running with AI Shuffle in minutes
order: 8
-->

# Quick Start

This guide walks you through the essential workflows once [First run](/docs/getting-started/first-run) is done.

## Start a Chat

1. Click **New Session** (+ button) in the sidebar
2. Select a **Profile** (or use Default) and a **Project**
3. Type your message and press Enter
4. The agent responds with streaming text, and you can see tool usage in real time

## Create a Profile

Profiles control how the agent behaves. To create one:

1. Navigate to **Profiles** in the sidebar
2. Click **Create Profile**
3. Configure:
   - **Name**: a descriptive name
   - **System Prompt**: instructions for the agent
   - **Model**: any model offered by the providers you set up
   - **Allowed Tools**: which tools the agent can use
   - **Thinking Mode**: how deeply the model reasons
4. Save the profile

You can also use **AI Generate** to create a profile from a description.

## Set Up a Project

Projects link to directories on your system:

1. Navigate to **Projects** in the sidebar
2. Click **Create Project**
3. Set the **path** to your code directory
4. Configure default profile and any project-specific settings
5. New sessions created with this project will have access to those files

## Connect a Platform

To receive messages from Telegram, Discord, etc.:

1. Navigate to the connector's setup guide in these docs
2. Create the bot/app on the platform
3. Go to **Settings > Connectors** in AI Shuffle
4. Click **Add Connector**, select the type, and enter credentials
5. Click **Activate** to start receiving messages

## Create an API Key

For programmatic access:

1. Go to **Settings > API Users**
2. Click **Create API User**
3. Set a name and optionally restrict to a project/profile
4. Copy the generated API key (shown only once)
5. Use it in the `Authorization: Bearer aih_...` header

## Example API Call

```bash
# One-shot query
curl -X POST http://localhost:8000/api/v1/query \
  -H "Authorization: Bearer aih_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Explain the difference between let and const in JavaScript",
    "profile": "your-profile-id"
  }'
```

```bash
# Start a conversation
curl -X POST http://localhost:8000/api/v1/conversation/start \
  -H "Authorization: Bearer aih_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Help me refactor this function",
    "profile": "your-profile-id",
    "project": "your-project-id"
  }'
```

## Next Steps

- **[Connectors](/docs/connectors)**: set up Telegram, Discord, and more
- **[Automations](/docs/features/automations)**: schedule AI workflows
- **[API Reference](/docs/api-reference)**: full endpoint documentation
- **[Hooks](/docs/features/hooks)**: custom event handlers
