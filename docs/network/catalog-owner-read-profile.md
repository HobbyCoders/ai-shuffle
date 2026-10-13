# Catalog ownership reads

Follow-up to #1285, measured on 2026-09-21 against PostgreSQL 16.10 on Windows.
The catalog previously fetched every session, runtime summary, tag and prompt
payload, then discarded rows owned by other workers in Python. Owner scans now
filter those payload reads in SQL. The existing Python checks remain as well.
No index, scan schedule, cursor or publication transaction changes.

The session scanner preserves its existing NULL-owner rule: NULL, empty or this
install's instance ID qualifies. Child rows retain their stricter existing rule:
NULL owners require this install's instance ID. Runtime rows have no instance
column. Record metadata remains complete so missing local records can still
produce tombstones; foreign-origin deletions remain excluded.

## Measurement

`scripts/profile_catalog_owner_reads.py` creates a random schema using the real
application DDL, with 10,000 sessions, one tag, prompt and runtime row per session,
and matching `sync_records` hashes. Prompts contain a synthetic 2 KiB question.
It runs the actual quiet scanner functions with a real pooled PostgreSQL
connection. The baseline cursor broadens only the payload SELECTs to their
previous form, retaining the original Python ownership filtering. Both paths
must publish zero changes. Seven sequential samples per path include record
metadata, decoding, Python filtering/hashing, and transaction completion.

With 100 owned sessions and 9,900 mirrors:

| Scanner | Median wall ms, before → after | Connection hold ms, before → after | Returned rows, before → after |
| --- | ---: | ---: | ---: |
| Sessions | 247.387 → 43.655 | 233.302 → 39.541 | 20,000 → 10,100 |
| Tags | 61.414 → 41.150 | 60.519 → 40.030 | 20,100 → 10,200 |
| Pending interactions | 180.474 → 44.325 | 176.931 → 43.351 | 20,100 → 10,200 |
| Runtime records | 63.735 → 39.193 | 60.066 → 36.798 | 20,000 → 10,100 |

Returned rows include all record metadata and, for child collections, owned
session IDs. Each payload SELECT itself returns 100 instead of 10,000 rows.
Query counts stay at two for sessions/runtime and three for tags/prompts; each
scanner still acquires one connection.

The plans do **not** show less table scanning. Sessions/runtime still scan
10,000 rows and filter out 9,900. Child plans hash-join sequential scans of both
tables. For example, the tag payload SELECT's server execution increases from
0.544 to 2.453 ms, while complete scanner time falls because fewer rows cross
the client boundary. Plans have shared-buffer hits and no reads in this sample;
they do not establish physical disk savings.

A second fixture with all 10,000 sessions owned locally checks the tradeoff:
session wall time is 447.263 → 445.267 ms, tags 117.235 → 118.052 ms, prompts
316.017 → 320.745 ms, and runtime 111.588 → 112.174 ms. Returned-row counts are
unchanged. This optimization helps catalogs with many mirrors; it does not
promise an improvement on a standalone worker.

These are local warm-cache observations, not production latency guarantees.
Full catalog orchestration, publication writes, network requests, discovery,
scan cadence and event-loop lag are outside this measurement. Per-session dirty
tracking and production busy/quiet attribution remain open under #1285.

## Reproduce and verify

Use a disposable loopback database, set `ALLOW_REAL_PG=1`, `TEST_POSTGRES_DSN`
and `PYTHONPATH` to include the repository and `fluid`, then run:

```sh
python scripts/profile_catalog_owner_reads.py --output catalog-owner-profile.json
python scripts/profile_catalog_owner_reads.py --owned 10000 --output catalog-all-owned-profile.json
```

The script retains its random schema for inspection and records raw samples and
full `EXPLAIN (ANALYZE, BUFFERS)` plans. It never reads or changes pre-existing
application tables. The local 100-owner run used schema
`catalog_profile_7b23d490a5f242f8a9dd240b322ac945`.

All 231 worker-network tests pass. New contracts observe rows returned at the DB
boundary and verify explicit ownership, legacy NULL/empty instance handling,
external edits, local tombstones, foreign tombstone exclusion and runtime rows
without an instance column. Existing two-worker catalog, transcript clear/replay,
snapshot, gap and rollback coverage also passes.
