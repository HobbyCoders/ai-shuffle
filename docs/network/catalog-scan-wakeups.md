# Session write wakeups

The post-commit resource hook requests all five owner-ordered session
collections after a session mutation. Before #1433, the scanner treated those
names as unknown and requested a full scan, including unrelated shared tables
and memory/plugin file publishers. Known catalog names now select the catalog
publisher, and shared collection names accumulated in the same burst are also
scanned.

## Dirty sessions

Every registered session mutation takes a `session_id`, and the hook passes
that id to the scanner along with the collection names. The scanner collects
the ids of one burst. A narrow catalog pass then reconciles only those
sessions:

- the session row and its runtime summary, including tombstones when a row
  no longer matches;
- tags, with retractions limited to records whose id starts with one of the
  dirty sessions;
- pending prompts. Prompt record ids do not name their session and prompt rows
  are only deleted together with their session, so a narrow pass publishes
  prompt upserts only.

The catalog publisher widens a narrow pass to the whole catalog when any dirty
id is not a session this device currently owns (deleted, handed off, or a
peer's mirror). Deleting a session also clears `parent_session_id` on its
forks, and the whole pass is what publishes those rows along with the deleted
session's tombstones. Peer-origin records are never retracted.

Transcripts are not narrowed. Event appends have no hook of their own, so every
catalog pass still runs the existing incremental probe over all eligible
sessions. When nothing is new, that probe is one query.

The whole catalog is reconciled instead of a narrow pass when:

- a catalog wake arrives without session ids (an unknown or bulk mutation);
- more than `SCAN_DIRTY_SESSIONS_MAX` (128) ids pile up in one burst.

## Unchanged

The scanner snapshots and clears its pending collections and session ids
before dispatch, so a write during a pass stays queued for the next one. The
two-second minimum spacing and the sixty-second full reconciliation bound are
unchanged. That full pass covers every session, which is how unhooked SQL
edits and deletions are found. Unknown names, explicit full requests, and
failed narrow passes still request full reconciliation. Memory/plugin
publishers run only on full passes.

## Tests

`tests/core/network/test_scan_pacing.py` runs the real post-commit hook,
scanner and catalog publisher against isolated SQLite test databases. It
checks that:

- hooked updates reach the scanner with their session ids;
- a narrow pass publishes the dirty session's row, runtime, tags and prompt,
  and its SQL trace contains no read of the other session;
- an unhooked edit to a session no wake names stays unpublished through narrow
  passes and is found by the sixty-second full pass;
- a deleted dirty session is retracted and its fork's cleared parent is
  published;
- ids that arrive during a pass, a wake without ids, the id cap and a failed
  narrow pass behave as described above;
- two paired workers converge after narrow updates and a deletion, and a
  local wake naming a peer's session leaves the peer's records alone.

These tests establish routing and reconciliation behaviour. They do not
measure production PostgreSQL latency, I/O, pool contention or event-loop
lag.
