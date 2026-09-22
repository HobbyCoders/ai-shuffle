# Operation-log head profiling

Issue #1285 includes residual work after collection-scoped scans and shared
exchange frontiers were merged. This follow-up measures the remaining
`GROUP BY origin_device_id, incarnation` over `sync_ops`; it does not attribute
the historical 51.91 pool acquisitions/second or 84 peer requests/minute.

## Change

`local_watermarks` and standalone `ops_since` now seek between streams using the
existing unique `(origin_device_id, incarnation, seq)` index, then read each
stream's maximum sequence. One SQL statement captures the complete log frontier
for fewer than 256 streams. At 256 streams, the bounded probe stops and the
original aggregate obtains a complete frontier in a second statement. The
partial probe is never advertised or used to select outgoing operations.

The fallback bounds index probes on unusually wide logs. Received and
snapshot-only watermarks still merge with the log frontier; outgoing batches
retain their captured upper bounds and gap checks. No maintained head table,
cache, migration, writer hook, or new index is introduced. The measured index
path removes the large-log aggregate cost for the common narrow shape without
adding head-repair responsibilities to pruning, restore and rollback.

## Isolated PostgreSQL observations

Measured on Windows, PostgreSQL 16.10, on 2026-09-21. A separate disposable
database and random schema contained synthetic operations, not user sessions.
Rows used 128-character synthetic ciphertext strings; these do not reproduce a
production envelope-size distribution. The schema and indexes came from the
real network store. Each dataset was vacuumed/analyzed before measurement.
Other host work continued, including a separate local validation worker.

The values below are medians of seven complete `local_watermarks` calls. Both
paths acquired one pooled connection per call. The baseline reads the aggregate
and received watermarks; the candidate reads indexed heads and received
watermarks, adding the aggregate only on fallback.

| Operations | Streams | Baseline wall ms | Candidate wall ms | Baseline hold ms | Candidate hold ms | Candidate statements |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 8 | 1.447 | 0.554 | 1.434 | 0.547 | 2 |
| 100,000 | 64 | 20.059 | 1.430 | 20.038 | 1.418 | 2 |
| 1,000,000 | 64 | 85.705 | 1.390 | 85.685 | 1.359 | 2 |
| 1,000,000 | 512 | 89.309 | 89.375 | 89.275 | 89.342 | 3 |

`EXPLAIN (ANALYZE, BUFFERS)` on these isolated tables showed aggregate scan nodes
reading the whole operation population. Indexed boundary/maximum probes return
at most one row per probe and avoid that full scan. For one million operations
and 64 streams, aggregate execution was 91.501 ms with 10,724 shared hits and
23,789 shared reads; the bounded probe was 1.016 ms with 655 hits and zero reads.
Shared reads are PostgreSQL buffer misses, **not proof of physical disk I/O**.
At 512 streams, the probe alone was 3.425 ms and 2,567 hits, followed by the
aggregate; its standalone plan must not be presented as the full fallback cost.

Thirty reads offloaded through `asyncio.to_thread` were also observed with a
5 ms ticker. Maximum clamped excess delay for baseline/candidate was 0/0 ms,
15.03/0 ms, 13.30/0 ms and 14.41/14.67 ms across the four datasets. Sample counts
were respectively 30/30, 73/30, 184/30 and 190/190. Windows timer granularity and
these short, sequential samples limit this evidence: zero does not establish
zero loop lag, and these are not production latency bounds or a load test.

## Reproduction and correctness

Set `ALLOW_REAL_PG=1` and `TEST_POSTGRES_DSN` to an explicitly disposable loopback
database. From the repository root, with the app and Fluid importable, run:

```sh
python scripts/profile_network_heads.py --output head-profile.json
```

The script creates and retains its own named schema, records all timing samples
and complete JSON plans, and neither reads nor clears existing application
tables. It rejects non-loopback targets. The output contains no DSN or secrets.

Eight real PostgreSQL contracts cover empty/single/multiple-incarnation logs,
both sides of the 256-stream boundary, outgoing operations and gaps, actual
pruning across owners, snapshot-only watermarks, direct tail replacement,
transaction rollback, and concurrent writes during both indexed and fallback
reads. CI runs these alongside network migration contracts. Existing network
fixtures exercise exchange frontiers, later appends, pruning, snapshot recovery,
rollback proofs and external collection edits/deletions.

The broader issue remains open for session-level dirty tracking and attribution
of production write wakes and busy/quiet peer scheduling. These measurements
justify the head-query change; they do not establish a cause for the reported
production request cadence or prove deployment-wide improvement.
