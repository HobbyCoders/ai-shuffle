<!--
title: Unified generation tools
description: Source selection, prices, confirmation receipts and unattended generation contracts
order: 14
-->

# Unified generation tools

`ai-tools.generateImage`, `generateVideo`, `generateAudio` and `textTo3D`
accept `prompt`, optional `source` (`fal`, `openai`, `xai`, `chatgpt`,
`supergrok`, case-insensitive, or an exact account ID), `model`, `worker` and
`options`. `options` uses the selected model's declared input schema, the same
schema service used by Default models. `searchModels` looks up sources,
accounts, holding workers, models, verified status, prices and schemas on
demand. No model catalog is added to session prompts. It quotes only the top
fal model per kind (three with `output_type`). Next to the quotes it returns
the previous fal catalog search as `fal_models`: every category, matched on
name, description and tags, with `endpoint_id` fields. Quote one by passing
`source: "fal"` and its `endpoint_id` as `model`. Peers are asked in parallel
with a 15-second limit; a request pinned to this worker does not ask them. A
malformed peer reply marks only that worker unavailable, and the fal catalog
search is returned even when the priced lookup fails.

The same source/account/model/options on several workers is one option: a usable
local holder is preferred, otherwise the holder is chosen by worker ID. Different
accounts or options remain separate. A single quoted match exposes its price both
on the option and at the top level; with several matches, read each option's price.
Default options are resolved by the same pricing code used for generation. Prices
that depend on missing call inputs (for example text length) remain unknown.

`searchModels` returns compact schemas by default: declared field names, types,
required flags, enums, defaults and numeric bounds, without descriptions or the
raw OpenAPI document. Each schema is limited to 32 fields and 4 KiB, with individual
values limited to 512 characters of JSON. Omitted values/fields set
`schema.truncated: true`; oversized option defaults set `options_omitted: true`.
The default response is at most 32 KiB of ASCII-escaped JSON; omitted results set
`truncated: true` and match counts remain unchanged. Pass `full_schema: true` to
request complete schemas and defaults. Choice cards still receive complete schemas.
Media-related boolean switches such as `generate_audio` stay boolean, not file inputs.

Every call to the four generation tools or `runFalModel`, including subscription
images priced **Included**, goes through the guard below: a known price,
confirmation above the threshold and the daily cap. Nothing unpriced is
submitted, including unattended calls.

- **Remembered choices.** A call without `source` or `model` uses the
  account's remembered choice for that kind (see below) without asking, in
  interactive and unattended runs alike. With none, an interactive run returns
  `choose` and the chat shows a choice card; an unattended run is skipped and
  reported, never guessed.
- **Saved Settings defaults.** A fal.ai model explicitly saved for image,
  video, audio or 3D generation in the previous Default models page is copied
  into the account's remembered choices once, with its saved options, before
  the run starts. It is then an ordinary remembered choice and always goes
  through the guard below. The previous unguarded fal path is never used for
  these four tools, because it would submit a fal job with no known price,
  cap or confirmation.
- **Saved choices that cannot be read.** If the account's choices cannot be
  read (database, account service or identity failure), nothing saved is
  assumed and nothing runs unasked. An unattended run returns
  `status: "skipped"`, `reason: "choices_unreadable"` with a report; an
  interactive run returns `choose` with `choices_unreadable: true`. A named,
  priced request still runs through the guard. The built-in fal default is not a choice: with nothing
  saved, the user is asked. The ChatGPT-plan image setting
  stays a provider rule and is not migrated. Chat mode has no Settings default.
- **Named choices in interactive runs** outrank a remembered choice for that request and go through the resolver below: price, confirmation and the
  daily cap. A named model the guard cannot price or resolve is refused, never
  run on another path. Outside chat mode, a guarded fal result run on this worker
  also returns the workspace `file_path`, so `editImage` and `imageToVideo`
  can chain on it. A fal request from a peer or a chat with no saved session
  is refused before submission, so no worker-local path is ever returned.
- **ChatGPT-plan images.** While Image generation is set to the ChatGPT plan,
  `generateImage` keeps main's rule (#1796): no fal source or fal model, on
  this worker or a peer, and no fal options in a choice card. The legacy Settings
  handler is never used by unified generation: choose a verified account/model
  from the card or Catalog and remember it to run unasked. A worker that holds
  a fal key applies its own setting too: if its Image generation is the
  ChatGPT plan, it lists no fal image models for peers and refuses to run one.
- **`runFalModel`** runs fal endpoints with a declared text prompt, including
  image-to-video, through the same guard. `output_type` is optional when the
  kind can be inferred. An unattended call needs a remembered or job-stored
  choice and must pass pricing, confirmation and daily-cap checks; otherwise
  it skips and reports why. Interactive calls the guard cannot price or resolve
  are refused, never sent to the previous handler. Missing schemas, unknown
  output types and prompt-less models (such as audio-to-audio) are refused
  interactively or skipped unattended. While images are set to the ChatGPT
  plan it still refuses fal image models.
- **Old or unreachable peers** are listed as unavailable but never make a
  named request ambiguous when a usable candidate matches.
- **Native plan tools.** `GenerateNativeImage` and `GenerateNativeVideo` stay
  registered for sessions running on a verified ChatGPT or SuperGrok account.
- **Stored Native generation settings** decide eligibility per account. A
  `null` group is shown as unavailable and never run. Unreadable stored JSON keeps the
  verified image default and disables video; an account with no stored row keeps
  the provider defaults, including SuperGrok video.

## Catalog and job-choice APIs

`GET /api/v1/generation/catalog` returns `entries`, `favourites`, `kinds` and
`limit`. Optional filters are `kind` (`image`, `video`, `audio`, `3d`), `source`,
`model`, `query` and `limit` (1–50, default 15, per kind/source/holding worker).
With no kind it queries all four. Use search filters to browse large fal catalogs;
this is a bounded on-demand view, not a complete offline model inventory.
Each entry contains `source`, `account`, `worker` (the holding worker ID),
`model`, `kind`, `verified`, `available`, `reason`, `options`, `schema`,
`quick_settings`, `price` and `status` (`available`, `unpriced`, `unavailable`).
Prices retain `currency`, `unit`, decimal-string `amount`/`total` and `status`;
unknown amounts are null, never zero. Native account-disabled routes remain
visible with their verified combination and price but `available: false`.
Ordinary API users see only their own permitted sources, not owner accounts.
Catalog lookup submits no generation; a quote/admission must still recheck cost.

`PUT /api/v1/generation/favourites/{kind}` accepts a choice directly
(`source` or `account`, `model`, optional `worker`, `options`).
`DELETE` clears it. There is one optional favourite per account/kind, returned
in the catalog's `favourites` map. It is a bookmark only: it never updates
remembered/last choices or authorizes unattended generation.

For scheduled automation jobs, owner-only
`GET /api/v1/generation/jobs/{automation_id}/choices` returns `job_id` and a
per-kind `choices` map. `PUT .../choices/{kind}` stores a choice directly;
it must pin `account`, `worker` and `model` (and may include `source`, `options`).
Its response adds the same save-time `price`, `priced`, `quote_error`,
`unattended_skip_reason` and `warning` as a remembered pick (below).
`DELETE .../choices/{kind}` clears it. Writes use the same preview-cookie
protection as preferences. Job choices are account-scoped preferences, separate
from favourites and remembered picks. A run session's stored `automation_id`
binds these choices in the production RunContext; tool arguments cannot invent
a job binding. Each kind uses its job choice first, then its remembered choice.
Unreadable job choices fail closed; current options, price and cap are checked
at execution time, not assumed from the save.

OpenAI/xAI API-key images remain unavailable until a reviewed live-probe record
is added to `app/core/native_image_verification.py`. The initially empty table
pins one exact image combination and explicit output/text-input USD rates per
provider; it drives native settings, catalog eligibility, admission pricing and
the official `images/generations` adapter. No subscription, custom gateway,
redirect, automatic retry or other-provider fallback is used for these API calls.
A table record is not evidence of access on every account: provider rejection
still fails without a second submission. No paid probe is part of this change.

## Conversational preferences

Owner sessions with the app-config tools enabled can call
`app-config.generationPreferences` with `action: "get"`, `"remember"` or `"forget"`.
`get` returns remembered picks (optionally filtered by `kind`) without migrating
legacy settings. Writes require a kind and `confirm: true` after the user's
acknowledgment; `remember` also requires a `choice` using discovered source/account,
model, worker and declared options. No caller-supplied preference identity is used.
Only interactive owner turns can write: unattended runs (including automation,
background, delegated, hidden and voice sessions) cannot save their own choices,
even with `confirm: true`. Discovery callers default to read-only.

An optional `job_id` targets an existing automation instead of the remembered pick.
A job choice must pin account, worker and model. Read/remember/forget use the same
store as the HTTP APIs and the production run context; favourites remain separate.
Writes take effect in subsequent turns, not the current turn's already-bound context.
Saving is not a quote, availability verification, generation or spend approval.
Every execution still validates the stored choice, current price, confirmation and
cap. API-user sessions do not get owner app-config tools; their existing scoped
choices API is unchanged.

## Pending API-image verification (disabled)

`VERIFIED_API_IMAGES` stays empty. These are **probe candidates**, not verified
capabilities or enabled table records. After owner approval, run each exactly once;
record a named smoke-run ID, revision, sanitized account alias, actual model/options,
decoded artifact/container/dimensions, usage/billing and reviewed conservative output
and input USD rates. Only then propose a code-reviewed `Verification` record. An
error or uncertain outcome must not trigger a retry or a different provider.

| Named probe | Endpoint and exact candidate options | Planning allowance | Enablement |
| --- | --- | --- | --- |
| `generation-379.openai-image` | OpenAI `/v1/images/generations`; `gpt-image-2.5-sunburst-2026-09-08`, `size=1024x1024`, `quality=low`, `output_format=png`, `n=1` | About $0.10, including input; recheck current pricing and entitlement first | Pending owner OK and live evidence |
| `generation-379.xai-image` | xAI `/v1/images/generations`; `grok-imagine-image`, `resolution=1k`, `aspect_ratio=1:1`, `response_format=b64_json`, `n=1` | About $0.02; recheck current pricing first | Pending owner OK and live evidence |

The $0.12 combined planning allowance is not a guaranteed charge or permission to
spend. Candidate IDs/rates come from the #507 probe plan and must be rechecked before
submission. Public documentation is not account-specific capability evidence. Other
models, sizes, formats, quality levels or API video routes need separate verification;
no result may be extrapolated to an untested combination.

## Selection contract

With no source/model or remembered/job choice, the tool returns `choose`.
It offers up to four options, preferring different accounts. It returns fewer
when fewer exist; it does not invent accounts to reach a minimum of two.
`Same as last time` comes first when supplied by the caller and still present.
Each option includes the complete options and schema; `quick_settings` contains
at most four defaults. IDs below are placeholders, not selectable models.

```json
{
  "status": "choose", "success": false,
  "price": {"currency": "USD", "unit": "item", "amount": null, "total": null, "status": "unknown"},
  "kind": "image",
  "options": [
    {"source": "fal", "account": "account-b", "worker": "worker-a", "model": "model-b", "options": {"size": "square"}, "kind": "image", "verified": true, "price": {"currency": "USD", "unit": "item", "amount": "0.05", "total": "0.05", "status": "estimated"}, "quick_settings": {"size": "square"}, "schema": null, "available": true, "reason": null, "label": "Same as last time"},
    {"source": "fal", "account": "account-a", "worker": "worker-a", "model": "model-a", "options": {"size": "square"}, "kind": "image", "verified": true, "price": {"currency": "USD", "unit": "item", "amount": "0.05", "total": "0.05", "status": "estimated"}, "quick_settings": {"size": "square"}, "schema": null, "available": true, "reason": null}
  ],
  "message": "The user sees these options as a choice card and can remember a pick. Do not list them again or pick for them; wait for their reply."
}
```

The `choose` result also carries a `message` telling the agent the user sees
a card and to wait for the reply. When the remembered choice is no longer
available (its account or model is gone, its worker is unreachable, or its
saved options are no longer declared), an interactive run returns `choose`
with `remembered_unavailable: true` instead of refusing. That happens only when the
saved options themselves no longer fit; options or a prompt supplied in the
call that the model rejects are refused with `reason: "invalid_request"`. When it has no known
price, an interactive run returns `choose` with `remembered_unpriced: true`,
so the user re-picks a priced option. An unattended run never submits an
unpriced remembered choice: it returns `status: "skipped"`,
`reason: "stored_choice_unpriced"` and a report saying the saved choice has no
known price and can be picked again in Settings > Default models.

The resolver takes a trusted `RunContext`: `remembered_choice`, `job_choice`,
`last_choice`, their per-kind maps `remembered_choices` and `last_choices`,
session identity and whether this is unattended. These are not agent-editable
tool arguments. A job choice takes precedence over a remembered choice. With
neither stored choice, an unattended run returns `status: "skipped"`,
`reason: "no_stored_choice"` and a plain-language report, even if an agent
supplies a source in its tool call. A result that ran the remembered choice has
`remembered: true`. If an unattended call names a source, account, model or worker
that differs from the remembered choice, it returns `status: "skipped"` and
`reason: "named_choice_differs_from_stored"` before any catalog lookup or submission.
The owner must select the named choice; tool arguments never replace the stored one.
A job's own stored choice is the owner's binding for that job: an unattended job run
uses it whatever source, account, model or worker the call names (declared options in
the call still apply, under the same price, confirmation and cap checks), and the
result has `job_choice: true`.
A matching named choice keeps the normal pricing, confirmation and daily-cap checks.

## Remembered choices

Choices are account preferences, in the same store as other per-account
preferences:

- `generation.choice.<kind>` (`image`, `video`, `audio`, `3d`): the remembered
  pick, `{source, account, worker, model, options, origin}`. `origin` is
  `card`, `settings` or `migrated`.
- `generation.lastChoice.<kind>`: the latest pick from a card, offered first as
  "Same as last time" but never run without asking.
- `generation.choicesMigrated`: set once the saved Settings defaults were
  copied. A remembered choice is never overwritten by the migration.

`GET /api/v1/generation/choices` lists them; `PUT /api/v1/generation/choices/{kind}`
saves a pick (`remember: false` records only the last choice). The save response
includes a server-side `price`, `priced`, `unattended_skip_reason` and a `warning` when the
choice cannot run unattended. An unpriced model reports `stored_choice_unpriced`.
An unavailable account/model or failed quote reports `stored_choice_unavailable`,
with the quote's error in `quote_error` and the warning. The optional quote has a
two-second timeout; a failed quote does not lose the saved pick. No price is inferred
for an unpriced model, and saving never reserves or submits a generation. Frontend
clients must display this warning after saving or remembering a pick: the chat
choice card, Settings, Default models and the automation editor show the fresh
price, or the warning and keep the card (or the job's picker) open so it stays
in view. A collapsed chat card still says unattended runs will skip the pick.
`DELETE /api/v1/generation/choices/{kind}` asks again next time.
`GET /api/v1/generation/options/{kind}` lists priced options across accounts
and `POST /api/v1/generation/quote` validates options against the model's
declared schema and prices them, with nothing reserved or submitted. Settings,
Default models shows and changes the remembered choice per kind.

## Confirmation contract

The threshold is **USD 0.10**. Either a per-item price or a batch total above
that threshold requires an explicit approved retry. No budget is reserved and
no provider is called when returning this result:

```json
{
  "status": "needs-confirmation", "success": false,
  "price": {"currency": "USD", "unit": "item", "amount": "0.25", "total": "0.25", "status": "estimated"},
  "kind": "image",
  "choice": {"source": "fal", "account": "account-a", "worker": "worker-a", "model": "model-a", "options": {"size": "square"}},
  "threshold_usd": "0.10", "confirmation_token": "<receipt>",
  "retry": {"source": "account-a", "model": "model-a", "worker": "worker-a", "options": {"size": "square"}, "confirmed": true, "confirmation_token": "<receipt>"}
}
```

Retry the same tool with the original prompt and the `retry` fields only after
the user approves. The receipt is one-use, expires after 15 minutes and is bound
to the session, prompt, kind, account, worker, model, options and quote. It
survives rebuilding the tool server on the next turn. Changing any bound value
requires a new confirmation. A bare `confirmed: true` does not authorize spend.
An unattended run cannot approve its own spend: above the threshold it returns
`status: "skipped"`, `reason: "needs_confirmation"`, with no receipt or retry.

Every result has a `price` object. USD amounts are decimal strings, not floats.
`estimated` is not an invoice. `unknown` has null amounts, never zero. An unknown
price returns `refused` with `reason: "price_unavailable"`, even when confirmed.
Every failed or unknown-outcome submission, local or on a peer, says "Do not
call again without the user's go-ahead." Expired unused receipts are deleted
when the next one is issued.

## Spending and worker protocol

A durable reservation applies the existing daily spending limit (Settings >
Usage) before submission. Reservations remain counted after errors, cancellations or unknown
outcomes, until the next day; they are conservative estimates, not invoices.
Networked generation reserves on the network founder so two account-holding
workers cannot independently spend the same generation budget. Spending by
coordination chats and the work they start is included using the existing
accounting rules.

Worker presence advertises `generation: 2`. Versioned catalog, prepare,
reservation, overrun accounting and execution use authenticated worker controls. An old
worker refuses with an update message; there is no fallback provider or replay
of an uncertain paid submission. Native media and fal outputs are returned as
session-owned artifacts, not peer-local paths. Chat generation cannot read
local media outside chat uploads.

## Current limits of this backend draft

Only verified subscription combinations from the native capability table are
eligible. The exact SuperGrok video combination has a USD 0.02 estimate from
the verified native probe: 1 second, 480p, 16:9, silent. It is not a quote for
other settings or proof of a subscription quota debit. The result carries the
backend's actual reported cost when present, with price status `reported`.
Reported overruns increase the founder's accounting, even beyond the daily cap;
they never authorize another submission. Lower costs do not release the
conservative reservation. A provider-reported cost is not a verified invoice.

fal rates come from the account's pricing API. The shared model-schema service
resolves item/request/generation counts, seconds or minutes with declared
duration units, characters or 1K-character blocks with known input length, and
megapixels with explicit declared dimensions. Fractional billing units round
up. A catalog-only character price has unknown total until text is supplied.
Unsupported units, missing dimensions or ambiguous duration/count parameters
refuse submission rather than guess. Conditional pricing formulas are not
inferred from resolution names.

OpenAI/xAI API media routes remain unavailable: this base's Default models
schema endpoint is fal-only, and its native API capability table declares no
available media combinations or pricing. ChatGPT/SuperGrok image costs are not
reported by their native adapters. Their verified image route runs on the
account's ChatGPT or SuperGrok subscription login and carries no per-item
charge, so it is priced `status: "included"` with a zero amount. It runs
without confirmation and is not counted toward the daily cap: it adds USD 0
to the day's spending (only a day already past the cap refuses it, like every
other generation). An included price is never used as a substitute for an
unknown price. They also keep working through the Settings default and the
native plan tools above. ChatGPT
video and native audio/3D have no verified combinations. No new live provider
generation was exercised for this draft.

Editing and media-transform tools retain their existing handlers and their
pinned model from the catalog.
