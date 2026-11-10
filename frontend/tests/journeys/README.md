# UI journeys

A journey drives the real app through one user flow in Chromium, records it
with the [flight recorder](../../../docs/internal/flight-recorder.md) and fails
on any anomaly: a surface that went backwards (`reversal`), a component that
remounted, an error, a failed request, a duplicate row, a big layout shift.
They run against a **dev instance** (`python -m scripts.devloop up`, see
[dev-loop.md](../../../docs/internal/dev-loop.md)): its own database, seed data
and the free scripted fake AI. They never touch a live install.

## Run locally only what you are fixing

Before pushing, locally run only the journey that reproduces your bug or the
one you are actively fixing. Every PR already repeats the journeys on GitHub;
do not run the full set or all selected journeys on the worker.

Private one-worker dev instances pause and wake automatically after about five
idle minutes. A journey wakes a paused instance and pins its lease for the whole
run, including slow actions and voice waits. CI (`--ci`) and `--workers 2` never
pause. For measurement runs use `--no-pause` (for example with `--seed long`).
Prefer `--no-vite` when you do not need hot reload: serving the stamped build
saves about 0.5 GB per instance. In a manual tab, the first Send after a pause
wakes the copy and reloads the page; the draft is preserved, so press Send again.

The aggregate commands below are available for CI; use the single-file commands
under **Run one** locally.

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
The `JavaScript contracts, docs and frontend audit` CI job enforces this check
on every PR, independently of the shards that run every journey. It also runs
`select.test.mjs`, which checks the exported validator and real CLI with valid
and invalid temporary maps. `--check --map <path>` validates an alternative
map against the same checkout without changing `map.json`.

## Responsive phone and native phone shell

Hosted CI deliberately builds only the desktop bundle. Phone variants exercise
that responsive UI, not `/mobile/`. Native cases in `notes-writing`, `rail-more`,
`view-shell-back`, `automations` and the transcript/composer journeys are opt-in with
`--mobile-shell`; their default cases retain the same phone-width behaviour.
For native evidence, build both bundles (`cd frontend && npm run build`), stamp
with `python -m app.preview stamp`, and start the dev instance with `--no-vite`.
Run the selected journey with `--mobile-shell` and report it separately from
hosted responsive results. Native setup must assert it stayed on `/mobile/` so
a missing bundle cannot silently fall back to the desktop UI.

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
repo.

A failed case also names the phase it failed in (`setup`, `act` or `verify`)
and adds a **page trace** ([`lib/trace.mjs`](lib/trace.mjs)): what the page
did over the whole case, the unrecorded `setup` included. It lists API requests
and their status (method and path, no query), WebSocket frames by type (a
`query` with whether it had a session, a request id and an agent, and the
prompt's length), console errors and warnings, page errors and navigations.
`typeAndSend` notes the composer before and after its click and 1.5 s later
(the text's length and first characters, Send enabled or not, the chat
signature, any banner or alert). A first send that went nowhere then shows
whether the text was there, Send was pressed, the `query` went out and what
came back. It records shapes, never message content beyond the journey's own
prompt start.

Options:

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
  [`lib/noise.mjs`](lib/noise.mjs); a case can add its own `allow`. A move
  the product makes on purpose is never an `allow` (see `expectShift`).
- A composer move the product **designs** without user input (a cleared chat
  opened by a link centres the composer once its load ends; a clear from the
  server centres it) is a layout shift by the recorder's rules. Declare it on
  the case, never as an `allow`:

  ```js
  expectShift: [{
    step: 'the server clears the open chat',   // the mark label of its step
    composer: ['bottom', 'centre'],             // the data-ui-composer change
    maxValue: 0.15,                             // the most that move may measure
    reason: 'the clear comes from the server, not from input on this page'
  }]
  ```

  That step must move the composer from → to **exactly once** (none or two
  fails). Only the layout-shift entries of the frames around that move
  (150 ms before to 400 ms after) that moved ChatView's own layout are
  excused, and only up to `maxValue` in total; a larger move, a second one,
  or any other shift still fails ([`lib/verdict.mjs`](lib/verdict.mjs)). Rows
  leaving the transcript when the server answers an action (a rewind on a
  phone) have no composer change to name, so they declare `messages: [4, 2]`
  instead, whose excused entries may also be the transcript's own
  (MessageArea), under the same rules. The
  report lists each excused move and its size. Pair it with a `verify` that
  names every composer position of the case (`expectComposerPositions`
  below), so an extra move too small or quick to be a shift fails too.
- A case that fails today because of a filed bug **in its own subject** gets
  `knownBug: { issue, error: /how it fails/, reason }`. It reports
  `KNOWN BUG #NNN` (not a failure) only while it fails exactly that way and
  the recording up to that failure caught nothing else (a flicker before the
  known failure still fails the case: a marker never hides a reversal). A
  failure whose recording was lost (the recorder returned no anomaly list,
  say after a navigation it could not resume on) is a plain FAIL with
  "recorder lost": nothing then shows the failure was the known one alone.
  Any other failure still fails, and once it passes the runner fails it with
  "remove knownBug", so the marker cannot outlive the fix.
- Checks over every painted frame go in the case's `verify({ recording })`
  ([`lib/timeline.mjs`](lib/timeline.mjs): `expectStopFollowsStreaming`,
  `spans`, `expectNeverDecreases`, `expectComposerPositions(recording,
  ['bottom', 'centre'])` for the designed composer positions, one entry per
  position); per-frame DOM watches started in `act`
  are in [`lib/app.mjs`](lib/app.mjs) (`watchUserText`, `watchRowFlicker`).
  A screenshot or a poll can miss a two-frame state; these cannot.
- Shared steps (open a new chat with the Dev agent, open a chat from the
  sidebar, send, wait for a settled turn, read the scroll position) are in
  `lib/app.mjs`; waits read the `data-ui-*` signature, never a fixed sleep
  for the outcome.
- In CI the instance is on loopback, a secure context, so the built app's
  service worker installs on a case's first load and the app reloads once
  when it takes control (a plain-HTTP dev instance has no worker, so this
  only happens in CI). A case that reloads on purpose calls
  `settleServiceWorker(page, status)` in `setup` first, so its reload is the
  only one.
- Add the journey to `map.json` (`select.mjs --check` tells you).
- How a case is judged (designed moves, known bugs, a lost recording) has its
  own self-test, no browser needed: `node --test
  tests/journeys/lib/verdict.test.mjs` (CI runs it beside the flight recorder
  proof page).
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
- Voice calls: pass `browser: voiceBrowser` (`lib/voice.mjs`) for Chromium's
  fake microphone, name the caller's script with `[voice:NAME]` in a chat
  message before the call, and judge the call by the fake voice provider's log
  (`fakeVoiceLog`: what the app asked the voice to say, what it said) as well
  as the UI. See [the fake voice provider](../../../docs/internal/dev-loop.md#voice-calls-the-fake-voice-provider).

## Journeys

| File | Guards |
| --- | --- |
| `new-worktree-session.journey.mjs` | Starting a new session (auto worktree, new branch, existing branch, main checkout) and sending its first message never shows the centred composer again, reloads or remounts the transcript. |
| `chat-send-stream.journey.mjs` | A message in a new chat streams a reply that settles: the message shows once, `[script:long]` renders its code block and table (no raw fences), a follow-up works, and on every frame Stop shows while the reply streams and is gone once it ends. A returning visit's first message typed and sent the moment the composer shows, while a boot load is still held (`holdBootLoad`), keeps its text up to Send and its chat stays open after the load ends (#811). |
| `tool-cards.journey.mjs` | `[script:tool-bash]` and `[script:tool-multi]`: the work log builds forward (no row leaves and comes back, no remount), the final text lands, the folded log names what ran, expanding shows one Complete card per call. |
| `question-tray.journey.mjs` | `[script:ask-question]` and `[script:ask-questions]`: the agent asks in the question tray. An answer, or a Skip of the only question, closes the tray and reaches the agent ("User skipped this question."); with two questions Skip moves to the next open one, undo works and skipping the last open one sends the batch once; while disconnected Skip is disabled and sends nothing; a delivery that fails with `relay_offline` brings the question back with its picks, written and (mobile shell) typed answers, and sending again reaches the agent. `--mobile-shell` runs the phone variants in the built `/mobile/` shell (one option question is a tap there, no Skip). |
| `retired-task-chips.journey.mjs` | Suggested-task chips are gone, and nothing an older worker still sends brings them back: a list row carrying `pending_task_chips`, a pushed count and a `task_chips_changed` frame (bare and wrapped) show no tray or row words, add nothing to Needs you or the tab title and never call the old endpoints, across a chat switch and a reload. A real `[script:ask-question]` still lifts its row to Needs you and counts in the title, and answering it moves the row back. Other runs stream and finish as normal. |
| `send-turn-rail.journey.mjs` | Sending a wrapped draft, or a "Not sent" row restored into a typed draft, keeps the turn rail still on every frame while the composer shrinks, and the composer moves in one direction only. `--mobile-shell` runs the phone variants in the built `/mobile/` shell. |
| `stop-midstream.journey.mjs` | Stop during `[script:slow tps=3]`: streaming ends and stays ended, Stop goes away, the partial reply stays without the rest arriving, the next message gets its reply. |
| `queue-while-busy.journey.mjs` | A second message sent while a slow reply streams shows at once (queued), is delivered once after the first turn and answered; never shown twice on any frame, no pending bubble left. `queue-scrolled-up`: a reader scrolled up stays put when the queued turn starts (#863). `restored-words-survive-a-reload-after-the-answer`: a restored "Not sent" message and the words already in the composer both come back once after a reload that lands near the answer, with boot held so the composer saves its empty first state first (#1031). |
| `stop-holds-the-reader.journey.mjs` | Stop with messages queued must not move the row being read, when the rows land or after the turn settles, at the bottom or scrolled up, with rows that fit or overflow; rows past the fold are one tap on Jump to latest away. Each message shows exactly once in every frame, and a message sent straight after Stop never reads "Not sent". `--mobile-shell` runs them in the real `/mobile/` app locally. |
| `stop-with-queued-messages.journey.mjs`, `stop-with-queued-messages-2.journey.mjs` (two halves of `stop-with-queued-messages.cases.mjs`) | The other ways Stop with messages queued can go: a Stop the server never confirms or that never leaves the page, a Stop before the feed has named the turn, a parked turn and its background job, a reload or chat switch straight after. Words stay shown exactly once and can always be got back. `--mobile-shell` as above. |
| `provider-errors.journey.mjs` | `[script:error-500]`, `[script:rate-limit-429]`, `[script:cut]`: an alert says what happened and can be dismissed, the turn ends (no Stop left), the composer sends again and the retry clears the error. |
| `switch-chats.journey.mjs` | The seeded 280-row transcript opened for the first time from another chat, that chat (opened once before, so cached; a first open of a short chat flashes the loading placeholder, #806), back: at its last rows when read at the bottom, at the same rows when scrolled up (#805); no reversal or remount. |
| `open-unloaded-chat.journey.mjs` | Opening a chat whose history is not loaded (long or cleared; from the new-session screen, another chat, a cleared chat, or a link) moves the composer at most once and never back: each case names its exact composer positions, and a link to a cleared chat declares its one designed move (`expectShift`). A populated chat whose first history response outlives the hydration ceiling (shortened through `window.__SHUFFLE_HYDRATION_CEILING_MS__`) never reads as cleared or centres the composer: it keeps the transcript slot with a "taking longer" (or, for a server error, "could not load") notice and Retry, and the late history paints in place (#913). A cold link to a mirrored chat plays no reveal; sending the first message from an empty chat does play it (`first-message-reveal`), and switching to an off-catalog mirrored chat while it plays keeps the mirror pane anchored to and filling the message column during and after the reveal (`mirror-pane-during-live-reveal`). |
| `mirrored-chat.journey.mjs` | A chat another worker owns (simulated in the page: its rows say `is_remote`, its sync status is scripted), opened from the new-session screen: the sync check runs while the hero holds ("Syncing from …"), no "Checking sync" pane replaces a shown transcript, the composer moves once (or not at all for an empty mirror, which is checked too). |
| `reload-midstream.journey.mjs` | Reloading during a slow reply (plain, or after opening it by link) goes straight back to the chat (never the new-chat screen on the way), the reply finishes, and message and reply each show once. |
| `composer-reload.journey.mjs` | A cold reload leaves the composer where it was, in a chat with history, an empty chat and a new chat: no layout shift, and no movement on screen frame by frame. `--long-draft` adds the open very-long-draft case from #1049, which still fails. |
| `settings-reload.journey.mjs` | Reloading on Settings > Security and Settings > Usage (desktop and phone): rows that wait for the worker already draw their loaded shape (value button, hint, meter), so nothing painted is pushed down when the answers land (#1301). `--all-pages` measures every other Settings page too. |
| `cleared-chat.journey.mjs` | The seeded cleared chat shows its own title and "Conversation cleared", not the new-session hero, without moving the composer; a chat cleared while open moves it to the centre once (its designed move, `expectShift`) and a send in it works (intermittent known bug #797). |
| `agent-model-pin.journey.mjs` | The Agents page's autosave through the preview cookie a dev instance is reached with: pin a model on an agent's Models tab, wait for Saved, reload, and read the pin back from the server and the page. The seeded agent's config is put back afterwards, pass or fail. |
| `insecure-context-ids.journey.mjs` | The ids the browser makes itself on a plain-HTTP address, where `crypto.randomUUID` is genuinely absent: the one-time pairing exchange and the setup wizard's first run. Only an instance reached over the network can prove the fallback; `--require-insecure` refuses anything less, and a loopback run (CI) still drives both flows. |
| `rewind-transcript.journey.mjs` | Rewinding the latest turn, an earlier one or the only one (and with the feed stalled, or a partial-success warning; on a phone also with a short reply, where the retained reply must come into view once and stay put): the rows leave once with no reload, what stays stays on screen, the words come back to the composer and the chat takes the next message. Each rewind is held past the browser's input grace, so the move it makes (`expectShift`) is always measured. |
| `new-chat-project.journey.mjs` | The new-session screen opens on the owner's last project choice: a picked project survives New session (on the screen itself and after opening a chat in another project) and a reload; a cleared project stays cleared through New session and a reload until one is picked again. |
| `project-workers.journey.mjs` | Cross-worker project sync with three peers simulated in the page: an older worker (overview 404) keeps the plain list with no chips; a dashed chip sets the project up there, spins, then reads ready; a project only on other workers is listed, opens on the Workers tab with the local tabs disabled, and "Set up on all missing" sets it up here; a new chat on a worker without the project keeps it selectable (offline blocked), shows the callout and "Set up & send", and a failed setup shows the reason with the draft kept. |
| `side-panels.journey.mjs` | Git panel, session side panel and Settings-and-back at desktop, phone and both Fold sizes: open and close in one move, same chat and transcript after, reading position kept. |
| `voice-call.journey.mjs` | A real call from the UI on the fake voice provider: it goes live, greets once, says nothing unasked and ends with End (`start-end`); the caller's request is handed to the chat agent and its whole reply is said once, never an acknowledgement in its place (`relay`, also with a slow provider); an update the agent writes before it waits is said, then the result (`wait-update`); a call placed while the chat answers says that reply when it lands, then relays a new request (`busy`). |
| `voice-provider-endings.journey.mjs` | The provider ends or breaks the call: an inactivity end (GPT Live) ends the call quietly with a notice and a new call works (Grok: renewed, no second greeting); a provider error shows its reason, leaves no call controls and a retry works; a session that expires while the agent works is renewed without a second greeting and the reply is said there once, in full. |
| `phone-composer.journey.mjs` | Phone and Fold cover only: focus/type/send never move the composer's bottom edge, round buttons are round and tappable, each composer sheet fits and closes (Escape or tap outside), and Send without an agent says why at once. |
| `compaction.journey.mjs` | A Claude Code chat on the dev instance's fake `claude`, which prints compaction as the real CLI does (replay of the whole conversation after the boundary included; [protocol](../../../docs/internal/claude-code-compaction-protocol.md)): automatic mid-turn, typed `/compact`, and right after a tool result. The compaction shows once (its card, or the turn's folded work log that names it and holds one card), no prompt or answer shows twice after the replay, the context meter does not rise, a follow-up is answered, and a reload goes straight back to the same rows. |
| `html-preview-image-click.journey.mjs` | An inline HTML preview whose page handles its own image clicks (an `onclick` gallery, an `addEventListener` one that changes the document) keeps them: its toggle runs both ways and the app's lightbox never opens over it. A plain `data:` image still opens the lightbox, also while the page keeps changing something else in tasks of its own, and Escape closes it. |
