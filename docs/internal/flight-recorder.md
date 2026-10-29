# UI flight recorder

Some UI bugs never show up in a screenshot. Opening a new worktree session can
send the chat full page, flash back to the centred new-chat composer for one
frame, then go back to the chat. A screenshot either misses that frame or
catches it halfway, and the bug gets misdiagnosed.

The flight recorder records the screen as text. On every painted frame it reads
a small signature: the URL plus a few `data-ui-*` attributes the app sets on
purpose. It keeps a line only when the signature changes. Detectors then flag
the patterns that mean something went wrong:

| Anomaly | Meaning |
| --- | --- |
| `reversal` | A field went back to a value it had already left within one step (`surface new → active → new`). The message gives the time away and the number of frames; one or two frames is called a flicker. If `path` or `session` changed in between, the screen is showing something new (chat A active → loading → chat B active). That only counts if it was a flash of 150ms or less, and the message says what changed. |
| `remount` | A component marked with `data-ui-mount` unmounted and mounted again, or mounted twice, within one step. |
| `layout_shift` | The layout moved without user input: one shift of 0.05 or more, or 0.1 in total. The message names what moved. |
| `errors` | Uncaught errors, unhandled rejections and `console.error`, with repeats grouped. |
| `failed_requests` | `fetch` or XHR calls that got HTTP 400 or above or failed on the network. When 3 or more URLs under one parent fail the same way, they are reported as one `parent/*` anomaly. The page's own aborts are shown in the timeline but not flagged. |
| `duplicate_rows` | The same transcript `data-ui-row` id appeared in the page twice. |
| `recording_gap` | Part of the recording was lost: a full page load to another origin, the earliest entries trimmed to fit a page-load handoff, or later entries dropped at the size cap. |
| `tab_changed` | Steps ran in another tab (after `browser_tabs` select or create), so the recording does not cover them. Only the browser tool reports it. |

A **step** is one user action. Steps are separated by marks, and reversals and
remounts are only judged inside one step. Going from a chat to a new chat and
back again across two marked clicks is therefore not a reversal. The browser
tool marks every acting step by itself. `loading` and `streaming` are allowed
to go A → B → A, because a load starts and then ends.

A timeline reads like this. Here is the proof page's flicker, trimmed:

```
   0ms  mounted at start: ChatView, NewSessionHero, ChatInput
   1ms  /  surface=new session=none composer=centre messages=0 loading=false streaming=false
   4ms  > mark "click flicker"
  69ms  surface=active session=s:k2x9qa composer=bottom messages=2   << reversal(surface), …
  86ms  surface=new session=none composer=centre messages=0   << reversal(surface), …
 103ms  surface=active session=s:k2x9qa composer=bottom messages=2   << reversal(surface), …

anomalies:
1. reversal: surface went new → active → new within one step: back to new after 17ms (1 frame) — a flicker
```

Other line prefixes: `> mark`, `+`/`-` for mounts and unmounts, `!` for errors,
failed requests and duplicate rows, `~` for layout shifts, `·` for aborted
requests and `*` for page loads, visibility changes and notes.

## The fields

All the names live in one place: `frontend/src/lib/utils/ui-signature.ts`.
The attributes only describe what the view shows. Nothing in the app reads
them, so they never change behaviour. They carry no user data: no titles, no
message text, no paths and no full ids.

On ChatView's root element, which also carries `data-ui-state="chat"` (this
marks it as a state carrier):

| Attribute | Values |
| --- | --- |
| `data-ui-surface` | `new`, `cleared`, `loading` or `active`, the output of `classifyChatSurface` in `layout/chat-empty-state.ts`; a mirrored chat waiting for its first sync reads `loading` (`chatLayoutSurface`) |
| `data-ui-session` | `none`, or `s:` plus a 6-character hash of the bound session id |
| `data-ui-composer` | `centre` (under the new-chat hero), `bottom` (docked) or `hidden` (archived chat) |
| `data-ui-messages` | the transcript message count, including pending peer messages; `0` while a mirrored chat waits for its first sync (its transcript is not shown) |
| `data-ui-loading` | `true` while history loads (opening a chat, boot restore or a deep link) |
| `data-ui-streaming` | `true` while a reply streams |

`data-ui-mount` markers are on the root elements of `ChatView`, `ChatInput`
(the composer), `NewSessionHero`, `MessageArea` (the transcript) and
`SidePanel`. Every transcript message row carries `data-ui-row` set to its
message id, both at the top level and inside work logs and system groups. A
top-level group carries its group key, and a row without an id carries none.

The recorder also records `path`, which is `location.pathname` plus the query
string. Query values whose names look like credentials (`token`, `key`,
`session`, `code` and so on) are replaced with `…`. Error text and request
URLs are scrubbed the same way, and request URLs keep no query string.

## Using it

### 1. The browser tool (dev instance, branch preview or the live app)

```
browser_record action=start cpu_throttle=4 keyframes=true
browser_act … / browser_navigate … / browser_eval …   (each one marks itself)
browser_record action=mark label="waiting for the reply"   (optional)
browser_record action=stop
```

`stop` returns a JSON block (anomalies, stats, keyframe captions), then the
text timeline, then one contact-sheet image when keyframes were on. The sheet
has up to 12 screenshots, one per signature change, and frames an anomaly
points at are outlined. Use `fields` to record only some attributes, and
`selectors: ["toasts=.toast"]` to add element counts as extra fields.
`network` can be `slow-3g`, `fast-3g` or `4g`. CPU and network throttling end
at `stop` or when `max_duration_ms` runs out (120 s by default).

The recording keeps going through SvelteKit client routing (same document). It
also survives full page loads within the same origin: the old document hands
its timeline over through `sessionStorage`, and an init script resumes it in
the new one. That handoff is capped at 1,000,000 characters (the oldest
entries go first, reported as a `recording_gap` naming how many and up to when)
and removed when the recording stops; `start` also clears any handoff an
earlier recording left behind.

`browser_act`, `browser_navigate`, `browser_eval`, `browser_upload` and
`browser_resize` mark themselves only on the recorded tab. Steps on another
tab are not marked; `stop` lists them in one `tab_changed` anomaly.

**Pass condition:** a step has no anomalies and its timeline only moves
forward. For a suspected flicker, record at 4x CPU, which is about a mid-range
phone.

### 2. Journey tests (Playwright)

```js
import { withRecording, contactSheet } from './flight-recorder/record.mjs';

const { text, anomalies, keyframes } = await withRecording(page, { cpu: 4 }, async ({ mark }) => {
  await mark('click New chat');
  await page.getByRole('button', { name: 'New chat' }).click();
  await page.locator('[data-ui-surface="new"]').waitFor();
});
assert.deepEqual(anomalies, [], text);
await contactSheet(keyframes, `${process.env.SCREENSHOT_DIR}/new-chat.jpg`);
```

`frontend/tests/flight-recorder/record.mjs` uses playwright-core from
`browser-mcp/node_modules`, the same as `visual-regression.mjs`, and works
with Chromium only (it needs CDP). If the actions throw, the recording still
stops, throttling is cleared, and `error.flightRecording` holds the timeline;
when the recording survived, `error.flightAnomalies` holds what it caught up
to the failure (the journey runner treats a failure without it as a lost
recording, never as a known bug).
`frontend/tests/verify-flight-recorder.mjs` is a working example. Journeys
against a dev instance use the shared runner in `frontend/tests/journeys/`
(viewport × throttle variants, one report per case; see its README). It runs in
CI after the Browser MCP tests, on the proof page
`frontend/tests/flight-recorder/fixture.html`:

```
cd frontend && node tests/verify-flight-recorder.mjs
FLIGHT_RECORDER_VERBOSE=1 SCREENSHOT_DIR=/tmp/fr node tests/verify-flight-recorder.mjs
```

### 3. Ad hoc, when the browser tool is older than `browser_record`

The recorder is one self-contained script:
`browser-mcp/src/recorder/flight-recorder.js`. Pass its whole text as the
`code` of `browser_eval`. The app's CSP blocks loading it from a URL or with
`eval`, but `browser_eval` is not subject to the page's CSP. Then:

```js
window.__flightRecorder.start()               // { ok, id, fields }
window.__flightRecorder.mark('open chat B')   // before each action
// … act …
window.__flightRecorder.stop().text           // timeline and anomalies
```

`stop()` also returns `anomalies`, `timeline` and `stats`. Without the tool,
nothing re-injects the recorder after a full page load, so stay within client
routing. There are no automatic marks either, so mark every action yourself;
otherwise two deliberate opposite actions read as one reversal.

## How it works, and its limits

- **Painted frames.** Each frame, the recorder's `requestAnimationFrame`
  callback resizes a hidden 1px probe. The probe's `ResizeObserver` runs after
  every rAF callback and layout, just before paint, and that is when the
  signature is read. So the recorder sees what the frame paints. A value set
  and undone within one frame is never on screen and is not recorded. The
  probe sits in a closed shadow root at the end of `<html>`, so the page's
  MutationObservers never see the per-frame changes. If the page removes the
  probe, sampling falls back to the rAF callback.
- **Cost.** Each frame runs a few `querySelectorAll` calls: about 0.05 ms per
  frame on a desktop, and 0.2 to 0.35 ms at 4x CPU throttling (see
  `stats.sampleMsAvg`). Every hook is wrapped so it can never throw into the
  page. The `fetch`, XHR and `console.error` wrappers are restored at stop,
  unless the page wrapped them again after us; in that case they stay as
  pass-throughs.
- **Keyframes** come from the Chromium screencast, matched to signature
  changes by swap time. The screencast sends a frame only when pixels change.
  A state that repainted nothing is shown with the last earlier frame and
  captioned `no repaint`, and only when an anomaly points at it. Keyframes and
  throttling need Chromium.
- **Limits.** Layout shifts are measured by Chromium only. A timeline is capped
  at 5000 entries; anything dropped is reported as a `recording_gap`. A
  navigation to another origin loses the earlier part of the recording. Iframes
  are not recorded.

## Adding a field

1. Put `data-ui-<name>` on ChatView's root, or on any element with
   `data-ui-state="<area>"` (each such element is a state carrier). Build the
   value in `ui-signature.ts` as a short, enum-like string, never user text.
2. Add it to the list in `ui-signature.ts` and to the table above.
3. If the field may legitimately go A → B → A within one action (like a
   spinner), pass it in `cyclic` when starting the recording, or add it to
   `DEFAULT_CYCLIC_FIELDS` in `browser-mcp/src/recorder/analyze.js`. If it
   names which thing is on screen (as `session` does), add it to
   `DEFAULT_SCOPE_FIELDS`.

The detectors live in `browser-mcp/src/recorder/analyze.js`. The recorder
embeds a copy of it, so after editing it run
`node browser-mcp/scripts/sync-recorder.mjs`; a test fails while the copy is
stale.
