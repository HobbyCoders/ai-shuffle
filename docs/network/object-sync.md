# Object sync protocol v2

This is the optional cloud-backup transport. It defaults off; normal worker sync
uses [direct peer exchange](peer-first.md). Enabling backup is a per-worker choice
in Settings > Devices. Existing cloud copies remain readable after disabling it.

The local PostgreSQL transaction remains the first durability boundary. Cloud
durability is acknowledged only after an immutable private object is uploaded,
verified, and its pointer is committed in D1. LAN and authenticated tunnel peer
delivery continue independently of this process.

## Storage and compatibility

New writes are gzip bundles of the existing end-to-end encrypted, individually
signed v1 envelopes. Compression never exposes decrypted record contents to the
hub. Objects are addressed by SHA-256. A signed descriptor binds the hash, byte
length, network, publishing device, and operation count. The hub authenticates
the publishing device and verifies object integrity before committing a cursor
range. Cursors describe delivery order only; they never substitute for each
origin's incarnation and sequence number.

D1 retains the account directory, one descriptor per bundle, and exact stream
intervals for duplicate discovery. Existing D1 history remains readable. Old
clients receive envelopes extracted from bundles through the existing feed;
new clients receive object descriptors and short-lived object-specific URLs.
There is no indefinite per-operation dual write. Migration archives old D1
ranges only after read-back equality verification, without deleting source rows
in the initial rollout.

Presigned transfers carry no server or bucket credential. Grants expire within
20 seconds and are restricted to one method and one immutable object. Each
grant request and commit rechecks the live account/device binding. An already
issued grant remains valid for its bounded lifetime; revocation prevents new
grants. Upload signatures bind length and an integrity checksum and require
create-only semantics. A failed transfer retains the durable local outbox.

## Recovery and retention

Recovery checkpoints include a consistent snapshot, the signed membership and
key history, and historical decryption keys encrypted under the current safe
network key. Checkpoint publication is separate from regular batches. A complete
verified checkpoint and its tail must exist before redundant sync bookkeeping
can be compacted. User-visible records and files are never retention targets.
Failed replacements leave the last valid checkpoint intact. Initial deployment
does not delete legacy history or committed bundles.

File backups use encrypted 2 MiB parts and a final manifest; checkpoint roots
reuse verified file manifests instead of uploading the bytes again. A snapshot
with missing local files or missing cloud receipts is not published. Roots are
captured in one PostgreSQL repeatable-read transaction and published after every
part commits. Recovery verifies all parts, membership signatures, historical keys,
file hashes, and the snapshot frontier before completing bootstrap. A prepared
snapshot is replaced when its generation or write key becomes stale.

Automatic garbage collection is deliberately disabled in this initial rollout.
No remote DELETE operation is implemented. Enabling compaction later requires a
separate retention migration and verified replay for old clients as well as new
clients; a checkpoint receipt alone is not permission to delete history.

## Traffic and costs

Upload at most once per minute while changes accumulate, or when a bounded
batch fills. Do not upload empty batches. Persist prepared bytes and acknowledgments
across process restarts. Poll the manifest conservatively while idle; live peer
delivery remains prompt. Usage reservations enforce the existing per-network
caps without claiming to enforce account-wide billing. R2 Standard allowances
are shared and are not a hard spending cap. No other storage class or paid
auxiliary service is needed.

## Rollout

Deploy the additive D1 schema, private R2 Standard binding, signing secrets, and
hub support before releasing clients. The feature switch controls new object
writes; reads of already committed objects must remain available on rollback.
Test migration equality, recovery with all original devices offline, key rotation,
out-of-order delivery, cross-account denial, process interruption, and concurrent
commit behavior before enabling production writes.

The deployment workflow binds private Standard bucket `shuffle-network-sync`
as `SYNC_BUCKET` and sets `SYNC_R2_ACCOUNT_ID` and `SYNC_R2_BUCKET`. The Worker
`shuffle-account-hub` needs two secrets from an R2 S3 credential with **Object
Read & Write** permission scoped to this bucket:

- `SYNC_R2_ACCESS_KEY_ID`
- `SYNC_R2_SECRET_ACCESS_KEY`

The `object_writes` workflow input defaults to false. Leave it false while
installing the additive schema or before live S3 PUT/GET verification. Missing
signing configuration also disables object writes and direct reads. With
credentials present, disabling writes retains direct reads; keep the bucket,
descriptors, and secrets during rollback. Clients can finish a prepared change
batch through the legacy route. No R2 or database credential belongs in a client.

The status API exposes pending changes, pending files, download dependencies,
batch due time, checkpoint identity/time, and the current quota reservation.
Cloudflare account analytics remains the source of billing/operation totals;
per-network reservations are not an account-wide cost meter. Check shared R2
storage/Class A/Class B usage and Worker/D1 totals before enabling writes and
after a representative live catch-up. Production S3 grants and installed-app
acceptance must be tested in addition to the local emulator and PostgreSQL suite.
