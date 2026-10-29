# UI journeys

A journey drives the real app through one user flow in Chromium, records it
with the [flight recorder](../../../docs/internal/flight-recorder.md) and fails
on any anomaly: a surface that went backwards (`reversal`), a component that
remounted, an error, a failed request, a duplicate row, a big layout shift.
They run against a **dev instance** (`python -m scripts.devloop up`, see
[dev-loop.md](../../../docs/internal/dev-loop.md)): its own database, seed data
and the free scripted fake AI. They never touch a live install.

## Run them all, or what a change needs

```bash
# terminal 1 (or a Monitor task): the dev instance
python -m scripts.devloop up

# terminal 2
cd frontend
node tests/journeys/run-all.mjs                       # every journey
node tests/journeys/run-all.mjs --changed             # what this branch's diff needs
node tests/journeys/run-all.mjs --changed --base origin/main
node tests/journeys/run-all.mjs chat-send-stream tool-cards
node tests/journeys/select.mjs --why                  # which journeys a diff needs, and why
node tests/journeys/select.mjs --check                # validate map.json
```

`run-all.mjs` runs the journeys one after another and prints pass/fail per
journey and case; it writes `summary.md`/`summary.json` (a table of every
case) and each journey's full log to `$JOURNEY_OUT`. Exit code 1 if any case
failed or a journey crashed. A full run takes about 30 minutes.

**Do not edit `frontend/src` while journeys run.** Vite hot-reloads the change
into the page and the recorder reports the reload as a `remount` (a real
failure from the journey's point of view). Finish edits, then run.

`select.mjs` reads [`map.json`](map.json): changed-file globs → journeys.
Files that every journey goes through (the chat surface, session stores, the
event stream, the harness itself, the dev instance's seed) are under `all` and
run everything; the rest name the journeys they affect. Unit test files and
files no rule covers (docs, unrelated backend) select nothing. `--check`
fails when a glob matches no file or a journey has no rule, so a rename
cannot silently drop coverage; run it when you add a journey or move files.

## Run one

```bash
# terminal 1 (or a Monitor task): the dev instance
python -m scripts.devloop up

# terminal 2
cd frontend
node tests/journeys/new-worktree-session.journey.mjs            # every case × variant
node tests/journeys/new-worktree-session.journey.mjs --list     # what would run
node tests/journeys/new-worktree-session.journey.mjs --only=auto@phone
```

Needs browser-mcp's playwright-core (`npm ci --prefix ../browser-mcp`) and
Chromium (`CHROMIUM_PATH`, default `/usr/bin/chromium`). The runner reads the
dev instance's status JSON (the same file `python -m scripts.devloop status`
prints; override with `DEVLOOP_STATUS`), opens its one-time access link once
and gives every case a fresh browser context with that cookie.

Output: one text report per case (the timeline, its anomalies and what was
allowed as noise) on stdout and in `$JOURNEY_OUT/<journey>/` (default
`$TMPDIR/journeys`). Exit code 1 if any case failed. Keep these out of the
repo. Options:

| Env | Effect |
| --- | --- |
| `JOURNEY_ONLY` | same as `--only` (substring of `case@viewport-throttle`) |
| `JOURNEY_SHEETS=1` | also save a keyframe contact sheet per case |
| `JOURNEY_CONSOLE=<regex>` | add matching page console lines to the report (diagnosis) |
| `JOURNEY_HEADED=1` | show the browser |

## Variants

Every case runs at each viewport × throttle unless it narrows them:

| Viewport | Size |
| --- | --- |
| `desktop` | 1440×900 |
| `phone` | 390×844, touch, mobile |
| `fold-cover` | 360×882, touch (Galaxy Z Fold cover screen, opt-in) |
| `fold-inner` | 884×1104, touch (Fold inner screen, opt-in) |

The Fold sizes match `tests/visual-regression.mjs`. A journey opts in with
`viewports: [...DEFAULT_VIEWPORTS, ...FOLD_VIEWPORTS]` (side-panels) or names
its own list (phone-composer: `phone`, `fold-cover`).

| Throttle | CPU | Fake-AI latency |
| --- | --- | --- |
| `fast` | 1× | none |
| `slow` | 4× (about a mid-range phone) | 1.5 s before the first token |

The latency rides in the prompt (`fakeAiTag(variant)` gives
`[script:default latency_ms=1500]`), so it needs no restart.

## Write one

```js
import { fakeAiTag, runJourney } from './runner.mjs';

await runJourney({
  name: 'my-flow',
  cases: [{
    name: 'basic',
    async setup({ page, status }) { /* unrecorded: navigate, stage the UI */ },
    async act({ page, mark, variant, log }) {
      await mark('click the thing');          // one mark per user action
      /* … act, then wait until the UI has settled … */
    },
  }],
  allow: [
    // Environment noise (or a labelled known bug elsewhere), always with a reason.
    { kind: 'failed_requests', status: 403, url: /\/preferences(\/|$)/, reason: 'preview cookie cannot write preferences' },
  ],
});
```

Conventions:

- File name `<flow>.journey.mjs`; one flow per file, several entry points as
  cases.
- Put navigation and staging in `setup` and only the judged actions in `act`.
  Reversals are judged within one step (between two marks), so mark each user
  action and end `act` on a settled UI (wait for the `data-ui-*` state you
  expect, then hold a moment: late reloads are the bugs these catch).
- Use roles and labels, not CSS classes, to find controls; the `data-ui-*`
  attributes are for asserting state.
- A new `allow` rule needs a `kind` and a reason a reviewer can check:
  environment noise of the dev instance, or a known app bug outside the
  journey's subject (`reason: 'known bug #NNN: …'`). The runner refuses rules
  for `reversal` and `remount`. Shared dev-instance noise lives in
  [`lib/noise.mjs`](lib/noise.mjs); a case can add its own `allow`.
- A case that fails today because of a filed bug **in its own subject** gets
  `knownBug: { issue, error: /how it fails/, reason }`. It reports
  `KNOWN BUG #NNN` (not a failure) only while it fails exactly that way and
  the recording up to that failure caught nothing else (a flicker before the
  known failure still fails the case: a marker never hides a reversal). Any
  other failure still fails, and once it passes the runner fails it with
  "remove knownBug", so the marker cannot outlive the fix.
- Checks over every painted frame go in the case's `verify({ recording })`
  ([`lib/timeline.mjs`](lib/timeline.mjs): `expectStopFollowsStreaming`,
  `spans`, `expectNeverDecreases`); per-frame DOM watches started in `act`
  are in [`lib/app.mjs`](lib/app.mjs) (`watchUserText`, `watchRowFlicker`).
  A screenshot or a poll can miss a two-frame state; these cannot.
- Shared steps (open a new chat with the Dev agent, open a chat from the
  sidebar, send, wait for a settled turn, read the scroll position) are in
  `lib/app.mjs`; waits read the `data-ui-*` signature, never a fixed sleep
  for the outcome.
- Add the journey to `map.json` (`select.mjs --check` tells you).
- Set the state your journey depends on instead of trusting the instance's
  defaults: tool access (`pickAccess`; under Ask a code-mode cell prompts or
  not depending on the host's sandbox, and GitHub's runners have none), the
  agent (the seed saves one for everybody; `withoutSavedAgentPick` gives a
  user without one).
- The runner deletes the chats a case created once it ends (pass or fail).
  The sidebar lists only the newest chats, so leftovers push the seeded ones
  out of it and break the next journey that opens one from the sidebar.
- Seed what a journey needs in `scripts/devloop/seed.py`; the preview cookie
  cannot change settings, providers or preferences.

## Journeys

| File | Guards |
| --- | --- |
| `new-worktree-session.journey.mjs` | Starting a new session (auto worktree, new branch, existing branch, main checkout) and sending its first message never shows the centred composer again, reloads or remounts the transcript. |
| `chat-send-stream.journey.mjs` | A message in a new chat streams a reply that settles: the message shows once, `[script:long]` renders its code block and table (no raw fences), a follow-up works, and on every frame Stop shows while the reply streams and is gone once it ends. |
| `tool-cards.journey.mjs` | `[script:tool-bash]` and `[script:tool-multi]`: the work log builds forward (no row leaves and comes back, no remount), the final text lands, the folded log names what ran, expanding shows one Complete card per call. |
| `stop-midstream.journey.mjs` | Stop during `[script:slow tps=3]`: streaming ends and stays ended, Stop goes away, the partial reply stays without the rest arriving, the next message gets its reply. |
| `queue-while-busy.journey.mjs` | A second message sent while a slow reply streams shows at once (queued), is delivered once after the first turn and answered; never shown twice on any frame, no pending bubble left. |
| `provider-errors.journey.mjs` | `[script:error-500]`, `[script:rate-limit-429]`, `[script:cut]`: an alert says what happened and can be dismissed, the turn ends (no Stop left), the composer sends again and the retry clears the error. |
| `switch-chats.journey.mjs` | The seeded 280-row transcript opened for the first time from another chat, that chat (opened once before, so cached; a first open of a short chat flashes the loading placeholder, #806), back: at its last rows when read at the bottom, at the same rows when scrolled up (known bug #805: a switch back always goes to the bottom); no reversal or remount. |
| `open-unloaded-chat.journey.mjs` | Opening a chat whose history is not loaded (long or cleared; from the new-session screen, another chat, a cleared chat, or a link) moves the composer at most once and never back. |
| `reload-midstream.journey.mjs` | Reloading during a slow reply (plain, or after opening it by link) goes straight back to the chat (never the new-chat screen on the way), the reply finishes, and message and reply each show once. |
| `cleared-chat.journey.mjs` | The seeded cleared chat shows its own title and "Conversation cleared", not the new-session hero; a chat cleared while open goes to that state and a send in it works (intermittent known bug #797). |
| `side-panels.journey.mjs` | Git panel, session side panel and Settings-and-back at desktop, phone and both Fold sizes: open and close in one move, same chat and transcript after, reading position kept. |
| `phone-composer.journey.mjs` | Phone and Fold cover only: focus/type/send never move the composer's bottom edge, round buttons are round and tappable, each composer sheet fits and closes (Escape or tap outside), and Send without an agent says why at once. |
