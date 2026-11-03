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
| replay | The mod's path only: every row of Fluid's answer, printed as an ordinary `user` or `assistant` frame with a **fresh uuid** and the same tool ids (#679). | What is attached to a row (hook context on a tool result, for example) comes back only if that row is handed back with its handle and the same content.

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
- That `logical_parent_uuid` names a row the mod posted. On 97 of 105 mechanical
  boundaries (2.1.312 to 2.1.316) it does not: it names an attachment, a stop-hook
  entry or a uuid not in the transcript. Prefer the answer it names, else take
  the newest open answer. A boundary with preserved rows or a summary is the
  summarizer's, and every open answer closes unapplied.
- That a `compacting` frame read after a bridge answer means the answer was not
  applied. The answer comes over HTTP and frames over stdout, so the frame may
  have been printed first.
- That hook context reaches the model whole. Over 10,000 characters it is saved
  to a file and the model gets a 2 KB preview. Exactly 10,000 arrives whole and
  10,001 does not, on SessionStart after a real `/compact` and on PostToolUse
  (2.1.316, `scripts/devloop/real_claude_hook_limit_probe.py`). The check is
  JavaScript's `e.length`, so these are UTF-16 units: an emoji counts two. With
  emoji filler, 10,000 units (6,675 Python characters) arrive whole, and 10,001
  units (also 6,675 characters) and 12,008 (8,013 characters) do not, on the same two
  events, though all three pass a Python `len()` check. So the leg never answers with more
  than 9,500 units, and never cuts inside a surrogate pair: an overlong restoration
  note goes out in parts, on later tool results or the next turn, and peers are
  batched against the whole answer, separators and what a hook already said
  included. `permissionDecisionReason` does not pass through this check, so a
  deny's message has no such budget. One peer message over
  the limit still goes out on an answer of its own, as holding it would lose it,
  and the preview names the file.
- That an open Fluid answer was applied. `PreCompact` runs only for Claude Code's
  own summarizer, and a real `/compact` was watched running `compacting`,
  `PreCompact`, `SessionStart(compact)`, closing status, boundary, in that order. So
  `PreCompact` closes every open answer. A note written while one is still open
  leans on it, so if the boundary then shows it did not apply, what the note left
  out is queued again under the same id. The replacement takes back what the old
  copy had not sent and the room of lower-priority text still waiting, ahead of it.
  What already went out stays counted, so the note's total never rises, and what no
  longer fits waits for the next turn's tail.
- That a turn can carry extra text whatever it is. The CLI reads a slash command
  from the last text block, so text after `/help` breaks it. Such a turn stays
  verbatim, and what restoration still owes waits for the next turn the model reads.
- That the same words are the same message. "Do not deploy" said twice with "you
  may deploy" between is three instructions, told apart by delivery id. A context
  that lacks one gets it back with everything delivered after it, so the newest
  instruction stays last.
- That replayed rows are new work. They carry fresh uuids and old tool ids
  (the leg's `ReplayFilter`).

## What survives a compaction

| What the agent was told | Where it lives after a compaction |
| --- | --- |
| A message on a tool result (PostToolUse hook, deny, stdin lane) | Stored on that tool result's node (`delivered_steers`), as native does. A fold renders it from there. Repeated by the restoration note when the answer dropped it. |
| A message that opened a turn (wake, queued row) | Pinned in its own budget in an epoch (`peer_pin_chars`), and a larger digest cap, from render version 3. |
| The session's opening request | Up to `brief_text_cap`, line breaks kept, in digest and epoch; on a model-written checkpoint too. |
| Any of them, with Claude Code's summarizer | The restoration note repeats them, as nothing says what the summary kept: one extra copy at most. |
| Any of them, on a re-seed to another worker | Added as a row right after the first when the entries no longer show them. |

Older persisted decisions keep their render version, so old sessions render
byte for byte as they did.

A model-written checkpoint stores its pin (brief and delivered messages, bounded)
on its decision, because a window drops the nodes it came from. A later checkpoint
rolls the earlier pin forward. In a fold or digest, messages from turns and from
tool results share one budget, newest first in delivery order, with a recall
reference for what stayed out.

The restoration note is a queue under one `compaction_id`, which the boundary,
the checkpoint and each `compaction_restoration` diagnostic share. Text is only
staged when composed. Cursors, callbacks and completion move once the hook answer
or the turn's frame is written; a failed write, a cancellation or an error answer
offers the same part again. A section is credited when its last part is written;
`pending`, `cut` and `omitted` stay uncredited and the rest is re-sent, from where
the section really stopped. Memory cut from the note is `replaced` only when the
turn holds a full snapshot. It always ends: the queue holds a bounded total, and a
section that cannot fit ends `cut` and uncredited.

## Not verified

Unless noted above as watched, these were read from the bundle, not on a running
CLI. The live smoke
rows `claude-compaction-replay-and-move` and `compaction-limit-continues`
([live-smoke-scenarios.md](live-smoke-scenarios.md)) run a real CLI through
compaction on a release candidate; that is where to confirm them:

- whether the CLI prints the `@internal` `compact_progress` events in
  stream-json (the app ignores them either way);
- the exact extra keys on the summary `user` frame;
- the attachment rule under a running fix. It comes from the 2.1.316 bundle and
  from transcripts. The 10,000-character limit was watched, but only through a
  probe answering the CLI's hooks, with no mod: no real CLI has run this fix or
  applied a Fluid answer under it;
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
