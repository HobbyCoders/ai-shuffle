<!--
title: Voice
description: Voice-mode settings, voice previews, starting a voice session and controlling the call
order: 17
-->

# Voice API

Endpoints for voice mode: its settings, a voice preview, starting a voice session and controlling the call. A voice session is a normal session whose `mode` is `voice`; the voice agent in the call hands work to agents that run in that session.

New calls use GPT Live 1 (`gpt-live-1`), and the call audio travels over the WebSocket `/api/v1/voice/sessions/{session_id}/audio`, which is not covered on this page. Sessions pinned to an older OpenAI Realtime model instead connect from the browser with a short-lived client secret and then report the call with [POST /api/v1/voice/sessions/{session_id}/call](#post-apiv1voicesessionssession_idcall).

Calls are paid for by an enabled OpenAI provider with a stored API key (Settings → Providers). ChatGPT-subscription logins cannot fund voice calls.

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
  "voice_options": { "instructions": "", "opening": "", "delegation_instructions": "" },
  "model_details": { "model": "gpt-live-1", "voices": [{ "id": "marin", "label": "Marin" }] }
}
```

`model_details.voices` lists the voices you can pick. `voice_input_models` lists the transcription models for `voice_input_model`, the model composer dictation uses through `POST /api/v1/settings/transcribe`. For which key pays for a fal.ai transcription model when an API key calls it, see [Speech and transcription](/docs/api-reference/miscellaneous#speech-and-transcription). `voice_app_tools_enabled` is always `true` and kept for older clients.

### PUT /api/v1/settings/voice

Change voice settings. Only the fields you send are written. Returns the full settings, like the GET.

**Auth:** Owner only

JSON body, all optional:

| Field | Notes |
|-------|-------|
| `voice_mode_enabled` | Turns voice mode on or off |
| `voice_model` | Only `"gpt-live-1"` is accepted |
| `voice_voice` | One of the ids in `model_details.voices` |
| `voice_provider_id` | Only `null` is accepted, which clears a saved provider. Providers are picked automatically |
| `voice_max_call_minutes` | Positive integer. A call is cut off at this length (default 240) |
| `voice_input_model` | One of the ids in `voice_input_models` |
| `voice_input_mode` | `auto` or `toggle` |
| `voice_input_silence_seconds` | Integer from 1 to 60 |
| `voice_options` | Object merged into the saved options: `instructions` (up to 16000 characters), `opening` (up to 2000), `delegation_instructions` (up to 16000). Send `""` to reset a prompt. Other keys return `400` |
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

JSON body: `voice` (required, one of the ids in `model_details.voices`). Returns `400` for an unknown voice or when no OpenAI API-key provider is configured, and `502` when the sample cannot be generated.

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
| `target_instance_id` | Run on another connected device |
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
  "options": { "instructions": "", "opening": "", "delegation_instructions": "" }
}
```

`client_secret` and `expires_at` are only filled for an OpenAI Realtime model; for GPT Live they are empty. Other errors: `400` for an invalid `execution_mode` or `session_mode` or a provider that cannot fund voice calls, `404` for a worktree that is not in the project, `409` when no agents exist or the target device is unknown, offline or awaiting approval.

## Call control

These endpoints act on a session created by [POST /api/v1/voice/sessions](#post-apiv1voicesessions). They return `404` when the session does not exist or is not a voice session.

An API key can only use them on voice sessions it started, and only while the session is inside the key's project and agent restrictions, as described in [Own Sessions](/docs/api-reference/api-users#own-sessions). Any other voice session, including one the owner started, returns `403`, so a key cannot get a client secret for, end, change or read another caller's call. The owner can use them on every voice session. The audio WebSocket applies the same check and closes with `4403` (`4404` for a session that does not exist or is not a voice session).

On a Realtime call the voice agent gets the app configuration tools only when the owner attaches the call to one of the owner's sessions; a call attached with an API key, or on a session an API key started, never gets them.

### POST /api/v1/voice/sessions/{session_id}/secret

Get a fresh client secret for an existing voice session, for example to rejoin it. The session keeps the model, voice and provider it was created with.

**Auth:** Required. An API key can use it only on its own voice sessions (`403` otherwise).

Returns `client_secret`, `expires_at`, `model` and `options` (the secret is empty for GPT Live). Returns `403` when voice mode is disabled and `409` once the call has reached `voice_max_call_minutes`.

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
  "active_tasks": 1
}
```

`active` without `connected` means the call's connection dropped; the server does not reconnect on its own. With no live call, `active` and `connected` are `false` and `started_at` and `model` are `null`. `active_tasks` is the number of agents currently running for the session. For a call on an OpenAI Realtime model, `usage` counts tokens for the whole call (for example `input_tokens`, `output_tokens`) and `estimated_cost_usd` is a list-price estimate (`null` for an unpriced model); GPT Live calls report `{}` and `null`.

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
