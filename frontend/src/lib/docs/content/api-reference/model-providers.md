<!--
title: Model Providers
description: Provider rows (OpenAI, ChatGPT, xAI, Grok, OpenRouter, custom), model catalog fetch, ChatGPT and Grok sign-in, banked rate-limit resets, usage tracking keys, and the model registry
order: 13
-->

# Model Providers API

Endpoints for the model providers stored as provider rows (OpenAI, ChatGPT, xAI, Grok, OpenRouter and custom endpoints), and for the model registry that lists every selectable model. Claude is set up through its own endpoints: see [claude-auth](/docs/api-reference/miscellaneous#get-apiv1settingsclaude-auth) and [Authentication](/docs/api-reference/authentication).

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
  "account_email": null,
  "account_plan": null,
  "openai_billing_org_id": null,
  "openai_billing_instance_id": null,
  "created_at": "2026-09-01T10:00:00+00:00",
  "updated_at": "2026-09-01T10:00:00+00:00"
}
```

`auth_mode` cannot be set through the API. It becomes `chatgpt` after a [ChatGPT sign-in](#post-apiv1model-providersprovider_idcodexdevicestart) and `grok` after a [Grok sign-in](#post-apiv1model-providersprovider_idxaidevicestart). `needs_reauth` is `true` when a stored sign-in has stopped working and must be done again. `account_email` and `account_plan` are the email and plan id (such as `plus` or `pro`) read from a stored ChatGPT sign-in, for display; each is `null` when the sign-in does not carry it, and the token itself is never returned.

### Per-model local sampling and reasoning (backend/API)

For a `custom` provider with `api_dialect: "openai"`, model entries may include
`local_settings`. Set these only for a server and chat template whose documented
API supports them; server capabilities are not guessed from the URL or model name.
The settings apply to new connections and model hot-swaps; reconnect an already
connected session after editing its provider configuration.

```json
{
  "id": "my-local-model",
  "label": "Local model",
  "local_settings": {
    "server": "llama_cpp",
    "sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "repeat_penalty": 1.1},
    "reasoning": true
  }
}
```

Use these entries in `models` on POST or PATCH. PATCH replaces the whole model
list: preserve other entries and their metadata. GET returns the stored settings.
Omit `local_settings`, set it to `null`, or replace the entry without it to clear
all overrides. New or changed local settings with unknown keys or invalid values
return `400`, as do local settings on another provider type/dialect. PATCH preserves
unchanged settings for the same model ID, even if a newer worker synced values this
worker does not recognize; renaming, toggling, and editing other models remain
possible. New IDs and changed settings are always validated. Changing the provider
type or dialect revalidates all remaining settings; clear incompatible settings first.

- `server` (required): `llama_cpp` or `vllm` (explicit protocol declaration, not detection).
- `sampling` (optional object): `temperature` in [0, 2], `top_p` and `min_p` in
  [0, 1], integer `top_k` >= 0 (0 disables top-k), `repeat_penalty` > 0. Only finite
  numbers are accepted; booleans are not numbers. All fields are optional.
- `reasoning` (optional boolean): sends `chat_template_kwargs.enable_thinking`.
  Use only with a compatible reasoning template (for example Qwen3). It is
  independent of effort: `reasoning.effort` is still sent only when the model's
  `reasoning_capabilities` allows it. Most local templates support a switch,
  not effort levels; declare `effort_levels: []` for those models.
- `preset` (optional): explicit vendor recommendations, `qwen3-thinking`
  (temperature 0.6, top_p 0.95) or `qwen3-non-thinking` (0.7, 0.8). Both set top_k
  20 and min_p 0 and select the corresponding reasoning switch. Explicit
  `sampling` fields and `reasoning` override preset values. Source:
  [Qwen3-8B best practices](https://huggingface.co/Qwen/Qwen3-8B#best-practices).
  Presets are opt-in, not inferred for quantizations, derivatives or future models.

Without an explicit preset, omitted settings keep server defaults. Existing
providers/sessions receive no new tuning defaults. llama.cpp receives sampling
fields as named above; vLLM receives `repetition_penalty` instead of
`repeat_penalty`, and `top_k: -1` instead of 0. Other local-server protocols are
not declared supported by this contract. No new UI controls are included.

#### Offline request comparison

With test dependencies installed, run from the repository root:

```bash
PYTHONPATH=fluid python -m scripts.effort_wire_snapshot --output /tmp/effort-before.json
# Repeat after a change with --output /tmp/effort-after.json
# Then compare:
diff -u /tmp/effort-before.json /tmp/effort-after.json
```

This deterministic harness drives stored session settings and turn overrides
through the real adapter/request builders, captures HTTP at a fake transport,
and records CLI effort flags without starting Claude. It makes no paid calls.
It records request parameters, **not** task quality, tokens or latency. A matching
snapshot is not evidence that a tuning change improves outcomes. Do not change
production defaults without an approved outcome benchmark; the separate
`scripts/effort_benchmark.py` is a live, opt-in outcome experiment, never run by
this snapshot harness or CI.

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
- `models` replaces the whole list. Send `expected_revision` from the provider response to guard a curation save. A stale revision returns `409` without changing the provider; reload its current selection before editing again.
- Disabling the provider or rotating its inference key invalidates readings but keeps its [OpenAI usage key](#openai-usage-key). Admin requests require an enabled, official OpenAI API provider; only explicit disconnect removes the shared Admin key.

```bash
curl -X PATCH http://localhost:8000/api/v1/model-providers/5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11 \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"default_model": "openai/gpt-5.6", "context_window": null}'
```

Returns `404` if the provider does not exist.

### DELETE /api/v1/model-providers/{provider_id}

Delete a provider. A stored ChatGPT sign-in is revoked first when possible; a failed revoke does not block the delete. Any OpenAI usage key and the shared discovery snapshot for the provider are removed too.

**Auth:** Owner only

```bash
curl -X DELETE http://localhost:8000/api/v1/model-providers/5f0c2a7e-1b3d-4c8e-9a61-2d4f7b9e0c11 \
  -H "Authorization: Bearer $OWNER_TOKEN"
```

**Response:** `{"success": true}`. Returns `404` if the provider does not exist.

### POST /api/v1/model-providers/{provider_id}/toggle

Flip `enabled`. No body. Returns the updated provider object. Disabling invalidates readings but keeps billing access, including the OpenAI Admin key, for re-enabling.

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
| `PROVIDER_READ_TIMEOUT` | `600` | Longest silence, in seconds, between two reads of a model response. For OpenAI (Responses), also the longest the provider may send only keep-alive heartbeats after answering and before its first event; the turn then ends as a reply cut off mid-stream. Once the first event has arrived, heartbeats keep the call open (a long reasoning step can send nothing else for minutes) and only `PROVIDER_STREAM_DEADLINE` limits it, so with that set to `0` nothing does. `0` keeps the default |
| `PROVIDER_STREAM_DEADLINE` | `900` | Wall-clock limit, in seconds, for one whole model call; `0` disables it |
| `PROVIDER_FIRST_BYTE_TIMEOUT` | `60` | Seconds to wait for a call's first byte (the response headers and the first event), on every provider these sessions call: Anthropic, OpenRouter, OpenAI (Responses) and `custom` rows in either `api_dialect`. The call is retried only when not even the response headers arrived; once the provider has answered, the turn ends as a reply cut off mid-stream instead of sending the request again. `0` disables it |
| `PROVIDER_FIRST_BYTE_TIMEOUT_LOCAL` | `0` | The same wait for a provider whose `base_url` is a server on this computer or the local network (a loopback, private or link-local address, `localhost`, a `.local` name or `host.docker.internal`), such as Ollama or vLLM. A local server can hold its headers while it loads a model, so `0` (the default) disables it; set it to bound local servers too |

A provider error carries the HTTP status and the provider's own short message (`provider returned HTTP 429: ...`), and a `429` shows the rate-limited notice. Billing errors that providers send as `429` (for example `insufficient_quota`) are reported as `402` and are not retried. A reply cut off mid-stream keeps its partial text, marked interrupted; [Resume](/docs/api-reference/sessions#post-apiv1sessionssession_idresume) re-sends the turn. When no tool call finished before the error, it starts again from the user's message and the partial reply stays on a side branch; when one did, the partial reply and its tool calls stay on the re-sent path, and the model reads them and the calls' error results before it continues.

## Model catalog

### GET /api/v1/model-providers/{provider_id}/model-catalog

**Auth:** Owner only

Read the last successful discovery snapshot without contacting upstream, or `null` if the provider has not been refreshed yet (`404` if the provider does not exist). The snapshot contains `source`, `models`, `hidden`, `recommended`, `fetched_at` (UTC), `fetched_by_device` and `fetched_by_name`. It is separate from the curated `models` selection.

Settings > Providers shows this shared snapshot and its **Last refreshed** time and worker. Refreshing on one worker publishes it to the others through settings sync; open Models tabs update without a page reload and newly discovered models stay unchecked. Concurrent selection edits are reconciled from the current provider revision.

### POST /api/v1/model-providers/{provider_id}/fetch-models

Fetch the provider's live model list from upstream so you can pick which ones to offer. Send the models you want back through [PATCH](#patch-apiv1model-providersprovider_id) as `models`; refresh never adds or removes curated models. Known per-model context windows are included in the result and missing windows on already-curated models are backfilled from presets or the upstream catalog. Existing per-model windows, including owner edits, are kept. Concurrent provider edits take precedence over the backfill.

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

A successful fetch saves a compact, token-free snapshot in the shared `model_catalog:<provider_id>` system setting (maximum 512 KiB). Failed fetches leave the previous snapshot intact. Snapshots contain public model metadata and worker attribution, never credentials or endpoints.

**Errors:** `404` provider not found. `409` when a ChatGPT or Grok sign-in is missing or expired (sign in again). `400` when credential storage is unavailable. `502` when the upstream request fails or the snapshot exceeds the size limit.

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

Optional admin keys that let the app read account spend for a provider. Each key is stored encrypted and never returned; the responses only say whether one is configured and show the latest reading. Readings appear in [usage limits](/docs/api-reference/miscellaneous#usage-limits). fal.ai needs no extra key: its balance is read with the [fal.ai API key](/docs/api-reference/miscellaneous#post-apiv1settingsintegrationsfal-ai).

**Auth:** Owner only for every endpoint in this section.

### Provider-reported API balances

In **Settings > Providers > the OpenAI or xAI API key > Usage**, configure balance
access separately from the inference key. Keys are write-only, encrypted at rest
and under the network key during sync, and included in secret re-encryption.
Balance keys (including Platform session keys), OpenAI Admin keys and organization
IDs sync across network workers. This gives every trusted worker the same billing
access, including higher-privilege Admin access. Each worker keeps its own readings
and errors and can fetch credit while the network's shared usage budget is
reachable. Keys become usable after the next sync; readings arrive on the next
poll. The network founder coordinates that budget: other workers defer new
requests while it is offline, even if they already have the synced key.
Disconnecting access syncs the removal. Older versions keep their local billing
configuration until upgraded. An upgraded worker that lacks the key gets it once
the worker holding the key is online and running this build.
Restoring another installation's database alone
does not authorize using or publishing its billing keys; authenticated sync does.

- **xAI (official):** create a management key in console.x.ai → Settings →
  Management Keys, with billing read permission. The normal API key resolves
  `team_id` through `GET https://api.x.ai/v1/api-key`; the management key reads
  `GET https://management-api.x.ai/v1/billing/teams/{team_id}/prepaid/balance`
  and `/postpaid/invoice/preview`. The UI shows prepaid remaining, cycle usage
  and postpaid spending limit. The documented `val` fields are **USD cents**;
  purchases are negative ledger entries, so remaining is `-total.val / 100`.
  Unknown units/currencies or non-finite amounts are rejected, never guessed.
  Invoice preview is optional: a refused/missing invoice does not hide prepaid
  credit; `invoice_error_kind` reports that failure separately. A malformed
  limit is omitted with `spending_limit_error_kind`, preserving cycle usage.
  A budget-deferred invoice keeps its previous cycle/limit values and optional
  error states; it is not a new failure. Proto JSON's
  omitted zero fields are treated as zero; an omitted spending limit is unknown.
  Cycle usage adds the absolute prepaid usage because its sign is undocumented;
  the sign and cycle totals still need comparison with a live account.
- **OpenAI Platform:** choose **Sign in to OpenAI Platform** and
  sign in yourself in the app-owned, isolated browser screen. The supervised
  browser must be enabled and owned by this worker's native or Docker supervisor;
  startup waits for Chrome and refuses an external CDP listener. It keeps its
  existing egress proxy; no agent tools or network transcript
  get this context. The app captures a dashboard `sess-` key server-side from
  the login response or dashboard Authorization header, then destroys the
  temporary cookies/context. If sign-in is refused (including CAPTCHA or popup
  limitations), paste your own Platform session key in the write-only fallback.
  The key reads `GET https://api.openai.com/v1/dashboard/billing/credit_grants`;
  `total_available` is remaining USD, not `total_granted` minus local spend.
  This undocumented endpoint can change and sessions expire. An inference key
  or ChatGPT/Codex OAuth token cannot replace a Platform session key.

Polling is demand-driven, with roughly one request per credential/endpoint every
300–330 seconds network-wide under the shared usage budget. With demand and the
budget reachable, readings refresh about every five minutes, not once per worker. Opening balance status or choosing
**Refresh balance status** requests a poll (30-second forced-refresh cooldown;
per-endpoint budgets still apply). The UI re-reads while that poll runs. Readings
remain fresh for ten minutes, twice the poll interval, to cover budget jitter;
`fresh_until` is supplied by the server. Status includes the source and last
successful fetched time. A newly synchronized billing key schedules a refresh
only after commit. Workers share successful normalized readings without adding
upstream calls; `observed_at` and `source_worker` retain the original time and
source. Configured access with no reading or error is `pending`; a shared-budget
wait is `deferred`, with a fixed `last_deferred_reason` (no retry deadline is
advertised). A failed first reading remains `unavailable`. Failed/expired readings never expose old credit as
current; 401/403 asks for renewed access and pauses requests until the credential
changes or is deliberately re-saved. Disconnecting balance access does not remove
inference/Admin credentials. Changes of inference or billing key invalidate old
readings and reject in-flight snapshots. Inference-key rotation, provider changes,
and disabling the provider (including the enable toggle) invalidate readings but
retain billing access, including the OpenAI Admin key. Only explicitly
disconnecting the relevant billing access removes that key across the network. Fresh, error-free Admin organization-spend fields
from the current UTC month remain alongside real remaining credit in the usage
popover. Stale, failed or prior-month Admin totals are withheld when a balance
is bound, rather than borrowing its newer timestamp. Balance freshness/error
state stays owned by the balance source. A failed WebSocket hello releases its ticket so an
immediate sign-in retry is possible.
Balance snapshots are stored independently from Admin spend and passive headers
so those readers cannot overwrite provider-reported credit.

Owner-only endpoints under `/api/v1/model-providers/{provider_id}`:

| Method | Path | Body / result |
| --- | --- | --- |
| GET | `/balance-access` | Configuration, source, freshness, credits, re-auth state; never the key |
| PUT | `/balance-access` | `{"key": "…"}` management key or Platform `sess-` key |
| DELETE | `/balance-access` | Remove balance binding and snapshot |
| POST | `/balance-access/sign-in` | OpenAI only; single-use 60-second browser ticket |
| WS | `/balance-access/browser` | Owner-authenticated screen/input; first frame supplies the ticket; ten-minute limit |

The Admin-key API below remains available for organization spend. The entered
balance below is a **fallback only when no real-balance binding exists**; it does
not mask an expired Platform session. Re-sign in or disconnect that binding to
use a manual estimate.

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

A restored installation's Admin key is not configured until saved here or received
through authenticated sync. Explicit save or disconnect replaces a restored
installation marker; it does not require that installation to come back online.

**Errors:** `404` provider not found. `409` on PUT when the provider is disabled or changes during the operation. `503` on PUT when secret storage is locked, or when the key was saved but polling could not resume yet (the detail says what to do; then save again).

### Entered OpenAI prepaid balance

An official OpenAI API key with neither Admin usage nor an entered balance is
omitted from the usage popover. In Settings > Providers > the key > Usage,
enter the current balance in USD (two decimals) and Save; Clear removes only
this estimate. Open billing links to the actual provider balance.

Owner-only `PUT /api/v1/model-providers/{provider_id}/openai-usage/balance`
accepts `{"balance_usd": 20.50}` (finite, 0–1,000,000,000 USD). The server rounds
to cents and records the current millisecond timestamp. `DELETE` at the same
path clears it; neither operation changes the inference or Admin key.
The existing `GET .../openai-usage` response also includes `manual_balance`:
`balance_remaining`, `manual_balance`, `estimated_spend`, `set_at` (epoch ms),
and `unpriced_turns`, or null when not set.

The popover shows **~$N left (estimated)**, not a provider reading. The app
subtracts Analytics' recorded `usage_log.cost_usd` for the exact provider ID
since Save. Reported and guessed token counts use the existing pricing path;
unpriced turns, unattributed calls and spending outside this app are omitted.
Prices and booking times may differ from billing; an in-flight turn booked
after Save is deducted in full. Top-ups/refunds are not detected, and the
estimate can go negative. Save again to reset the baseline. Changing the API
key clears the baseline; resaving or re-encrypting the same key preserves it.
Baselines sync with providers, but deductions cover only this worker's tracked
spend: paired workers' spend is not included, as both UI surfaces state. If Admin
usage is available, its organization spend stays primary with the manual
estimate alongside it.

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
