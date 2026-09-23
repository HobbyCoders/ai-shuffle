# Provider usage request budgets

Usage reads return cached snapshots. Manual refresh, background refresh and
duplicate account rows all use the same credential-and-endpoint budget. The
identity includes the endpoint, authorization header and organization scope;
credentials themselves are never written to the budget table or sent to peers.

For an enrolled worker network, its founder is the fixed budget authority.
Workers request a lease through the authenticated `/api/v1/network/peer/usage-budget`
API before contacting a provider. The founder uses the same transaction-backed
claim when polling locally. Each winning worker makes the provider request with
its own credential and saves its own observation. Existing cluster usage reads
continue to collect those observations. The authority receives only the hashed
identity, a random lease token, response status and retry delay.

This deliberately trades refresh availability for bounded requests: an offline,
revoked, missing or older founder without this API means cached usage only. There
is no online-peer election or local fallback during a partition. All participating
workers must be upgraded; older workers still enforce only their local budgets.
The founder's database must remain available to resume polling. Automatic
coordinator failover or reassignment is not implemented.

Each claim reserves at least ten minutes (or the source interval plus thirty
seconds when longer). An upstream observation has a four-minute total
deadline, including all pages, so a slow response cannot outlive its lease.
Completion releases the lease into the normal cooldown. A late or replayed
completion cannot overwrite a newer claim. If the worker crashes or the
completion cannot reach the authority, the ten-minute reservation remains in
place; its unknown response status cannot be applied until a later observation.
No provider request is replayed after an uncertain claim response.

Ordinary usage reads never wait on an upstream request. A stale Claude reading
starts one shared background refresh (at most one attempt per minute per
worker); explicit refreshes join the same task and still need a budget lease.
A capability refused with 401, 403, 405, 410 or 501 is also recorded on the
worker's snapshot for that credential revision, so later polls skip it without
claiming a lease; see the [provider source matrix](../provider-usage-sources.md#failure-policy-and-negative-caching).

Current application budgets (not provider-approved rate guarantees), taken
from the source registry in `app/core/usage_sources.py`:

| Source | Successful observation minimum interval |
| --- | --- |
| OpenRouter key and account credits, each endpoint | 300 seconds |
| Grok subscription billing and settings, each endpoint | 300 seconds |
| Claude OAuth usage | 300 seconds |
| OpenAI Admin usage or costs, each endpoint | 900 seconds |
| fal.ai Admin billing | 300 seconds |

All add zero to thirty seconds of jitter. Failures back off exponentially to
one hour, honoring a longer `Retry-After`. Authentication and unsupported-capability
responses (401, 403, 405, 410, 501) pause that identity until explicit credential
configuration resumes it, or a changed credential creates a new identity.
Resuming a paused identity cannot bypass a successful-observation cooldown.
Configuration on another worker sends the resume to the same authority; if it
cannot reach it, the API reports that the key was saved and polling still needs
the founder to reconnect.

Regression coverage counts actual HTTP-client requests through the peer API
with separate worker databases, including simultaneous refresh, auth pauses,
Retry-After, credential changes, cancellation, coordinator outage and late
completion. Opt-in PostgreSQL contracts cover competing connections, pool
restart and migration of existing paused rows.

The [provider source matrix](../provider-usage-sources.md) describes credentials,
account scopes, support gaps and remaining live acceptance.
