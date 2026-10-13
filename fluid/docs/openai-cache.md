# OpenAI prompt cache on the ChatGPT Codex backend

## What changed

Codex-backend (ChatGPT subscription) sessions now send the Codex identity
headers by default. That is routing arm B: `session-id`, `thread-id` and
`x-client-request-id`, all equal to the session's `prompt_cache_key` for the
main role, and `learner:<key>` for the memory learner. The request body is
unchanged.

Before this, every client ran arm A, which sends no per-session header. The
Codex source says ChatGPT derives cache affinity from the Responses
`session-id` header, not from the body's `prompt_cache_key`. Production Usage
rows showed one route hash (`_routing_headers` 91fb81af8b63bf9d) across every
session, and calls sent under 30 seconds apart often read nothing from cache
even though the body bytes matched. Session token-hit rates were 7-43%, while
Codex on the same account read 88-99% (97-99% typical).

## Switches

| Variable | Default | Effect |
| --- | --- | --- |
| `FLUID_RESPONSES_ROUTING_ARM` | unset: `B` on the Codex backend, `A` elsewhere | `A` restores the old wire byte for byte. `C` adds `x-codex-turn-state` replay within a turn. `D` is B plus a logging-only prewarm hook. |
| `FLUID_RESPONSES_ROUTING_HINT` | off | `1`, `true` or `yes` sends `x-codex-routing-hint: model=<slug>[;tier=<tier>]` under arms B-D on the Codex backend. |

Both are read when the client is built, so a change takes effect on the next
worker restart or session connect. The public API endpoint and other
Responses hosts stay on arm A and never receive Codex headers.

## Telemetry

Each Usage row's `raw` carries:

- `prefix_hashes._routing_arm`: the arm that sent the request. It is
  diagnostic only and never counts as a request-identity change.
- `prefix_hashes._routing_headers`: the hash of the routing headers. Under arm
  B it differs per session and stays constant within one.
- `prev_prompt_coverage`: this call's cache read divided by the previous
  call's total prompt tokens (fresh, cache read and cache write), capped at
  1.0. It is absent on a session's first call. Near 1.0 means the call landed
  where the previous request was cached.

A session resumed across the upgrade shows one `routing_headers_changed` on
its first call, because the new arm sends new headers once. That is expected.

## Live verification

1. Install the new image or desktop build and leave
   `FLUID_RESPONSES_ROUTING_ARM` unset.
2. Start two or three fresh sessions on gpt-6-astra or gpt-6.1-sol through the
   ChatGPT provider row. Run a turn with five or more tool calls in each,
   within a few minutes.
3. Check the Usage rows (`fluid_session_events`, `_cls = Usage`):
   - `raw.prefix_hashes._routing_arm` is `"B"`.
   - `raw.prefix_hashes._routing_headers` is no longer 91fb81af8b63bf9d, and
     differs between sessions while staying constant within one.
   - On the second and later calls of a turn, `raw.prev_prompt_coverage` is at
     least 0.95 on at least 90% of calls where `cache_miss.reason` is absent or
     `prefix_grew_only` and `request_interval_seconds` is under 60.
   - The session token-hit rate (sum of cache reads over sum of input plus
     cache reads) is at least 85%, against 7-24% on the 09-24..09-30 baseline.
     Calls under 30 seconds apart should almost never read zero.
   - Rows with `cache_miss_class` `provider_fallback` drop to a few percent.
4. If B lands between 60% and 85%, or mid-turn zero reads persist, set
   `FLUID_RESPONSES_ROUTING_ARM=C`, restart the worker, repeat, and compare
   `prev_prompt_coverage`.
5. If B is worse than A, or requests fail with 400 or 403 on the new headers,
   set `FLUID_RESPONSES_ROUTING_ARM=A` to roll back and report.
6. Optional: `scripts/experiments/e1_cache_routing.py` compares arms A, B and C
   directly. It makes live billed calls, so run it only with the user present.

Expected: HTTP with `session-id` should read at least 85-95% of the prompt
from cache. The rest of the gap to Codex Desktop (88-99% observed, 97-99% typical) is its WebSocket
transport with `previous_response_id` continuations, a possible follow-up.

## Follow-ups outside this change

- The `# Workspace facts` block in the system prompt is rebuilt on every
  adapter connect with a freshly minted preview token, which re-bills the full
  prefix after each reconnect. The fix belongs in
  `app/core/provider/harness_adapter.py` and `app/core/preview_access.py`.
- A Responses WebSocket transport with `previous_response_id` and a real
  `generate: false` prewarm, only if arms B and C still leave mid-turn
  grew-only zero reads above about 5%.
- The Responses Lite request shape and Codex body field order, behind a
  default-off flag, measured separately.
