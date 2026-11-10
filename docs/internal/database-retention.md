# Database retention without deleting chats

No chat age limit or size cap is imposed. A database containing every chat will
still grow with new conversation content. The goal is to stop retaining
reconstructible streaming fragments and obsolete transport copies indefinitely,
not to pretend that keeping unlimited history uses constant space.

## Online maintenance

`app/db/fluid_events.py` keeps the existing seven-day **session idle** rule and
class allowlist (`ToolArgsDelta`, `TextDelta`, `ThinkingDelta`, `ProviderAlive`).
It dates sessions first, then walks at most 5,000 existing primary-key entries
per transaction, including nonmatching entries. Each delete has that same bounded
sequence range and rechecks idleness. No new index or startup migration is needed.
If a range's delete fails after its selection completed (a held row lock or a
spent statement budget), that range is rolled back and skipped, and the same
session continues after it; each pass restarts every session from its beginning,
so skipped rows are retried next pass without a stored cursor. A failed selection,
or three failed ranges in a row, stops that session for the pass, which bounds a
fully blocked session to about three statement timeouts. Any session with a
failed range is logged, other sessions continue, and maintenance receives an
explicit error with the committed row count; the next pass retries it. A supplied
cursor retains the caller's transaction and error/rollback ownership. Stop requests
are checked between ranges. Orphaned sessions and all other event classes remain.

The same walk also compacts the `context_usage` snapshot of those idle
sessions' `Usage` rows to the usage-bar form, exactly
`compact_context_usage(snapshot)` from `app/core/transcript_replay.py` (the form
the app log's `turn.usage` already stores): the totals, `model`, `measured`,
`isAutoCompactEnabled` and the category rows' `name`/`tokens`/`color`/`isDeferred`.
The breakdown detail (`gridRows`, `mcpTools`, `skills`, `messageBreakdown`,
`systemTools`, ...) that made up most of each row is dropped. Token counters,
`raw`, ids, `ts`, `seq` and every other key are kept byte-for-byte, so the
accounting backfill is unaffected; no `Usage` row is deleted. One `UPDATE` per
range runs in the range's transaction after its delete, with the same idle
recheck, so a failed range rolls back whole and is retried next pass. It matches
only snapshots that compaction would change (an object with a key outside the
compact keys, or a category object with a key outside the category keys);
compacted, null or missing snapshots are never rewritten again. Readers of an
idle session's old `Usage` rows need only `model` (native resume) and the
counters/`raw`; the context panel builds its breakdown live, and a resumed
session's subscriber starts at the log head instead of replaying old rows.
Maintenance reports the rewritten row count as `compacted_fluid_usage_snapshots`
beside `cleaned_fluid_delta_events`. The dropped detail is not recoverable from
the database afterwards; restore an offline backup if it is ever needed.

Normal maintenance uses plain `VACUUM (SKIP_LOCKED, TRUNCATE FALSE, PARALLEL 0)`;
non-streaming passes also request `ANALYZE`. The legacy `skip_vacuum=True`
streaming hint now selects the lighter mode instead of disabling reclamation.
Disabling tail truncation avoids its exclusive table lock. This is never
`VACUUM FULL`: ordinary reads/writes can continue, busy relation locks are skipped,
and reclaimed pages are reusable **inside the existing files**. Autovacuum still
matters, and these options do not promise zero I/O or zero index-lock contention.
Session SQL budgets and pooled autocommit state are restored on success/failure.

## Measured reproduction

Disposable PostgreSQL 16, 16,000,000 event rows, 100 sessions (90 recently active,
10 idle), 70% argument deltas and 30% required results, 120-character payloads:

| Measurement | Before | After |
| --- | --- | --- |
| Pruning | Old first 5,000-row delete: 12.55 s, 14,407,142 input rows examined | Entire 1,120,000 eligible deltas: 8.89 s |
| Required `ToolResult` rows | 4,800,000 | 4,800,000 |
| Total surviving rows | 16,000,000 | 14,880,000 |
| Relation bytes, including indexes | 3,954,802,688 before plain vacuum | 3,954,892,800 after plain vacuum (not a shrink) |
| Exact heap free space after plain vacuum (`pgstattuple`) | 278,840,900; dead tuple bytes 0 | After FULL: 42,290,956; dead tuple bytes 0 |
| Offline FULL | 3,954,892,800 bytes | 3,677,200,384 bytes, 16.84 s, saving 277,692,416 bytes |

These timings are not a production forecast: the disposable cluster has fsync off,
small synthetic rows, no competing traffic and different TOAST/index proportions.
The first attempt to FULL with an ordinary 30-second SQL deadline was cancelled;
the successful offline run explicitly removed that deadline. Startup never does so
for compaction. The cancellation did not lose any rows.

A private running devloop also produced two turns through the browser using its
scripted provider and the real engine/recorder. After aging only that disposable
session, pruning removed 32 transient events. Hashes of all 11 required Fluid
events, four canonical nodes and 14 app events stayed identical. Reloading the
chat still displayed the reply, and a third turn completed after pruning. This
is not a real-provider, multi-worker or fork/rewind/search parity test; those unchanged readers were inspected, not all
exercised. No live database rows were changed.

## What is genuinely duplicated, and what is not

Content-free, read-only field samples on 2026-11-09 (decimal GB; block samples are
not exact counts or reclaim forecasts):

- `sync_ops`: 19.63 GB; 0.1% transcript sample: 3,688 envelopes / 8,728,084 bytes.
  Every sampled envelope had a version record, 3,586 had the exact event, 102
  lacked it, and 66 missing-event envelopes were over 30 days old. This is real
  transport duplication, but not every missing materialization is safe to delete.
- `fluid_session_events`: 14.05 GB, about 16 million rows. In a 0.5% sample,
  54,881 `ToolArgsDelta` rows averaged 304 bytes; 6,917 `TextDelta` rows 216 bytes;
  2,352 `Usage` rows 3,451 bytes; 3,429 `ToolResult` rows 6,365 bytes.
- `fluid_session_nodes`: 8.88 GB. Nodes are model-context/resume/rewind authority,
  not an interchangeable copy of the app event log. Code-mode tool calls/results
  can exist only in the Fluid log and must stay there.
- `session_events`: 7.70 GB. In a 1% sample, 5,769 `turn.usage` rows averaged
  2,823 bytes, 19,085 input-delta rows 146 bytes, and 10,675 progress rows 788 bytes.
  App events drive chat rendering, fork grouping and worker transcript backfill.
- Usage is mostly context snapshots, not just token counters. Fluid snapshots
  averaged 14,860 decoded JSONB bytes in the sample, versus 1,137 for `raw`.
  A separate capped historical app-usage sample found 84 of 150 rows carried an
  **exactly identical** snapshot at both `context_usage` and
  `usage.context_usage`. That capped sample averaged 7,443 stored bytes / 45,925
  JSON-text bytes and is not representative of all usage rows. Recent sampled
  rows already lack the nested copy: existing recorder slimming is working.

`Usage` also backs accounting recovery. Dropping it, nodes, or all app events
because another table has similar text would remove distinct functionality.
`subagent.progress` still has chat/tool-step reducers: old rows are not all
replaceable status snapshots. Input deltas are redundant only with a matching
later final `tool.use`; interrupted arguments must remain. App retention currently
merges only consecutive assistant text/thinking runs, not tool input or progress.
Canonical replay already suppresses superseded tool inputs and coalesces text
progress, but doing the same on disk requires sync/cursor/rollback handling.

Follow-ups with measurements and parity requirements:
[#1327](https://github.com/HobbyCoders/ai-shuffle/issues/1327) (usage/input/progress)
and [#1328](https://github.com/HobbyCoders/ai-shuffle/issues/1328) (proven compacted
sync fragments). The idle Fluid `Usage` snapshot compaction described under
"Online maintenance" is the first #1327 step: it touches only the worker-local
`fluid_session_events` table, never an app usage payload, sync version/hash, or
visible chat row.
Existing delta pruning is unchanged in scope; restore an offline backup to recover
transient fragments if operational rollback requires those too.

## Is the sync log bounded?

The normal row path is bounded by #1277's peer/frontier policy, **not** by settled
or archived state. A settled event still ages out of `sync_ops` if its canonical
row/version exists and all other pins permit it. Every signed stream keeps its
head. Recent peers inside the default 30-day contact grace can pin at zero when
no authenticated frontier is held. The short one-hour floor also requires every
active worker member's acknowledgement; unacknowledged operations keep the legacy
30-day age even for a peer past contact grace. Older peers recover through the
existing state-copy and transcript-backfill paths. No wire/schema change here.

The field sample had one peer inside the contact grace and another outside it.
It cannot prove a time-series plateau or reveal process-local authenticated
frontiers. Existing missing-event envelopes are a known exception: exact-row
retention cannot remove them, even after 30 days. The 66 sampled older examples
suggest roughly 66,000 such rows but do not prove why each row is missing.
Do not describe the entire table as steady until #1328 is solved and successive
maintenance counts/bytes are measured. See [sync-log-retention.md](sync-log-retention.md).

## One-time physical shrink: operator-controlled, app stopped

**Design/runbook, not an automatic startup action. Owner approval is required
before any real database is compacted. No live database was compacted for this
change.** PostgreSQL's file rewrite is transactional per relation and keeps all
logical rows, but takes `ACCESS EXCLUSIVE`, temporarily needs a second relation
copy and can generate substantial WAL. Cancelling it rolls that relation back;
earlier completed relations stay compacted.

1. Deploy the retention repair and let normal maintenance run. Record row counts,
   database/table/index/TOAST sizes and exact candidate bytes before scheduling
   downtime. Do not change retention ages or membership to manufacture savings.
2. Stop the app **and its restart/readiness supervisor** cleanly; disable automatic
   restart for the maintenance window. Never issue FULL through the serving pool,
   an app startup hook or a readiness-limited startup migration.
3. Use the installed runtime's database-only maintenance lifecycle (the same
   `Database` owner used by offline `dump`/`restore`) under the correct OS identity
   and PostgreSQL major version. It must acquire `postgres/lifecycle.lock`
   nonblocking and refuse an existing server. Open only the retained cluster
   (`purpose='dump'` refuses to initialize a missing one), never another data root.
   The app must not start. Use its private admin socket; never print descriptors,
   put passwords on a command line, or copy an environment file.
4. Before changing anything, create a new custom-format logical archive with the
   existing offline `dump --file <new-backup-file>` path. It refuses overwrites and
   uses private permissions. Verify `pg_restore --list`, and rehearse restoration
   into a separate empty disposable cluster, including row counts and a chat,
   tool result, fork/resume/search and worker-history read. Also retain a clean
   cold filesystem backup of the complete original data root for exact rollback.
   Abort on timeout or insufficient backup space; the source is unchanged.
5. Inspect `pg_stat_activity` and refuse every other client, prepared transaction
   or replication slot that could pin cleanup. Isolate app access for the whole
   rewrite (private socket / stopped restart supervisor). A database-only executor
   should temporarily refuse new app connections, then restore that setting even
   on failure; never kill sessions to force admission.
6. For actual free/dead bytes, use `pgstattuple` on the offline cluster's heaps,
   TOAST relations and indexes (install the extension **only in this approved
   offline window**, if needed). `n_dead_tup` is an estimate of dead tuples, not
   free bytes; after VACUUM it can be zero while gigabytes remain reusable. The
   live read-only audit could not get an exact dead-space number because neither
   `pgstattuple` nor free-space inspection was installed. Do not call the gap
   between sampled payload sizes and relation size exact bloat.
7. Confirm filesystem free space **after backup** is at least the largest chosen
   total relation size plus 20%/1 GB headroom, plus separately budgeted WAL and
   concurrent filesystem needs. On the measured database, `sync_ops` alone was
   19.63 GB: reserve at least about 24 GB for that rewrite, excluding backup/WAL.
   Sequential table rewrites reuse headroom; do not compact all tables in one
   giant transaction. Fail closed if the volume, backup or WAL budget is unknown.
8. From an autocommit admin connection, set `lock_timeout='5s'` and an explicit
   offline-only statement deadline sized to the approved maintenance window
   (or zero under a separately bounded operator process). Run **one** at a time:

   ```sql
   VACUUM (FULL, ANALYZE) sync_ops;
   VACUUM (FULL, ANALYZE) fluid_session_events;
   VACUUM (FULL, ANALYZE) session_events;
   ```

   Choose only tables justified by the exact measurements. FULL rebuilds their
   indexes too; do not add a redundant REINDEX. Never rewrite required nodes just
   because their files are large. No retention deletes are part of these SQLs.
9. Record each table's before/after bytes, elapsed time and unchanged logical row
   counts. Recheck final free space, restore connection admission, stop PostgreSQL
   cleanly through its owner, release the lifecycle lock, then restart normally.
   Check readiness, old chat/tool detail, Stats, fork/rewind/resume/search and peer
   handoff. Keep the backups until those checks and subsequent maintenance pass.
   If interrupted, inspect which table completed and resume only the remaining
   work. If forced termination left connection admission disabled, restore it
   from the private admin maintenance connection before restarting the app.

**Time/savings budget:** the synthetic 3.95 GB relation rewrote in 16.84 seconds
and saved 264.83 MiB after pruning 7% of all rows. This validates the distinction
between reusable free pages and returned file space; it is not the field savings
percentage. The field `sync_ops` envelope sample extrapolates to about 8.73 GB
inside 19.63 GB total files, but headers/indexes/TOAST, sampling variance and live
updates make the difference only a planning clue, not a promised 10.9 GB saving.
Plan an initial **30–60 minute app outage**, plus backup/restore rehearsal outside
that outage where possible, and allow a longer window on slow disks. Use measured
clone throughput and exact free-space scans to approve the final duration and
savings. Disk space is returned only after each successful rewrite; any final
production estimate remains pending those measurements.
