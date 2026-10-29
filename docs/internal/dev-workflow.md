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
4. **Run the journeys the change touches.** `node frontend/tests/journeys/select.mjs`
   names them from your diff; run them before pushing. The reproduction
   journey must now pass and stay in the suite.
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
database with a fake AI. It does not cover: real provider behaviour (voice
models, real Claude Code compaction output), two real workers, Android
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
