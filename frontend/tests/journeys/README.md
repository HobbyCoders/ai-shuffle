# UI journeys

A journey drives the real app through one user flow in Chromium, records it
with the [flight recorder](../../../docs/internal/flight-recorder.md) and fails
on any anomaly: a surface that went backwards (`reversal`), a component that
remounted, an error, a failed request, a duplicate row, a big layout shift.
They run against a **dev instance** (`python -m scripts.devloop up`, see
[dev-loop.md](../../../docs/internal/dev-loop.md)): its own database, seed data
and the free scripted fake AI. They never touch a live install.

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
- A new `allow` rule needs a reason a reviewer can check: environment noise
  of the dev instance, or a known app bug outside the journey's subject,
  labelled `KNOWN APP BUG` so it is removed once fixed. Never allow
  `reversal` or `remount`.
- Seed what a journey needs in `scripts/devloop/seed.py`; the preview cookie
  cannot change settings, providers or preferences.

## Journeys

| File | Guards |
| --- | --- |
| `new-worktree-session.journey.mjs` | Starting a new session (auto worktree, new branch, existing branch, main checkout) and sending its first message never shows the centred composer again, reloads or remounts the transcript. |
