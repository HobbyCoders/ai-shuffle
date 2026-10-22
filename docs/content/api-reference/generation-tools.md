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

This first step is additive: nothing that generates on the previous release
stops working.

- **Settings default.** A call without `source`, `model`, `worker` or
  `options` runs the Default models choice exactly as before: the configured
  fal.ai model, or the ChatGPT-plan image model when Image generation is set to
  it. Unattended runs (automations, worker and Task sessions, voice, hidden
  sessions, API-key turns) also keep that path for every call the previous
  tool schema could express. Chat mode has no Settings default.
- **Named choices** go through the resolver below: price, confirmation and the
  daily cap. A named fal model that cannot be priced or resolved (unknown
  price, a model outside the text-to catalog, undeclared inputs) runs on the
  Settings default path as before. A cap, confirmation or chat-mode refusal
  never falls back. Outside chat mode, a guarded fal result run on this worker
  also returns the workspace `file_path`, so `editImage` and `imageToVideo`
  can chain on it. A fal request from a peer or a chat with no saved session
  is refused before submission, so no worker-local path is ever returned.
- **ChatGPT-plan images.** While Image generation is set to the ChatGPT plan,
  `generateImage` keeps main's rule (#1796): no fal source or fal model, on
  this worker or a peer, and no fal options in a choice card. The configured
  plan model ID (or its model tail) is accepted as before. A worker that holds
  a fal key applies its own setting too: if its Image generation is the
  ChatGPT plan, it lists no fal image models for peers and refuses to run one.
- **`runFalModel`** runs any fal endpoint again, including image-to-video and
  audio-to-audio, with `output_type` optional. A priced call with a text prompt
  gets the same guard; otherwise it takes the previous path. While images are
  set to the ChatGPT plan it still refuses fal image models.
- **Old or unreachable peers** are listed as unavailable but never make a
  named request ambiguous when a usable candidate matches.
- **Native plan tools.** `GenerateNativeImage` and `GenerateNativeVideo` stay
  registered for sessions running on a verified ChatGPT or SuperGrok account.
- **Stored Native generation settings** decide eligibility per account. A
  `null` group is never offered or run. Unreadable stored JSON keeps the
  verified image default and disables video; an account with no stored row keeps
  the provider defaults, including SuperGrok video.

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
  ]
}
```

The resolver takes a trusted `RunContext`: `remembered_choice`, `job_choice`,
`last_choice`, session identity and whether this is unattended. These are not
agent-editable tool arguments. A job choice takes precedence over a remembered
choice. Storage is not implemented here. With neither stored choice, an
unattended run returns `status: "skipped"`, `reason: "no_stored_choice"` and a
plain-language report, even if an agent supplies a source in its tool call
(the Settings default above is the exception, unchanged from before).

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

A durable reservation applies the existing daily assistant cap before
submission. Reservations remain counted after errors, cancellations or unknown
outcomes, until the next day; they are conservative estimates, not invoices.
Networked generation reserves on the network founder so two account-holding
workers cannot independently spend the same generation budget. Ordinary
assistant spending is included using the existing accounting rules.

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
reported by their native adapters, so naming those accounts in the unified
tools still refuses; they are not labelled free or included in a plan. They keep
working through the Settings default and the native plan tools above. ChatGPT
video and native audio/3D have no verified combinations. No new live provider
generation was exercised for this draft.

The choice-card UI, remembered-choice storage and Settings changes are separate
steps. Editing and media-transform tools retain their existing handlers.
