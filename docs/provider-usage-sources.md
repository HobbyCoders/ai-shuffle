# Provider usage sources

Source review: 2026-09-30. This describes the implemented readers and their
limits. An endpoint returning a successful response does not establish support
for a third-party integration or permission under a subscription's terms.

## Capability registry

Every source is defined once in `app/core/usage_sources.py`. Each entry names
the endpoint, the credential, the scope, whether the data is read passively or
polled, the minimum interval, the failure policy and the provider dashboard
where the owner can compare the figure. The poller takes its jobs from the
registry, and every polled request goes through a guard that refuses URLs the
registry does not list. There is no fallback to dashboard-internal or alpha
billing endpoints beyond the ones listed: a figure no source can report is shown
as unavailable, with the dashboard link.

The usage API (`GET /api/v1/usage/limits`) returns the registry entry with each
provider row (`source_info`) and with the local Claude reading
(`claude_source`). Rows also carry `last_success_at` and `last_attempt_at`; a
failed attempt moves only the second. `cooldown_until` is the earliest time a
refresh will request that provider again. The usage popover shows the dashboard
link and, once a reading is stale, when it was last updated and when the next
check can run. If the cached rows cannot be read, `providers` is null and the
popover keeps the rows it already shows.

### Matrix

| Source id | Endpoint | Credential | Scope | Measures | Collection / minimum interval | Support label | Dashboard |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `anthropic_headers` | `anthropic-ratelimit-*` response headers | Anthropic API key | Organization | Request/token rate limits | Passive; **not wired yet** (normalizer only) | Response metadata | [Limits](https://console.anthropic.com/settings/limits) |
| `openai_headers` | `x-ratelimit-*` response headers | OpenAI API key | Project | Request/token rate limits | Passive; stored, hidden in the popover | Response metadata | [Limits](https://platform.openai.com/settings/organization/limits) |
| `compatible_headers` | `x-ratelimit-*` from xAI and custom OpenAI-compatible hosts | Provider API key | API key | Request/token rate limits | Passive; stored, hidden | Response metadata | [xAI console](https://console.x.ai/) |
| `codex_headers` | `x-codex-*` headers on Codex inference responses | ChatGPT sign-in | Subscription | Subscription allowance | Passive; stored, hidden in the popover | Unofficial, subscription-specific | [Codex usage](https://chatgpt.com/codex/settings/usage) |
| `codex_usage` | `GET https://chatgpt.com/backend-api/wham/usage` | ChatGPT sign-in used for Codex inference | Subscription | Subscription allowance and plan | Polled on demand, 300 s | Unofficial, subscription-specific | [Codex usage](https://chatgpt.com/codex/settings/usage) |
| `openai_manual` | No endpoint; Analytics usage ledger | User-entered USD balance | API key | Estimated credit balance | Local calculation, no polling | Local estimate, not a provider reading | [Billing](https://platform.openai.com/settings/organization/billing/overview) |
| `openai_admin` | `GET /v1/organization/costs` and `/v1/organization/usage/completions` | Separate OpenAI Admin key and organization ID (local only) | Organization | Provider-reported spend (current UTC month) and completions counts | Polled on demand, 900 s per endpoint | Documented API | [Usage](https://platform.openai.com/usage) |
| `openrouter_key` | `GET {provider host}/api/v1/key` | OpenRouter model key | API key | Provider-reported spend and limit of that key | Polled on demand, 300 s | Documented API | [Keys](https://openrouter.ai/settings/keys) |
| `openrouter_credits` | `GET https://openrouter.ai/api/v1/credits` | Existing model key; an already-saved legacy management key is preserved | Account | Provider-reported credit balance | Polled on demand, 300 s | Documented API | [Credits](https://openrouter.ai/settings/credits) |
| `fal_billing` | `GET https://rest.alpha.fal.ai/billing/user_balance` | fal.ai API key (the media tools key: saved, or `FAL_KEY` / `FAL_API_KEY`) | Account | Provider-reported credit balance | Polled on demand, 300 s | Undocumented endpoint | [Billing](https://fal.ai/dashboard/billing) |
| `claude_rate_limit_events` | Rate-limit events from the running Claude Code session | None beyond the session | Subscription | Subscription allowance (one window at a time) | Passive | Response metadata | [Usage](https://claude.ai/settings/usage) |
| `grok_subscription` | `GET https://cli-chat-proxy.grok.com/v1/billing?format=credits` and `/v1/settings` | SuperGrok sign-in (Grok CLI OAuth) | Subscription | Subscription allowance and plan | Polled on demand, 300 s per endpoint | Unofficial, subscription-specific | [Grok](https://grok.com/) |

What each source cannot report is listed with it in the registry (for example,
the OpenAI Admin API has no prepaid credit balance and the OpenRouter key
endpoint has no account balance). The popover shows these as "Not reported by
this source" next to the dashboard link.

Intervals are application limits, not provider-approved rate guarantees.

## Remaining-first panel and setup

The panel headlines remaining balances, never lifetime spending. OpenRouter
reads its account ledger using the already-configured model key on the pinned
HTTPS official host. A live read on the configured account returned HTTP 200
with that key on 2026-09-30; other keys may be refused. Refusals pause that
capability until its credential changes. Key spending limits remain a separately
labeled fallback ("left on this key"), not account credit. An account balance
suppresses the duplicate key-spend section. Credential rotation/removal or a
provider origin change hides an old model-key account snapshot and prevents
in-flight observations from publishing it.

ChatGPT shows weekly subscription allowance plus extra credits only when their
reported finite balance is greater than zero. Those credits are **not USD** and
are unrelated to OpenAI API prepaid billing. They appear in the ChatGPT row's
expanded details, not on its collapsed header.

OpenAI model keys do not expose a prepaid balance. With neither an Admin
reading nor an entered balance, the OpenAI row is omitted instead of displaying
an unavailable placeholder. Previously configured Admin access still reports
organization spend, not prepaid credit.

In **Settings > Providers > the key > Usage**, enter the current prepaid balance
in USD (two decimals), then **Save**. The `openai_manual` source displays
**~$N left (estimated)**: the entered balance minus this provider's recorded
costs since Save. **Clear** removes this estimate without removing an Admin key
or the inference key. Saving again restarts the baseline and timestamp. The
Open billing link lets you compare and reset it against the actual balance.

The estimate reuses Analytics' `usage_log.cost_usd` aggregation, filtered by the
recorded `provider_id` and an exact UTC millisecond cutoff. It includes priced
turns with reported or estimated token counts, and attributed internal calls;
it does not infer old rows from a session's current provider. Unpriced turns,
unattributed calls, missing/deleted ledger history and spending outside this
app are not deducted. Prices, estimated token counts and booking time can
differ from provider billing; an in-flight turn booked after Save is deducted
in full. Refunds and top-ups are not detected, and the result can go negative.
The non-secret baseline syncs with the provider; each worker subtracts only
its own tracked spend. Paired workers' spend is not included, and both the
Usage tab and popover tooltip state this scope. Replacing the API key clears
the balance and cutoff; resaving or re-encrypting the same key preserves them.
If an Admin reading also exists, it
remains the primary source and the manual estimate appears alongside it.

fal.ai reads its balance with the ordinary fal.ai API key the media tools use.
fal's documented billing API (`api.fal.ai/v1/account/billing`) needs a separate
Admin key, so the registry lists the `rest.alpha.fal.ai/billing/user_balance`
endpoint that accepts the ordinary key, labeled "Undocumented endpoint". The
separate fal Admin billing key and its settings were removed; an upgrade
deletes a stored one. When no key is saved, the poller uses `FAL_KEY` or
`FAL_API_KEY` from the environment, the same fallback the media tools use. A
saved key always wins. The cached balance records a digest of the environment
key (never the key), so a changed or removed environment key hides it.

Claude usage is still read passively from Claude Code's own live usage
reports; refresh cannot fetch usage spent outside a running CLI session. The
app never reads the CLI's sign-in.
Old readings are labeled "Last reported" with their age, on Claude and on every
provider row. A passed reset does not prove
100% remains: the panel retains the last reported percentage and says it awaits
a new report. Compare the provider dashboard for exact idle/external usage.
No browser scraping or challenge bypass is used. Entered-balance tracking is
explicitly a local estimate, never a provider-reported balance.

Two snapshot types have both a passive and a polled writer. `codex_headers` and
`codex_usage` describe the same ChatGPT subscription windows, and the header
capture is free, so it keeps writing whenever a Codex turn runs. The polled
source is the one the row is labeled with, because only a poll bounds how stale
the reading can get: an allowance spent in the ChatGPT web client, in the Codex
CLI or on another worker, or a window that rolled over while the app sat idle,
reaches the panel on the next poll rather than on the next turn. A poll is
skipped when the snapshot was already refreshed inside the interval, so an
actively used account rarely costs a request. A poll that returns no usable
window leaves the previous windows in place instead of erasing them.

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
measure, labeled "Estimated spend" and "app estimate" in Stats. The
`openai_manual` registry source uses collection/support `local_estimate`, no
endpoint or polling, and labels its derived balance as estimated.

### Subscription-specific endpoints

`grok_subscription`, `codex_headers` and `codex_usage` are labeled
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
- Replacing or re-saving the credential changes the digest. The fal.ai API key
  and OpenRouter management keys use the stored setting revision (a fal.ai key
  from the environment uses a digest of that key); the OpenAI Admin
  key is re-encrypted on every save; OAuth tokens change on refresh or sign-in.
  The next poll probes again.
- 404, 429, 5xx and timeouts are not negative-cached. They back off
  exponentially to one hour and honor `Retry-After`. A timeout is recorded
  on the row as `timeout`, next to its last successful reading.

A refusal is remembered for as long as the credential revision lasts, not for a
fixed time. A timed expiry would re-probe a revoked key several times an hour.
To retry after granting access, re-save the credential.

### Refresh cooldown

Opening the popover asks for a forced refresh (`refresh=true`). A forced
refresh skips the monitor's 300 s spacing, but it cannot bypass any of these
limits:

- A worker starts at most one poll pass every 30 s. Inside that window the API
  returns the cached rows with `cooldown_until`.
- A pass skips any provider whose snapshot was written or attempted in the last
  30 s. This applies to every worker that shares the database. It needs no
  budget claim and no round trip to the coordinator. The snapshot timestamp is
  the point where workers coordinate.
- The per-credential request budget still limits upstream requests to one per
  endpoint interval (300 s, or 900 s for OpenAI Admin). If several workers
  claim at the same moment, one request is made and the others are deferred.
- Refreshes started by saving a credential have no cooldown, so a new key is
  probed at once.

On the Claude side, `refresh=true` re-persists and re-broadcasts the in-memory
windows at most once per 30 s. It never makes a request.

The last successful reading is kept after a failure, and the API reports the
failure time separately from the last success.

### Banked resets

When a Codex or SuperGrok banked reset is consumed with outcome `reset`, windows
already at 100% used are removed from that provider's snapshot. The popover
then stops showing the exhausted window on its next read. The consume response
does not say which windows were reset, so windows below 100% keep their last
reading until the next observation: Codex headers on the next inference, or the
next budgeted SuperGrok poll. The reset does not shorten the SuperGrok request
budget.

## Claude usage

AI Shuffle calls no Claude usage or profile endpoint for the meters. The only
Claude usage source is
`claude_rate_limit_events`: the rate-limit events the running CLI reports in
its own output stream. Each event refines one window, so the popover shows
only the windows the CLI has reported, and nothing before the first agent run.
Each window carries its own `observed_at`. A window the CLI last reported long
ago still counts as stale when a newer event has refreshed another window.
The popover links to the user's own usage page,
[claude.ai/settings/usage](https://claude.ai/settings/usage), for the full
picture. Snapshots carry no Claude account identity or plan.

### Claude resets

AI Shuffle has no Claude reset controls. An earlier version read the Claude
Code CLI's sign-in token to show and claim weekly and 5-hour resets; that was
removed because Anthropic's terms do not allow third-party apps to collect or
intermediate Claude.ai session tokens. Settings > Providers > Claude
subscription > Usage links to
[claude.ai/settings/usage](https://claude.ai/settings/usage) instead.
`tests/core/test_claude_cli_no_token_access.py` fails if any app or Fluid
module names the CLI's credentials file or its `claudeAiOauth` token again.

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
covers its Admin-key reader. That reader has a different scope; a working
inference key does not establish access to the administration API. fal's
balance endpoint for the ordinary key is undocumented and may change without
notice; a refusal is shown as unavailable rather than as a zero balance.

The [Claude Code authentication guidance](https://code.claude.com/docs/en/legal-and-compliance)
distinguishes an end user's login in the unmodified native client from collecting
or intermediating subscription OAuth credentials in a third-party app, which is
why the app has no Claude usage reader of its own. The reset controls read the
CLI's token on the owner's own worker, on demand, and never store or refresh
it; whether that use is supported is part of the same open review (#1387). xAI's [documented management billing API](https://docs.x.ai/developers/rest-api-reference/management/billing)
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
