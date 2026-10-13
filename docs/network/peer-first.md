# Peer-first worker sync

Workers are the default holders and distributors of network data. An existing
worker continues using its local database while alone. When two members become
reachable, they exchange missing signed operations over LAN or their existing
authenticated HTTPS tunnel. Both sides' offline edits participate in the merge;
the losing version of a conflicting setting remains in the conflicts list.

Cloudflare Tunnel still carries remote traffic. Conversation content and file
parts are encrypted between devices. The account Worker and D1 retain account
and membership information rather than receiving every normal sync operation.
Clerk is used for account sign-in, not peer messages; GitHub is used for code and
distribution, not as a runtime sync service.

## Worker-local settings

The sharing policy in `app/network/policy.py` applies to incoming operations
and state transfers as well as outgoing rows. Local-only settings, including
`claude_auth_mode:<instance id>`, `claude_cloud_config` and
`claude_cloud_secrets`, cannot be inserted, overwritten or deleted by a peer.
A refused operation advances its origin stream without retaining its row in
records, conflicts or the forwarding log; later shared operations still apply.

Upgrading does not delete settings or historical sync data already stored by
older builds. Previously synced copies stay on disk; review the Claude sign-in
method on each worker and change it locally if needed. Existing encrypted
operation history may still be served; this change does not purge it. Updated
receivers refuse these rows, including their tombstones, and updated senders
do not publish new tombstones to retract now-local copies. Upgrade all paired
workers to enforce the policy everywhere. No owner data is purged by this
change.

## Initial copies and long absences

Account admission contains device-wrapped keys and peer addresses. The joining
worker persists that response, waits for an admitted peer to become reachable,
and resumes after a restart. It does not mark an empty or partial cloud feed as
complete. A first worker can still create a network by itself.

A peer may hold current records without their original operation log, for
example after receiving an initial copy. The exchange endpoint detects gaps
between the recipient's frontier and the operations it can actually serve.
It offers a direct state transfer in that case.

State transfers use one PostgreSQL repeatable-read view. A signed manifest binds
the recipient, publisher, network, frontier, and exact encrypted pages. The
receiver verifies every page before applying records and advancing the frontier
in one transaction. Repeated transfers merge records without overwriting newer
local edits. Missing or altered pages and interrupted transactions do not advance
progress. Temporary encrypted pages stay on the serving worker for at most the
15-minute transfer lifetime plus the five-minute cleanup interval. No cloud
snapshot is required. Files transfer separately as signed, encrypted 2 MiB parts,
with a final content-hash check before local storage acknowledges them.

Every page request checks active membership; a generation change expires the
transfer. Deletions, membership history, and key rotations remain part of sync.
This release does not automatically delete local operation history or cloud data.

## Desktop startup and recovery

Desktop keeps its startup screen open while its worker checks account admission,
refreshes known peer addresses, and exchanges updates with paired workers. This
also happens when desktop attaches to a worker that is already running. Each
reachable peer is read through the watermark advertised at the start of the
exchange, including additional operation pages or a verified state snapshot.
The first exchange asks the peer to publish pending edits before advertising its
watermark. Referenced file bytes are fetched before the check reports success.
Older peers without this handshake are reported as incomplete; update all workers.

The startup check has a 60-second budget. Account discovery gets up to 15 seconds;
peer transfers run concurrently with a 30-second limit per peer. Offline or
incomplete checks open saved local data and report that state in Devices. Normal
background sync continues. A failed startup API request stays on the desktop
error screen so the user can retry.

Cloud backup, cloud file fallback, and cloud restore are no longer available.
The account hub handles discovery and membership, not worker data. Historical
cloud storage and migration tables are left intact; this change does not delete
existing stored copies. New workers need an existing worker online. If every
worker copy is permanently lost, peer sync cannot recover it. Keep a separate
backup of the worker's data for that case.

Upgrade all workers for the new state and encrypted-file transfer routes. The
old signed operation exchange and invite snapshot routes remain readable for
compatibility, but old clients cannot consume a new peer's compact state path.
Phones and browsers control workers; they are not full database replicas.

## Shared-service usage

An established worker checks account admission at most once every four minutes;
local changes do not accelerate that poll. Pending enrollment backs off to that
same interval. Recent successful admission-list authorization also satisfies the
tunnel supervisor's authorization check for up to five minutes. Display caches
never provide authorization. A changed binding or observed authorization rejection
invalidates the cached proof. Remote account revocation can take up to five
minutes to stop a connector; local sign-out and peer membership revocation retain
their immediate checks.

For 100 users with two continuously online workers each, the admission cadence
is approximately 72,000 requests/day, plus starts, failures, enrollment, interactive
account activity, and updates. This is an estimate, not a billing guarantee. The
free allowance is shared across the entire Cloudflare account. Local peer polls
do not invoke the account Worker. Measure aggregate requests, database rows, and
tunnel counts as usage grows; do not multiply per-network quotas and assume the
sum is free.

Cloudflare publishes [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/),
[D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/), and
[tunnel account limits](https://developers.cloudflare.com/cloudflare-one/account-limits/).
Clerk's [user allowances](https://clerk.com/pricing) and GitHub's
[Actions allowances](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
are separate. Existing CI already avoids per-merge Docker builds and skips
unchanged scheduled tests; preserve those controls and avoid duplicate releases.

## Verification

Regression coverage includes account bootstrap with the admitting worker offline,
resume after restart, third-device forwarding without original operations,
conflicting offline changes, key rotation and revoked-device denial, encrypted
attachment transfer, tampered/incomplete pages, and zero cloud payload traffic in
default mode. Real PostgreSQL tests exercise concurrent capture and atomic rollback.
Startup tests cover multi-page peer catch-up, repeat desktop launches, and bounded offline fallback.
