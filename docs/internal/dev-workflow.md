# Working on AI Shuffle: prove it on a running copy

Most bugs that reached the owner in October 2026 only appeared in a running
app: a screen that flashed back to an earlier state, a reply that never
showed, a duplicate row, a chat stuck on "working". Unit tests and component
fixtures passed because they assert against stand-ins. This page is the loop
every change follows so those bugs are caught before merge, by the agent that
made them.

The tools:

| Tool | What it gives you | Page |
| --- | --- | --- |
| Dev instance | `python -m scripts.devloop up`: a private copy of the branch with its own database, live reload, seed data (git projects, long chats, a worktree chat) and a free scripted fake AI | [dev-loop.md](dev-loop.md) |
| Flight recorder | The UI's state every frame as text, with automatic flags for screens going backwards, remounts, layout jumps, errors, failed requests and duplicate rows | [flight-recorder.md](flight-recorder.md) |
| Journeys | Real user flows driven through the real UI against the dev instance, failing on any recorder anomaly, at desktop and phone sizes, fast and slowed down | [frontend/tests/journeys/README.md](../../frontend/tests/journeys/README.md) |

Private one-worker dev instances pause and wake automatically after about five
idle minutes, keeping data and the same access link; the first use waits for
startup. Use `--no-vite` when hot reload is unnecessary (about 0.5 GB less memory
per instance), and `--no-pause` for uninterrupted measurement runs or long manual
sessions. Journeys, live turns, voice, in-flight requests and client frames are
protected; local-only typing or scrolling does not extend the idle timer. CI and
`--workers 2` stay running; a running journey also holds its instance awake.
The first Send after a pause wakes the copy and reloads the page; the draft
survives, so press Send again.
A failed clean stop reports `pause-failed` and retains its data until `down` or
TTL; it never silently starts a second cluster. See the dev-loop guide for the
dev-only five-second shutdown fallback after admission closes and the
whole-pause deadline. The normal app closes passive event subscriptions and
allows finite requests to finish before retiring remaining raw query engine
reads. The default ten-second stop budget allows 6.5 seconds for that drain,
then a shared 1.5-second query-settlement reserve and two seconds for lifespan.
The Python container supervisor passes its unchanged 30-second stop budget as
`SHUFFLE_SUPERVISOR_STOP_SECONDS`, giving it a 23.5-second finite drain and a
4.5-second settlement reserve instead. Settlement retains its 15% share of the
supplied budget; the two-second lifespan reservation stays fixed.
This is deployment metadata, not a user setting; absent, invalid or out-of-range
values retain the 6.5-second default. The Windows desktop pre-stop route also
settles headless API queries, which do not belong to the session pool.

The first signal or Windows pre-stop establishes one absolute query-drain
deadline; subsequent waiters cannot renew it. Retirement ends provider reads,
not query-owned finalization. An HTTP disconnect can no longer cancel the
retired query between its issued persistence, terminal record and memory push.
Lifespan waits on the same deadline before database teardown, rather than
cancelling the finalizer when the initial HTTP-drain reserve expires.

Each non-scheduler API query commits a `query_recovery` receipt before sending
the provider prompt. The caller allocates the terminal ID before issuing that
write, so cancellation of its awaiter cannot create a second identity. Inserting
the terminal and consuming the receipt share one database transaction. A killed
process leaves either its terminal or a receipt; approved worker startup recovers
prior-process receipts for this install as aborted terminals, never runnable
work. A normal terminal wins unchanged on replay. The additive schema migration
does not infer outcomes for pre-upgrade history. This path is also portable to
Windows, where the launcher may terminate the process tree without SIGTERM.

Recovery is historical evidence, not a new execution. Recovered terminals keep
`ended_at=NULL`: the crash leaves no trustworthy completion time, and startup
must not charge the offline interval as work. Their original start dates
`created_at` and slots them below later immutable history without renumbering
existing turns. Execution-context and usage-hold readers use the original start
when an end is unknown; an observed completion wins an exact-time tie. Thus an
older recovered receipt cannot replace newer model/provider/permission settings,
while a newest recovered query still hands its context to the next wake. Missing
legacy receipt timing uses the earliest retained history/session timestamp as a
conservative ordering anchor, not as a measured execution interval.
No event timestamp or sequence is rewritten, and recovery invents no completion
event, fresh unread/settle marker, usage booking, or session-activity timestamp.
Work-time backfill also skips the unknown interval. Both terminal/receipt
atomicity and normal-completion accounting remain unchanged.

**Remaining limit:** the shortest normal stop budget is 10 seconds, lifespan
`stop_running_sessions` can take 25 seconds and adapter disconnect can take
5 seconds. Thus full lifecycle cleanup can still exceed an outer kill budget;
terminal durability is not a guarantee that every shutdown write completes.
An idle full-app shutdown measured 0.282 seconds. The retained controlled
two-second memory-publication case now finishes on both 10- and 30-second
budgets despite HTTP-owner cancellation. A legacy memory-mirror push stalled
past the absolute deadline can still be unpublished, but the already-written
local file is durable. `push_memory` is native per-project file replication,
not the durable `Remember` fact store: the next qualifying native/project turn
reconciles without overwriting local-only/newer files, then uploads them.
Startup terminal recovery does not retry that mirror push; sync must remain
enabled and the original files present (normal last-write-wins rules apply).
Real stalled PostgreSQL/adapter teardown and real-provider or cross-worker
shutdown have not been verified. Remaining lifecycle-budget and legacy mirror
work is tracked in [#1305](https://github.com/HobbyCoders/ai-shuffle/issues/1305);
launcher budgets are unchanged here.

## The loop

1. **Reproduce first.** A bug is not worked on until it reproduces on a dev
   instance. Write the reproduction as a journey (or a case in an existing
   one) and run it: it must fail for the reported reason. Timing bugs need the
   `slow` variant (4x CPU, delayed fake AI); stretch further with
   `[script:slow tps=2]` or `latency_ms`. If it truly does not reproduce, say
   so with the recorder timeline instead of guessing a fix.
2. **Find every place the rule lives.** Before fixing, search for every code
   path that applies the same rule (every way a session is created, every
   read path for ownership, every place a flag is set) and list them in the
   PR. Fixes in this repo have repeatedly patched one path while the bug lived
   on in others.
3. **Change it on the running copy.** Svelte edits hot-reload in about two
   seconds; Python edits restart only the app. Watch the recorder, not
   screenshots: inject it with `browser_record` (or the eval route in
   flight-recorder.md), act, stop, read the timeline.
4. **Run only the journey reproducing your bug or the one you are actively
   fixing locally before pushing.** `node frontend/tests/journeys/select.mjs`
   names affected journeys, but is not a request to run them all locally.
   The reproduction journey must now pass and stay in the suite. Every PR
   already repeats the journeys on GitHub.
5. **Get a second pair of eyes on the running app.** For any user-visible
   change, a separate tester agent that did not write the code uses the dev
   instance at desktop and phone sizes with the recorder on and tries to break
   it (the `live-qa` procedure). Its findings are fixed or filed before merge.
6. **Say what was exercised.** The PR's "Running-app checks" section names the
   journeys run, the recorder result (anomalies: none, or each one explained)
   and what the tester agent tried. "Fixtures only" or "not tried live" is
   allowed only with a reason and a tracked follow-up, never as a footnote.
7. **CI repeats it.** Every PR runs the journeys and the visual baselines on
   GitHub. A red journey is a real failure until proven otherwise; never
   weaken a journey or widen an allow-list without a reason that names the
   cause (environment noise, or a known bug with its issue number).
8. **Fix-chain alarm.** If an area gets its third fix within a week, stop
   patching. Write down why the earlier fixes did not hold (usually: the rule
   lives in several places, or the tests stub the part that broke), fix that
   cause, and add the journey that would have caught all three.

## What still needs a real device or a real provider

The dev instance covers the native harness, the UI, the stream and the
database with a fake AI, and voice calls (the app's call code end to end) with
a fake voice provider. Claude Code chats run on a fake `claude` that prints
compaction the way the real CLI does, from fixtures derived from it
([claude-code-compaction-protocol.md](claude-code-compaction-protocol.md));
what that page lists as not verified still needs a real CLI. It does not
cover: real provider behaviour (what a real voice model says and hears, a real
Claude Code model), workers on two different machines (`up --workers 2`
covers two real paired workers on one Linux machine; see [dev-loop.md](dev-loop.md#two-real-workers-on-one-linux-machine)), Android
native behaviour (locked-screen calls), Windows installers and updates. Those
stay in the live smoke (`docs/internal/live-smoke.md`). Its hand-checked rows
say why with `needs` (a device, a real provider, a disposable install or two
workers); rows a journey covers are `journey` rows whose evidence is the
journeys' CI pass, and rows tagged `dev-instance` are journeys still to write.

## Rules that do not change

- Never point a dev instance, journey or recorder at the owner's live worker
  data or database. The dev instance refuses to seed anything it did not
  create.
- Testing evidence (screenshots, contact sheets, timelines) stays in scratch,
  never in the PR or the repo. Describe it in text. Visual baselines in
  `frontend/tests/visual-baselines/` are the exception.
- Locally, run only the test files you added or changed; the full suites run
  on GitHub CI for every PR.
