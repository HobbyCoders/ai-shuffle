# Session write wakeups

The post-commit resource hook requests all five owner-ordered session
collections after a session mutation. Previously, the scanner treated those
names as unknown and requested a full scan, including unrelated shared tables
and memory/plugin file publishers.

Known catalog names now select the existing complete catalog publisher.
Shared collection names accumulated in the same burst are also scanned. The
catalog's ownership checks, transcript cursor handling, runtime summaries,
child records and deletion reconciliation are unchanged. This is collection
selection, not per-session dirty tracking: every catalog wake still considers
all eligible local sessions.

The scanner snapshots and clears its pending set before dispatch, so a write
during a pass remains queued. The existing two-second minimum spacing and
sixty-second full reconciliation bound are unchanged. Unknown names, explicit
full requests, and failed narrow passes still request full reconciliation.
Memory/plugin changes continue through full reconciliation as before.

`tests/core/network/test_scan_pacing.py` exercises the real post-commit hook,
scanner and catalog publisher against an isolated SQLite test database. It
updates and deletes sessions while requesting continuous follow-up passes,
checks database traces for the absence of unrelated agent payload reads, and
confirms that a direct external agent edit is discovered by the full pass at
sixty seconds of virtual time. Memory/plugin publishers run only on full
passes. Separate pacing contracts cover mixed shared/catalog bursts, failures
and writes arriving during dispatch.

This test establishes routing and reconciliation behavior. It does not measure
production PostgreSQL latency, I/O, pool contention or event-loop lag. Issue
#1285 still needs per-session dirty tracking and busy/quiet fleet attribution.
