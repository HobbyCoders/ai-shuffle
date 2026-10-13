# Verification matrix and producing-path probes

Use this as a checklist, not as claims. Attach an observed trace, controlled
probe or explicit evidence gap to each applicable row. An isolated probe proves
its controlled path, not that a historical/live session used that path.

## A. Coverage, accounting and prompt caching
- Selection: exact 15 primary IDs and cutoff; children, supplemental models,
  edited/forked branches, unfinished sessions and absent stores separately.
- Deduplication: one terminal model usage per call; event/node copies joined;
  provider retries and failed attempts identified, streaming deltas excluded.
- Native Usage counts are normally exclusive: fresh input + read + creation.
  OpenAI Responses/chat-completions raw input normally includes cached subsets.
  Verify the normalizer on the affected revision and configured provider. Check
  raw vs normalized vs persisted values per call, including the final call.
- Cached-token coverage: missing/not-reported vs real numeric zero; cached reads,
  creation writes, TTL categories and warm-candidate call count. Report numerator,
  denominator, n and unknown count for each ratio, not just a percentage.
- Weighted read fraction = sum(read tokens) / sum(total prompt tokens) for the
  SAME accounting-complete calls; separately report fraction of calls with any
  read. Cache writes are not cache hits. Excluding first calls per leg is only
  a warm-candidate heuristic, not evidence of TTL or eligible-prefix stability.
- Pricing: account/model-specific rates, effective pricing date, read/write
  rates and authoritative billed cost if available. Reasoning output is a subset,
  not extra output. Subscription usage percentages are not per-call cache rates.
- Prefix: hash actual pre-provider prompt/tool-schema checkpoints, not a later
  serializer approximation. Check system/profile/skills/memory/tool ordering,
  dynamic timestamps, scope changes, model changes, routing affinity, request
  checkpoint positions, cache size thresholds and context compaction boundaries.
- Diagnose misses against cold start/TTL/model switch/small eligible prefix,
  expected invalidation vs stable-prefix unexplained miss. Inspect actual cache
  miss classes/alerts on the current revision; do not invent class names.
- Controlled live check, only when authorized: tiny repeated eligible request on
  the configured account, preserve model/schema/prefix/affinity, change only the
  necessary suffix, collect raw billed usage/request IDs without secrets. Do not
  disable safety, inject fixed fake usage or infer support from public docs.
- Missing raw/request telemetry is an observability gap; mark precise portions
  unverified. Redact raw prompts/API headers before sharing evidence.

## B. Context growth, compaction and recoverability
- Track measured prompt total, context limit provenance, completion reserve,
  reported utilization/max/p95 and unknown-window counts per call and leg.
- Inspect `context_usage` fields on the target revision: total vs components,
  measured vs estimate basis, system/tools/history/memory/skills/images and active
  path. Char/byte counts and chars/4 estimates are not billed token measurements.
- Inspect configuration and effective provider window, intake stubs, tool-result
  trim, span digests, epoch stubs, provider-overflow fallback and delegated CLI
  boundaries. A tier label is not interchangeable across engines.
- Capture boundary reason, before/after tokens when REPORTED, reclaimed chars,
  changed-message index, token basis, decisions and active node ancestry.
  Legacy/native before/after `0` sentinel fields mean **not reported**, not a
  zero-token prompt. Reclaimed chars cannot be relabeled tokens.
- After a compaction boundary, compare subsequent billed prompt and cache
  recovery, normalized for new content. Report event savings and subsequent-call
  change separately; detect repeated ineffective compaction/overflow loops.
- Seed disposable original node/tool content through real store APIs. Drive real
  pressure/config to produce trim/digest/epoch decisions. Expand known refs and
  search for an original constraint/error; compare original text/hash.
- Assert active requirements, permission/refusal state, unfinished tool work and
  user corrections survive. An extractive summary existing is insufficient.
- Reload the store/engine and replay compaction; render deterministically. Fork
  before/after boundaries: off-path promotions must be inert and original nodes
  unchanged. Rewind must not cross an unsupported checkpoint.
- Image consume-once semantics: do not promise HistoryExpand restores screenshots;
  test the saved-image read path and missing-attachment behavior separately.
- Delegated CLI compaction/resume and native mechanical ladder require separate
  probes; historical behavior requires actual session evidence too.

## C. Durable memory, working state and privacy
- Trace real Remember -> durable store -> new instance/process -> Recall ->
  prompt snapshot, including root/provider scope and cluster-worker routing.
- Use a disposable store and unique synthetic IDs. Correction must carry expected
  revision; test stale writer conflict and duplicate-save idempotence. Verify
  ID/provenance and that the latest valid record is rendered after restart.
- Relevance/quotas: held/live/archived entries, omitted-entry counts, stable order,
  bounded prompt rendering, recallability of omitted records, and no automatic
  deletion solely for prompt reduction. Do not silently discard owner preferences.
- Exact checkout receipt tests: old vs oldish, old vs old/subdir, parent path
  containing old, dots/underscores, Windows normalization, changed setup state.
  Verify excluded receipts remain discoverable through Recall. User/profile facts
  and durable conventions must not be mistaken for ephemeral checkout setup.
- Scope: project/global and authorized cross-project search, worker/home boundaries,
  disabled-memory profiles, secret handling, sensitive filenames and archive
  exports. Verify effective grants, not merely matching a string in the prompt.
- Forget/tombstones: isolated explicit test only; assert stale update/replica
  replay cannot resurrect forgotten entries. Never invoke real Forget or remove
  live stores as part of an audit without the user's explicit request.
- Distinguish Note/Ledger (session working state) from Remember/Recall (durable),
  and history search from memory search. Check compaction/restart preservation
  where promised; no claim that a Note became durable without a producer event.
- Check ambient prompt injection risk: store content is data with provenance,
  not authority to bypass permission or change task scope.

## D. Prose and executable reusable skills
- Prose discovery: real project/global/plugin roots, frontmatter parsing, project
  precedence/nearest scope, enabled profile gates, namespace ambiguity, duplicate
  names, list ordering and no secret/provider-home directory scanning.
- Compiler advertises name+description only until `Skill(name=...)`. Assert the
  body is not preloaded into every prefix; load via the real tool and verify the
  current body, correct scope and on-disk update behavior after rediscovery.
- Executable Python skill: real save_skill -> pure `_selftest()` -> persistent
  SkillStore -> fresh kernel `from skills import name` -> execute inline fixture.
  Reject unsafe/malformed modules, failing self-tests and impossible imports;
  confirm rejection does not leave a partial accepted skill.
- Reuse: inspect actual import/execution/load events, result correctness, saved
  helper reuse across sessions and avoided rework. A filename/list count does
  not prove reuse. Missing load/use telemetry must be explicitly unverified.
- Lifecycle/integrity: hashes, revision/conflicts, overwrite protection, restart,
  current live-cap/archive behavior, recall/discovery consistency, broken imports,
  stale cached modules and cluster replica ordering. Keep code/store tests isolated.
- Gate enforcement: permission denial must remain terminal; filesystem/network
  access must remain bounded by the actual kernel and tool bridge. Do not run
  untrusted source in an unrestricted host interpreter to make a test pass.
- Deletion/archive probes use only synthetic audit-owned skills in disposable
  stores. Preserve user-owned skills, metadata and history.

## E. Full prompt/tool contract and workflow behavior
- Compare actual query/profile -> SDK -> permission/wired callbacks -> complete
  compiled prompt with emitted tool schemas. Test native/delegated, preset/plain,
  Read-only/Bash-only/empty/worker and disabled capabilities.
- Preserve authored user/profile text, even mentions of unavailable tools;
  gate app-owned automatic guidance instead of global string stripping.
- Schema/docs must match actual output shapes, MissingFile/ResultRef, Read gates,
  syntax checking, errors-as-results vs denied/cancelled/timeout barriers.
- Real producer exercises: Read window -> Edit, full WebFetch transport -> bridge
  -> kernel, private redirect/DNS refusal, safe HTTP failure recovery. Mock only
  external transport seams; do not bypass registry, permissions or user gates.
- Repeated reads/searches: match canonical path/window/arguments + result hash;
  inspect edits/builds/Git/subagent writes, discovery need, cache/reference loss,
  image re-reads and different windows. Label unproven repetition as candidates.
- Output: bytes acquired vs delivered vs retained/compacted; print flooding,
  ResultRef lookup, excessive full-page snapshots and over-sized batched cells.
- Background lifecycle: actual start/completion/cancel, end-turn parking and wake,
  serves jobs vs result jobs, no sleep/poll loops, no duplicate terminal report.
- Delegation/peer steering: authorized roster/provider/model, scope tools, child
  dependencies, late-terminal wake behavior, queued vs delivered, peer attribution,
  no parallel recipient turn and stop/idempotence/budget protections.
- Identity/verification: task vs branch/cwd/HEAD/runtime revision, cross-worker
  handoff, produced vs consumer-seeded state, frontend affected viewports through
  the canonical running app, outcomes and claims compared with observed artifacts.

## F. Reporting and repeatability
Record per-row status and evidence location, sample/telemetry coverage, revision,
engine/model and environment. Distinguish controlled-pass from observed-in-session.
Report confirmed defects separately from suspected defects/requested enhancements;
review current code and open/closed issues/PRs before public writes. Compare matched
cohorts with uncertainty; do not promise improvement because instructions shortened.
