<!--
title: Voice Mode
description: Talk to an agent that hands the work to your other agents
order: 10
-->

# Voice Mode

Voice mode puts a spoken conversation on top of your agents. You talk; the voice agent listens, answers, and hands real work to the agents you have configured. It runs on OpenAI Realtime over a direct browser connection, so audio never passes through the AI Shuffle server.

## What the voice agent is

The voice agent is a **coordinator**, not a coder. It never edits files, runs commands, or reads a codebase itself. When you ask for anything substantive it dispatches one of your agents as a background worker, tells you it has done so, and lets you know when the worker finishes. In between, the conversation keeps going.

It can do the things you would expect of an assistant you talk to:

- **Dispatch work** to any agent that has a voice model assigned (see below), by name or by letting it pick.
- **Steer running work.** If you change your mind mid-task ("actually make the toggle default to dark"), it sends a follow-up to the worker that is already on it instead of starting a second one. A running worker gets the message mid-work; a finished worker resumes with its full context.
- **Stop a worker** when you cancel.
- **Check on progress** and report what landed.
- **Relay permission prompts.** When a worker needs approval for a tool, the voice agent reads the request to you and acts only on your spoken yes or no.
- **Manage the app**: create and change automations, hooks, agents, MCP servers and connectors during the call. This is always available to the owner, but never to API-key users. It says what it is about to do first and waits for a yes before anything destructive.

## Setup

1. **Add an OpenAI API-key provider** under Settings → Providers. Realtime is API-key only; a ChatGPT-subscription login cannot fund calls.
2. **Turn on voice mode** under Settings → Voice and pick the provider, model, and voice. Press play next to the voice dropdown to hear a sample.
3. **Give at least one agent a voice model.** Open the agent, go to *Provider models*, and set the **Voice** row. This is the model (native Claude, or a model on any configured provider) the agent runs on when the voice agent hands it work. An agent with no voice model is never offered in a call.

## Starting a call

With voice mode on, every new chat shows a call button next to the send button. Press it to create a voice session and start the call; the agent greets you once it is connected. On an existing voice session the same button reopens the call with the conversation so far as context.

The agent you had selected when you started the call is the default worker: "just get an agent on it" goes to that agent when it has a voice model, otherwise to the alphabetically first eligible agent. Ask for another agent by name at any time.

During a call the composer is replaced by the call dock, which shows the agent's state (listening, hearing you, speaking), the elapsed time, how many agents are working, and an estimated spend. The three controls are stop, mute your mic, and mute the agent's audio. The transcript is written to the session as it happens, so it stays readable like any other chat.

Hanging up ends the call only. Dispatched workers keep running and their results stay in the session.

## Settings reference

| Setting | What it does |
| --- | --- |
| Enable voice mode | Shows the call button in new chats. |
| Provider | Which OpenAI API-key provider funds calls. Automatic when only one qualifies. |
| Model | The speech-to-speech model. Billed per minute of conversation. |
| Voice | How the agent sounds. |
| Max call minutes | Total length cap for one call. OpenAI's own 60-minute session wall is bridged automatically. |

## API

- `GET /api/v1/settings/voice`, `PUT /api/v1/settings/voice` — the settings above.
- `GET /api/v1/voice/providers` — provider rows eligible to fund calls.
- `GET /api/v1/voice/agents` — agents with a voice model, and the leg each runs on.
- `POST /api/v1/voice/sessions` — create a voice session and mint the browser's short-lived credential.
- `POST /api/v1/voice/sessions/{id}/secret`, `/call`, `/hangup`, `GET .../state` — the call lifecycle.
