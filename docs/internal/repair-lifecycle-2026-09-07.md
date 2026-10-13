# Lifecycle repair dispositions (2026-09-07)

Base: `170ff629`. Changes were made in an isolated checkout; no live worker,
shared PostgreSQL database, or model provider was exercised.

| Finding | Disposition and validation |
| --- | --- |
| LIFE-01 | Both REST and chat pass the incoming lane to pool eviction. Native admission cannot evict a busy delegated session. Real caller and pool regressions cover a fully busy delegated hard cap. Existing delegated backpressure remains. |
| LIFE-02 | Detached dispatch supervisors are registered for Stop cancellation. Generations checked before attempts and after backoff/admission prevent an old retry reviving after Stop then Resume; stale reservations are released. |
| LIFE-03 | Failed-wake recovery checks Stop generations around DB count, client teardown, and enqueue. An enqueue that races Stop is removed. Queue kicks check again after DB reads. Runtime checks claim/start and pre-provider initialization windows too. |
| LIFE-04 | Absolute synchronous deadlines are checked even when idle resolves immediately; parked polling cannot reset or overrun the deadline. The regression asserts interruption instead of leaving a pending task. |
| LIFE-05 | Abandonment awaits the existing process-tree stop primitive for identified owned shells, with per-shell failure handling. Serving Monitors and descendant agents are excluded. A benign Python sleeper verifies actual process termination. Missing handles or surviving processes are logged rather than fabricated as successful cleanup. |
| LIFE-06 | Inline Monitor persists its derived status. The process reader applies the same final EXIT marker before emitting the first terminal chip. Explicit Stop remains authoritative. |
| AUDIT-SYNC-02 | Durable `is_error` and nonempty `errors` produce `session.error` even without HTTP status. The regression uses a real runtime and TurnRecorder with a fake provider pump. Webhook summaries do not expose arbitrary provider error strings. |
| QA-02 | Shared tests now isolate the entire computer-use halt/generation registry per test. Clearing only global/s1/s2 did not isolate arbitrary session IDs written by runtime teardown. No production halt behavior is weakened. |

Validation: 1,096 related backend tests passed, 2 skipped. After the runtime
startup and first-chip refinements, the affected selection passed 188 tests,
2 skipped. Tests use disposable SQLite and `pytest-socket` blocking outbound
connections. Existing warnings include fixture timing/AsyncMock warnings and
blocked probes to a non-routable fixture host; no external provider call occurred.
The Stop/process regression module includes real termination only of its own
benign sleeper. Provider billing behavior and cross-host Stop delivery still
require the coordinated live environment; these tests do not claim device or
provider end-to-end verification.

## Repeated working-set notice (user bullet 11)

Confirmed source: the runtime tries to restore a retained foreign working-set snapshot every turn. A dirty checkout, fetch failure, or divergence returns the same warning each time, which previously appended another system note. The new hook compares the latest durable working-set note using a snapshot/destination/outcome fingerprint. Recovery still runs on every turn. Changed outcomes and snapshots remain visible; successful recovery retains its completion note. Runtime restarts do not reset deduplication. A history lookup failure still surfaces the warning.

The turn-end snapshot hook now preserves an unresolved foreign scratch ref. Previously a dirty destination could replace that ref with its own changes before the foreign work had been recovered. The guard leaves the foreign pointer/ref and local tracked/untracked files intact. Existing same-runner snapshots continue normally. No database schema changes.

Validation: real disposable Git repos and file:// bare origin, in-memory DB, socket blocking. Tests recreate the runtime over repeated attempts, verify one durable warning, ensure turn-end snapshot attempts preserve both copies, then remove only fixture blockers and verify successful restore and pointer clearing. Other tests cover changed failure/new SHA, unrelated notes/session scoping, own/foreign/malformed/empty pointers, and history lookup failure. No live model/provider calls or production checkout/database modifications.

## Runtime-aware slash menu and command discovery access (user bullet 6)

The app submits user text directly to NativeLoop. Fluid's CLI REPL contains slash dispatch, but it is not wired into the app. DelegatedLoop sends text to Claude CLI; its init translator exposes tools/MCP metadata, not a command manifest. The menu now uses the adapter's existing provider-route decision with selected model/profile and durable session provider binding. Native/voice/unavailable routes advertise no raw slash commands and show an explicit empty state. This leaves manual ordinary prompts and model-selected skill tools unchanged; native slash command expansion remains unsupported.

Delegated menus retain custom commands, enabled plugin commands, skills, and noninteractive built-ins (compact, context, cost, review, init, security-review, pr-comments). Terminal/settings-only entries are excluded. Changing model/checkout clears and cancels the old request; a late response cannot restore stale commands, and a failed replacement cannot leave an old menu available.

List, detail, and explicit expansion authorize the session/project/worktree before filesystem discovery. Foreign API-user sessions and mismatched worktrees are refused; scoped callers default to their assigned project rather than the global workspace. A v2-only session checks origin access. Explicit custom expansion uses the session's actual checkout rather than its base project. No new CLI wiring or model connections were introduced.

Validation: command API/discovery/routing tests, including native/delegated provider-bound Claude and model switching, scoped ownership, actual temporary command-file expansion, and frontend late-response/403/empty-state tests. CUA verified the actual production CommandAutocomplete component in an isolated mock-transport browser fixture: switching to native replaces the list with the honest empty message, switching back restores delegated options, and custom selection works. Screenshots in lifecycle/.screenshots/commands-delegated.png and commands-native.png. No paid calls or live DB/checkout changes.
