# Claude mod mechanical compaction: measurements

Issue: #375. Date: 2026-10-21. The setting remains **off by default**.

## Method and limits

Four real long Claude transcripts were copied read-only into a private scratch directory. No original transcript was modified. Each copy was replayed with `claude --resume <copy> --fork-session` on both paths, using the installed, signed-in Claude CLI 2.1.300 and Sonnet. The native arm used the existing note-only hook. Its scratch-only instrumentation recorded the rows before and after `next(e)`; it did not replace the summarizer. The Fluid arm used the actual delegated-loop HTTP handler and an in-memory store populated through the stream message mapper. Both arms had tool execution disabled during the replay. No transcript text, filenames, account details, or credentials are included here.

Token counts in the tables are the CLI's reported `pre_tokens` and `post_tokens`, not independently tokenized counts or billed usage. Fluid core time covers stored-row mapping, compaction, and output validation; native core time covers `next(e)`, including its model request. Wall time includes CLI startup and transcript loading. Small token and timing changes between native runs are expected.

A successful Fluid handler replaces the summarizer request; a fallback does not. Avoided summarizer requests are counted, but dollars are **not measured**: subscription usage and inherited cumulative session cost cannot establish an incremental dollar saving. Treat pre-compaction token counts only as estimates of the input quota a summarizer would have consumed. Output quota and invoice savings are unknown.

Replay `/compact` invocations make no subsequent task request, so their next-request cache-hit rates are not measured. The live continuation below supplies real cache readings. Do not infer better caching from faster compaction.

## Paired replay results

| Sample | Requested path / actual result | CLI tokens before | CLI tokens after | Core seconds | Wall seconds | Summarizer requests avoided |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| A | Fluid requested; native path, no bridge dispatch | 684,154 | 6,574 | Not measured | 71.951 | 0 |
| A | Claude Code | 684,154 | 6,540 | Not measured | 81.233 | — |
| B | Fluid | 207,260 | 48,820 | 0.047259 | 11.779 | 1 |
| B | Claude Code | 207,260 | 5,198 | 30.049 | 41.450 | — |
| C | Fluid | 114,392 | 31,120 | 0.032649 | 11.584 | 1 |
| C | Claude Code | 114,392 | 4,280 | 17.290 | 28.590 | — |
| D | Fluid requested; safe opaque-group fallback | 93,844 | 3,507 | Not measured | 28.171 | 0 |
| D | Claude Code | 93,844 | 3,622 | 15.521 | 26.885 | — |

B and C resolved all **29/29** and **17/17** emitted history references. Their bridge bodies were 3,242,870 and 514,049 bytes. D reached the bridge but contained a parallel group that could not be rebuilt without discarding opaque state, so it correctly fell back. A never dispatched to the bridge, even after startup-order handling was strengthened; the reason is unresolved. Its timings are native compaction, not a Fluid speedup. Raising the experimental body cap to 64 MiB did not change that result, so the product keeps its 8 MiB cap.

The replays exposed valid split parallel frames and the possibility of compaction before or inside session.start. Failing regression tests preceded fixes for both. The latter tests pass, but they do **not** establish why A bypassed the bridge. Two successful mechanical replays and two native/fallback cases are reported rather than selecting only favorable sessions.

B and C retained the exact latest request and all 3/3 and 4/4 recent tool ids. Native summaries retained no raw recent tool ids and did not contain the entire request verbatim. These are structural observations, not semantic task-retention scores. Open-item and file-state gold data were absent from those sampled tails. D's native summary retained 98.1% of the detected open-item word anchors; that lexical coverage is not a judged completion result.

## Live isolated preview

The branch frontend was built and stamped. A separate worker, port, data directory, and disposable PostgreSQL cluster were used, with `AI_SHUFFLE_PREVIEW=1`. The installed worker, its interpreter, and its data were not changed or restarted. A real Claude session read thirty synthetic files, one at a time. Its 1M context window was exercised with a temporary 20% auto-compaction threshold. The threshold override was confined to the preview.

| Trigger | CLI tokens before | CLI tokens after | Fluid core seconds | Summarizer requests avoided |
| --- | ---: | ---: | ---: | ---: |
| Auto 1 | 195,225 | 31,827 | 0.031 | 1 |
| Auto 2 | 199,244 | 33,790 | 0.032 | 1 |
| Manual | 96,984 | 34,275 | 0.016 | 1 |

The three bridge success records said `no summarizer`; there were no fallback records. The CLI transcript had thirty distinct Read calls over thirty distinct files, followed by ToolSearch, HistorySearch, and HistoryExpand. HistorySearch found the first evicted result; HistoryExpand returned its synthetic marker without another file read. The manual compaction did not increment the session's reported model cost or token usage.

Cache-hit fraction is `cache_read_input_tokens / (input_tokens + cache_read_input_tokens + cache_creation_input_tokens)`. The first three model requests after the final compaction measured **22.48%, 99.15%, and 99.10%**. The cold first request is a real cost of rewriting the prompt prefix, not a cache improvement.

The preview session was archived and its worker and PostgreSQL processes stopped. This was driven through the real session SDK/API, not a mocked provider. The browser UI was not exercised; no rendered-UI verification is claimed.

## Fixed retention rubric

Each live criterion scores one only when the reply agrees with the recorded task, tools, or file hashes. Missing evidence is not a pass.

| Criterion | Required evidence | Live score |
| --- | --- | ---: |
| Latest request | Correctly identifies the read-only thirty-file request and the new retention check | 1 |
| Open items | Names the pending retention item without claiming it was already closed | 1 |
| File state | Reports no edits; fixture hashes remain unchanged | 1 |
| Tool state | Reports thirty Reads, then discovery/search/expand, and no Bash or delegation; distinct CLI tool ids agree | 1 |

The live case scored **4/4**. Replay structural checks are weaker: exact latest-request containment and retained recent tool ids can be counted, but a missing verbatim quote does not prove a native summary lost the task. Open-item and file-state criteria are unscored where the selected replay tail has no independently identified gold state. These structural screens must not be advertised as three successful end-to-end task-retention trials.

## Risks and decision

- Mechanical compaction keeps substantially more active context than a model-written summary in the successful paired replays. Faster compaction alone is not evidence of lower total session cost.
- Unknown CLI-only rows stay opaque. Ambiguous legacy matches, unsafe signed/image groups, invalid pairs, missing history tools, an over-limit body, no safe saving, or a ten-second bridge timeout fall back to Claude Code.
- Error results remain verbatim, which can limit savings. Images remain text-only on HistoryExpand and may need to be read again.
- Stored references are session-scoped. Cross-worker reseeding and long-horizon task completion were not validated by this experiment.
- A broader paired continuation study is still needed for cache behavior and task retention on real work. The owner, not this change, decides whether the default changes.
