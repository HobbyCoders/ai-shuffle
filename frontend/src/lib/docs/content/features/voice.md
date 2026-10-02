<!--
title: Voice Mode
description: Start voice calls and understand how each engine handles audio and tasks
order: 10
-->

# Voice Mode

Voice mode adds a spoken conversation to a chat session. New calls use **GPT Live**: audio passes through the AI Shuffle server, and task requests run through the chat model you select. Existing sessions can retain **OpenAI Realtime** or **PersonaPlex** bindings, which handle audio, delegation, and transcripts differently.

This guide describes AI Shuffle's integration, not a guarantee that a model is available on your provider account.

## Setup

1. **Add an OpenAI API-key provider** under Settings → Providers. GPT Live and Realtime require an enabled provider with a stored API key; a ChatGPT-subscription login cannot fund their calls. AI Shuffle selects the voice provider automatically for new calls.
2. **Turn on Voice calls** under Settings → Voice. Choose a voice, set **Max call length**, and adjust **Call prompts** if needed. The play button beside each voice generates a GPT Live sample using your OpenAI API account.
3. **Choose the chat model for tasks** in the voice session's composer controls, along with its effort and permissions. GPT Live uses these selections for client delegation. You do not need to assign a per-agent Voice model for this path.

Settings → Voice currently configures GPT Live for new calls; it does not offer an engine or provider dropdown. The **Dictation** settings on the same page, including the **Voice input hotkey**, control dictation into the composer, not the voice call engine. Existing sessions keep their saved voice configuration when settings change.

## Starting a call

Select **Voice** in the session mode switcher and press **Start a voice call** on the new-session screen. On an existing voice session, use the composer button labelled **Start a call on this voice session** to reconnect. The call button is not offered in every ordinary text chat.

During a call, the call dock shows connection and speaking state, elapsed time, and controls to stop the call, mute your mic, or mute the agent's audio. Realtime calls can also show a worker count and an estimated call spend; GPT Live and PersonaPlex do not show those Realtime indicators. GPT Live waits for you to speak unless opening words are configured.

Mic mute stops outgoing microphone frames and discards partial buffered samples on streaming calls; it does not retract audio or tasks already sent before you muted. Muting the agent's audio only silences playback, not your microphone. Typed messages can still be sent while your mic is muted.

During a GPT Live app call, you can send composer text and attachments to the same chat model without ending the call. The composer waits for the voice server to accept the request before clearing your draft. Rejected or unconfirmed requests retain the draft; check the conversation before retrying an unconfirmed send. PersonaPlex has no text/attachment delegation in this integration: end its call to send your draft in chat.

Hanging up ends the audio connection. Realtime workers and GPT Live tasks already submitted to the chat can continue; stop the work separately if you want to cancel it.

## How the engines differ

### GPT Live

The browser streams microphone audio to AI Shuffle over a WebSocket. The server connects to OpenAI and sends response audio back to the browser. This is a server-owned audio transport, not a direct browser-to-OpenAI call.

With **client delegation**, task requests are submitted to the **same chat session**, using its selected model, agent instructions, conversation, tools, effort, and approval rules. The handoff does not create a background worker or use the Realtime agent roster. Task progress and results can be relayed into the voice conversation.

The backend also retains a **Responses delegation** path for saved GPT Live session configurations. OpenAI handles those requests using a model on the call's OpenAI API provider. AI Shuffle exposes **no app tools** to that backend and does not also dispatch a local chat turn for the same delegation. This is not the current settings UI's task-model selector; new calls use client delegation and the composer selections.

GPT Live input and output transcript events are saved as user and assistant text. Because this stream has no authoritative transcript turn-end event in the integration, saved text is segmented and may not align exactly with spoken turns.

### OpenAI Realtime

Existing Realtime sessions use **WebRTC audio directly between the browser and OpenAI**. AI Shuffle still handles credentials, server-side tools, transcript events, and usage. The direct-audio claim applies to this browser transport only, not to GPT Live or PersonaPlex.

The Realtime voice agent is a **coordinator**, not a coder. It does not edit files, run commands, or read a codebase itself. Its instructions direct substantive work to background agents while the spoken conversation continues.

It can do the things you would expect of an assistant you talk to:

- **Dispatch work** to any agent that has a voice model assigned (see below), by name or by letting it pick.
- **Steer running work.** If you change your mind mid-task ("actually make the toggle default to dark"), it sends a follow-up to the worker that is already on it instead of starting a second one. A running worker gets the message mid-work; a finished worker resumes with its full context.
- **Stop a worker** when you cancel.
- **Check on progress** and report what landed.
- **Relay permission prompts.** When a worker needs approval for a tool, the voice agent reads the request to you and acts only on your spoken yes or no.
- **Manage the app**: create and change automations, hooks, agents, MCP servers and connectors during the call. This is always available to the owner, but never to API-key users. It says what it is about to do first and waits for a yes before anything destructive.

For the **Realtime worker roster only**, open an agent's *Provider models* and set its **Voice** row. This selects the model that agent uses for delegated work. Agents without a Voice model are excluded from that roster. The agent selected when the session was created is the default worker: "just get an agent on it" goes to that agent if eligible, otherwise to the alphabetically first eligible agent.

Realtime saves user transcription and assistant speech transcripts in the session. User transcription uses the input transcription model in that session's voice options.

### PersonaPlex

Existing PersonaPlex sessions use the configured **fal.ai API key**, rather than an OpenAI provider. Microphone and response audio pass through the AI Shuffle server over WebSockets to and from fal.ai.

This integration supports a spoken conversation with a voice prompt, but **no tools, agent roster, or delegation**. Assigning an agent a Voice model does not add those capabilities.

Only assistant text returned by the provider is saved. **User transcription is not provided** in this integration, so the session does not contain a complete two-sided transcript.

## Billing and call limits

Voice audio and delegated tasks can use different accounts: OpenAI funds GPT Live and Realtime audio, while fal.ai funds PersonaPlex. GPT Live client-delegated chat turns and Realtime workers use their selected model's provider or subscription independently of the voice account. Responses delegation stays with the call's OpenAI API provider.

The Realtime cost display is an **estimate based on reported token usage**, including audio and text, not a flat per-minute conversation charge. It is not a combined bill for delegated work. GPT Live and PersonaPlex do not provide that Realtime estimate in the call dock; no displayed estimate does not mean the call is free. Check the provider's usage and pricing for actual charges. Generating voice previews also uses your OpenAI API account.

**Max call minutes** limits call duration, not spending. Realtime's browser connection is renewed during longer calls while retaining the total call limit; that renewal behavior is not shared by the server-stream engines.

## API

- `GET /api/v1/settings/voice`, `PUT /api/v1/settings/voice` — current GPT Live defaults, prompts, call limit, and separate dictation settings. These settings do not select Realtime or PersonaPlex for new calls.
- `GET /api/v1/voice/providers` — configured voice-provider credentials eligible for the integration; this is not a live model-access probe.
- `GET /api/v1/voice/agents` — the Realtime worker roster: agents with a Voice model and the provider each uses.
- `POST /api/v1/voice/sessions` — create a voice session. Realtime uses a short-lived browser credential; stream engines do not send the upstream API key to the browser.
- `GET /api/v1/voice/sessions/{id}/config` — the session's saved voice binding.
- `POST /api/v1/voice/sessions/{id}/secret`, `/call`, `/hangup`, `GET .../state` — the call lifecycle. Only Realtime needs a short-lived provider credential for the browser.
- `WS /api/v1/voice/sessions/{id}/audio` — server-owned GPT Live and PersonaPlex audio transport.

The session routes above and the audio socket apply the same access check as other session requests: an API key can use only the voice sessions it started (and only within its project and agents), while the owner can use any. Workers the call hands work to run as the same key, on the key's agents only.
