# Memory architecture

Updated 2026-10-03. The research and pre-change reproductions are in PR #1185;
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
budget; leaving a fact out of the prompt does not delete it. Conflicted facts
are withheld from normal answers. Facts whose only evidence is tool output
render with a hint to confirm them with Remember. Recall exposes a conflict's
ID and revision for an explicit correction, rather than presenting a guessed
winner as fact. Saved text is data, not an instruction layer.

## Grounded prose workflow proposals

Python helper synthesis remains separate from prose `SKILL.md` workflows.
Synthesizer can queue at most two workflow proposals per pass; it never installs
one. The conservative first slice requires two distinct project-scoped journal
sessions with successful execution, a completion reply and an explicit user
receipt `workflow accepted: <name>`. Tool output cannot provide that receipt.
`workflow corrected: <name>` or `workflow rejected: <name>` blocks the workflow.
These receipts support evidence; they are **not** installation approval. Natural
language corrections must also be considered during owner review.

Proposals retain owner/project identity, exact content digests and bounded
source episodes/hashes. Content and evidence pass secret/injection screening;
capability metadata and executable substitutions are rejected. Installed skill
names and near-identical descriptions are checked at proposal time when the
host supplies the working directory, and always checked again at promotion.
The per-owner queue/audit holds at most 32 records and 256 KiB including rejected
and promoted records. Capacity exhaustion refuses new proposals, not old audit.
There is no automatic skill target count, deletion or overwrite.

Standalone Fluid's local owner can inspect with `fluid workflows list` and
`fluid workflows show <digest>`, then `fluid workflows approve <digest> --cwd
<project>` (or `reject`). Supply `--skills-dir` when using a custom store. Approval
requires an interactive terminal and typing the full digest after reviewing the
complete source work, scope, exceptions and successful outcomes. Source evidence
is revalidated; stale/reaped/corrected evidence fails closed. Approval creates a
new `.fluid/skills/<name>/SKILL.md` exclusively in the same project, with no tool
grants or permission bypass. A fresh native/delegated session uses ordinary
skill discovery and the Skill loader, not a new injection surface.

API-owner and application-ID project promotion is deliberately refused by this
local CLI: an authenticated application approval UI and project-location binding
remain product work. No API-owner proposal may install into shared global
roots. Global promotion and replacing an owner-authored skill are not supported.
Pending proposals are inert until this explicit local-owner approval; production
skills must not be installed merely to test the learner.

## Automatic lifecycle

Hard limits and expiry are enforced by code at ingestion and maintenance, not by
a cleanup prompt. The app runs maintenance every five minutes even without an
active chat or learning model. Reads also apply overdue retention.

Current per-owner defaults:

| Resource | Limit/policy |
|---|---|
| User-saved (explicit) facts | 512, live and archived together. At 512 a new user-saved fact, or an agent fact promoted to one, is refused with "Memory is full: … Forget or merge one first; nothing was saved." Corrections of a saved fact still work. Nothing already saved is archived or dropped to make room, and a merge never trims them; a merged store above 512 refuses new ones until enough are forgotten |
| Agent-saved (durable) active facts | 120; overflow enters the archive; valid 120 days from the last save or recall |
| Inferred active facts | 40; overflow enters the archive |
| Archived facts | 200 agent-saved and learned facts; when full the least useful learned facts go first, then agent-saved ones, counted in the store's loss counters. User-saved facts in the archive (from older versions) do not count and are never dropped |
| Inferred validity | 90 days; retrieval does not renew validity. Facts naming one branch or worktree: 14 days |
| Expiry | An expired agent-saved or learned fact moves to the archive, where Recall still finds it; saving it again brings it back with a fresh lifetime. It is removed for good 365 days after its expiry date, counted as `expired_archived`. User-saved facts never expire |
| Retained fact revisions | At most 2, for at most 30 days |
| Fact text | 500 characters; oversize saves fail rather than truncate meaning |
| Fact document | 2 MiB; commit rejected if exceeded |
| Prompt fact lines | 8,000 characters in the stable, task-independent base: unprotected facts reserve up to 3,000; the remainder goes to profile and other protected facts. An additional task slice delivers at most 1,000 new fact-line characters per owner turn and at most 2,000 held total, requiring two distinct whole-word matches. It never displaces base facts. When full, oldest-admitted task-only facts that no longer match the current query can yield space to a new two-term match; matching facts and all base facts are preserved. Admission age is recovered from the provenance-marked chain, not a query log. Topic-driven changes have an 8,000-character cumulative allowance since the trusted full snapshot (including change-list wrappers and retired IDs), then explicit Recall is the fallback until a genuine restore/base update. This bounds 100-turn topic-switch history to the initial snapshot plus 8,000 characters, rather than buying another base snapshot on every switch. Actual source corrections, deletion, safety and scope removals reconcile even after the novelty allowance is spent. Task-only changes never cause a full base re-send. All selection follows owner, scope, checkout, conflict and injection screening; archived facts still require explicit Recall. No query log, model call or embeddings. Additions use the existing provenance-marked user-tail memory/change-list chain and its compaction preservation, never the cached system prefix. A closing note counts what is left out that Recall finds in that session: live facts omitted for space and archived facts in scope ("(144 saved facts omitted for space and 12 archived; Recall finds them by keyword.)"). Other projects' facts and setup receipts for other checkouts have their own notes; another worker's environment facts are neither shown nor counted |
| Raw inference journal | 1 MiB; consumed lines are reaped first; past 90% with nothing consumed left to reap, the oldest unsynthesized lines (never explicit ones) are dropped down to half the cap with a warning; events may still be skipped under lock contention |
| Model-assisted inference | At most 48 attempted calls per UTC day, per store, spread over the day: by a given time a store may have used 2 calls plus its share of the day so far, a pass starts at least 15 minutes after the previous call, and it runs ahead of that pace only while the journal is past 60% of its cap; a pending Remember entry is not paced until a pass has read it. A pass makes one call per project in its slice and stops the slice before a project the day's remaining calls cannot pay for; when a later project's call fails or is refused, the entries the answered projects cover are still consumed. Input capped at 32,000 characters plus the fixed system prompt (6,000 for saved facts, the journal slice of up to 400 lines, then the skill library) when the learner model's context window is unknown or small, as for most local models; a known window raises it, up to 96,000. Successful read-only calls and cells (Read, Glob, Grep, LS, TaskList, ToolSearch) are consumed without being shown, unless they contain a top-level function/class definition or a lambda assigned to a name, import a saved skill, cannot be parsed safely, or name a path outside the session's working directory and the temp directory (home config, notes, another checkout); other working cells are shown with code and output clipped to their head and tail, failed ones whole. Output bounded by the configured synthesis token limit; a failed call backs off per model, 5 minutes doubling to 6 hours, shared by every session on the store |
| Recent deletion records | 256, plus a fixed 16 KiB monotonic deletion filter (32 KiB as hexadecimal JSON) |

These defaults are conservative initial policy, not benchmark-derived optimal
values. Inference is allowed to be lossy; acknowledged explicit saves are not:
the explicit tier refuses a save rather than lose one it already holds.
The existing deterministic skill reaper remains separate. Model consolidation
can propose merges or retirements of inferred and agent-saved facts it was shown,
but cannot update unknown IDs, overwrite a protected fact, or retire one. A
learner or consolidation retirement moves the fact to the archive with no
deletion barrier: Recall still finds it, a later agent save brings it back, and
the retirement replicates instead of being undone by the next merge. Facts that
leave without a Forget (the end of archive retention, a full archive) are counted in the document's
`losses`; failed saves are counted in `meta.json` without their text. Consolidation runs only after a
successful synthesis pass, under the same lock; a failed consolidation is retried
after six hours instead of waiting a full interval. Inference batches retain
source-project identity; a forget during a model call invalidates that proposal.
Every pass, failed or not, writes a row to `.synthesis-log.jsonl` in the store
and a line to the process log; a row for a pass that made no model call (a held
lock, a spent budget, a slice of low-value entries only) carries `"ran": false`
and is not counted as a silent pass; a provider's error message is kept, not
reported as an empty completion.

## Self-maintenance

`fluid/fluid/codemode/memory_health.py` checks each store without a model. The
app's maintenance loop runs it once an hour per store and again after a
curation pass (`app/core/fluid_memory.py`). It reports nine signals, each
healthy or needing attention with one sentence: whether the learner produces
facts given recent sessions and whether it keeps up with the journal (attention
when more than 10% of the entries taken in over three days were dropped unread;
days on which the daily budget ran out, as the store records them, are named either way), facts never recalled or shown, unconfirmed
harvests and how often they are rewritten, duplicate groups, capacity (it warns
at 90% of the 512 user-saved facts, before saves are refused), how much
each project's snapshot leaves out, failed curation passes, user-saved facts
lost without a Forget, and failed saves. The result is kept in
`memory-health.json` beside the store; it is local to the worker and is not
replicated.

Repairs only retire unprotected facts into the archive: exact duplicates (every
worker keeps the same copy, chosen from replicated fields) and harvested facts
that nobody recalled or saw in a snapshot for 30 days and that have not changed
for 14. Near-duplicates are reported, not merged. A user-saved fact is never
repaired away. The owner sees the result under Settings, System, Memory health
(`GET /api/v1/memory/health`), and gets one notification per stretch of
attention, at most once a day per worker. The thresholds are first estimates.

## Replication and concurrency

`app/network/memory_records.py` publishes the durable snapshot through the
existing memory collection. The committed local file is the durable source for
retrying publication, so a crash between a save and a publish does not lose the
save. A local receipt does not claim that an offline worker has received it.
User-level prose skill folders (`~/.fluid/skills/<name>/SKILL.md`) travel in
the same collection through `app/network/prose_skills.py`; they are
instructions, not memory, and docs/network/README.md describes their rules.

Snapshots carry ownership, origin IDs, revisions, causal clocks, expiry and
explicit deletion state. They include archived records. An entry's absence from
a hot list is never interpreted as deletion. All imports use the same capacity,
trust and owner checks as local writes. Equal or unequal-length concurrent edits
are detected by their causal clocks rather than timestamps. Unresolved
contradictions are withheld; a user correction can resolve them against a known
revision. An older replica cannot overwrite a causally newer value.

Environment facts the harness harvests from tool output (whether pytest is
importable from the worker's Python and where, missing `node_modules`, the
browser host) describe one worker. They are stored per worker: each carries
the store ID of the worker that harvested it, a harvest matches only that
worker's own copy, and each worker's copy of a key is its own slot, so two
workers no longer rewrite one shared fact with their own paths. They replicate
like any fact but are shown in the snapshot, found by Recall, curated and
checked by the health check only on the worker they describe; elsewhere they
are hidden and not counted. That is simpler and safer than labelling them with
a worker name, which Fluid does not reliably know, and a path from another
machine is noise there. The first per-worker harvest of a key archives the
shared copy older versions kept under it. A fact the user saves is always
shared: a user-requested save of a harvested fact drops its worker binding,
and harvests never rewrite it; an agent confirming a harvest keeps it on its
worker.

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

`SessionSearch` prefilters in `session_recall_text` (`app/db/session_recall.py`):
only the text recall scores, lowercased and cut into overlapping pieces of
about a kilobyte, kept current by triggers on the transcript tables. A message
longer than 200,000 characters is not cut up during its own write: it gets a
marker, search reads that row from its transcript table, and a background step
cuts it into pieces within about a minute. At most the first 5 million
characters of one message are indexed. An existing database installs the
triggers (one table at a time, waiting at most a quarter of a second for each)
and fills the table in short background batches after start-up, keeping the
older raw-JSON queries until that finishes. If the table or one of its
functions goes missing, the triggers skip it rather than fail the transcript
write, search falls back to the raw-JSON queries, and the next background pass
rebuilds and refills it. A term matching more than a couple of thousand pieces
is read newest-first, so a very common word ranks recent sessions rather than
scanning the whole history.

The table takes roughly the size of the searchable text once more, plus its
trigram index; text from mirrored sessions (event rows) is kept twice, as
pieces and as the original text the search folds. An app version from before
the table ignores it, but its triggers keep maintaining it; remove it there
with `python -m app.maintenance.session_recall_projection --remove`.

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
