# Offline measurement contract (schema version 1)

`scripts/metrics.py` is a privacy-minimized arithmetic helper, not a collector.
Feed it one normalized frozen snapshot after joining/deduplicating real terminal
usage by stable call ID. Do not feed raw streamed nodes/events or invent missing
fields. Translate source field names ONLY after verifying the target normalizer.

Required top-level fields: `schema_version: 1`, `sessions: [...]`. Optional
private metadata: `captured_at`, `source_revision`, `selection`. It does not
emit arbitrary source metadata/user text. Each session has unique anonymized
`key`, `cohort` (e.g. primary, child, supplemental), `calls` and optionally
`compactions`. Every call requires unique `call_id` within the session, `provider`,
`model`, `engine` and `input_semantics` (`exclusive`, `inclusive`, or `unknown`).
Unknown modes/providers should be explicit, not guessed from a session title.

Call token fields: `input_tokens`, `cache_read_tokens`, `cache_creation_tokens`,
`output_tokens`, optional `context_limit_tokens`; integer or null. Missing/null
means unknown; negative, float or boolean counts are invalid. Explicit normalized
zeros are usable arithmetic but **do not prove the provider reported a field**.
Add `cache_read_reported` and `cache_creation_reported` booleans when the producer
zero-fills absent values. False makes that cache field unknown. Missing flags
mean the collector has already preserved missing-vs-zero faithfully. Inspect raw
provider payloads to establish this; don't set flags from default dataclasses.
`transport_failed: true` excludes a call from usage/cache totals but retains the
failure count. Failed attempt cost needs separate raw/authoritative accounting.

For exclusive counts, total prompt = input + read + creation only if all are
known. For inclusive counts, input already is total; read/creation are subsets
and must not exceed it. Unknown semantics exclude the call from prompt arithmetic.
Cache fraction uses paired known read+prompt calls, not unrelated totals.
Context-limit utilization includes calls with known prompt and a positive known
window. Prompt p95 uses nearest rank. Cumulative totals mean repeated exposure,
not unique data. First call per session/provider/model/engine is excluded from
`warm_candidates`; TTL/prefix eligibility are not inferred.

Compaction entries: `tier` and `reason` labels plus optional `decision_reason`,
`before_tokens`, `after_tokens`. Normalize legacy/native unreported zero sentinel fields to null
before input. The helper only aggregates positive reported before and nonnegative
reported after pairs; it counts events with missing token pairs separately.
It does not infer recovery, task-state preservation, cache cause or token savings
from `reclaimed_chars`. Verify those manually using the verification matrix.

Minimal synthetic example (not historical evidence):

```json
{"schema_version":1,"sessions":[{"key":"session-01","cohort":"primary",
 "calls":[{"call_id":"call-01","provider":"example","model":"model-a",
 "engine":"native","input_semantics":"exclusive","input_tokens":100,
 "cache_read_tokens":200,"cache_creation_tokens":0,"output_tokens":25,
 "context_limit_tokens":1000}],
 "compactions":[{"tier":1,"reason":"trigger","before_tokens":900,
 "after_tokens":500}]}]}
```

Run with `--output` in a private scratch directory; output files are created
exclusively (never overwrite evidence or input). Without it, sanitized numeric
JSON prints to stdout; bind then selectively inspect in code mode. Baseline
comparison requires the same schema, compares only matching cohort/provider/
model/engine groups, records unmatched groups and emits absolute deltas only.
Task mix/revisions/coverage still need human review; deltas are not causal proof.

Useful current source entry points (verify paths/functions at audit time):
- `fluid/fluid/events/model.py`: Usage, ContextCompaction, CacheMissAlert.
- `fluid/fluid/providers/openai_responses/serialize.py`,
  `fluid/fluid/providers/_chat_completions.py`: inclusive-to-exclusive accounting.
- `fluid/fluid/context/usage.py`, `cache_miss_classes.py`, `compaction.py`,
  `trim.py`; `fluid/fluid/engine/`: request checkpoints and compaction producers.
- `app/core/provider/harness_adapter.py`, `cache_miss_classes.py`,
  `fluid_transcript.py`, `app/core/query_options.py`: propagation/compiled prompts.
- `fluid/fluid/context/instructions.py`, `fluid/fluid/tools/builtins/skill.py`:
  prose skill discovery and actual body loading.
- `fluid/fluid/codemode/skills.py`, `app/core/fluid_memory.py`,
  `app/mcp/builtin_tools.py`: durable stores and native/delegated memory bridges.
- Tests: discover current cache/compaction/history/memory/skill producer suites by
  name rather than assuming every historical filename still exists.
