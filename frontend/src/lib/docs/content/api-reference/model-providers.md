<!--
title: Model Providers
description: Non-Anthropic model providers, model catalog fetch, ChatGPT and Grok sign-in, banked rate-limit resets, usage tracking keys, and the model registry
order: 13
-->

# Model Providers API

Endpoints for the model providers (OpenRouter, OpenAI, ChatGPT, xAI, Grok and custom endpoints) that sessions can use besides Claude, and for the model registry that lists every selectable model.

Every endpoint on this page except `GET /api/v1/models` and `POST /api/v1/model-providers/refresh-capabilities` needs owner access. API-user keys (`aih_...`) get `403`. The examples send an owner token from `POST /api/v1/owner/login` (see [Owner password](/docs/api-reference/authentication#owner-password)).

## Providers

### The provider object

Every endpoint that returns a provider returns this shape. Stored secrets are never returned: `auth_token_set` and `auth_token_masked` show whether an API key is stored (the mask is the key's first 7 and last 4 characters, or `""` for a key under 22 characters), and `codex_access_token_set` / `codex_refresh_token_set` show whether a ChatGPT or Grok sign-in is stored.

```json
{
  "id": "5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11",
  "name": "OpenRouter",
  "provider_type": "openrouter",
  "base_url": "https://openrouter.ai/api",
  "api_dialect": null,
  "auth_token_set": true,
  "auth_token_masked": "sk-or-v...9f2c",
  "models": [{ "id": "openai/gpt-5.6", "label": "GPT-5.6", "context_window": 400000 }],
  "default_model": "openai/gpt-5.6",
  "small_fast_model": null,
  "extra_env": {},
  "context_window": null,
  "max_output_tokens": null,
  "enabled": true,
  "auth_mode": null,
  "codex_account_id": null,
  "needs_reauth": false,
  "codex_access_token_set": false,
  "codex_refresh_token_set": false,
  "openai_billing_org_id": null,
  "openai_billing_instance_id": null,
  "created_at": "2026-09-01T10:00:00+00:00",
  "updated_at": "2026-09-01T10:00:00+00:00"
}
```

`auth_mode` cannot be set through the API. It becomes `chatgpt` after a [ChatGPT sign-in](#post-apiv1model-providersprovider_idcodexdevicestart) and `grok` after a [Grok sign-in](#post-apiv1model-providersprovider_idxaidevicestart). `needs_reauth` is `true` when a stored sign-in has stopped working and must be done again.

### GET /api/v1/model-providers

List all providers, enabled or not.

**Auth:** Owner only

```bash
curl http://localhost:8000/api/v1/model-providers \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:** `{"providers": [ ...provider objects... ]}`

### POST /api/v1/model-providers

Create a provider. Returns the new provider object (`200`).

**Auth:** Owner only

**JSON body:**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | string | Yes | Display name |
| `provider_type` | string | Yes | One of `openrouter`, `custom`, `openai`, `chatgpt`, `xai`, `grok`. Anything else is `400`. |
| `base_url` | string | Yes | Must not be blank (`400`). For `openrouter`, a trailing `/v1` is removed. |
| `api_dialect` | string | No | `anthropic` or `openai` (anything else is `400`): the wire format a `custom` endpoint speaks |
| `auth_token` | string | No | API key. Stored encrypted and never returned. Blank means no key. |
| `models` | array | No | Objects with string `id` and `label` (`400` otherwise). Audio, voice, image and video models are dropped on save. |
| `default_model` | string | No | |
| `small_fast_model` | string | No | |
| `extra_env` | object | No | String-to-string map (`400` otherwise) |
| `context_window` | integer | No | |
| `max_output_tokens` | integer | No | |
| `enabled` | boolean | No | Default `true` |

```bash
curl -X POST http://localhost:8000/api/v1/model-providers \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "OpenRouter",
    "provider_type": "openrouter",
    "base_url": "https://openrouter.ai/api",
    "auth_token": "sk-or-v1-your-key",
    "models": [{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
    "default_model": "openai/gpt-5.6"
  }'
```

A `400` is also returned when credential storage is unavailable and an `auth_token` was sent.

### PATCH /api/v1/model-providers/{provider_id}

Update a provider. Takes the same fields as create, all optional, with the same validation. Only the fields you send change. Returns the updated provider object.

**Auth:** Owner only

- Omitting `auth_token` or sending it blank keeps the stored key.
- Sending `context_window` or `max_output_tokens` as `null` clears it; omitting it leaves it unchanged.
- `models` replaces the whole list.
- Disabling the provider, or changing it so it is no longer an official OpenAI API provider, removes its [OpenAI usage key](#openai-usage-key).

```bash
curl -X PATCH http://localhost:8000/api/v1/model-providers/5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11 \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"default_model": "openai/gpt-5.6", "context_window": null}'
```

Returns `404` if the provider does not exist.

### DELETE /api/v1/model-providers/{provider_id}

Delete a provider. A stored ChatGPT sign-in is revoked first when possible; a failed revoke does not block the delete. Any OpenAI usage key on the provider is removed too.

**Auth:** Owner only

```bash
curl -X DELETE http://localhost:8000/api/v1/model-providers/5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11 \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:** `{"success": true}`. Returns `404` if the provider does not exist.

### POST /api/v1/model-providers/{provider_id}/toggle

Flip `enabled`. No body. Returns the updated provider object. Disabling removes the provider's OpenAI usage key.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/model-providers/5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11/toggle \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

Returns `404` if the provider does not exist.

### Timeouts and provider errors

Sessions on these providers call the provider directly. These settings bound those calls; set them as environment variables or in `.env` and restart. They apply to every such session the app starts.

| Setting | Default | Meaning |
|---------|---------|---------|
| `PROVIDER_READ_TIMEOUT` | `600` | Longest silence, in seconds, between two reads of a model response |
| `PROVIDER_STREAM_DEADLINE` | `900` | Wall-clock limit, in seconds, for one whole model call; `0` disables it |
| `PROVIDER_FIRST_BYTE_TIMEOUT` | `60` | Seconds to wait for a call's first byte (the response headers and the first event), on every provider these sessions call: Anthropic, OpenRouter, OpenAI (Responses) and `custom` rows in either `api_dialect`. The call is retried only when not even the response headers arrived; once the provider has answered, the turn ends as a reply cut off mid-stream instead of sending the request again. `0` disables it |
| `PROVIDER_FIRST_BYTE_TIMEOUT_LOCAL` | `0` | The same wait for a provider whose `base_url` is a server on this computer or the local network (a loopback, private or link-local address, `localhost`, a `.local` name or `host.docker.internal`), such as Ollama or vLLM. A local server can hold its headers while it loads a model, so `0` (the default) disables it; set it to bound local servers too |

A provider error carries the HTTP status and the provider's own short message (`provider returned HTTP 429: ...`), and a `429` shows the rate-limited notice. Billing errors that providers send as `429` (for example `insufficient_quota`) are reported as `402` and are not retried. A reply cut off mid-stream keeps its partial text, marked interrupted; [Resume](/docs/api-reference/sessions#post-apiv1sessionssession_idresume) re-sends the turn. When no tool call finished before the error, it starts again from the user's message and the partial reply stays on a side branch; when one did, the partial reply and its tool calls stay on the re-sent path, and the model reads them and the calls' error results before it continues.

## Model catalog

### POST /api/v1/model-providers/{provider_id}/fetch-models

Fetch the provider's live model list from upstream so you can pick which ones to offer. Nothing is saved: send the models you want back through [PATCH](#patch-apiv1model-providersprovider_id) as `models`.

**Auth:** Owner only

- A provider signed in with ChatGPT lists the ChatGPT/Codex models for that account.
- A provider signed in with Grok, and every other provider, calls `{base_url}/models` when `base_url` already ends in `/v1`, otherwise `{base_url}/v1/models`, with the stored key.

```bash
curl -X POST http://localhost:8000/api/v1/model-providers/5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11/fetch-models \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:**
```json
{
  "source": "upstream",
  "models": [
    { "id": "openai/gpt-5.6", "label": "GPT-5.6", "context_window": 400000, "pricing": { "prompt": "0.000002", "completion": "0.000008" } }
  ],
  "hidden": [
    { "id": "openai/gpt-image-2", "label": "GPT Image 2", "media_kind": "image" }
  ]
}
```

`models` holds text models only, at most 2000. Each has `id` and `label`, plus `context_window`, `pricing` (OpenRouter prices) and `reasoning_capabilities` when upstream reports them. `hidden` lists the audio, voice, image and video models that were held back, each with a `media_kind`.

**Errors:** `404` provider not found. `409` when a ChatGPT or Grok sign-in is missing or expired (sign in again). `400` when credential storage is unavailable. `502` when the upstream request fails.

### POST /api/v1/model-providers/refresh-capabilities

Refresh the reasoning-effort information of the models already saved on the given providers, from each provider's live catalog. The saved model list is not otherwise changed, and a model whose support is unknown upstream keeps the support already on record. Disabled or unknown providers are reported as failed. A result for a provider is reused for 60 seconds.

**Auth:** Required

**JSON body:** `provider_ids` (array of strings, required): at most 100 ids, each 1 to 200 characters (`422` otherwise). Duplicates are ignored.

```bash
curl -X POST http://localhost:8000/api/v1/model-providers/refresh-capabilities \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"provider_ids": ["5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11"]}'
```

**Response:** `{"refreshed": ["5f0c2a7e-..."], "failed": []}`

## ChatGPT and Grok sign-in

Providers can sign in with a ChatGPT subscription or a Grok (SuperGrok) subscription instead of an API key, using a device code: start the sign-in, open `verification_uri` in any browser and enter `user_code`, then poll until the status is `complete`. The pending sign-in is held in server memory, so after a server restart start again. The tokens are stored encrypted and never returned.

### POST /api/v1/model-providers/{provider_id}/codex/device/start

Start a ChatGPT sign-in. No body.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/model-providers/5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11/codex/device/start \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:** `{"user_code": "ABCD-1234", "verification_uri": "https://..."}`

**Errors:** `400` credential storage unavailable. `404` provider not found. `502` the sign-in could not be started upstream.

### POST /api/v1/model-providers/{provider_id}/codex/device/poll

Check a ChatGPT sign-in once. No body. Poll until done.

**Auth:** Owner only

**Response:** `{"status": "pending"}` or `{"status": "slow_down"}` (keep polling; for `slow_down`, poll less often), or `{"status": "complete", "provider": { ... }}` with the updated provider object, now with `auth_mode` `chatgpt` and `needs_reauth` `false`.

**Errors:** `400` when no sign-in was started for this provider, or when the sign-in failed or expired (start again). `404` provider not found.

### POST /api/v1/model-providers/{provider_id}/xai/device/start

Start a Grok sign-in. Same response and errors as the ChatGPT start. `verification_uri` already contains the code when Grok supplies such a link.

**Auth:** Owner only

```bash
curl -X POST http://localhost:8000/api/v1/model-providers/5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11/xai/device/start \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

### POST /api/v1/model-providers/{provider_id}/xai/device/poll

Check a Grok sign-in once. Same statuses and errors as the ChatGPT poll. On `complete`, `auth_mode` is `grok`, and a provider with no saved models gets the default Grok model list.

**Auth:** Owner only

## Banked rate-limit resets

ChatGPT and Grok subscriptions can hold banked credits that reset a used-up rate-limit window. These endpoints list them and spend one.

**Auth:** Owner only for every endpoint in this section.

| Method | Path | Body | Notes |
|--------|------|------|-------|
| `GET` | `/api/v1/model-providers/{provider_id}/codex/rate-limit-reset-credits` | | ChatGPT provider (`provider_type` or `auth_mode` `chatgpt`) |
| `POST` | `/api/v1/model-providers/{provider_id}/codex/rate-limit-reset-credits/consume` | `credit_id`, `idempotency_key` | Spend one ChatGPT credit |
| `GET` | `/api/v1/model-providers/{provider_id}/grok/rate-limit-reset-credits` | | Grok provider (`provider_type` or `auth_mode` `grok`) |
| `POST` | `/api/v1/model-providers/{provider_id}/grok/rate-limit-reset-credits/consume` | `credit_id`, `idempotency_key` | Spend one Grok credit |

The list response:

```json
{
  "available_count": 1,
  "credits": [
    { "id": "cr_123", "reset_type": "codex_rate_limits", "status": "available", "granted_at": 1790000000, "expires_at": 1792600000, "title": null, "description": null }
  ]
}
```

`reset_type` is `codex_rate_limits`, `grok_rate_limits` or `unknown`. `status` is `available`, `redeeming`, `redeemed` or `unknown`. Times are Unix seconds; `expires_at` may be `null`.

The consume body is JSON with two required, non-blank strings, `credit_id` and `idempotency_key`; any other field is `422`. Reuse the same `idempotency_key` when retrying the same request.

```bash
curl -X POST http://localhost:8000/api/v1/model-providers/5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11/codex/rate-limit-reset-credits/consume \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"credit_id": "cr_123", "idempotency_key": "b7e1c9d2-reset-1"}'
```

**Response:** `{"outcome": "reset", "windows_reset": 1}`. `outcome` is `reset`, `nothing_to_reset`, `no_credit` or `already_redeemed`. After a `reset`, the provider's used-up windows are cleared from [usage limits](/docs/api-reference/miscellaneous#usage-limits).

**Errors:** `404` provider not found. `400` wrong provider kind, or credential storage unavailable. `409` the sign-in is missing or expired (sign in again). `502` the upstream reset-credit service is unavailable.

## Usage tracking keys

Optional admin keys that let the app read account spend for a provider. Each key is stored encrypted and never returned; the responses only say whether one is configured and show the latest reading. Readings appear in [usage limits](/docs/api-reference/miscellaneous#usage-limits). The fal.ai billing key is saved under [settings](/docs/api-reference/miscellaneous#post-apiv1settingsintegrationsfal-billing) instead.

**Auth:** Owner only for every endpoint in this section.

### OpenAI usage key

For an official OpenAI API provider only: `provider_type` `openai`, `base_url` on `https://api.openai.com`, not signed in with ChatGPT. Other providers get `400`. The key is an OpenAI organization Admin API key; the reading covers organization spend and completions token and request counts for the current month (UTC).

| Method | Path | Body | Notes |
|--------|------|------|-------|
| `GET` | `/api/v1/model-providers/{provider_id}/openai-usage` | | Status |
| `PUT` | `/api/v1/model-providers/{provider_id}/openai-usage` | `admin_key`, `organization_id` | Save the key and start polling |
| `DELETE` | `/api/v1/model-providers/{provider_id}/openai-usage` | | Remove the key |

PUT body (JSON, both required): `admin_key` (no whitespace, at most 4096 characters, `400` otherwise) and `organization_id` (must match `org-` followed by 1 to 180 letters, digits, `_` or `-`, `422` otherwise).

```bash
curl -X PUT http://localhost:8000/api/v1/model-providers/5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11/openai-usage \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"admin_key": "sk-admin-your-key", "organization_id": "org-abc123"}'
```

**Response** (all three endpoints):
```json
{
  "configured": true,
  "organization_id": "org-abc123",
  "source": "OpenAI organization Admin API",
  "scope": "Organization spend; completions token/request usage; current month (UTC)",
  "spend": null,
  "last_error_kind": null,
  "fetched_at": null
}
```

`spend` holds the latest reading (`spend_month` in USD with `input_tokens`, `output_tokens` and `num_model_requests`), or `null` until one arrives.

**Errors:** `404` provider not found. `409` on PUT when the provider is disabled, and on DELETE when the key was saved by a different server instance. `503` on PUT when secret storage is locked, or when the key was saved but polling could not resume yet (the detail says what to do; then save again).

### OpenRouter usage key

For an official OpenRouter provider only: `provider_type` `openrouter` with `base_url` on `https://openrouter.ai`. Other providers get `400` on GET and PUT. The key is an OpenRouter management key; the reading is the account credit balance in USD.

| Method | Path | Body | Notes |
|--------|------|------|-------|
| `GET` | `/api/v1/model-providers/{provider_id}/openrouter-usage` | | Status |
| `PUT` | `/api/v1/model-providers/{provider_id}/openrouter-usage` | `management_key` | Save the key and start polling |
| `DELETE` | `/api/v1/model-providers/{provider_id}/openrouter-usage` | | Remove the key; works even if the provider has since changed type or URL |

PUT body (JSON): `management_key` (required; no whitespace, at most 4096 characters, `400` otherwise).

```bash
curl -X PUT http://localhost:8000/api/v1/model-providers/5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11/openrouter-usage \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"management_key": "sk-or-v1-your-management-key"}'
```

**Response** (all three endpoints):
```json
{
  "configured": true,
  "source": "OpenRouter management API",
  "scope": "Account credit ledger (USD)",
  "credits": { "total_credits": 50.0, "total_usage": 12.5, "balance_remaining": 37.5, "currency": "USD" },
  "fetched_at": null,
  "last_error_kind": null
}
```

`credits` is `null` until the first reading arrives.

**Errors:** `404` provider not found. `409` on PUT when the provider is disabled. `503` on PUT when secret storage is locked, or when the key was saved but polling could not resume yet.

## Model registry

### GET /api/v1/models

The full list of selectable models: the built-in Claude models plus the models of every enabled provider. Use it to fill model pickers and to check model ids.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/models \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
{
  "models": [
    { "value": "sonnet", "label": "Sonnet", "family": "sonnet", "auto_permission_supported": true, "context_window": 1000000, "effort_levels": ["low", "medium", "high", "xhigh", "max"], "default_effort": "high", "effort_metadata_version": 0 }
  ],
  "default": "sonnet",
  "inherit_value": "",
  "legacy_map": { "claude-sonnet-4-6": "sonnet" },
  "providers": [
    {
      "id": "5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11",
      "name": "OpenRouter",
      "provider_type": "openrouter",
      "auth_kind": "api",
      "plan_type": null,
      "capability_generation": "9b2e4f10-7c3a-4d8e-b1f6-0a5c3e7d2b94",
      "auto_permission_supported": false,
      "models": [
        { "id": "openai/gpt-5.6", "label": "GPT-5.6", "context_window": 400000, "context_window_choices": [], "max_output_tokens": null, "effort_levels": null, "default_effort": null, "effort_metadata_version": 0 }
      ],
      "default_model": "openai/gpt-5.6"
    }
  ],
  "claude": { "auth_kind": "api", "plan_type": null }
}
```

- `models`: built-in Claude models. Use `value` as the model id.
- `default`: the default model. `inherit_value`: the value that means "inherit from the parent" (for subagents). `legacy_map`: full Claude model ids and the short id each maps to.
- `providers`: enabled providers only, never with keys. `auth_kind` is `subscription` for ChatGPT and Grok subscription providers and `api` otherwise; `plan_type` names the subscription plan when known. `capability_generation` changes whenever the provider is edited. `effort_levels` lists the reasoning-effort levels a model accepts: `[]` means none, `null` means unknown (see [refresh-capabilities](#post-apiv1model-providersrefresh-capabilities)).
- `claude`: how this install signs in to Claude (`auth_kind` `api`, `subscription` or `null`, with `plan_type` when known).
