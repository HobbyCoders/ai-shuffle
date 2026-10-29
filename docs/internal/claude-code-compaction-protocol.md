# Claude Code compaction: what the real CLI prints

Five compaction fixes landed in one week of October 2026, and several broke
again on real workers. They passed their tests because the fake CLI the tests
used printed compaction differently from the real one. This page records what
Claude Code really does around a compaction. The dev loop's fake CLI
(`scripts/devloop/fake_claude`) plays it from replay fixtures, and the app's
tests run against that fake.

## Where this comes from

No real `claude` was run and no model was called.

- **The CLI itself, read, not run.** The 2.1.312 bundle's embedded JavaScript
  was read: the SDK message schemas (with their descriptions), the engine
  loop that prints frames in `-p --output-format stream-json`, the automatic
  and `/compact` compaction paths, and the path where a `session.compact`
  hook (AI Shuffle's mod) replaces the compaction.
- **Real transcripts.** 582 compaction boundaries in Claude Code session
  files on one worker, CLI 2.1.289 to 2.1.311, read only for their shape
  (entry types, key sets, order, sizes). No content was copied.
- **The app's own records.** Audit exports of real Claude Code chats (event
  order around compactions) and the app log of a real worker (bridge answers
  and what the delegated leg did with the boundary that followed).

Nothing in the fixtures was copied from any of these. They are synthetic
re-creations: every id is a placeholder the fake fills in at run time, and a
test (`tests/scripts/test_fake_claude_fixtures.py`) fails on paths, emails,
tokens, literal ids, private addresses or the owner's names in them.

## The protocol (2.1.312)

### Frames

| Frame | Shape |
| --- | --- |
| progress | `{"type":"system","subtype":"status","status":"compacting","uuid","session_id"}`; an automatic compaction through the summarizer adds `compact_output_chars` (summary characters streamed so far, starting at 0). |
| close | `{"type":"system","subtype":"status","status":null,"compact_result":"success"\|"failed","compact_error"?,"uuid","session_id"}`. `compact_result` is only ever `success` or `failed`. |
| boundary | `{"type":"system","subtype":"compact_boundary","uuid","session_id","compact_metadata":{...},"logical_parent_uuid"}`. It never has `parent_tool_use_id`, so a subagent's boundary looks the same as the main thread's. |
| `compact_metadata` | `trigger` (`auto`/`manual`), `pre_tokens`, `post_tokens`, `cumulative_dropped_tokens` (running total of what compaction removed), `duration_ms`; through the summarizer also `preserved_segment {head_uuid, anchor_uuid, tail_uuid}` and `preserved_messages {anchor_uuid, uuids}` (the kept tail: 2 to 19 messages, median 4, anchored at the summary), sometimes `pre_compact_discovered_tools`. |
| `logical_parent_uuid` | The uuid of the last message before the compaction. In the transcripts that is usually not a user or assistant message: 402 of 524 summarizer boundaries point at an attachment (a token-count reminder), 70 at a system entry, 14 at a user message. The rows the mod posts carry the same uuids as handles. |
| summary | Summarizer only: one `user` frame whose `message.content` is the summary as a string (1.5k to 30k characters in the transcripts, median 16k), flagged `isCompactSummary` and `isVisibleInTranscriptOnly`. |
| replay | The mod's path only: every row of Fluid's answer, printed as an ordinary `user` or `assistant` frame with a **fresh uuid** and the same tool ids (#679). |

The session id does not change across a compaction (582 of 582). On
`--resume` the CLI prints only `system/init`; it never prints the history.

### Order

**Automatic, answered by Fluid** (`compaction/auto-fluid.json`). The mod's
hook replaces the compaction, so the step that prints `compacting` and runs
PreCompact never runs:

1. the mod posts the conversation to the app's compaction route and gets rows back;
2. SessionStart hook (`source: compact`);
3. close, `compact_result: success`;
4. boundary;
5. replay of the answer's rows;
6. the next model call (`message_start`, ...), and the turn goes on.

**Automatic, through the summarizer** (`compaction/auto-summarizer.json`: no
mod, the setting is off, or the mod fell back): progress (`compacting`) ->
PreCompact hook -> the summarizer's model call -> SessionStart -> close ->
boundary (with the kept tail) -> summary frame -> the next model call. The
kept messages are **not** printed again.

**Typed `/compact`** (`compaction/manual-*.json`): the command prints
`compacting` first, before the mod's hook runs. Answered by Fluid: SessionStart
-> close -> boundary -> replay -> `result` with `num_turns: 0`. Through the
summarizer: PreCompact -> summarizer -> SessionStart -> close -> boundary ->
summary -> `result`.

**`/compact` that the mod skips** (`compaction/manual-skip.json`: nothing more
to reclaim, or the bridge failed): `compacting` -> close with
`compact_result: "failed"` and `compact_error: "skipped: <reason>"` -> a
synthetic assistant line `Not compacted · <reason>` (model `<synthetic>`) ->
`result` (success, `num_turns: 0`, the same text). Nothing is summarized.

**During a tool call.** The CLI compacts before a model call. A mid-turn
compaction therefore falls between a tool's result and the model call that
reads it. Audit exports show the tool call and result, then the compaction
events, then the next call (`scenarios/compact-after-tool.json` plays it).

### What the app must not assume

- That a `compacting` status comes before every main-thread boundary. It does
  not after a Fluid answer to an automatic compaction. Until this page's fix,
  the delegated leg took every such boundary for a subagent's ("a subagent's
  compaction boundary; not reported", 30-300 ms after each "compacted through
  Fluid (auto ...)" in a real worker's log). The result: no compaction card,
  no portable checkpoint, and SessionStart did not put the host's context
  (memory, working record, environment) back. The leg now also takes the
  closing `compact_result: success`, and a boundary or SessionStart while a
  Fluid answer is open, as the main thread's.
- That `compact_result: failed` means a failure: a `skipped: ...` error is the
  mod leaving the conversation as it is, a no-op.
- That the boundary comes before the closing status. It comes after.
- That replayed rows are new work. They carry fresh uuids and old tool ids
  (the leg's `ReplayFilter`).

## Not verified

These were read from the bundle, not watched on a running CLI. The live smoke
rows `claude-compaction-replay-and-move` and `compaction-limit-continues`
([live-smoke-scenarios.md](live-smoke-scenarios.md)) run a real CLI through
compaction on a release candidate; that is where to confirm them:

- whether the CLI prints the `@internal` `compact_progress` events in
  stream-json (the app ignores them either way);
- the exact extra keys on the summary `user` frame;
- whether a typed `/compact` also prints its own `<command-name>` and
  `<local-command-stdout>` user lines, and whether the summarizer path of a
  typed `/compact` prints the kept tail again with the old uuids. The app
  ignores user frames without tool results, and the replay filter covers old
  uuids once the leg has answered a compaction.

## Using it

- `python -m scripts.devloop up`: Claude Code chats run on the fake CLI.
  `[script:compact]` compacts mid-turn, `[script:compact-after-tool]`
  compacts right after a tool result, and `/compact` compacts by hand. Each
  plays the fixture for its trigger and the app's answer (Fluid's rows, a
  skip or a fallback). See [dev-loop.md](dev-loop.md).
- A change in what a new CLI prints means changing the fixture, not the
  fake's code. `tests/scripts/test_devloop_fake_claude.py` checks that the
  fake prints exactly each fixture's sequence.
- `tests/core/test_claude_code_compaction_replay.py` drives the real
  HarnessAdapter, DelegatedLoop and compaction route against the fake for
  every case. The UI journey `frontend/tests/journeys/compaction.journey.mjs`
  does the same through the browser.
