<!--
title: Unified generation tools
description: Source selection, prices, confirmation receipts and unattended generation contracts
order: 14
-->

# Unified generation tools

`ai-tools.generateImage`, `generateVideo`, `generateAudio` and `textTo3D`
accept `prompt`, optional `source` (provider or exact account ID), `model`,
`worker` and `options`. `options` uses the selected model's declared input
schema, the same schema service used by Default models. `searchModels` looks
up sources, accounts, holding workers, models, verified status, prices and
schemas on demand. No model catalog is added to session prompts.

The provider-bound `GenerateNativeImage` and `GenerateNativeVideo` aliases are
not registered. Their verified native adapters are used behind the unified
names. No Native generation Settings screen is planned. Saved defaults are
not silently treated as remembered choices; their migration belongs to step 3.

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
plain-language report, even if an agent supplies a source in its tool call.

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

Every result has a `price` object. USD amounts are decimal strings, not floats.
`estimated` is not an invoice. `unknown` has null amounts, never zero. An unknown
price returns `refused` with `reason: "price_unavailable"`, even when confirmed.

## Spending and worker protocol

A durable reservation applies the existing daily assistant cap before
submission. Reservations remain counted after errors, cancellations or unknown
outcomes, until the next day; they are conservative estimates, not invoices.
Networked generation reserves on the network founder so two account-holding
workers cannot independently spend the same generation budget. Ordinary
assistant spending is included using the existing accounting rules.

Worker presence advertises `generation: 1`. Versioned catalog, prepare,
reservation and execution operations use authenticated worker controls. An old
worker refuses with an update message; there is no fallback provider or replay
of an uncertain paid submission. Native media and fal outputs are returned as
session-owned artifacts, not peer-local paths. Chat generation cannot read
local media outside chat uploads.

## Current limits of this backend draft

Only verified subscription combinations from the native capability table are
eligible. API-key accounts without a declared media schema remain unavailable.
Subscription prices and non-flat fal pricing formulas have not been established
here, so those routes refuse submission rather than fabricate a price. Flat fal
item/request quotes come from the account's pricing API and use the validated
count. These limitations must be resolved before step 1 can be called complete.
No live provider generation was exercised for this draft.

The choice-card UI, remembered-choice storage and Settings changes are separate
steps. Editing and media-transform tools retain their existing handlers; this
contract covers the four generation entry points and text-based `runFalModel`.
