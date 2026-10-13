# Handoff: split-chips chat composer

Branch: `claude/composer-split-chips-handoff-lqvbcz` (based on `main` at `6fd74393`, the Tailwind CSS 4
upgrade; continues `claude/composer-split-chips`).
Status: **implemented and verified** — chip row, composer box, split controls row, effort popover,
mobile, tests. Ready for the user's review.

## What the user asked for

The user rejected the current composer as cramped and unintuitive. Three mockups were made; the user
chose the "floating split chips" design (Cursor-style) and asked for it to be implemented in the app.

The chosen mockup is committed at [docs/design/composer-split-chips.mockup.html](../design/composer-split-chips.mockup.html).
Open it in a browser. It is the source of truth for layout, spacing, sizes and hierarchy. It shows
three states: empty (hero + composer), in-conversation (docked at the bottom of a chat column), and
the effort popover open.

**One explicit exclusion:** the mockup originally had a pixel-art ghost mascot peeking over the
top-right corner of the box. The user said not to include it. It has been stripped from the committed
copy. Do not add it back.

Colours in the mockup are hardcoded (charcoal, blue accent). In the app use the tokens from
`frontend/src/app.css` instead: `--muted` for chip fill, `--muted-foreground` for chip text,
`--primary` where the mockup uses blue, `--border` / `--border-strong` for hairlines, `--card` or
`--popover` for the box and popovers. The accent is themeable, so nothing may hardcode blue.

## The design in one paragraph

Context items (agent, runner, repository, branch, worktree checkbox, new-worktree icon) are pulled out
of the input box into a loose row of small filled chips floating above it with a visible gap. The box
is a flat rectangle with a 1px hairline border and about 10px radius. Inside the box: the editor with
placeholder "Describe a task or ask a question", a small return-glyph send button at the top-right
that turns accent-coloured once there is text, and a bare-text controls row. Controls row left:
permission mode as plain clickable text ("Auto", "Bypass"), "+" for attach, mic for dictation, the
voice-call waveform, and a small chevron that opens the full controls popover. Controls row right:
model name as plain text (opens the model picker), effort as a subtle filled tag (opens an effort
popover), then the context-usage ring. Under the box a dim hint line: "@ files · / commands · shift ↵
newline". The effort popover is a compact card titled "Effort High" with a help icon, a 5-stop slider
(Low, Medium, High, XHigh, Max), "Faster" and "Smarter" end labels, and a one-line description.

## What was built

### Chip row (first session, reviewed and kept)

- `frontend/src/app.css`: shared `.ctx-chip` block at the end of the file (22px tall, 5px radius,
  `--muted` fill, no border, hover and open states, focus ring), plus `.ctx-chip-label`,
  `.ctx-chip-chev`, `.ctx-chip-icon`, the visually hidden checkbox pattern `.ctx-chip-cb`, and the
  `.ctx-sep` hairline separator.
- `ProfileSelector.svelte`, `BranchSelector.svelte`: chips on `.ctx-chip`; chevrons only in sheet
  mode; `aria-label` / `aria-haspopup` / `aria-expanded`. The sheet-mode branch label is now
  left-aligned (a button centres its text by default).
- `ContextPillBar.svelte`: the chip row, order agent · | · runner · repository · branch · worktree, in
  both the new-chat state (selectors) and the active chat (read-only runner / repository / branch /
  worktree indicator, the remote runner's "Move this session" menu, the degraded warning chip).
- `ExecutionModeBar.svelte`: the worktree checkbox chip and the "new worktree" icon chip; the
  `between` snippet slots the repository and local-branch chips between runner and worktree.

The `contextPills.ts` gating and its tests still hold: `computeContextBadge`, `shouldShowBranchPill`
and `shouldShowEmptyStateBranchPill` decide exactly what they did before; the bar only changed how
the results are drawn. No dead CSS was left in `ExecutionModeBar.svelte` or `BranchSelector.svelte`
(`svelte-check` reports no unused selectors there).

### Composer box — `frontend/src/lib/components/chat/ChatInput.svelte`

- `ContextPillBar` moved out of `.chat-island` to sit above the box (ContextPillBar's own bottom
  margin is the gap), rendered in both the empty and active states. Mobile still hides it; the
  mobile sheet's Chat-setup section carries the same selectors.
- `.chat-island` is the flat box: 1px `--border`, `var(--radius)` (10px), `--card` fill, no glass, no
  backdrop-filter. It deliberately establishes no stacking context, so chip row (z 10) < input area
  (z 20) < controls row (z 30) order themselves and every popover opens out of the box unclipped.
- Placeholder "Describe a task or ask a question".
- The send cluster sits at the top-right of the input area as a flex sibling of the editor (not an
  absolute overlay, so text never runs under it and the mobile collapsed row keeps it at the end of
  the single input line). It hosts the same logic the old foot-row cluster had: the return glyph
  (`CornerDownLeft`), muted at rest and accent once enabled; send-while-streaming as a pending
  message next to `StopButton` (which still confirms when background agents are live); the
  creating-worktree spinner; disabled when no credential / uploading / recording / transcribing.
- The dim hint line under the box (desktop only — on mobile Enter inserts a newline).
- Kept: drag-and-drop overlay, upload and worktree error banners, the Tiptap editor, the `@` file and
  `/` command autocomplete panels (they still anchor to `.textarea-wrapper` and open upward).
- Mobile: the idle collapsed surface hides the whole controls row (`.mobile-collapsed .controls-dock`)
  and keeps the input line with the send glyph at its end; thumb-sized send/stop (34px).
- `ControlsDock`'s snippet is now `voiceActions` and carries only `VoiceRecorder` and
  `VoiceCallButton`; they render right after "+" in the controls row and stay bound to ChatInput's
  recording state.

### Controls row — `frontend/src/lib/components/chat/ControlsDock.svelte`

- Left: permission mode as plain text (`.dock-txt`, opens the tool-access menu — the same five
  rows as before), "+" attach (`attach-disable.ts` behaviour kept), the mic and voice-call buttons
  from the snippet (24px on desktop), and a small chevron (`.dock-ico.small`) that opens the full
  controls popover (model picker, `EffortSliderCard` with the Fast tier, tool access). The popover's
  "Chat setup" section is gone — the chip row above the box is that, on both states.
- Right: model name as plain text (opens `ModelPickerPanel` in a right-anchored popover; amber
  `.warn` when rerouted, as before), effort as a filled tag (`.dock-tag`, opens the compact effort
  popover), then `ContextUsageBar`'s ring.
- One dock popover open at a time (`openPopover`), outside-click and Escape close it. Popovers open
  upward; on open the row measures the room above it (`--dock-pop-max`) so the tall ones (full
  controls, model picker) never run past the top of the viewport when the box sits mid-screen.
- Mobile: the settings trigger + ring lead the row (unchanged, `MobileComposerControlsSheet` still
  owns the configuration matrix); attach / mic / voice hug the right edge.

### Effort popover — `frontend/src/lib/components/chat/EffortPopoverCard.svelte` (new)

Header "Effort · <level>" with a help toggle (expands a guide of all five levels) and the Fast-tier
toggle when the model carries it; a five-stop slider (hand-drawn track, fill and ticks under a native
range input whose own track is transparent, so the thumb is a real keyboard control); the stop names;
"Faster" / "Smarter"; the current level's description. `value === null` (nothing pinned) reads
"Auto", rests the thumb on the middle stop with no stop selected, and the first drag pins a level.

## Tests

New:

- `EffortPopoverCard.test.ts` — pinned level, emitted stop, no re-emit, unpinned "Auto" state,
  pinning from the resting position, Fast toggle gating, help guide.
- `ControlsDock.split-controls.test.ts` — the split controls exist and the summary chip is gone,
  left/right grouping, effort / permission / model / full-controls popovers open, one at a time,
  Escape closes, the full popover no longer repeats Chat setup.

Changed on purpose (asserted DOM changed):

- `ChatInput.mobile-collapse.test.ts` — the collapsed rules now hide `.controls-dock` as a whole and
  never hide `.send-cluster`; the old `.controls-right` selectors no longer exist.

Unchanged and still passing: `contextPills.test.ts`, `MobileContextSelectors.test.ts`,
`MobileComposerControlsSheet.test.ts`, `ControlsDock.mobile-order.test.ts`.

## Verification record

- `cd frontend && npm install` (required after the Tailwind 4 upgrade), then `npm run check` on
  2026-09-02: 0 errors, 3 warnings, all pre-existing (`.effort-slider-card` unused selector in
  `MobileComposerControlsSheet.svelte:445`, two `state_referenced_locally` in the provider device
  flows).
- `npm test` on 2026-09-02: see the PR body for the final count (the chat directory alone:
  26 files, 288 tests, all passing).
- Browser check: the frontend served with `vite dev`, the API mocked with Playwright routes (auth
  ok, one agent, one project, git status on `main`), headless Chromium at 1440×900 dark and light, and
  390×844 mobile. Compared against the mockup: the empty state (chips, box, return glyph, controls
  row, hint line), the worktree chip checked (Auto + "from main" chips appear), a draft (accent send
  button), and the effort, permission, model and full-controls popovers all match the design. Mobile:
  empty, draft, collapsed idle line, and the controls sheet all render.

## Environment notes

- `main` upgraded to Tailwind CSS 4 in `6fd74393`; run `npm install` in `frontend/` before
  `npm run check`, or every file fails with "Cannot find package '@tailwindcss/vite'".
- On Windows, git worktrees in this repo get `frontend/node_modules` as a junction to the main
  checkout. Never run `git worktree remove` by hand on a provisioned worktree; the removal traverses
  the junction. See the app's worktree tooling.
- The app registers a service worker. When mocking the API from Playwright, create the context with
  `serviceWorkers: 'block'`, otherwise the worker's own fetches bypass `page.route`.
