# Memory architecture

Updated 2026-10-03. The research and pre-change reproductions are in PR #1185;
`tools/research/memory_contract_baseline.json` is historical evidence, not the
current expected behavior. `memory_contract_probe.py --strict` exercises the
current contracts with synthetic data.

## Durable facts

`FactStore` in `fluid/fluid/codemode/facts.py` is the mutation boundary behind
`SkillStore`, Remember, Recall, Forget, synthesis and worker imports. Its rows
live in one SQLite database per store (`fluid/fluid/codemode/fact_db.py`, see
"Storage" below); live facts, archive, revisions and tombstones change together
in one transaction. Nothing outside the store API reads the files. The store
uses only the standard library, so standalone Fluid keeps working; application
imports do not leak into the vendored library.

### Storage

Since Memory v3 phase 3b each store is `<store root>/memory.db` (stdlib
`sqlite3`). Tables: `facts` (one row per live or archived fact: id, state,
list position, scope, protected and always flags, `updated_at`, `uses`,
`last_recalled_at`, and the fact's JSON body), `revisions` (retained earlier
values, one row each), `tombstones` (one row per forgotten fact), `deliveries`
(just-in-time deliveries, re-arms and Recalls: local usage, never replicated)
and `meta` (owner, store id, budgets, loss counters, the schema version, the
import marker). The journal is WAL; where WAL cannot be set up (some network
and FUSE filesystems) the store uses `journal_mode=DELETE`. Connections wait up
to 10 seconds for a busy store (`busy_timeout`); transactions are short.

**Migration.** The first open of a store whose database has no import marker
imports `memories.json` (and a pre-3b `jit_deliveries.jsonl`) in one
`BEGIN IMMEDIATE` transaction that also writes the marker (`sqlite.imported`
in `meta`, with the counts). A crash mid-import rolls the whole transaction
back, so the next open starts again; a second process waits on the write lock
and then finds the marker and imports nothing.
`memories.json` and the delivery log are never modified or deleted: they stay
as a backup for at least one release. If the import fails (the file does not
parse, a newer schema, another owner, a full disk), the store serves the facts
read-only from `memories.json` when it parses, refuses every write with "Memory
storage could not be upgraded (…); nothing was saved", and memory health
raises a `capacity` alert naming the reason. Nothing is dropped; the import is
retried on every open until it succeeds.

**Downgrade.** `python -m fluid.codemode.fact_db export <store dir> <file>`
writes a `memories.json` document an older build reads (live, archive,
revisions, tombstones, plus the deletion filter older builds expect).
`python -m fluid.codemode.fact_db downgrade <store dir>` (app stopped) writes it
in place and moves `memory.db` aside, so an older build reads it and a newer
one imports it again. `status <store dir>` prints the storage state. Changes an
older build makes to `memories.json` while `memory.db` exists are not seen by
a newer build; downgrade first.

Remember commits before returning a saved receipt. It does not journal a promise
for a second model to fulfill. Explicit facts are protected from inference
rewrites, automatic deletion and unrelated context eviction. Exact retries are
idempotent; corrections require the current revision. Stable single-valued keys
allow a correction to supersede its earlier value rather than create another
current fact. The narrow built-in name key does not equate nicknames and legal
names. Arbitrary facts can be addressed by their IDs.

Remember guards what it writes (Memory v3 phase 2a):

- **Task progress is refused.** `fluid/fluid/codemode/task_state.py` flags a
  save whose progress wording ("continuing", "left off", "in progress", "next
  step", "still needed", WIP as a status and similar) is about a pull request,
  issue or branch reference (`#123`, `PR 123`, an issue or PR link, `feat/x`,
  `branch fix-login`): the reference is the status's subject,
  joined by a be-verb, "remains", "still" or a colon ("Issue #88 is in
  progress", "PR #1824: next step is…"), a continuation names it
  ("Continuing PR #1824"), or the fact is anchored to it (its when-line names
  a PR, issue or work branch such as `feat/x`, or a clause of the text opens
  "on branch <name>"). A named branch in the when-line ("When deploying on
  branch main", "On branch main") is lasting context and anchors nothing. A
  default branch (main, master, develop, trunk) never counts as the work, and
  an unquoted single name after "on branch" is prose ("On branch creation, …").
  Nearness alone does not
  count, so a lasting rule that cites a PR ("Since PR #1909 the pre-push hook
  refuses…", "never merge while a migration is in progress; see issue #88")
  saves. Remember passes the check to `FactStore.save` as `check(text, when)`,
  which runs once the save has resolved its target (by ID, including an
  archived fact, or by key in the resolved scope) and before anything is
  written, so a correction that omits `when` is checked with the when-line it
  keeps. An agent's save is refused and pointed at WorkingRecord or Note; a save the
  owner asked for is kept, and its receipt says task progress belongs in the
  working record. Facts naming a PR do not retire when that PR closes: the
  only PR state the app tracks is the owner's PR watches, which cover only
  watched PRs and live in the app database that Fluid's maintenance does not
  read.
- **Similar facts are named.** A new agent or owner save lists, in its
  receipt, up to three live facts in the same scope (or the session's
  project, for a global save) that share at least five subject terms, also a
  quarter of the smaller fact's terms (`FactStore._similar`), each by ID,
  revision and when-line, so a near-duplicate becomes a `replaces` or a
  correction. The fact just saved and anything it replaced are excluded;
  corrections get no list. A failure there never fails the committed save.
  `fluid/tests/fixtures/memory_similar.py` holds the invented store the
  threshold is tested on.
- **Environment facts are not memories.** Which Python has pytest, missing
  `node_modules` and where the agent browser runs are probed live at session
  start (the runtime-facts block in `fluid/fluid/context/runtime_facts.py`;
  in the app, the "# Workspace facts" card in `app/core/environment.py`) and
  are no longer harvested into the store. `env.*` facts harvested before stay
  until their TTL.

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

## Just-in-time delivery

The snapshot's `# Memory` index tells the model that a fact exists and when it
applies. Just-in-time delivery (Memory v3 phase 1,
`fluid/fluid/codemode/jit_delivery.py`) hands the model a fact's full text
when its situation happens. It runs beside the index, which is unchanged.

Triggers come from `fluid/fluid/codemode/triggers.py`. Each fact's are derived
from its when-line, plus any explicit triggers saved with Remember: `command`
(literal strings matched as whole words against the program that runs, never
compiled as a regular expression), `paths` (globs) and `tools` (tool names,
transport prefix ignored). Explicit triggers are stored on the fact as
`triggers` and stamped `triggers_at`, like `when` and `always`. They replicate
as per-field metadata and never bump the text revision. A worker from before
this field keeps the key untouched in the entry it holds and merges as before.
When a save gives no triggers and only word triggers can be derived, the
receipt suggests correcting the fact to add `command` or `paths`.

The app adapter (`app/core/provider/harness_adapter.py`) delivers on two
events, for native and delegated sessions alike:

- **User turn or delegation brief.** `match_turn` adds the matching facts' full
  text to that turn's context note as a `# Memory for this step` block. A
  spawned session's brief arrives as its first user turn. These deliveries
  are recorded as on time.
- **Tool call.** On PostToolUse, `match_tool` matches the call's input and
  hands the facts to the gate as an attachment (`fluid.hooks.bus.Attachment`).
  Every gate sends PostToolUse with the call's input and places the
  attachment where the model reads it: on the call's result in the native
  loop and the bundled MCP server, in the Claude Code CLI hook's
  `additionalContext`, and in code mode on the enclosing cell's result,
  outside its output fence, because a cell may never print an inner call's
  result. A fact counts as delivered only once its gate settles the
  attachment as placed; a dropped one is offered again with the next match. These deliveries are recorded as late. A
  model reads added context only in its next request, after the call already
  ran, on either leg. Context added at PreToolUse (which the CLI accepts as
  `additionalContext`) arrives in that same request. Reading a fact before
  its call executes would mean holding or denying the call, which delivery
  never does.

Only delivery candidates are delivered: not ALWAYS, identity, superseded,
conflicted, behaviour-rule or Recall-only facts. Facts outside the session's
scope (global plus its project), facts not `visible_on` this worker, and facts
failing the injection screen are also left out, as are setup receipts for
another checkout of the project (the index's own filter). Each event carries
at most 3 facts, and the whole block, the line naming further matches
included, stays within 1,500 characters; further matches are named by ID for
Recall as far as they fit, then counted. Each fact is delivered once per
session, and PreCompact re-arms the session, because the summary may have
dropped the text. A replacement adapter for the same session (after idle
eviction or a restart) takes up the session's deliveries since its last
re-arm from the local log. Candidates come from the store's read-only
snapshot, without the writer lock, and are prepared once per store file
revision and checkout; a read that takes longer than a quarter second
delivers nothing for that event. Proactive assistant and one-shot runs do
not deliver.

Each delivery is recorded only on this worker, as a row in the store's
`deliveries` table (see "Storage"), which is never replicated. A row holds
the fact ID, the trigger kind (command, path, tool or words), the session,
the time, and whether it was on time or late; compaction re-arms are rows
too. The insert waits at most one second for a busy store and is otherwise
dropped, so recording never slows or fails a tool call. Rows older than 365
days are pruned. A pre-3b `jit_deliveries.jsonl` (and its rotated
`jit_deliveries.1.jsonl`) is imported once with the store and then left as it
is. A delivery counts toward its fact's value (see "Automatic lifecycle").
Totals, per-fact counts (the 200 most recently delivered facts) and the last
200 deliveries are computed on read. Memory health reads the totals as its
`delivery` signal, which is a count and never an attention. Set
`FLUID_MEMORY_JIT=false` to turn delivery off; the index is unaffected.

Two accepted limits: a stop that lands while a gate places the attachment can
mark a fact delivered unseen until the next compaction (the index and Recall
still have it), and a session idle across two log rotations can get a fact
once more after resuming.

## Session reflection and the proposal inbox

Memory v3 phase 2 (`fluid/fluid/codemode/reflection.py`) replaced the
continuous learner as the default way Fluid learns. A finished turn no longer
starts a pass (`Synthesizer.kick` does nothing). The pass runs once, when the
chat's engine closes: `CodeModeSession.aclose` or the delegated
`ToolJournal.aclose` drains the learner when the pool evicts an idle engine,
the chat settles or the worker shuts down. That close is the one event both legs
already share, and it fires once per idle stretch. The native engine shields
its close, so the call outlives the adapter's 5-second close grace, and the
learner's HTTP client closes only once no call is using it.

**Gate.** `reflection.gate` runs first and uses no model. The chat qualifies
when a user turn after an agent reply reads as a correction (starting "no",
"wrong" or "not that", or containing "don't", "stop", "instead", "I prefer",
"from now on", "next time", "I meant", "that's not right" and similar), a
user turn carries the frustration
mark the journal already sets, the user asked to remember something, or the
same friction signal appeared at least twice. Text inside code doesn't count.
A chat that fails the gate is marked reflected and logged as
`{"kind": "reflection", "outcome": "skipped", "reason": "no_signal", "ran":
false}` with no call. Other skip reasons are `backoff` (the chat's model is
backing off after a failure) and `busy` (another pass holds the synthesis
lock for 30 seconds).

**Input.** The input is the session's own `user`, `assistant`, `remember` and
`friction` journal lines newer than its last reflection (`.reflection-state.json`
keeps a per-session high-water mark, capped at 500 sessions), the facts and
rules in scope, and why the chat was picked. There are no tool or cell lines
and no subagent lines. It is bounded to the smaller of 28,000 characters
(about 7k tokens) and the chat route's prompt limit. User turns are clipped to
2,000 characters and kept first (newest first). Replies (800) and friction
(300) fill the rest, in order. A chat that comes back and settles again is
reflected on its new lines alone, and a Forget during the call drops the
answer.

**Routing and cost.** A reflection reads one session, so it uses that
session's own route from PR #1996: the chat's model, provider row and effort
(`FLUID_CONTINUAL_MODEL` still pins the model), its `model@row` backoff, and
the same per-billing daily call budget and pacing as every learner call. The
synthesis-log row records the model, billing and prompt limit used.

**Output.** The model answers `NO_CHANGES` or up to five tagged items:

- `<rule applies_to quote>` saves a behaviour rule through the existing
  `_apply_rules` path. A rule still needs a quote found in the user's turns,
  as `behavior.py` requires.
- `<proposal kind="owner_preference" quote="…">` whose quote is found in the
  user's turns becomes a behaviour rule ("When <its when-line>: <text>")
  through the same `_apply_rules` checks (quote, permission, injection, cap),
  so it shows under Learned behaviour and is revertible there. It keeps its
  proposal's scope: `scope="project"` makes a rule for the chat's project
  only, never a global one (a chat with no project sends it to the inbox).
  One those checks refuse goes to the inbox instead.
- Any other `<proposal kind="owner_preference|owner_fact|project">` goes to
  the inbox. An unverified quote is dropped.

Skills and consolidation do not run in reflection mode.

**Inbox.** `proposals.json` beside the store holds this worker's open
proposals and the owner's decisions. A proposal has an id (a hash of kind,
scope and its text with case, spacing and punctuation removed), kind, text,
scope, quote, when-line, sessions, and created and expiry times. Only an exact
duplicate after that normalisation joins an open row, adding its session;
"seen in N chats" is shown to the owner as information and never saves
anything. A proposal becomes an agent-saved durable fact (`provenance=
"session"`, quoted or not) only when the owner accepts it (`GET
/api/v1/memories/proposals`, `POST /api/v1/memories/proposals/{id}/accept`,
`POST …/dismiss`). Accept is one transition under the inbox lock: the
proposal must still be open, the fact is saved, then the decision is
recorded; a proposal already decided on any worker, or expired, answers 409.
Decisions (id, status, time) are kept for good and never pruned, so a
dismissed proposal does not come back from any worker. Open proposals expire
14 days after they were first made and are left out before any lookup or
merge, so the same text later starts fresh. The file holds at most 100 open
proposals. A proposal is not a fact: it takes no slot, is not in the index,
just-in-time delivery or Recall, and expiring it is not counted as a loss.

**Concurrency.** Chats reflect concurrently. Only two closes of the same chat
take turns, behind an asyncio lock keyed by store and session (waiting never
blocks the event loop; a close waiting over 60 seconds is skipped as `busy`
and retried at the chat's next close). The session's watermark is read again
once the lock is held, before any call is paid for. No file lock is held
across the model call: inbox and state writes run in a worker thread under
their own short advisory locks, and a writer that could not take its lock
writes nothing. A proposal the inbox could not store (busy or a write error)
leaves the watermark where it was, so the chat's next close reflects on the
same turns again. On that retry a rule already saved matches its own key and
scope (no copy), and a proposal this chat already queued is neither written
nor reported again.

**Engines.** Native chats drain the journal in their shielded close. Claude
Code and Antigravity chats start the drain as its own tracked task once the
CLI or agent turn has stopped, so the host's five-second close deadline ends
only the wait for it, never the reflection call, and the agent process is
stopped without waiting for it. At app shutdown the session pool first stops
every engine without waiting for its reflection, then waits for all running
reflections together for at most 10 seconds. That wait, and the unwinding of
the ones it cancels, ends before the two-second cleanup reserve ahead of the
supervisor's stop deadline, which stays for connectors, pending event writes
and database teardown. Only shutdown's own engine closes skip their wait (a
task-local flag that ends with shutdown); a concurrent eviction still waits.
If a reflection cut off there did not advance its watermark, the chat's next
close after the restart retries those turns. A watermark write already running
in a worker thread can still finish after the cutoff.

**Rollback.** `FLUID_MEMORY_LEARNER=continuous` (a plain env read, listed as
internal in `scripts/ci/env_allowlist.json`) brings back the continuous
learner described in the rest of this document until phase 3 deletes it.
`fluid_continual` still turns learning on and off in both modes.

## Learning from Claude Code sessions

Claude Code (delegated) sessions feed the same journal as native JSON-mode
sessions. The adapter builds a `ToolJournal` on the session owner's store,
stamped with the app session, the project key and the checkout. `DelegatedLoop`
records the user text with the app's reminders stripped, one `tool` entry per
tool result the CLI reports, and the final reply. Each tool entry carries the
compact input, a clipped output and an error flag. `friction` entries come from
the same detector, which also recognizes the CLI's own wording for a missing
file and an unchanged re-read. There are no `cell` entries: the CLI has no
Python kernel, so every call is a direct `tool` record, as in native JSON mode.
The same caps, the low-value read filter and the secret screen on saved facts
apply.

Closing the session reflects on it once (see Session reflection above). Under
the `FLUID_MEMORY_LEARNER=continuous` rollback, a finished reply kicks a
learner pass and closing the session drains it. Both run under the store's
shared pacing and daily budget. The CLI is not a client the learner
can call, so passes use, in order: the session's own provider row when a native
client can call it, with `FLUID_CONTINUAL_MODEL` or the session model; for the
installation owner's sessions only, a usable flat-rate subscription row
(ChatGPT, SuperGrok), on the row that lists `FLUID_CONTINUAL_MODEL` when one
does, else that row's default model. An API-key row is never picked for this,
and an API user's session never uses the owner's subscription. With no such
provider, the session still journals and its entries wait for the next pass any
session on the same store runs. The learner client is built only when a pass
first needs it. Passes announce their changes on the session's event stream,
as on the native leg.

These sessions list skills by their `SKILL.md` path and the model reads the
file instead of calling Skill. A successful Read of a discovered skill's
`SKILL.md` counts as a load in `~/.fluid/skills/.usage.json`.

## Automatic lifecycle

Hard limits and expiry are enforced by code at ingestion and maintenance, not by
a cleanup prompt. The app runs maintenance every five minutes even without an
active chat or learning model. Reads also apply overdue retention.

Current per-owner defaults:

| Resource | Limit/policy |
|---|---|
| User-saved (explicit) facts | 512, live and archived together. At 512 a new user-saved fact, or an agent fact promoted to one, is refused with "Memory is full: … Forget or merge one first; nothing was saved." Corrections of a saved fact still work. Nothing already saved is archived or dropped to make room, and a merge never trims them; a merged store above 512 refuses new ones until enough are forgotten |
| Agent facts (agent-saved, accepted proposals and learned facts: one pool, Memory v3 phase 3b) | Valid 90 days from the last save, just-in-time delivery or Recall; facts naming one branch or worktree: 14 days. A fact's value is its deliveries plus Recalls in the last 60 days. Past 160 live agent facts the lowest-value ones (then the least recently used) move to the archive |
| Archived facts | No count limit: one row each. Recall still finds them; saving one again brings it back with a fresh lifetime. An archived agent fact is removed for good 365 days after its expiry date, counted as `expired_archived`. User-saved facts in the archive (from older versions) are never removed |
| Expiry | An expired agent fact moves to the archive (above). User-saved facts never expire |
| Retained fact revisions | At most 2, for at most 30 days |
| Fact text | 2,000 characters (one fact, or one rule with its exceptions); oversize saves fail rather than truncate meaning |
| When-line (`when`) | 1–140 characters, one line, screened for credentials and injection patterns: the situation in which a future agent should load the fact ("When choosing a model for a subagent"), not a summary. Required by Remember for a new fact; a correction keeps the old one unless a new one is given. A fact without one (older stores, the learner, harvests) is listed by a line derived from its text at render time (first sentence or clause, at most 100 characters), never stored. Memory health counts facts that have none |
| ALWAYS flag (`always`) | Only on a user-saved fact, at the owner's request: rendered in full in every session. The ALWAYS memories visible in one project (global plus that project's) share 3,000 characters of text; a save past that is refused with the list of current ones |
| Metadata merge | `when` and `always` are metadata with their own stamps (`when_at`/`always_at`: hybrid timestamp and store id). Replicas settle each field on its newest stamp, independently of the fact's text revision clock, so a metadata edit never bumps the revision, never turns a concurrent text edit into a conflict, and never loses another worker's edit of the other field. A missing stamp (older stores and replicas) is the oldest |
| Recall by id | `Recall(ids=[...])`, at most 10 ids, local or global: full text, when-line and labels, live or archived. Scoped like keyword Recall: global facts and the session's project; another project's id is reported as not found in this project |
| Prompt fact lines | The stable, task-independent base is an index: ALWAYS memories in full within their own 3,000 characters, then identity facts (`profile.name`, `profile.preferred_name`, `profile.pronouns`, `profile.language`, `profile.github_username`) always listed by their text cut to 120 characters, then one `- [id rN; source] <when-line>` line per other fact in scope, in 10,000 characters shared by the identity lines, the index and conflict/withheld notes. Index order: facts the task names, then the session's project, then the most useful. The model Recalls a fact by id for its text; an index-only line is not a use for memory health's usage signal or its unconfirmed-harvest repair. An additional task slice delivers at most 1,000 new fact-line characters per owner turn and at most 2,000 held total, requiring two distinct whole-word matches. It never displaces base facts. When full, oldest-admitted task-only facts that no longer match the current query can yield space to a new two-term match; matching facts and all base facts are preserved. Admission age is recovered from the provenance-marked chain, not a query log. Topic-driven changes have an 8,000-character cumulative allowance since the trusted full snapshot (including change-list wrappers and retired IDs), then explicit Recall is the fallback until a genuine restore/base update. This bounds 100-turn topic-switch history to the initial snapshot plus 8,000 characters, rather than buying another base snapshot on every switch. Actual source corrections, deletion, safety and scope removals reconcile even after the novelty allowance is spent. Task-only changes never cause a full base re-send. All selection follows owner, scope, checkout, conflict and injection screening; archived facts still require explicit Recall. No query log, model call or embeddings. Additions use the existing provenance-marked user-tail memory/change-list chain and its compaction preservation, never the cached system prefix. A closing note counts what is left out that Recall finds in that session: live facts omitted for space and archived facts in scope ("(144 saved facts omitted for space and 12 archived; Recall finds them by keyword.)"). Other projects' facts and setup receipts for other checkouts have their own notes; another worker's environment facts are neither shown nor counted |
| Raw inference journal | 1 MiB; consumed lines are reaped first; past 90% with nothing consumed left to reap, unsynthesized lines (never explicit ones, never the slice a running pass is reading) are dropped down to half the cap with a warning, lowest value first and oldest first within each tier: successful read-only cells and calls, then other successful cells and calls, and last failed cells and calls, friction, and user and assistant prose. `journal_loss.json` counts the drops in `dropped_unsynthesized` and per kind in `dropped_by_kind` (`cell`, `tool`, `user`, `assistant`, `friction`, `other`; records from older builds have the total only); events may still be skipped under lock contention |
| Model-assisted inference | A daily call budget per store that follows how the pass is billed: 200 attempted calls per UTC day when the learner's provider row is a flat-rate subscription (ChatGPT/Codex, SuperGrok: the rows `credential_gate.subscription_provider_model` treats as subscriptions), 48 for an API-key, metered or unknown provider. The decision is per learner, from the provider its passes use, so one store served by both kinds claims each pass against that pass's own limit. The owner policy setting `learner_daily_calls` (`PUT /api/v1/settings/owner-policy`; 0 = automatic, the default; fluid's own config key is `learner_daily_calls`) overrides either, clamped to 4–1,000, and is read at pass time, so a change applies from the next pass without a restart. Calls kept for consolidation scale with the budget (2 of 48, 8 of 200, at most 10). Each synthesis-log row records the budget that applied (`budget`: `daily`, `reserved`, `source` = `setting`, `subscription_default` or `metered_default`). The calls are spread over the day: by a given time a store may have used 2 calls plus its share of the day so far, a pass starts at least 15 minutes after the previous call (with a bigger budget, proportionally less, never under a minute), and it runs ahead of that pace only while the journal is past 60% of its cap; a pending Remember entry is not paced until a pass has read it. A pass makes one call per project in its slice and stops the slice before a project the day's remaining calls cannot pay for; when a later project's call fails or is refused, the entries the answered projects cover are still consumed. Input capped at 32,000 characters plus the fixed system prompt (6,000 for saved facts, the journal slice of up to 400 lines, then the skill library) when the learner model's context window is unknown or small, as for most local models; a known window raises it, up to 96,000. Successful read-only calls and cells (Read, Glob, Grep, LS, TaskList, ToolSearch) are consumed without being shown, unless they contain a top-level function/class definition or a lambda assigned to a name, import a saved skill, cannot be parsed safely, or name a path outside the session's working directory and the temp directory (home config, notes, another checkout); other working cells are shown with code and output clipped to their head and tail, failed ones whole. Output bounded by the configured synthesis token limit; a failed call backs off per model, 5 minutes doubling to 6 hours, shared by every session on the store |
| Deletion records (tombstones) | One row per forgotten fact, kept and replicated for 365 days, no count limit. A store imported from an older version keeps its 16 KiB deletion filter read-only, so what it fenced stays fenced |

These defaults are conservative initial policy, not benchmark-derived optimal
values. Inference is allowed to be lossy; acknowledged explicit saves are not:
the explicit tier refuses a save rather than lose one it already holds.
The existing deterministic skill reaper remains separate. Model consolidation
can propose merges or retirements of inferred and agent-saved facts it was shown,
but cannot update unknown IDs, overwrite a protected fact, or retire one. A
learner or consolidation retirement moves the fact to the archive with no
deletion barrier: Recall still finds it, a later agent save brings it back, and
the retirement replicates instead of being undone by the next merge. Facts that
leave without a Forget (the end of archive retention) are counted in the store's
`losses`; failed saves are counted in `meta.json` without their text. Consolidation runs only after a
successful synthesis pass, under the same lock; a failed consolidation is retried
after six hours instead of waiting a full interval. Inference batches retain
source-project identity; a forget during a model call invalidates that proposal.
The synthesis cursor is a line position in the retained journal. While a pass
waits for its model, its slice is leased (`journal.jsonl.lease`, line positions
only): a reap in that time leaves those lines in place and records how far it
moved them, so the pass advances the cursor past exactly the lines it read.
Every pass, failed or not, writes a row to `.synthesis-log.jsonl` in the store
and a line to the process log; a row for a pass that made no model call (a held
lock, a spent budget, a slice of low-value entries only) carries `"ran": false`
and is not counted as a silent pass; a provider's error message is kept, not
reported as an empty completion.

## Contradictions between owner rules

Two rules the owner asked to keep can contradict each other after the owner
changes their mind. A deterministic check (`possible_conflicts` in
`fluid/fluid/codemode/triggers.py`, no model call) flags a pair of live
owner-kept rules in overlapping scopes when they fire in the same situation (a
command, path or named tool in common, a two-word phrase in common, or two
strong words in common) and only one of the two texts says "not", "never",
"avoid" or the like. It is a hint, never a verdict, and takes no rule's
authority away. It shows in two places: the Remember receipt of an owner save
names up to three such rules ("Possible conflict: ..."), and Memory health's
"Possible conflicts" signal counts the pairs. The owner settles one by
correcting, replacing or forgetting a rule.

Memory v3 phase 3a removed the model conflict review that ran here before.
Its verdict file, `conflict-review.json`, is left on disk and no longer read.
Its "both stand" answers, saved under `memory.coexist.` keys, stay in the
store: they replicate like any fact, Recall labels them historical, and they
never render in a prompt or count as model-routing authority.

## Self-maintenance

`fluid/fluid/codemode/memory_health.py` checks each store without a model. The
app's maintenance loop runs it once an hour per store and again after a
curation pass (`app/core/fluid_memory.py`). It reports signals, each
healthy or needing attention with one sentence: whether the learner produces
facts given recent sessions and whether it keeps up with the journal (attention
when more than 10% of the entries taken in over three days were dropped unread,
naming how many of them were user, assistant or friction entries; the detail
splits the drops by kind in `dropped_3d_by_kind`; days on which the daily budget
ran out, as the store records them, are named either way), facts never recalled or shown, unconfirmed
harvests and how often they are rewritten, duplicate groups, capacity (it warns
at 90% of the 512 user-saved facts, before saves are refused), how much
each project's snapshot leaves out, failed curation passes, user-saved facts
lost without a Forget, failed saves, owner rules that may contradict (see
above), facts with no written when-line, and just-in-time deliveries. The result is kept in
`memory-health.json` beside the store; it is local to the worker and is not
replicated.

Repairs only retire unprotected facts into the archive: exact duplicates (every
worker keeps the same copy, chosen from replicated fields) and harvested facts
that nobody recalled or saw in a snapshot for 30 days and that have not changed
for 14. Near-duplicates are reported, not merged. A user-saved fact is never
repaired away. The owner sees the result under Settings, Memory, Memory health
(`GET /api/v1/memory/health`), and gets one notification per stretch of
attention, at most once a day per worker. The row's hint says when the check
last ran (`checked_at`; "Not checked yet" when it has not), the opened list
ends with the full time, and while anything needs attention a warning with the
first check's name and reason sits under the collapsed row. The thresholds are
first estimates.

## Replication and concurrency

`app/network/memory_records.py` publishes the durable store through the
existing memory collection as one small record per fact
(`memory-fact:<device>:<owner-hash>:<fact-hash>`, live and archived facts) and
one barrier record per store (`memory-barrier:<device>:<owner-hash>`: its
tombstones and forget filter). The merge is entry-wise, so one fact's record
merges exactly like that fact inside a whole snapshot, and a change to one fact
sends only that fact's record. Records are keyed by the publishing worker, so
two workers' different views of one fact never overwrite each other. A fact a
worker stops holding has its record marked deleted, and receivers ignore
deleted fact records: forgetting travels only through the barrier. A receiver
merges one apply batch's records per store in one write. While another active
worker does not advertise `memory_fact_records` in its signed presence (or its
presence is unknown), each worker also keeps writing the older whole-store
record (`memory-v2:<device>:<owner-hash>`); once every other worker advertises
it, that record is marked deleted. A receiver merges every live whole-store
record, whoever published it, so mixed-version networks keep converging.
Received records are committed before they are written to the files, so each
worker re-applies every peer's live memory record once at network start (a
crash between the two) and again on the next publish pass after a write failed
with an I/O error; its own records are left out, since its files are their
source. The committed local file is the durable source for
retrying publication, so a crash between a save and a publish does not lose the
save. A local receipt does not claim that an offline worker has received it.
Reflection proposals travel as one record per store
(`memory-proposal:<device>:<owner-hash>`, kind `memory_proposal`, content
`{schema, owner, device, proposals, decisions}`) whenever `proposals.json`
changes. A receiver keeps each peer's lists in `proposals.remote.json`, which
is never read as facts, and copies the peer's decisions into its own
`proposals.json` for good. The inbox merges open proposals by id (their chats
are listed together, as information) and a decision made on any worker closes
the proposal everywhere; the earliest decision wins. A deleted record removes
that peer's open list, never a decision already copied. Older workers ignore these records: every earlier `_apply`
acts only on kinds `skill` and `prose_skill` and on ids starting with
`memory-fact:`, `memory-barrier:`, `memory-v2:` or the legacy `fact:`, and
skips any other record. So a proposal can never reach an older worker's facts.
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

Environment facts the harness used to harvest from tool output (whether pytest
is importable from the worker's Python and where, missing `node_modules`, the
browser host; no longer written, see Durable facts) describe one worker. They are stored per worker: each carries
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

A deletion is monotonic for the deleted ID. Each Forget writes a tombstone row,
kept and replicated for 365 days with no count limit (phase 3b replaced the
fixed-size Bloom filter). A store imported from an older version keeps its
filter read-only, so what it fenced stays fenced; it is never grown. On the
wire nothing changed: the barrier record still carries tombstones and a
`forgotten` filter, built at publish time from the tombstone rows and the
imported filter, so an older worker (which keeps only its newest 256
tombstones) still never resurrects a forgotten fact. A filter received from an
older worker fences that merge and is not stored. A restore gets a new ID.
Transport operation-log retention is owned by the network subsystem.

### Concurrency map

Several processes on one worker open the same store: the app server, a
standalone `fluid` run, the `fact_db` command line, and during an update two
app versions for a moment. SQLite's file locks order them; `.memory.lock` (a
cross-process file lock plus a thread lock, `codemode/transactions.py`)
serializes the read-modify-write transactions.

| Reader or writer | Process | Rule |
|---|---|---|
| Native Fluid agent: Remember, Forget, harvests, corrections | App server (or a standalone `fluid` process) | `.memory.lock`, then one `BEGIN IMMEDIATE` transaction that writes only the changed rows |
| Delegated Claude Code: memory tools through the bundled MCP bridge (`app/mcp/builtin_tools.py` → `app/core/fluid_memory.py`) | App server: the bridge is an in-process tool server | Same as the native agent |
| Recall (both legs) | App server or standalone `fluid` | Lock-free snapshot read, then a usage write: one short transaction (one row update per fact plus a usage row), waiting at most 1 second and dropped if the store stays busy. A concurrent structural write keeps the higher count (`MAX`) |
| Prompt index render, just-in-time candidates | App server or standalone `fluid` | Lock-free read (WAL snapshot); cached on the store stamp |
| Just-in-time delivery log | Wherever the agent loop runs | One insert transaction, 1-second wait, dropped if busy; never fails a tool call |
| Settings > Memory API (`app/api/memories.py`) | App server | Reads lock-free; edits, deletes and scoping go through `FactStore` under `.memory.lock` |
| Replication export and import (`app/network/memory_records.py`) | App server (network task) | Export is a lock-free read; a received batch merges under `.memory.lock` in one transaction |
| Learner, reflection and proposal acceptance (`continual.py`, `reflection.py`) | App server background tasks | Reads lock-free; a write (accepted proposal, consolidation) is a structural write under `.memory.lock`. `proposals.json` keeps its own lock file |
| Maintenance (expiry, retirement, every five minutes) | App server | Structural write under `.memory.lock`; nothing is written when nothing changed |
| Memory health | App server | Lock-free reads; `memory_health.json` has its own atomic write |
| Code-mode kernel | Kernel subprocess | Does not open the store; memory tools run in the agent's process |
| `python -m fluid.codemode.fact_db` | Its own process | `status`/`export` read; `downgrade` needs the app stopped |
| First open (import) | Whichever process opens first | One `BEGIN IMMEDIATE` transaction with the marker (see Migration) |

## Forgetting and migration

Forgetting a fact requires an explicit request and current revision. It removes live,
archived and retained historical values, purges old inference evidence and legacy
synthesis previews, and writes and publishes a tombstone. Normal learning cannot
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
Since phase 3b a store can be taken back to an older build with the downgrade
command above; an older build reads the exported document like its own.

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

Deferred history indexing, which keeps GIN merging out of hand-off imports, is
experimental and off by default pending latency acceptance.
`python -m app.maintenance.history_indexing --apply` requests the online upgrade
on a disposable candidate; its worker-local migration stamp resumes interrupted
work after restart. No migration or deferral runs without that explicit opt-in.
The upgrade replaces transcript and recall GIN indexes with disjoint live/history
partial indexes. Existing rows remain live-indexed, adding the state columns is
metadata-only, and indexes are built and retired concurrently with a 250 ms
lock-acquisition limit that covers only the metadata lock: a build then waits for
overlapping writers and old snapshots as long as it needs. A build needs room for
its copy and that copy's WAL on the data and WAL volumes plus a 1 GiB reserve; when
short it logs why and retries. Before the migration completes, imports use the
original synchronous path.

Imported rows are written as `history_index_state = 2`, ordinary writes as 0.
Search prefilters use UNION ALL over those states so each arm can use its partial
GIN index, and imported text is searchable at once. Each history GIN has a
pending list of just under 2 GiB, the most Windows PostgreSQL allows, so an import
only appends to it and never pays for a merge. A list that does fill is merged
by the importer that filled it, with the background cleaner's memory and a
ten-minute limit instead of the ordinary thirty seconds. Live inserts and edits always enter state 0, so
they cannot inherit a history pending-list flush. Live GIN settings and the
global pending-list limit are unchanged.

A background `gin_clean_pending_list` then merges each history list, five seconds
after the last import has ended and only while no foreground turn is running or waiting to
start. A watcher cancels the statement within about a second of a turn appearing,
including one still held in the database-busy queue. A cancelled merge keeps the
pages it finished, the rest stays searchable in the list, and the next pass
retries; a restart merges whatever the previous process left. Autovacuum may also
clean these lists. Until a merge finishes, a search scans the list linearly.

State 1 is the queue left by builds that promoted imported rows one at a time.
Nothing writes it now. Remaining rows drain in batches of at most 32, located by
the queue index and visited in heap order, within 128 KB and 100 ms budgets that
span all tables in a call (one oversize row may progress). Those transactions are
short, paced with 100 ms pauses, and checked against foreground runs before every
row.

Measured on a disposable PostgreSQL 16 with the 24,333-row, 78 MB all-text bundle:
the import fell from 223 s to about 25 s and total WAL including the merge from
9.5 GB to 1.1 GB, on a shared host. An index build already in progress still
finishes before it yields.

Once the projection is ready, native transcript search in both session-list
APIs also prefilters through it, then checks the original visible text for
exact phrase/snippet semantics. Oversized rows waiting for promotion and rows
whose projection reaches the five-million-character cap remain search
candidates, including text past the cap. A small partial btree on existing
cap-reaching part numbers is built concurrently; no projection version bump,
rewrite or replay is needed. Native session-list search keeps its legacy path
until that index is valid, and only then may legacy GIN retirement begin.
The background recall task retires the six legacy raw-JSON and stored-
`recall_text` trigram GINs (including history `_h0`/`_h2` variants) with
`DROP INDEX CONCURRENTLY`, autocommit and a shared history-migration advisory
lock. Online maintenance also non-blockingly takes startup's existing schema
advisory lock; projection object/trigger repair and explicit offline DDL use
that same key. This prevents a queued non-concurrent DDL request from making
ordinary writes queue behind a waiting concurrent drop. Startup takes the key
with try-locks, rolling back before each 0.25–2 second backoff: it never holds a
waiting snapshot that a concurrent build needs to drain. Server startup and
the owner-password/pairing CLIs can overlap a serving process. After ten seconds,
startup cancels only the tagged online-maintenance backend currently holding
that exact schema key in this database. Cancelled builds/drops retry later;
unrelated backends and another startup's schema work are never cancelled.
Online maintenance closes its backend before returning the pool slot, so even
an already-selected, delayed cancellation cannot hit a subsequent request.
The two-second pass budget is checked between statements; drops may run for
up to three minutes.
The one-time concurrent cap-row index build has no statement timeout, so slow
workers can finish its heap scans instead of restarting them every pass. Lock
timeout is disabled for concurrent DDL so waits for existing writers can
finish even on a busy worker; ordinary writes and searches continue. Failed
drops retry on a later pass; sessions keep writing throughout. Generated columns are
left in place to avoid an ACCESS EXCLUSIVE table lock. Bootstrap and startup
migrations never rebuild the raw indexes; unready projections or unavailable
`pg_trgm` retain correct, potentially unindexed legacy search. The old explicit
offline cache command is a no-op once the projection is ready.

The table takes roughly the size of the searchable text once more, plus its
trigram index; text from mirrored sessions (event rows) is kept twice, as
pieces and as the original text the search folds. An app version from before
the table ignores it, but its triggers keep maintaining it; remove it there
with `python -m app.maintenance.session_recall_projection --remove`.

### Claude Code CLI memory

The delegated leg runs the claude CLI, which has its own native memory:
CLAUDE.md and AGENTS.md instruction files, and auto-memory (`MEMORY.md` under
`~/.claude/projects/<slug>/memory/`). The app spawns the CLI with
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (`DELEGATED_CLI_ENV_DEFAULTS` in
`app/core/environment.py`), so by default the CLI neither loads nor writes
auto-memory on top of the app's `# Memory` snapshot. CLAUDE.md and AGENTS.md
still load. The default is added only when neither the agent profile, the
provider nor the worker's own environment sets the variable, so an owner can
turn auto-memory back on (for example with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=0`).
The app does not read, write or sync the CLI's files, and existing
auto-memory files stay on disk untouched. A fact Remembered through the MCP
bridge is app memory; if auto-memory is turned back on, what the CLI saves
itself stays CLI memory.

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
