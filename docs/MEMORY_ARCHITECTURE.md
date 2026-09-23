# Memory architecture

Updated 2026-09-10. The research and pre-change reproductions are in PR #1185;
`tools/research/memory_contract_baseline.json` is historical evidence, not the
current expected behavior. `memory_contract_probe.py --strict` exercises the
current contracts with synthetic data.

## Durable facts

`FactStore` in `fluid/fluid/codemode/facts.py` is the mutation boundary behind
`SkillStore`, Remember, Recall, Forget, synthesis and worker imports. A process
lock plus a cross-thread lock protects each read-modify-write transaction.
Live entries, archive, bounded revisions and deletion barriers are one atomic,
fsynced JSON replacement. The existing file store remains usable by standalone
Fluid; application imports do not leak into the vendored library.

Remember commits before returning a saved receipt. It does not journal a promise
for a second model to fulfill. Explicit facts are protected from inference
rewrites, automatic deletion and unrelated context eviction. Exact retries are
idempotent; corrections require the current revision. Stable single-valued keys
allow a correction to supersede its earlier value rather than create another
current fact. The narrow built-in name key does not equate nicknames and legal
names. Arbitrary facts can be addressed by their IDs.

The app resolves the authenticated session through `app/core/fluid_memory.py`.
Installation-owner facts stay at `<data_dir>/fluid/skills`; API users have
separate hashed roots below `.users/`, with the owner recorded in the document.
A database-backed session with unknown identity fails closed. Project scopes use
the application's project ID, not a worktree path. Standalone Fluid derives a
project key from a shared git directory or remote, falling back to the path for
non-repositories. Legacy global facts are not copied into API-user stores.

Both native and delegated sessions receive the app's owner-scoped snapshot and
use the same tools. Chat mode explicitly permits the three account-memory tools
in both native and MCP spellings, without adding a shell, a general file writer
or a Python kernel. Agent-level tool denials still apply. The delegated bridge
resolves the host bundle's transport-qualified names to registered names for
permission checks and warm-cache surface filtering; foreign prefixes and
unregistered names remain untrusted. Provider-owned memory is not required. A snapshot has its own
budget; leaving a fact out of the prompt does not delete it. Tool-only and
conflicted facts are withheld from normal answers. Recall exposes a conflict's
ID and revision for an explicit correction, rather than presenting a guessed
winner as fact. Saved text is data, not an instruction layer.

## Automatic lifecycle

Hard limits and expiry are enforced by code at ingestion and maintenance, not by
a cleanup prompt. The app runs maintenance every five minutes even without an
active chat or learning model. Reads also apply overdue retention.

Current per-owner defaults:

| Resource | Limit/policy |
|---|---|
| Explicit active facts | 256; a full quota rejects a new save without evicting an earlier promise |
| Inferred active facts | 40; overflow enters the archive |
| Archived facts | 200 |
| Inferred validity | 90 days; retrieval does not renew validity |
| Retained fact revisions | At most 2, for at most 30 days |
| Fact text | 500 characters; oversize saves fail rather than truncate meaning |
| Fact document | 2 MiB; commit rejected if exceeded |
| Prompt fact lines | 8,000 characters, with profile facts prioritized |
| Raw inference journal | 1 MiB; consumed lines are reaped first; past 90% with nothing consumed left to reap, the oldest unsynthesized lines (never explicit ones) are dropped down to half the cap with a warning; events may still be skipped under lock contention |
| Model-assisted inference | At most 48 attempted calls per UTC day, per store; input capped at 32,000 characters plus the fixed system prompt (6,000 for saved facts, the journal slice, then the skill library); output bounded by the configured synthesis token limit; a failed call backs off per model, 5 minutes doubling to 6 hours, shared by every session on the store |
| Recent deletion records | 256, plus a fixed 16 KiB monotonic deletion filter (32 KiB as hexadecimal JSON) |

These defaults are conservative initial policy, not benchmark-derived optimal
values. Inference is allowed to be lossy; acknowledged explicit saves are not.
The existing deterministic skill reaper remains separate. Model consolidation
can propose merges or retirements of inferred facts, but cannot update unknown
IDs, overwrite a protected fact, or delete one. Consolidation runs only after a
successful synthesis pass, under the same lock; a failed consolidation is retried
after six hours instead of waiting a full interval. Inference batches retain
source-project identity; a forget during a model call invalidates that proposal.
Every pass, failed or not, writes a row to `.synthesis-log.jsonl` in the store
and a line to the process log; a provider's error message is kept, not
reported as an empty completion.

## Replication and concurrency

`app/network/memory_records.py` publishes the durable snapshot through the
existing memory collection. The committed local file is the durable source for
retrying publication, so a crash between a save and a publish does not lose the
save. A local receipt does not claim that an offline worker has received it.

Snapshots carry ownership, origin IDs, revisions, causal clocks, expiry and
explicit deletion state. They include archived records. An entry's absence from
a hot list is never interpreted as deletion. All imports use the same capacity,
trust and owner checks as local writes. Equal or unequal-length concurrent edits
are detected by their causal clocks rather than timestamps. Unresolved
contradictions are withheld; a user correction can resolve them against a known
revision. An older replica cannot overwrite a causally newer value.

A deletion is monotonic for the deleted ID. Compaction keeps a fixed-size filter
instead of an unbounded tombstone log. This is deliberately conservative: filter
false positives may withhold a fact or require an explicit restore, but cannot
resurrect an erased ID. A restore gets a new ID. At extreme lifetime churn the
filter can saturate; this tradeoff must remain visible, not be described as exact
unlimited-history deletion. A future acknowledged replica-epoch reset could
replace this conservative policy. Transport operation-log retention is owned by
the network subsystem, not this fact-document limit.

## Forgetting and migration

Forget requires an explicit request and current revision. It removes live,
archived and retained historical values, purges old inference evidence and legacy
synthesis previews, and publishes a deletion barrier. Normal learning cannot
reintroduce a matching forgotten key/value without an explicit restore. It does
not erase source conversations, provider logs, backups or requests already sent
to a provider. Offline replicas apply the deletion when they synchronize. Full
personal-data erasure is a separate operation; this tool must not claim it.

Deleting a session is different: an explicitly saved fact has no cascading
lifetime dependency on its source session. Memory retains only a source-session
identifier, not a mandatory pointer that must be dereferenced to answer.

Migration imports the old archive into the atomic document, preserves coarse
provenance, and removes the legacy archive only after a successful commit.
Legacy inferred facts are not silently promoted into user-authorized protected
facts. Legacy network fact records are accepted on upgrade, but their ambiguous
absence/deletion does not override new-format explicit saves or forget barriers.
Backward application downgrades are not supported after this migration: old
writers do not understand the new lifecycle fields.

## Other systems

### Session recall (transcripts)

Past conversations, in Postgres (`fluid_session_nodes` + the SDK mirror),
served by `PgFluidStore`'s recall hooks. Tools: `SessionSearch` (keyword
search / recent listing, project-scoped by default) and `SessionView` (a
message window from one session). Native legs get fluid's builtins via
`ctx.extra`; the delegated leg gets the bridged MCP copies. This system
stores nothing the transcript does not already contain — it is retrieval,
not learning.

### Claude Code CLI memory

The delegated leg runs the claude CLI, which keeps its own native memory
(`~/.claude/projects/<slug>/memory/`, CLAUDE.md, auto-memory). The app does
not read, write, or sync those provider-owned files. The CLI leg is therefore the one place two memory systems coexist:
the CLI's own, plus the app's via the MCP bridge. They do not overlap in
storage; a fact Remembered through the bridge is app memory, a fact the CLI
saves itself stays CLI memory.

### `agent_memory_files` (legacy mirror)

`app/core/memory_sync.py` mirrors per-project `*.md` memory dirs across
installs sharing a git remote, keyed by `link_group_id`. No longer active in current prompts: no prompt section points at those dirs and no tool writes them — the
sync now serves only pre-existing content and the explicit per-project
import (`app/core/memory_import_service.py`).


## Verification boundaries

Deterministic tests cover producing paths, parallel saves, inference budgets,
source-project grouping, corrections, expiry, protected retention, archive
migration, stale replica replay, causal conflicts and owner isolation. App tests
exercise the native/delegated bridge and a two-worker network. The research
probe's simulated network DB is not a transport test. Live UI/provider checks and
remaining gaps are recorded in the PR, not inferred from a passing unit count.
