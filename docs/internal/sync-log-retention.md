# Transcript sync-log retention

This changes the **duplicate operation log**, not chat retention. Maintenance
never deletes `session_events`, Fluid stores, legacy transcripts, search text,
`sync_records`, membership certificates or pending envelopes. No physical
rewrite runs automatically. Disable the new pass with
`AI_SHUFFLE_PERF_TRANSCRIPT_OP_RETENTION=0` (`false`/`off` also work).

## Why it grew, and who needs old operations

The supplied read-only field measurement was a 42 GB application database:
`sync_ops` 17 GB (4.5 GB heap, 12 GB TOAST, 0.9 GB indexes; rounded), 3.9 million
rows. `session_events` accounted for 3.8 million operations / 14 GB. Most event
records have exactly one operation, so the old “never delete a record's latest
op” condition retained a second signed/encrypted payload indefinitely. Memory
(1,748 ops / 946 MB) and session headers (48k / 176 MB) were much smaller.
These are supplied measurements, not a new production query or an after-size
forecast. Age distribution and peer backlog determine actual eligible bytes.

Audit of the production readers/writers:

| Path | What it reads; what a missing old op means |
| --- | --- |
| `app/db/network.py:ops_since`, `exchange_batch`; `app/api/network.py:peer_exchange`; `app/network/transport.py:PeerLink._sync_once` | Bounded signed log deficits per origin/incarnation. `requires_snapshot` compares the available count with the entire advertised interval, so a gap anywhere (not only below MIN(seq)) sets `snapshot_required`. Push refuses an incomplete prefix. |
| `network.py:local_watermarks`, `_log_heads`, `next_origin_seq`, `own_watermark` | Received applied frontiers live in `sync_watermarks`; local/forwarding heads and local sequence allocation use the remaining log. Keep the **last envelope of every origin/incarnation**, even when ancient. Both pruning passes protect the same captured signed heads **in every collection**, even when another origin superseded their record. |
| `app/api/network.py:peer_op`; `transport.py:PeerLink._check_rollback` | A retained signed op at-or-after the requested seq proves an own-stream backup rollback. Keeping a stream-head envelope preserves that proof; it need not be every event's envelope. |
| `network.py:ops_after_ingest` | Log-tail helper; no production callers (only its store test). It is not a durable chat consumer. |
| `network.py:sync_pending` helpers; `engine.py:apply_batch` / pending drain | Independently stored envelopes for ordering, FK and key recovery. Retention never touches this table. Applied streams are contiguous; a receiver behind a pruned deficit uses state copy rather than inventing an acknowledgement. |
| `engine.py:snapshot_manifest`, `_snapshot_page`, `snapshot_item`; `peer_snapshot.py` | Enumerate **sync_records plus current materialized rows/tombstones**, not sync_ops. Captures use a repeatable-read frontier; pages apply before adopting it. Pruning does not remove versions, tombstones or rows. |
| `transcripts.py:register`, `serve`, `_session_items`, `apply_reply`, `loop`; `network.py:insert_snapshot_events` | Format 2 copies headers/settings first and persists a transcript-backfill floor/generation. The scheduled loop fills every published event from canonical rows + sync_records, with durable per-session progress and LWW/idempotent inserts. A partial source refuses to serve state/transcripts. A copy carries records, not operations. |
| `engine.py:enroll_from_response`; `PeerLink.receive_state` | New peers already take state copies. Format 1 includes transcripts; format 2 defers them to backfill. No new wire fields are needed. |
| `engine.py:revoke_device`, membership replay, `keyring.py`, recovery | Signed certificates/revocations are in network_membership_records and key state is persisted separately. Snapshots wrap keys for current recipients. Old event envelopes are not the membership/key authority. Revoked devices are excluded from retention pins immediately. |
| `engine.py` conflict/LWW handling; `network.py:sync_conflicts` | Current versions/hashes are in sync_records and losing payloads are in their own expiring conflict rows. Keeping every event operation is not conflict recovery. This PR does not shorten conflict retention. |

The old latest-op comment described a conservative snapshot safety assumption,
not the current implementation. It does **not** apply to each append-only event:
state copies reconstruct that event from its row/version. The stream-head
exception above is real and remains. Other collections keep their existing
conservative latest-record retention rule.

## Exact policy

The existing daily maintenance loop (five-minute startup delay) runs the new
transcript pass before the generic superseded-op scan, so millions of unique
ids cannot starve it. For each origin/incarnation it deletes only operations:

1. In `session_events`, received/created locally more than 30 days ago.
2. At or below the minimum **actually advertised** frontier of every recent,
   active **worker** peer; absent streams/frontiers mean zero. Consumers (phones
   and browsers) are not database replicas and do not pin it.
3. Strictly below the stream's retained head (including old incarnations).
4. With a materialized sync version at least as new as the operation, and either
   its canonical event row/order_seq or a deletion tombstone still present.
   Damaged/incomplete materializations are kept, not assumed disposable.

Recent means a successful authenticated sync observation within
`AI_SHUFFLE_SYNC_OP_PEER_GRACE_DAYS` (default **30**, positive integer days).
Invalid/non-positive configuration fails closed for the new pass. A peer that
has been offline longer stops pinning, but is **not revoked or forgotten**.
When it returns behind the retained interval it automatically takes a current
state copy and fills its transcripts, then resumes ordinary operation sync.
The trade-off is a full catch-up copy for a long-offline peer instead of an
unbounded second copy of all history on every worker.

Only the signed/local membership's active workers count; signed revocation
unpins immediately. Never-contacted peers receive a one-time grace baseline.
Maintenance stores authenticated observation times in the existing local
`sync_peers.last_contact`, and uses its original `updated_at` as that baseline
when no contact exists. Repeated maintenance does not renew the baseline.
Do **not** use `network_members.last_seen`: directory/address discovery also
updates it without contacting that worker.

Frontiers stay in memory, copied when exchange observes them, not advanced by
optimistic push accounting. Contact times are persisted only during maintenance
(no extra per-exchange SQL). After restart, a recent peer without a new frontier
pins at zero until it exchanges again or its persisted grace expires. Existing
expired peers stay expired across restart. A new/missing peer row gets a full
grace rather than being treated as already absent.

`delete_batches` owns each at-most-5,000-row transaction, releases its connection
between batches, applies the existing 30-second statement deadline, and checks
the maintenance cancellation event before starting another batch. Candidates
keyset-page the existing `(origin_device_id, incarnation, seq)` unique index;
no table rewrite, new index, migration, payload decryption or chat compaction is
in this change. Each pass logs structured `deleted_ops`, `bytes_estimate`, peer
frontiers, expired peers and per-stream retained frontiers. `bytes_estimate` is
`pg_column_size(envelope)` summed over deleted rows, not promised filesystem
savings (heap/index/TOAST overhead and bloat are not included).

This is a bound by recent writes + genuinely unacknowledged recent-peer backlog
+ one stream anchor + unreconstructable records, not a fixed row cap. A broken
but continually exchanging peer that never advances can still pin its backlog;
we do not discard its recent unacknowledged work to satisfy a size target.

## Evidence and limits

`tests/core/network/test_transcript_op_retention_postgres.py` starts one private
PostgreSQL 16 cluster with independent worker schemas/identities. It uses real
event append/catalog publication, signed authenticated HTTP exchange through the
actual peer router, automatic state capture/apply and transcript backfill.
The three-worker case disconnects C, lets B commit/advertise the new tail, expires
C's actual contact, prunes beyond C's prefix, then reconnects C through ordinary
sync. All event ids, seqs and payloads match, without duplicates; B and C both
receive the following events through normal operation sync. Recent unknown
peers, minimum frontiers, restart pinning, never-contacted expiry, revocation /
rekey, kill switch, invalid config, cancellable committed batches, materialization
checks, stream anchors and legacy full-copy enrollment are also covered. The
rollback regression publishes aged events followed by a session-header head,
then a valid signed header update from another worker. Both retention passes
remove all event envelopes without removing the superseded header anchor. After
rewinding the source outbox to an older backup frontier, an authenticated proof
request makes `_check_rollback` bump the identity incarnation; the next signed
operations use the new stream and apply at the relay without sequence reuse.
This simulates an older outbox, not a full managed-database restore rehearsal.
The merged whole-tail compatibility check is covered with real PostgreSQL for
both disk and in-flight peer-snapshot caches serving a legacy recipient. A
complete compatible tail reuses the old transfer; pruning that tail down to its
signed head invalidates it and produces a fresh, reusable frontier. Both cache
paths explicitly opt into the whole-tail check; retained heads are never
mistaken for a complete catch-up log.

The deterministic count ratchet was run on unchanged main before implementation:
all three runs retained **12 of 12** aged unique event ops and failed the target
of one anchor. With retention, all three retain **1 of 12**. Canonical event,
Fluid and transcript-store rows and sync_records are fingerprinted unchanged.
This tiny fixture is a correctness/count lab, not a production backup benchmark.
No real-worker UI, installed mixed-version binaries, update timing, production
space reclaim or production after-count has been verified. The follow-up is
`two-worker-smoke.transcript-op-retention` in the generated live-smoke checklist;
format-1 protocol coverage is not a claim to have run an older installed binary.

## Memory op compaction

The `memory` collection syncs one whole-store record per worker and owner
(`memory-v2:<device>:<owner-hash>`), so every memory change appends a signed
copy of the entire fact store. A later field measurement found 792 kB average
envelopes (961 kB max) and 1,666 copies of one worker's record in 30 days
(1.3 GB), 2.1 GB for 27 records, nearly all superseded. The generic pass only
dropped them after 30 days.

`compact_memory_ops` runs in the same daily maintenance loop, after the
transcript pass, with the same captured heads and peer observations. It deletes
a `memory` envelope only when **all** of these hold:

1. A newer envelope of the same record (same collection and record id, later
   ingest, strictly higher Lamport clock) is still in the log. The newest
   envelope of each record is never deleted.
2. It is at or below the minimum advertised frontier of every recent active
   worker peer (the transcript pass's rule, grace and fail-closed behaviour).
   So no online peer, whatever its version, ever sees a gap.
3. It is strictly below its stream's retained head.

There is no age floor. This is safe because the record is whole-state: a
receiver applies only the current materialized `network_memory_records` row
(`memory_records.apply_to_files` reads it by id, even when one batch carried
several copies), and state copies read `sync_records` plus that row, never old
envelopes. A peer beyond the contact grace meets a gap and takes the existing
state copy, as after `prune_ops`. Nothing changes on the wire: older peers keep
their own 30-day retention and still exchange, forward and copy normally.

The first pass on an upgraded worker is also the one-time cleanup of the
already superseded copies; nothing else is migrated. It runs in committed
batches of 100 rows (`MEMORY_COMPACTION_BATCH`, about 80 MB of envelopes at the
field size) under the 30-second statement deadline and stops between batches on
cancel. Each pass logs `Memory sync operation compaction` with `deleted_ops`,
`bytes_estimate`, peer frontiers and expired peers. Disable it with
`AI_SHUFFLE_SYNC_MEMORY_OP_COMPACTION=0` (`false`/`off` also work).

What it does not change: bytes **written** per memory change are the same
(one whole-store envelope). What it bounds is bytes **kept**: roughly one
envelope per memory record plus whatever recent peers have not yet received
and up to a day of new copies, instead of 30 days. Per-fact or delta records
would also cut the bytes written, but would need a new record format, merge
rules shared with the fact store, and a mixed-version plan (older peers would
ignore per-fact records and stop converging). That is left as a follow-up.

`tests/core/network/test_memory_op_compaction_postgres.py` covers it with real
PostgreSQL workers, the real memory producer and signed exchange through the
peer router. With 60 seed facts and 12 changes, main kept 13 of 13 copies
(807 kB) after the maintenance pass; with compaction it keeps 1 (66 kB, the
newest). The online peer then receives later changes through ordinary op sync
with no state copy. A lagging recent peer pins what it lacks, the kill switch
holds everything, and an expired peer recovers through a state copy to the same
records and facts.

## Transcript op volume: findings only

The second half of the same report (about 1.1 million `session_events` ops a
week) is one op per saved transcript event. The turn recorder saves streamed
text, thinking and tool-input in **chunks**, not per token: one event per kind
when 150 ms have passed since the first buffered delta, or at 2,048 characters,
or before any other event (`app/core/turn_recorder.py`). The catalog publisher
then publishes every new event row once, by its cursor
(`catalog._publish_session_events`). The later idle-session delta compaction
(`compact_session_deltas`) merges rows after publication and adds no ops. So
about 6–7 ops per second of streaming per running chat, plus tool and status
events. 1.1 million a week is about 1.8 ops per second around the clock, about
seven hours of streaming a day across all chats. That fits several agents
running in parallel. It grows with use, not because of a bug.

These ops are already retired after 30 days by the transcript pass above, so
their total is bounded at about four weeks of activity. Options to lower the
rate, none changed here:

- Publish deltas coalesced: merge consecutive delta rows of one turn into one
  envelope at publish time. That needs a mirror format for merged runs (the
  owner's dense `seq` is the mirror's order), so it is a protocol change.
- Don't sync delta rows of finished turns at all and rely on the final message
  events. Mirrors replay deltas for text today, so this needs reader changes.
- A longer flush window (for example 500 ms) would cut ops about 3x but makes
  local streaming feel slower. Not recommended.

To confirm on a busy worker (read-only), compare
`SELECT event_type, COUNT(*) FROM session_events WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY 1 ORDER BY 2 DESC`
with the week's `session_events` op count. Delta compaction merges older rows,
so the table undercounts the deltas that were published.

## Other measured tables: findings only

| Supplied field size | Why it exists / options (not changed here) |
| --- | --- |
| `sync_records`: 1.3 GB / 3.8M rows | One current version/hash/order/tombstone per published record, not an operation history. Snapshot enumeration, transcript catch-up and conflict ordering use it. Deleting live event versions breaks the very fallback this PR relies on. A separate normalized/compact transcript-version representation could reduce overhead but needs snapshot, LWW, tombstone and mixed-version migration analysis. |
| `fluid_session_nodes` indexes: 5.1 GB, including raw-message trigram GIN | These index canonical history for recall, not never-pruned sync ops. Inventory actual indexes and query plans; after compact recall projection readiness is proven, retiring redundant raw-JSON GIN indexes may save space. Check fallback/mixed-version readers first. Reindexing fixes bloat, not intentional index coverage; do not remove chat nodes. |
| `session_recall_text`: 2.6 GB | Trigger-maintained searchable text projection with overlapping chunks and resumable backfill, not a signed log. Source deletes/updates maintain it; it intentionally grows with retained chats. Audit readiness, duplicate search-index coverage and chunk/overlap costs. It is rebuildable, but removing it changes recall's fallback/performance; the existing explicit projection maintenance command is a separate owner decision. |

No changes to these tables/indexes, their retention, or their maintenance flags.

## Follow-up only: avoid putting the full payload into future event envelopes

Referencing the canonical row is not a wire-compatible one-line replacement.
Current signed envelopes carry encrypted records; relays must forward those
original signed bytes, including after key rotation or while the origin is
offline. They cannot re-sign for the original author, and encrypting the same
row again does not reproduce its nonce/ciphertext/signature. Older receivers
expect the whole event in the envelope.

A future version could negotiate signed content-addressed event references plus
an authenticated blob/record fetch and immutable digest/version binding. It must
specify origin-offline relaying, authorization, rollback proof, pending/order
recovery, retention of referenced objects, and capability negotiation. During a
mixed-version period it must still provide original full envelopes to old peers
(or explicitly upgrade/reject them); a relay cannot silently down-convert a
reference using another device's signing key. Do not change the signed payload
or erase its sole retrievable object in this PR.

## One-time physical space reclaim — owner approval required

**Not run by this change or by its author.** Deletes free PostgreSQL space for
reuse; they do not immediately shrink the files or the next physical update
backup. Ordinary autovacuum can run normally. The existing managed runtime has
reviewed database-only dump/restore commands, but no generic offline SQL action.
Use a logical rebuild into a **new** volume, not an improvised live `VACUUM FULL`
or `pg_repack`. This is slower and needs more headroom, but keeps the original
cluster intact for rollback and avoids hand-editing managed credentials.

Rehearse [the complete managed restore procedure](../RESTORE.md#restore-embedded-data-offline-into-an-empty-disposable-target)
first. Use the same tested image, original UID/GID mapping, protected backup
storage, and the original non-database volumes. Below, set `RECOVERY_FILES` to a
private absolute backup directory, `IMAGE` to the current tested image id,
and `NEW_DATA` to a unique **new empty** volume name. Never print a descriptor,
copy a connection string, use `down -v`, restore over the original, or remove its
volumes. For non-Compose deployments use their equivalent stopped-container
maintenance invocation; do not guess their mount layout.

```sh
# In the deployment directory, AFTER explicit maintenance approval.
# Set RECOVERY_FILES, IMAGE, NEW_DATA, PUID and PGID locally first.
# PUID/PGID must match this deployment, not another machine's defaults.
umask 077
docker compose stop --timeout 120 ai-shuffle
docker compose run --rm --no-deps ai-shuffle dump --file /data/sync-log-reclaim.dump
docker compose cp ai-shuffle:/data/sync-log-reclaim.dump "$RECOVERY_FILES/sync-log-reclaim.dump"

# Capture matching private app data while writers remain stopped. This reads
# the stopped original volumes; it does not change their PostgreSQL state.
docker run --rm --network none --volumes-from ai-shuffle:ro \
  -v "$RECOVERY_FILES:/recovery" --entrypoint tar "$IMAGE" \
  -czf /recovery/sync-log-app-data.tar.gz -C /data \
  --exclude=./postgres --exclude=./database-mode.json \
  --exclude=./managed-postgres.json --exclude=./backups \
  --exclude=./sync-log-reclaim.dump .

# Restore only into a fresh volume. The CLI rejects nonempty destinations.
docker volume create "$NEW_DATA"
docker run --rm --network none --stop-timeout 120 \
  -e PUID="$PUID" -e PGID="$PGID" \
  -v "$NEW_DATA:/data" -v "$RECOVERY_FILES:/restore:ro" \
  "$IMAGE" restore --file /restore/sync-log-reclaim.dump

# Keep the target's newly generated PostgreSQL lifecycle/binding files.
docker run --rm --network none --entrypoint tar \
  -v "$NEW_DATA:/data" -v "$RECOVERY_FILES:/restore:ro" "$IMAGE" \
  -xzf /restore/sync-log-app-data.tar.gz -C /data --numeric-owner \
  --exclude=./postgres --exclude=postgres \
  --exclude=./database-mode.json --exclude=database-mode.json \
  --exclude=./managed-postgres.json --exclude=managed-postgres.json
```

Complete the binding/key validation and candidate startup steps in RESTORE.md;
switch **only** the data-volume mapping to the new volume, retaining the exact
other volume mappings, and start with `docker compose up -d ai-shuffle`. Keep
writers blocked until canonical session/event/Fluid counts, transcript samples,
credential readability and network catch-up match the offline source. Measure
`pg_database_size(current_database())` and `pg_total_relation_size('sync_ops')`
through the approved local database maintenance channel before/after; don't
extract or paste connection details. No pruning/deletion of chat rows is part
of this rebuild.

**Downtime:** from stopping writers until dump + restore + index rebuild +
validation finish. For a tens-of-GB database, reserve a multi-hour maintenance
window; there is no measured time guarantee. A rehearsal determines it.
**Headroom:** retain the entire old cluster, plus the dump/app-data archives,
plus the entire new cluster and its peak WAL/temp/index-build space. As a
conservative planning floor for the supplied 42 GB DB, allow an additional
42 GB for the new DB plus the measured logical archive size (budget another
42 GB until measured), **plus** WAL/temp and app-file headroom. A logical archive
can exceed physical DB size; 84 GB extra is not a guaranteed sufficient cap.
Do not begin if the measured rehearsal peak does not fit.
**Rollback:** before permitting new writes, stop the candidate, restore the
original data-volume mapping and tested image, then `docker compose up -d
ai-shuffle`. The original volume was never rewritten. After accepting new writes,
a blind switch back would lose those writes: take another coordinated backup /
plan a migration back instead. Preserve both volumes and backups until approved
retirement. Net filesystem space is recovered only after the owner approves
retiring the old volume; while both exist, this procedure uses MORE disk.
This dump is a post-retention recovery point; restoring pruned log history would
require a separate pre-retention backup, not this archive.
