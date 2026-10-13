---
name: session-harness-audit
description: Audit AI Shuffle and Fluid sessions for harness friction, prompt/tool correctness, cache effectiveness, context growth and compaction, memory and skill-system integrity; produce evidence-backed findings and comparable before/after measurements. Use for session reviews, harness audits or requests to investigate repeated work and token efficiency.
---

# AI Shuffle / Fluid session harness audit

## Purpose and limits
Audit observed behavior and the producing paths, not just what prompts or tests
say should happen. Default to the latest **15 eligible top-level sessions created
before capture**, excluding this audit. Scope to the current project unless the
user requests all projects; record that choice. Include unfinished/cancelled
sessions, marking their observation cutoff. Linked children and supplemental
provider/model examples are separate cohorts, never added to the primary denominator.
This skill is a procedure, not evidence that any check has already passed.

An audit is read-only by default. Do not edit application settings, delete live
memories/skills, rewrite session trees, trigger paid provider calls or deploy
updates merely to obtain evidence. Use disposable fixtures for controlled checks;
obtain the required permission before a paid live probe or production mutation.
Only delegate when the user's current instructions permit it. Without permission
for subagents, perform the same work directly.

Read `references/verification-matrix.md` before the deep review. Use the bundled
`scripts/metrics.py` only on the documented normalized private snapshot; it is
not a raw-session exporter or a replacement for qualitative review. Its input
contract is in `references/measurement-contract.md`.

## 1. Establish the audit boundary
1. Discover available tools and existing audit skills/reports; do not duplicate
   an audit already running. Check current Git state, active sessions and issues.
2. Record UTC capture time, project/worker scope, exact eligible session IDs in a
   **private** manifest, source HEAD, running app revision, engines/modes,
   provider/model changes, permission mode and available telemetry. Never assume
   the running app matches the checkout. Identify old/new harness revisions per
   session; do not assign every historical session the current HEAD.
3. State the question and comparison baseline. This audit's own tooling activity
   is excluded. A default sample is newest-created, not most recently touched.
   Summarize missing/foreign-worker/legacy/deleted transcripts rather than silently
   replacing selected sessions. Report sample bias and small model cohorts.
4. Choose a private output directory outside tested/typechecked source roots.
   Do not commit transcripts, user text, credentials, connection details, prompt
   dumps or raw memory contents. Keep baseline artifacts private and access-bounded.

## 2. Freeze complete evidence, without leaking credentials
Start with session search/view, logs and error tools, then obtain persisted
transcripts where summaries lack tool arguments, usage or compaction events.
Session summaries alone cannot establish read repetition, cache rates or the
actual prompt received by a model. Respect search budgets; bulk capture is not a
reason to repeatedly page the same summary.

Before querying storage, read the current schema and application adapters:
- `app/db/sessions.py`, `app/core/provider/fluid_transcript.py`, `fluid/fluid/store/`;
- native metadata/events/nodes commonly use `fluid_sessions`,
  `fluid_session_events`, `fluid_session_nodes`; `sessions.sdk_session_id` links
  application rows. Verify this on the target revision, including delegated data.

Use the app's supported authorized database connection, never copy `.env`, print
credentials, invent a DSN or reach an ungranted machine. Freeze one bounded,
read-only snapshot (a consistent transaction if supported), with explicit
rollback/close. Confirm what storage limits and retention omitted. If storage
access is unavailable, report those domains **unverified**, not healthy.

Capture selected metadata, full nodes, runtime events, active-path IDs, linked
child IDs, usage/raw provider usage, context snapshots, compaction decisions,
cache classifications and available timings. Follow the active parent chain or
verified rewind/fork selection; report ambiguity rather than counting every
branch. Preserve alternate paths separately for rewind investigations.

Record a manifest, schema version, export method, row counts and hashes. Make
redacted aliases for report session names. Keep full private evidence locally;
only sanitized summaries belong in GitHub. Missing usage is unknown, not zero.

## 3. Build the evidence map and metrics
For every cohort, reconcile model requests, terminal usage, outer tool requests,
actual dispatches, results, background starts/completions and rendered delivery.
Join stable IDs before counting. Ignore text/argument deltas as actions. Static
Python AST callsites are not execution counts (loops/branches matter). Separate
acquired ResultRef bytes from bytes actually delivered; never sum overlapping
representations. Separate requested, dispatched, denied, failed and cancelled calls.

Compute and report denominators for:
- model calls, tool dispatches, read/search calls, error/denial/recovery candidates;
- cumulative prompt/output exposure, cache hit fractions and reporting coverage;
- context utilization/headroom, growth, compaction boundaries and recovery;
- memory/skill writes, recall/load/import usage, failures and lifecycle evidence;
- latency/cost where measured, useful outcome and rework, not tool counts alone.

Run the offline helper after adapting the frozen data to its explicit contract:

```sh
python3 .fluid/skills/session-harness-audit/scripts/metrics.py /private/audit/calls.json --output /private/audit/metrics.json
python3 .fluid/skills/session-harness-audit/scripts/metrics.py /private/audit/calls.json --baseline /private/baseline/calls.json --output /private/audit/comparison.json
```

The helper uses stdlib only, makes no network/database/tool calls, never mutates
its input, and refuses to overwrite an output. Aggregate ratios are weighted
by tokens, not an average of per-call percentages. Native exclusive counts and
provider-inclusive counts must not be added the same way. Comparison groups
are separated by cohort/provider/model/engine; unmatched groups remain explicit.

## 4. Deep verification: cover every domain
Use the verification matrix, not only the following headlines:

**Cache:** trace raw provider usage -> normalizer -> engine Usage -> adapter ->
storage/UI/cost. Check read/write accounting, field absence, first and final call
persistence, duplicate terminal usage, reasoning-token subsets, effective prefix
stability, affinity/checkpoints, model/engine changes, TTL/eligibility, miss
classification and compaction-induced invalidation. A live billed response on the
configured account is evidence of cache support; docs/default zeros are not.

**Context and compaction:** inspect per-call context breakdown and configured vs
actual window, measured vs estimated input, tool/image/profile/memory/skill
contributions, pressure/reserve/truncation, all applicable compaction tiers,
restart/fork replay and HistoryExpand/Search recovery. Preserve user constraints,
permission barriers, pending work and error evidence. Context reduction is not
proof of task-state survival. Rehydration must match original text/hash; images
may require re-reading their saved file rather than HistoryExpand.

**Memory:** test write -> persisted record -> fresh store/session -> Recall and
prompt snapshot, revision-safe correction, relevance/scope/privacy, current vs
similarly named checkout receipts, cache-stable rendering and prompt quotas.
Distinguish durable memory from Note/Ledger working state. Production forgetting
requires explicit user instruction; test deletion/tombstones only in a disposable
store. A fixture that seeds the consumer field is insufficient.

**Reusable skills:** distinguish prose `Skill` discovery/loading from executable
`save_skill` / `from skills import ...`. Verify scope/precedence, progressive
loading, body freshness, persistence after restart, real import/execute behavior,
self-test enforcement, integrity, permissions and use telemetry. Do not execute
untrusted saved code outside its real gate or claim a catalog listing proves use.

**Prompt/tool/workflow:** trace profile/query options -> SDK -> effective wired
registry/permissions -> full compiled prompt, including custom text and delegated
presets. Check Read-only, Bash-only, no-tools, worker and disabled-tool surfaces.
Verify schema/docs/return shapes and failure propagation with the real registry,
permission bridge and kernel. Test actual Read->Edit and forbidden-action paths,
not only hand-seeded contexts. Do not strip user text to hide unavailable names.

**Session behavior and outcomes:** diagnose repeat-read/search candidates by exact
arguments, returned hashes, state changes and purpose; distinguish legitimate
new evidence from unchanged repeats. Check excessive output, premature batching,
stale refs, repeated polls, wasted installs, delegated failure recovery, wake/
park semantics, cross-worker identity, cancellation, peer attribution and
subagent tool scope. Include finished artifacts, real-app verification and any
claims that exceeded the evidence.

## 5. Synthesize findings and reproduce confirmed defects
Every finding needs: severity, confirmed/suspected/enhancement classification,
user impact, affected revision/engine/cohort, bounded session/node/event evidence,
observed vs expected behavior, producing-path reproduction, competing explanations,
coverage limits and a small completion criterion. Counts flag candidates; quoted
errors in source fixtures are not automatically runtime failures.

Prioritize contract/safety failures, then context/cost/latency and usability.
Distinguish model behavior from harness defects, environment limits and provider
behavior. A one-off timeout, blocked browser, missing dependency or credential
is not an application defect by itself. Follow current permission policy; no
alternate route around a refusal.

Before any issue/comment/PR, inspect current code and open **and closed** issues
and relevant PRs. Update the same-root issue; never broaden unrelated issues.
Respect public-write ownership/approval. This audit does not authorize fixes,
merges, releases or settings changes. If fixes are requested separately, preserve
safety assertions and verify the producing path plus affected running surfaces.

## 6. Deliver a compact decision summary plus reproducible detail
Lead with a short owner summary (what is wrong, what to do, what is unknown),
then link the private detailed report and any actual issues/PRs. Produce:
- selection/coverage manifest and measurement definitions;
- per-session/cohort findings, sanitized evidence table and numeric metrics;
- verification matrix with **observed / controlled-pass / failed / unverified /
  not-applicable**, and artifact links explaining each status;
- prioritized recommendations, known blockers and next-audit comparison plan.

Do not label all requested domains verified because tests or a helper passed.
If providing interactive HTML, follow the self-contained file-preview rules;
otherwise link the written report file. Never expose a raw filesystem-only path.

## 7. Compare fairly and clean up
Compare equivalent task type/size, provider/model, engine, harness revision,
context-window setting and completion state. Report changes in weighted cache
reads, fresh prompt tokens, context growth, compaction effectiveness/recovery,
rework, latency/cost and successful outcomes, with coverage and small-sample
caveats. New telemetry changes the denominator: do not call it improvement.
The first 2026-09-30 audit is a legacy baseline with narrower coverage, not a
cache/compaction/memory-system certification; unavailable earlier measurements
must remain unavailable.

Stop temporary servers/jobs you started and remove only your disposable fixtures
and test stores. Preserve private baseline/manifest/evidence needed for review,
with a clear retention location; never delete user memories/skills or managed
worktrees for cleanup. Report what was actually exercised and what remains unknown.
