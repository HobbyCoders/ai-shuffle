<!--
title: Voice
description: Voice-mode settings, voice previews, starting a voice session and controlling the call
order: 17
-->

# Voice API

Endpoints for voice mode: its settings, a voice preview, starting a voice session and controlling the call. A voice session is a normal session whose `mode` is `voice`; the voice agent in the call hands work to agents that run in that session.

New calls use the selected engine, GPT Live 1 (`gpt-live-1`), GPT-Live-1.1 (`gpt-live-1.1`, listed only when your OpenAI API key can use it), Grok Voice (`grok-voice-latest`) or Grok voice on a SuperGrok subscription (`grok-voice-supergrok`), and the call audio travels over the WebSocket `/api/v1/voice/sessions/{session_id}/audio`, which is not covered on this page. If that socket drops during a GPT Live call, the app keeps trying to reopen it for about 30 seconds with backoff, using `?resume=1`, which replaces the session's leftover audio connection instead of being refused with `4409`. While the server restores its own provider connection, it sends `{"type": "status", "state": "reconnecting"}` on the socket, then `"live"` once restored. Sessions pinned to an older OpenAI Realtime model instead connect from the browser with a short-lived client secret and then report the call with [POST /api/v1/voice/sessions/{session_id}/call](#post-apiv1voicesessionssession_idcall).

GPT Live and Grok Voice calls are paid for by an enabled OpenAI (GPT Live) or xAI (Grok Voice) provider with a stored API key (Settings → Providers). `voice_model: "gpt-live-1-codex"` selects **ChatGPT voice**, which uses your already connected ChatGPT subscription login; it doesn't fall back to an API key. It is off until selected: the default engine remains API-key GPT Live. This subscription path uses server-owned WebRTC behind the same PCM browser socket; credentials never reach the client. It runs GPT-Live-1.1 (the wire model id stays `gpt-live-1-codex`) and offers the 25 ChatGPT voices (`juniper`, `maple`, `spruce`, `ember`, `vale`, `breeze`, `arbor`, `sol`, `cove`, `aube`, `bubbie`, `haetsal`, `hanul`, `himari`, `leher`, `miko`, `neer`, `porto`, `rindo`, `rio`, `rivage`, `selva`, `sonna`, `tinta`, `viola`; default `cove`), client delegation to the selected chat agent, no preview, and no automatic retry of failed starts or early provider failures. If ChatGPT refuses the login, the call does not start. Optional hash-pinned media libraries require glibc 2.28+ or macOS ARM64 14+ (Windows x64 wheels are also available). Their installation is best-effort on native/source updates and never gates app startup. A failed or partial install is not marked complete: an authorized attempt to start this engine can retry the hash-pinned libraries for up to 180 seconds before any subscription token is loaded. After a failed attempt, call starts refuse immediately for a 10-minute cool-down; the next start after that window retries the install. Reading settings/model details does not install anything; if they cannot load, model details report `available: false` and a clean `unavailable_reason`, and starting this engine is refused without exposing native-loader details. `voice_model: "grok-voice-supergrok"` runs the same Grok Voice protocol on a connected SuperGrok login (Settings → Providers) instead of an xAI API key. Selecting it is the opt-in; it is never the default. The login's access token is read server-side when the call starts and never reaches the browser. Funding never crosses over: the SuperGrok engine never uses an xAI API key, and API-key Grok Voice never uses the login. A call that fails to start is not retried automatically. The login is renewed by the SuperGrok provider's own refresh; if xAI refuses an expired login when a call starts, the server renews it once and asks you to start the call again. Without a connected login, starting a call returns `400` asking you to connect one. Like ChatGPT subscription voice, it is available to the owner and paired devices only and has no voice preview. Both Grok engines use a server-owned xAI socket, PCM16 mono at 24 kHz, `server_vad`, transcripts and function handoffs to the selected chat agent. No xAI key is sent to the browser. xAI sessions last at most an hour, so the server opens a new one in the background before that, at a quiet moment, and replays the conversation into it without a new greeting; a dropped or provider-closed Grok session is restored like a GPT Live one. The SuperGrok engine reads the login's current access token for each new session and never uses an API key.

ChatGPT voice renews long-running provider sessions without a second greeting. Its fixed session limit is undocumented. If `session.started` or a startup `session.updated` reports a finite numeric `session.expires_at`, the server uses GPT Live's renewal window: about two minutes before expiry at a quiet moment, no later than 15 seconds before expiry. Without that signal, no fixed-time renewal is scheduled. A provider `session.closed`, WebRTC disconnect/close or data-channel close is restored only after the session has lasted at least 50 minutes (a conservative expiry heuristic, not a documented ChatGPT limit). Early closes, rejected events and other failures are counted as failures and end the call without automatic retry. Unknown explicit close reasons are not treated as expiry. Renewal attempts use the shared backoff, 20-second reconnect window and 10-second per-attempt bound; planned renewals and old-session expiries do not spend the hourly failure budget. The new WebRTC peer starts with fresh transport state and the saved conversation in `initial_items`, including the instruction not to greet again. Work notes received during the swap are held, old delegation results become plain context, and microphone audio from the swap is dropped rather than replayed. Every new session reads the current subscription credentials; an HTTP 401 uses the shared ChatGPT login-renewal path, with no API-key fallback. A failed initial call is not retried; a bounded renewal attempt can use the rotated login on its next attempt. PersonaPlex calls are not renewed or reconnected because their conversation state cannot be replayed.

## Voice settings

### GET /api/v1/settings/voice

Read the voice settings, with defaults applied.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/settings/voice \
  -H "Authorization: Bearer aih_your_key"
```

**Response** (defaults; `model_details` and `voice_input_models` shortened):
```json
{
  "voice_mode_enabled": false,
  "voice_model": "gpt-live-1",
  "voice_voice": "marin",
  "voice_provider_id": null,
  "voice_max_call_minutes": 240,
  "voice_input_model": "fal-ai/whisper",
  "voice_input_models": [{ "id": "fal-ai/whisper", "name": "Whisper (Large v3)", "provider": "fal-ai" }],
  "voice_input_mode": "auto",
  "voice_input_silence_seconds": 5,
  "voice_app_tools_enabled": true,
  "voice_options": { "instructions": "", "delegation_instructions": "", "mic_threshold_db": -42.0 },
  "model_details": { "model": "gpt-live-1", "voices": [{ "id": "marin", "label": "Marin" }] },
  "voice_engines": [{ "id": "gpt-live-1", "label": "GPT Live 1", "funding": "openai" }]
}
```

`model_details.voices` lists the voices you can pick. `voice_input_models` lists the transcription models for `voice_input_model`, the model composer dictation uses through `POST /api/v1/settings/transcribe`. For which key pays for a fal.ai transcription model when an API key calls it, see [Speech and transcription](/docs/api-reference/miscellaneous#speech-and-transcription). `voice_engines` lists the engines you can choose now, in order, with the kind of provider that pays for each (`openai`, `xai`, `grok` for a SuperGrok login, `chatgpt` for a ChatGPT login). `gpt-live-1.1` appears only after `GET https://api.openai.com/v1/models/gpt-live-1.1` succeeds for the OpenAI API key a call would use; the answer is cached for 6 hours (1 hour when the model is not found). If a saved `gpt-live-1.1` choice is no longer available, settings and new calls use `gpt-live-1` instead, which has the same voices and shares its saved options. `voice_app_tools_enabled` is always `true` and kept for older clients.

### PUT /api/v1/settings/voice

Change voice settings. Only the fields you send are written. Returns the full settings, like the GET.

**Auth:** Owner only

JSON body, all optional:

| Field | Notes |
|-------|-------|
| `voice_mode_enabled` | Turns voice mode on or off |
| `voice_model` | `"gpt-live-1"`, `"gpt-live-1.1"` (only when listed in `voice_engines`), `"grok-voice-latest"` or `"grok-voice-supergrok"` (Grok Voice on a connected SuperGrok login); defaults to API-key GPT Live. `"gpt-live-1-codex"` selects ChatGPT voice on your ChatGPT subscription login (opt-in; no API-key fallback) |
| `voice_voice` | One of the ids in `model_details.voices`. When you switch engines without sending a voice, a voice the new engine lacks becomes its default (`marin` for GPT Live, `eve` for Grok, `cove` for ChatGPT voice) |
| `voice_provider_id` | An enabled provider for the selected engine: an API key for GPT Live and Grok Voice, a connected SuperGrok login for `grok-voice-supergrok`, or a connected ChatGPT login for ChatGPT voice (`gpt-live-1-codex`). Subscription calls are owner/paired-device only. `null` selects automatically by name within that engine; it never substitutes a subscription for an API key. Switching engines without sending a provider drops a pin the new engine can't use |
| `voice_max_call_minutes` | 0 or a positive integer. A call is cut off at this length (default 240); 0 means no limit, and the call lasts as long as the caller stays connected. Negative values return `400` |
| `voice_input_model` | One of the ids in `voice_input_models` |
| `voice_input_mode` | `auto` or `toggle` |
| `voice_input_silence_seconds` | Integer from 1 to 60 |
| `voice_options` | Object merged into the saved options: `instructions` (up to 16000 characters), `delegation_instructions` (up to 16000), `mic_threshold_db` (number from -80 to -20, default -42: microphone audio quieter than this many dBFS is sent as silence; -80 turns the gate off). Both Grok engines additionally accept `turn_detection: "server_vad"` only. Send `""` to reset a prompt. Other keys return `400` |
| `voice_options_model` | Optional guard: the model your `voice_options` were written for. A mismatch returns `409` |
| `voice_app_tools_enabled` | Accepted and ignored |

Invalid values return `400`.

```bash
curl -X PUT http://localhost:8000/api/v1/settings/voice \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"voice_mode_enabled": true, "voice_voice": "cedar", "voice_max_call_minutes": 60}'
```

### POST /api/v1/voice/preview

Generate a short spoken sample of a voice. Returns `audio/wav` bytes.

**Auth:** Required

JSON body: `voice` (required, one of the ids in `model_details.voices`), `model` (`gpt-live-1` by default, `gpt-live-1.1` when available, or `grok-voice-latest`) and optional `provider_id`. Returns `400` for an unknown voice or when no matching API-key provider is configured, and `502` when the sample cannot be generated.

```bash
curl -X POST http://localhost:8000/api/v1/voice/preview \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"voice": "cedar"}' \
  -o sample.wav
```

## Starting a call

### POST /api/v1/voice/sessions

Create a voice session. Voice mode must be enabled, or the request returns `403`.

**Auth:** Required. An API key restricted to certain agents can only start voice sessions with those agents, as described in [Agent Restriction](/docs/api-reference/api-users#agent-restriction). A voice session started with an API key belongs to that key, so only that key and the owner can use the call control endpoints below on it.

JSON body. Every field is optional, but `model` is needed for GPT Live calls (`400` without it):

| Field | Notes |
|-------|-------|
| `model` | The chat model the dispatched agents run on |
| `effort`, `permission_mode`, `context_window`, `fast` | Same meaning as for a normal session |
| `profile_id` | Agent for the session. Default: the project's default agent, else the first configured agent |
| `project_id` or `project_v2_id` | Project for the session. If both are sent they must name the same project |
| `provider_id` | OpenAI provider that pays for the call. Default: the first eligible one by name |
| `execution_mode` | `local` or `worktree` |
| `worktree_branch`, `base_branch` | With `worktree` mode, the branch to use; without `worktree_branch` a new `voice/...` branch is created from `base_branch` (default `main`). A project is required |
| `worktree_id` | An existing worktree of the project (needs `project_id`) |
| `target_instance_id` | Only this device. Any other device returns `400`: a voice session starts on the device that hosts the call. To call a chat another device runs, create the chat there ([POST /api/v1/sessions](/docs/api-reference/sessions#post-apiv1sessions) with `target_instance_id`) and start the call from it |
| `session_mode` | Only `chat` is accepted (sent by the native apps); an empty string is the same as leaving it out. Any other string returns `400` |

```bash
curl -X POST http://localhost:8000/api/v1/voice/sessions \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"project_id": "my-web-app", "model": "sonnet"}'
```

**Response:**
```json
{
  "session_id": "0f6c2d1e-7a4b-4c1d-9e2f-5b8a7c6d4e3f",
  "client_secret": "",
  "expires_at": null,
  "model": "gpt-live-1",
  "options": { "instructions": "", "delegation_instructions": "" }
}
```

`client_secret` and `expires_at` are only filled for an OpenAI Realtime model; for GPT Live they are empty. Other errors: `400` for an invalid `execution_mode` or `session_mode` or a provider that cannot fund voice calls, `404` for a worktree that is not in the project, `409` when no agents exist.

## Call control

These endpoints act on a session created by [POST /api/v1/voice/sessions](#post-apiv1voicesessions). They return `404` when the session does not exist or is not a voice session.

An API key can only use them on voice sessions it started, and only while the session is inside the key's project and agent restrictions, as described in [Own Sessions](/docs/api-reference/api-users#own-sessions). Any other voice session, including one the owner started, returns `403`, so a key cannot get a client secret for, end, change or read another caller's call. The owner can use them on every voice session. The audio WebSocket applies the same check and closes with `4403` (`4404` for a session that does not exist or is not a voice session).

A call can attach to a chat another device runs. The call runs on this device; requests and spoken approvals go to the device that runs the chat, and nothing from the call is written into this device's copy of it. Attaching returns `409` when that device is offline or has left the network. A voice session (`mode` `voice`) another device owns returns `404` on these endpoints.

On a Realtime call the voice agent gets the app configuration tools only when the owner attaches the call to one of the owner's sessions; a call attached with an API key, or on a session an API key started, never gets them.

### POST /api/v1/voice/sessions/{session_id}/secret

Get a fresh client secret for an existing voice session. Without a body it refreshes the running call, which keeps the engine, voice and provider it started with. Send `{"new_call": true}` when you start a new call on the session: if no call is running, the call uses the current voice settings (engine, voice, provider and options), with the same fallbacks as a new session.

**Auth:** Required. An API key can use it only on its own voice sessions (`403` otherwise).

Returns `client_secret`, `expires_at`, `model` and `options` (the secret is empty for GPT Live). Returns `403` when voice mode is disabled and `409` once the call has reached `voice_max_call_minutes` (never when it is `0`).

```bash
curl -X POST http://localhost:8000/api/v1/voice/sessions/0f6c2d1e-7a4b-4c1d-9e2f-5b8a7c6d4e3f/secret \
  -H "Authorization: Bearer aih_your_key"
```

### POST /api/v1/voice/sessions/{session_id}/call

For a session on an OpenAI Realtime model: after the client has connected to OpenAI, send the `call_id` it received so the server can join the call. Sending a new `call_id` replaces the previous one without restarting the call-length limit. Returns `{"ok": true}`.

**Auth:** Required. An API key can use it only on its own voice sessions (`403` otherwise).

JSON body:

| Field | Required | Notes |
|-------|----------|-------|
| `call_id` | Yes | The OpenAI call id |
| `client_tools` | No | Up to 32 extra function tools the client handles itself. Each needs a unique `name` (up to 64 letters, digits, `_` or `-`) and may have `description` and `parameters` |
| `client_instructions` | No | Extra instructions for those tools, up to 6000 characters |
| `session_mode` | No | Only `chat` is accepted. It adds the native app's instructions to the call; browser calls leave it out |

Returns `400` for a GPT Live session or an invalid body, `403` when voice mode is disabled, `409` once the call has reached its length limit, and `502` when the server cannot join the call.

```bash
curl -X POST http://localhost:8000/api/v1/voice/sessions/0f6c2d1e-7a4b-4c1d-9e2f-5b8a7c6d4e3f/call \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"call_id": "rtc_abc123"}'
```

### POST /api/v1/voice/sessions/{session_id}/hangup

End the call and mark the session inactive. Agents the call started keep running. Safe to repeat, and it works even if voice mode was turned off during the call. Returns `{"ok": true}`.

The body is optional. `{"reason": "user"}` names what ended the call, such as `user`, `replaced`, `playback-backlog` or `provider-closed`. The reason is only written to the server log; a value that is not a short lowercase label is logged as `unrecognized` and never fails the request.

**Auth:** Required. An API key can use it only on its own voice sessions (`403` otherwise).

```bash
curl -X POST http://localhost:8000/api/v1/voice/sessions/0f6c2d1e-7a4b-4c1d-9e2f-5b8a7c6d4e3f/hangup \
  -H "Authorization: Bearer aih_your_key"
```

### GET /api/v1/voice/sessions/{session_id}/state

Whether the call is live.

**Auth:** Required. An API key can use it only on its own voice sessions (`403` otherwise).

```bash
curl http://localhost:8000/api/v1/voice/sessions/0f6c2d1e-7a4b-4c1d-9e2f-5b8a7c6d4e3f/state \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{
  "active": true,
  "connected": true,
  "started_at": 1790320000.0,
  "model": "gpt-live-1",
  "usage": {},
  "estimated_cost_usd": null,
  "active_tasks": 1,
  "max_call_minutes": 240,
  "remaining_seconds": 13950.4
}
```

`active` without `connected` means the call's provider connection is down. For a GPT Live call the server is reconnecting it: it retries with backoff for about 20 seconds, and `connected` returns to `true` when an attempt succeeds. If none does, the call ends. For a call on an older Realtime model the server does not reconnect on its own. With no live call, `active` and `connected` are `false` and `started_at` and `model` are `null`. `active_tasks` is the number of agents currently running for the session. For a call on an OpenAI Realtime model, `usage` counts tokens for the whole call (for example `input_tokens`, `output_tokens`) and `estimated_cost_usd` is a list-price estimate (`null` for an unpriced model); GPT Live calls report `{}` and `null`. `max_call_minutes` is the live call's length limit and `remaining_seconds` the time left on it, never negative. A call with no limit reports `max_call_minutes: 0` and `remaining_seconds: null`; with no live call both are `null`. Planned provider-session renewals and provider-ended sessions never count toward the reconnect limit; only failures do (at most 12 within an hour). A streamed call ends when its audio socket has received nothing for two minutes (the browser sends audio, or `client_stats` every 5 seconds even while muted). An OpenAI Realtime call ends when its sideband has been closed for ten minutes without the client re-attaching.

### GET /api/v1/voice/sessions/{session_id}/config

The voice setup the session was created with: `model`, `voice`, `provider_id` and `options`. Sessions created before this was recorded report the current settings.

**Auth:** Required. An API key can use it only on its own voice sessions (`403` otherwise).

```bash
curl http://localhost:8000/api/v1/voice/sessions/0f6c2d1e-7a4b-4c1d-9e2f-5b8a7c6d4e3f/config \
  -H "Authorization: Bearer aih_your_key"
```

### PATCH /api/v1/voice/sessions/{session_id}/context

Change the model and options used by agents the call starts from now on. Only the fields you send change.

**Auth:** Required. An API key can use it only on its own voice sessions (`403` otherwise).

JSON body, all optional:

| Field | Notes |
|-------|-------|
| `model` | A model id, or a provider model selection (`prov:...`) that must exist on that provider |
| `effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` or `ultra` |
| `permission_mode` | `default`, `auto`, `acceptEdits` or `bypassPermissions` |
| `context_window` | Integer greater than 0 |
| `fast` | Boolean |

Invalid values return `400`.

```bash
curl -X PATCH http://localhost:8000/api/v1/voice/sessions/0f6c2d1e-7a4b-4c1d-9e2f-5b8a7c6d4e3f/context \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"effort": "high", "permission_mode": "acceptEdits"}'
```

**Response:** `session_id`, `model`, `effort`, `permission_mode`, `context_window` and `fast` as now stored.
