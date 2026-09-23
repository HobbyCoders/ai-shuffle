# Provider usage sources

Source review: 2026-09-22. This describes the implemented readers and their
limits. An endpoint returning a successful response does not establish support
for a third-party integration or permission under a subscription's terms.

## Capability registry

Every source is defined once in `app/core/usage_sources.py`. Each entry names
the endpoint, the credential, the scope, whether the data is read passively or
polled, the minimum interval, the failure policy and the provider dashboard
where the owner can compare the figure. The poller takes its jobs from the
registry, and every polled request goes through a guard that refuses URLs the
registry does not list. There is no fallback to dashboard-internal or alpha
billing endpoints: a figure no source can report is shown as unavailable, with
the dashboard link.

The usage API (`GET /api/v1/usage/limits`) returns the registry entry with each
provider row (`source_info`) and with the local Claude reading
(`claude_source`). Rows also carry `last_success_at` and `last_attempt_at`; a
failed attempt moves only the second. The usage popover shows the source, the
support label, the scope, what the figure measures, both timestamps and the
dashboard link.

### Matrix

| Source id | Endpoint | Credential | Scope | Measures | Collection / minimum interval | Support label | Dashboard |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `anthropic_headers` | `anthropic-ratelimit-*` response headers | Anthropic API key | Organization | Request/token rate limits | Passive; **not wired yet** (normalizer only) | Response metadata | [Limits](https://console.anthropic.com/settings/limits) |
| `openai_headers` | `x-ratelimit-*` response headers | OpenAI API key | Project | Request/token rate limits | Passive; stored, hidden in the popover | Response metadata | [Limits](https://platform.openai.com/settings/organization/limits) |
| `compatible_headers` | `x-ratelimit-*` from xAI and custom OpenAI-compatible hosts | Provider API key | API key | Request/token rate limits | Passive; stored, hidden | Response metadata | [xAI console](https://console.x.ai/) |
| `codex_headers` | `x-codex-*` headers on Codex inference responses | ChatGPT sign-in | Subscription | Subscription allowance | Passive; no extra request | Unofficial, subscription-specific | [Codex usage](https://chatgpt.com/codex/settings/usage) |
| `openai_admin` | `GET /v1/organization/costs` and `/v1/organization/usage/completions` | Separate OpenAI Admin key and organization ID (local only) | Organization | Provider-reported spend (current UTC month) and completions counts | Polled on demand, 900 s per endpoint | Documented API | [Usage](https://platform.openai.com/usage) |
| `openrouter_key` | `GET {provider host}/api/v1/key` | OpenRouter model key | API key | Provider-reported spend and limit of that key | Polled on demand, 300 s | Documented API | [Keys](https://openrouter.ai/settings/keys) |
| `openrouter_credits` | `GET https://openrouter.ai/api/v1/credits` | Separate management key (local only) | Account | Provider-reported credit balance | Polled on demand, 300 s | Documented API | [Credits](https://openrouter.ai/settings/credits) |
| `fal_billing` | `GET https://api.fal.ai/v1/account/billing?expand=credits` | Separate fal Admin key (local only) | Account | Provider-reported credit balance | Polled on demand, 300 s | Documented API | [Billing](https://fal.ai/dashboard/billing) |
| `claude_oauth_usage` | `GET https://api.anthropic.com/api/oauth/usage` | Claude subscription sign-in of the active account | Subscription | Subscription allowance | Polled in the background, 300 s | Unofficial, subscription-specific | [Usage](https://claude.ai/settings/usage) |
| `claude_rate_limit_events` | Rate-limit events from the running Claude Code session | None beyond the session | Subscription | Subscription allowance (one window at a time) | Passive | Response metadata | [Usage](https://claude.ai/settings/usage) |
| `grok_subscription` | `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits` and `/v1/settings` | SuperGrok sign-in (Grok CLI OAuth) | Subscription | Subscription allowance and plan | Polled on demand, 300 s per endpoint | Unofficial, subscription-specific | [Grok](https://grok.com/) |

What each source cannot report is listed with it in the registry (for example,
the OpenAI Admin API has no prepaid credit balance and the OpenRouter key
endpoint has no account balance). The popover shows these as "Not reported by
this source" next to the dashboard link.

Intervals are application limits, not provider-approved rate guarantees.

Banked reset-credit inventories for Codex and SuperGrok are read only when the
owner opens that settings panel. They are not usage readings and are not in the
registry.

### Four kinds of figures

The registry labels every reading with one of four measures, and the UI keeps
them apart:

- **Request/token rate limits**: short-term headroom from response headers.
- **Subscription allowance**: plan windows (Claude, ChatGPT/Codex, SuperGrok).
- **Provider-reported spend**: spend the provider reports for an organization
  or key.
- **Provider-reported credit balance**: remaining purchased credit.

App-local cost figures (token counts multiplied by catalog prices) are a fifth
thing and never come from these sources. The Stats card labels them
"Estimated spend" and "app estimate".

### Subscription-specific endpoints

`claude_oauth_usage`, `grok_subscription` and `codex_headers` are labeled
"Unofficial, subscription-specific" in the registry, the API and the popover.
They are used by the providers' own subscription clients and are not published
as third-party APIs. The app reads them only for the account the owner signed
in. The label describes the source; it is not a statement about support or
terms. The support review for these readers remains open in #1387.

## Failure policy and negative caching

Polled requests share the durable, cross-worker budget described in
[request budgets](network/usage-budgets.md). On top of that, a refused
capability is cached per credential revision:

- 401 and 403 are recorded as `billing_access_denied`; 405, 410 and 501 as
  `unsupported`. The snapshot stores a digest of the source, credential and
  credential revision (never the credential).
- While the digest matches, the poller skips that capability entirely: no
  budget claim, no peer round trip and no upstream request.
- Replacing or re-saving the credential changes the digest. The fal and
  OpenRouter management keys use the stored setting revision; the OpenAI Admin
  key is re-encrypted on every save; OAuth tokens change on refresh or sign-in.
  The next poll probes again.
- 404, 429, 5xx and timeouts are not negative-cached. They back off
  exponentially to one hour and honor `Retry-After`.

The last successful reading is kept after a failure, and the API reports the
failure time separately from the last success.

## Claude subscription reads

Ordinary reads of `/api/v1/usage/limits` never wait on the Claude endpoint. A
stale read returns the cached snapshot and starts one shared background
refresh; further stale reads within 60 seconds of an attempt do not start
another. An explicit refresh uses the same task and the same budget, so it
cannot bypass the 300-second minimum. Workers with the same Claude sign-in
share one budget through the network founder, so simultaneous reads on several
workers produce one upstream request.

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
readers are labeled unofficial and subscription-specific; deciding their
supported replacement or default behavior remains part of issue #1387.

Fixture tests cover the registry, credential isolation, denied access,
replacement, deletion, late responses and malformed ledgers, and count upstream
requests at the HTTP transport. Live acceptance still requires configuring the
owner's credentials through the private settings form, comparing each
capability with the dashboard linked above, and observing refresh and
revocation behavior across deployed workers. Do not paste credentials into an
issue or test fixture. This document does not claim that live acceptance or the
subscription support review is complete.
