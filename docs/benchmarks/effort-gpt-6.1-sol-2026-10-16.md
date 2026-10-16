# Effort wire verification and GPT-6.1 Sol benchmark

Refs #95. Measurements: 2026-10-16. This is a **lab result, not a production-quality or billing claim**.

## What changed

The app-level wire tests start at the picker capability/configuration path,
construct `AgentOptions`, connect the real `HarnessAdapter`, and retain the real
Fluid loop, request producer, provider client and serializer. Only credentials,
HTTP transport, CLI discovery and unrelated workspace discovery are fixtures.
HTTP requests are captured with `httpx.MockTransport`; no provider network call
or Claude process is needed.

Coverage includes Anthropic API, OpenAI Responses API, ChatGPT subscription,
xAI Responses, OpenRouter, custom OpenAI-dialect local servers, and delegated
Claude CLI. It exercises every advertised effort vocabulary value, unknown and
explicitly unsupported models, omission when nothing is selected, and both CLI
`--effort` and its `effortLevel` control-setting payload. Subscription catalog
capabilities are fixtures of the actual catalog schema, not name-based guesses.

Two bugs surfaced:

1. A bound Anthropic API catalog pin such as `claude-opus-4-6` was passed through
   legacy preset migration and silently upgraded to the current `opus` model.
   Consequently the 4.6 effort clamp was never exercised. Bound concrete Claude
   IDs now remain concrete at connect and hot-swap; unbound legacy presets and
   short aliases retain their existing migration behaviour.
2. Remembered/inherited Claude efforts were validated before being clamped.
   For example, `xhigh` on Opus 4.6 could disappear when metadata excluded it,
   or pass unmodified to the delegated CLI when metadata was unknown. Connect
   now uses the same version-aware Claude clamp as the native serializer before
   metadata validation: that example becomes `high`. Explicit live changes
   remain strict; unsupported picks on other routes are not silently fitted.

A local app provider is currently a custom OpenAI-dialect row using the generic
chat-completions client, not a distinct `LocalClient` UI route. Its no-effort
metadata fixture asserts no reasoning field is emitted. The separate Fluid
`LocalClient` candidate tests capture its actual request too.

## Reproduce the benchmark

Run **on the app's own worker**, using an interpreter with the app dependencies:

```sh
python -m scripts.effort_benchmark   --output /tmp/effort-results.json   --jobs 4 --repetitions 3 --seed 95   --configs default low medium high xhigh --max-seconds 2600
python -m scripts.effort_benchmark_analysis /tmp/effort-results.json
```

The runner talks to passwordless local app HTTP/WebSocket endpoints, exactly as
the local UI does. It selects the configured **ChatGPT subscription** row for
`gpt-6.1-sol`; it never reads, decrypts or refreshes OAuth tokens. A desktop
listener refusing local access is a setup failure, not permission to obtain its
credentials. It changes no worker settings and starts no replacement worker.
Keep the output and its private `.sessions.json` recovery journal **outside the
repository**. The journal holds only the sessions this run owns; do not publish it.

The suite contains 20 independently checked tasks:

- 12 coding fixes in newly created disposable Git repositories: overlapping
  search, retry bounds, stable deduplication, chunk tails, weighted means,
  interval merging, CSV quoting, TTL boundaries, topological ordering, JSON
  pointers, SemVer ordering and non-mutating config merging.
- Seven multi-file tool tasks: a ledger, dependency lock selection, log
  aggregation, inventory/reservation joins, translation fallback, signed release
  manifest selection and routing priority.
- One search-and-answer task against a fixed document corpus, with a conflicting
  proposal and later release as distractors. This is **local corpus search, not
  live web-search quality**.

All coding fixtures compile but fail their initial tests. Tests/input fixtures
are immutable, and successful answers must pass the external automatic checker.
Each task/configuration runs three times in a fresh session and fresh repository.
The 300 jobs were globally shuffled with seed 95 and concurrency four.
`default` omits an effort override; the session effort read back as `NULL`.
On that app-to-Responses serialization path the outgoing request omits the
`reasoning` object entirely (no `reasoning.effort`), as the omission wire test
asserts. It was **not an explicit low request**; low was the live provider
catalog's advertised default. Explicit effort selections were read back from
session state.
Wall time covers fixture/session startup through finished reply, not cleanup.
Tool count is app `tool.use` events, including code-mode subcalls.

The runner has bounded waits, a run deadline, incremental result writes, signal
cleanup, archive readback and a private recovery journal. `--resume` continues
unstarted combinations against the same task hash, model, concurrency and app
revision. An administrative interruption remains recorded as an interrupted
trial; do not quietly exclude or relabel it as a model failure when comparing
runs. Exit zero means the benchmark completed, not that every task passed.

## Results

Raw per-trial data and recomputable summaries:
[`effort-gpt-6.1-sol-2026-10-16.json`](effort-gpt-6.1-sol-2026-10-16.json).
All numbers below are **mean ± sample standard deviation per task run**.
Each configuration has 60 trials, including timeouts in its pass rate and latency.

| Configuration | Passed | Input tokens* | Cache-read tokens* | Output tokens* | Reported reasoning† | Wall seconds | Tool calls |
|---|---:|---:|---:|---:|---:|---:|---:|
| Default / no override | 60/60 | 22,517 ± 19,654 | 96,881 ± 24,378 | 880 ± 1,031 | 1.13 ± 5.31 | 27.16 ± 17.59 | 6.70 ± 1.41 |
| Low | 58/60 | 29,557 ± 16,153 | 86,946 ± 25,979 | 1,252 ± 1,741 | 0.00 ± 0.00 | 37.29 ± 55.98 | 6.23 ± 1.59 |
| Medium | 58/60 | 28,239 ± 20,299 | 91,883 ± 29,335 | 1,200 ± 2,087 | 2.60 ± 11.18 | 37.72 ± 52.74 | 6.52 ± 1.85 |
| High | 60/60 | 23,600 ± 12,603 | 83,915 ± 27,769 | 503 ± 391 | 4.25 ± 17.08 | 20.01 ± 8.89 | 6.70 ± 1.33 |
| Xhigh | 59/60 | 24,850 ± 12,342 | 88,506 ± 29,917 | 950 ± 1,592 | 175.56 ± 183.13 | 33.05 ± 45.71 | 10.98 ± 30.41 |

\* Input excludes separately reported cache reads. Interrupted-call estimates
are included **and flagged in every raw row**; input/output had at least one
estimated call in 16 default, 21 low, 18 medium, six high and three xhigh trials.
These are mixed provider-reported/estimated token totals, **not billed totals**.

† Reasoning is a subset of output, never an additional token charge to add to it.
This column sums **reported** reasoning only. Five timed-out trials have no
reasoning count (JSON `null`, not zero), and an estimated call can leave part of
a successful trial's reasoning unreported. `reasoning_tokens_complete` records
that distinction. Do not interpret low's observed zeros as proof it never used
reasoning tokens.

The five 300-second timeouts were search-answer/medium,
translation-fallback/xhigh, chunk-tail/low, merge-intervals/medium and
dependency-lock/low. They remain failures. Their partial token/tool metrics were
recovered from archived app events by matching the two successful trials in the
same task/configuration group, leaving exactly one unmatched session; no session
identity was guessed. Xhigh also had a successful outlier with 242 tool events.
No failure was rerun or dropped to improve the score.

### Task-type breakdown (wall seconds)

| Task type | Default | Low | Medium | High | Xhigh |
|---|---:|---:|---:|---:|---:|
| Coding, 36 trials/config | 31.19 ± 20.76 (36/36) | 39.14 ± 55.38 (35/36) | 37.03 ± 49.40 (35/36) | 21.02 ± 10.57 (36/36) | 32.22 ± 36.15 (36/36) |
| Multi-file tools, 21/config | 19.77 ± 7.16 (21/21) | 36.27 ± 61.94 (20/21) | 27.35 ± 16.79 (21/21) | 18.35 ± 5.72 (21/21) | 36.00 ± 62.04 (20/21) |
| Corpus search, 3/config | 30.50 ± 13.98 (3/3) | 22.22 ± 1.76 (3/3) | 118.69 ± 159.84 (2/3) | 19.47 ± 1.63 (3/3) | 22.32 ± 1.63 (3/3) |

High versus default finished 7.15 seconds faster on average (26.3%), with the
same observed pass rate. The paired, task-cluster bootstrap 95% interval for
high-minus-default latency is **[-12.41, -2.75] seconds** (10,000 resamples,
seed 95, 20 task clusters). Output was 377.25 tokens lower on average, but token
estimation uncertainty above applies. Small coding tasks show the strongest
practical latency signal: **32.6% faster**, 36/36 versus 36/36. Three search
repetitions of one task cannot establish a general search default.

## Tuning decision

- **Ship a visible, table-driven high initial pick for ChatGPT gpt-6.1-sol only.**
  `app/core/model_effort.py` points directly to these results and exposes both
  `default_effort: high` and `initial_effort: high`, only if the actual catalog
  advertises high. The composer consumes `initial_effort` only when there is no
  remembered per-model choice. Explicit/remembered strings and remembered `NULL`
  (provider default) remain unchanged. The implicit initial choice is never
  persisted as a user preference. No hidden adapter override is installed.
- **Do not generalize to other models or add a task classifier.** These short
  synthetic tasks do not validate other models, long coding projects or
  conversations. A 60/60 result is not proof of population-level equivalence.
  Raw vendor capability metadata retains its original default; deliberate
  provider-default omissions still omit `reasoning.effort` rather than forcing
  high. Existing sessions are not rewritten.
- **Do not lower background effort.** Learner, summaries and titles were not
  isolated treatment arms with the main effort held constant. Making that code
  active would require an isolated authenticated preview; this run neither
  reconfigured the running worker nor copied its credentials. Lookup subagents
  were also not an isolated treatment arm. No saving is claimed for either.
- **No local-model benchmark ran.** No local provider was configured/reachable
  through the available app catalog without new access. An opt-in Qwen3 +
  llama.cpp vendor candidate is implemented and wire-tested, including thinking
  on/off. It is **not applied by any app/client path**. Unknown families and
  server dialects fail closed; no generic sampling or reasoning guess ships.

No prompt text, tool descriptions, tool input schemas or deferred-tool policy
changed. The only automatic tuning is the advertised gpt-6.1-sol initial choice
above. Prefix-size/duplicate-paragraph prompt guards are unchanged. The correctness fixes were verified with captured
requests, not live Anthropic/xAI/OpenRouter/Claude calls. The GPT benchmark used
the existing running app revision recorded in the JSON, not this branch's fixes.

## Verification and cleanup

- Focused app-to-wire, effort reuse/inheritance and assistant-home tests passed.
- Local provider/preset unit tests: 25 passed.
- Required `scripts/local_ci.py check`: backend and Fluid passed before pushing.
- The separately requested raw backend invocation completed with 25,434 passed,
  681 skipped, one xfailed and three environment errors: one private PostgreSQL
  fast-shutdown deadline and two integration fixtures missing `TEST_POSTGRES_DSN`.
  These were not `SET` shim errors and are not being called a green run.
- All **300 sweep sessions plus the pilot** were archived and read back as
  archived. Disposable repositories were removed by their temporary-directory
  contexts. No preview instance or extra worker was started.
- The live workload was driven through the real app HTTP/WebSocket session API.
  Frontend tests cover the new initial choice, explicit low and remembered NULL;
  a branch UI with the changed backend catalog was not browser-verified. No
  rendered markup/styles changed. The new initial choice was captured at the
  request wire; the high treatment itself was exercised by 60 live sessions.
