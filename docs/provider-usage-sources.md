# Provider usage sources

Source review: 2026-09-21. This describes the implemented readers and their
limits. An endpoint returning a successful response does not establish support
for a third-party integration or permission under a subscription's terms.

| Capability | Source and credential | Scope and meaning | Collection / minimum interval |
| --- | --- | --- | --- |
| OpenRouter model key | `/api/v1/key`, the configured model key | That key's provider-reported spend and configured spending limit; not the account balance | On demand, 5 minutes |
| OpenRouter account credit | Official `/api/v1/credits`, a separate optional management key | Purchased credit minus reported account usage, in USD; separate from model-key limits | On demand, 5 minutes |
| OpenAI API usage | Official `/v1/organization/costs` and `/v1/organization/usage/completions`, separate local Admin key and organization ID | Current UTC month organization spend; completions request/token totals only. No prepaid credit or ChatGPT allowance | On demand, 15 minutes per endpoint |
| fal.ai account credit | Official `/v1/account/billing?expand=credits`, separate local Admin key | Account current credit balance and reported currency; the generation key is not a fallback | On demand, 5 minutes |
| ChatGPT / Codex subscription | Metadata on existing inference responses | Reported subscription windows; not OpenAI API credit. Availability depends on the native transport exposing the fields | Passive; no extra usage request |
| Claude subscription | Native rate-limit events plus existing `/api/oauth/usage` reader using the selected account's OAuth credential | Account subscription windows; current persisted data does not distinguish the two observation sources | Passive events and on-demand polling, 5 minutes; direct OAuth reader support review remains open |
| SuperGrok subscription | Existing `cli-chat-proxy.grok.com/v1/billing?format=credits` and `/v1/settings` readers with subscription OAuth | Subscription allowance and plan settings; not xAI API billing | On demand, 5 minutes per endpoint; support review remains open |
| xAI API and other compatible APIs | Inference response rate-limit metadata where present | Short-term request/token headroom, not account balance; xAI rows are excluded from the account usage popover | Passive; no inferred billing endpoint |

Intervals above are application limits, not provider-approved rate guarantees.
See [request budgets](network/usage-budgets.md) for shared founder coordination,
jitter, backoff, total deadlines and the cached-only behavior when the authority
is unavailable. Authentication and unsupported-capability failures pause a
credential/endpoint identity. Re-saving a billing credential explicitly resumes
its budget; changing it creates a new identity. A refresh button cannot bypass
the budget. Retained readings expose the time of the last successful observation,
not the latest failed attempt.

## OpenRouter setup and account separation

Under an enabled official OpenRouter provider, Account credits accepts a separate
management key. The key is encrypted, write-only through the API, and excluded
from worker-to-worker settings synchronization. Only the fixed official credits
URL receives it; model requests continue using the model key. Custom provider
hosts cannot configure this reader.

OpenRouter documents the management credential requirement for the
[credits endpoint](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits).
Its [management-key documentation](https://openrouter.ai/docs/guides/overview/auth/management-api-keys)
also describes key creation, editing and deletion powers. The settings form
discloses these broader permissions; this integration only reads credits.
Management keys cannot be used for model completions.

The configured management key determines the observed account. The credits
response does not provide an account ID to compare against the model key, so the
app does not verify that the two keys belong to the same account. Configure the
intended account's key and compare the result with its official dashboard.
The usage popover keeps the model key's spend/limit and the management account's
credit ledger in separate rows with source and scope labels. Legacy combined
snapshots no longer expose account credit on the model-key row.

Replacing or removing a management key invalidates its cached account balance
in the same transaction. In-flight observations must still match the credential
revision and an enabled official provider before writing. Provider removal
deletes its local management credential and snapshot. A failed observation keeps
only the last successful reading for the same credential; it cannot carry a
previous account's balance into a replacement account. No app-local token-cost
estimate is presented as a provider-reported account balance.

## Documentation and remaining acceptance

The [OpenAI organization usage reference](https://developers.openai.com/api/reference/python/resources/admin/subresources/organization/subresources/usage)
and [fal account billing reference](https://fal.ai/docs/platform-apis/v1/account/billing)
cover their Admin-key readers. These readers have different scopes; a working
inference key does not establish access to either administration API.

The [Claude Code authentication guidance](https://code.claude.com/docs/en/legal-and-compliance)
distinguishes an end user's login in the unmodified native client from collecting
or intermediating subscription OAuth credentials in a third-party app. This
review does not establish support for this app's direct OAuth usage reader.
Likewise, xAI's [documented management billing API](https://docs.x.ai/developers/rest-api-reference/management/billing)
does not establish support for the existing SuperGrok proxy endpoints. Those
existing readers have not been reclassified as documented integrations by this
change. Deciding their supported replacement/default behavior remains part of
issue #1387.

Fixture tests cover credential isolation, denied access, replacement, deletion,
late responses and malformed ledgers. Live acceptance still requires configuring
the owner's credentials through the private settings form, comparing each
capability with the appropriate provider dashboard, and observing refresh and
revocation behavior across deployed workers. Do not paste credentials into an
issue or test fixture. This change does not claim that live acceptance or the
subscription support review is complete.
