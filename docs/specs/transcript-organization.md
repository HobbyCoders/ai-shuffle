# Transcript organization: what shows, when, and how it groups

Scope: **organization only.** No component is restyled. `UserMessage`, `AssistantMessage`, `ToolUseMessage`, `ThinkingBlock`, `SubagentMessage`, `SwarmCard`, `WorkLogGroup`, `QuestionTray`, `PermissionQueue`, `PrivateAccessPrompt`, `RunningTasksList` keep their current look. What changes is the model that decides which rows are visible, which are behind a fold, when a fold happens, and what state survives.

Everything below is a rule the transcript builder (`frontend/src/lib/utils/work-log.ts`) and `MessageArea` can enforce. Line references are to `main` when the spec was written and will drift.

---

## 1. The unit is the turn, and a turn is one group

**Now:** a settled turn is cut into one fold per steer (work-log.ts:760-768), each repeating the whole turn's duration; a send during a parked turn opens a *new* turn and folds everything (message-sender.ts:441, 452).

**Rule:**
1. One turn → at most **one** `WorkLogGroup`. Steers do not split it.
2. A steer is a user message whose `turnId` equals the live turn. The optimistic send while the turn is **parked** (background work running, not streaming) must carry the live turn id too, so `splitTurns` keeps it in the turn. If the backend defers it to the next turn (`queued_turn_id`), the UI keeps it inside the current turn until `turn.started` for the new id actually arrives.
3. A turn **never folds because the user sent something.** It folds only when (a) `turn.completed` has arrived *and* (b) no task, permission or blocking question for that turn is pending *and* (c) the settle-hold has elapsed. A user send does not short-circuit the hold.
4. Steer rows inside the turn are **protected** (never behind the fold) and sit in time order between the rows they interrupted. The only visible marker is the time line under the bubble reading `20:19 · mid-turn` (the existing actions row; no new bubble style).

---

## 2. Live turn: progressive folding, not a wall

**Now:** an unsettled turn renders every row flat (work-log.ts:607-622); `WorkLogGroup` live mode is dead code.

**Rule:** the live turn is split into **runs** at every *protected* row (assistant prose, question, steer, worker card, failed tool, special tool). Within a run:
- the **current** run (the one still receiving rows) is open: every row visible, newest at the bottom, live labels as today;
- every **earlier** run collapses into the existing live fold label `+N tool calls · Read 3 · Ran 2` (WorkLogGroup:117-121), one click to open. Its protected row that ended the run stays visible under it.

So a 40-step turn reads as: `[+9 steps] finding · [+12 steps] worker card · [open run: 3 rows] · Working…`. The same component tree is used for live and settled (one `WorkLogGroup` per run, `mode` flips), so per-row expansion survives the settle.

---

## 3. What is never behind a fold (the protected set)

| Row | Rule |
|---|---|
| Assistant prose that **asks** (contains `?` or an AskUserQuestion) | always visible |
| Assistant prose ≥ 2 lines or containing a heading, list, table or code | always visible (it is a finding or a result) |
| Assistant prose of one short line without a question ("Now checking the workflow.") | folds (it is narration) |
| The last prose of the turn | always visible |
| Steer bubble | always visible, in place |
| `SubagentMessage` card, any status | always visible (today completed/error cards fold) |
| Stopped subagent, special tools (Task, swarm, TodoWrite, AskUser*) | unchanged: visible |
| Failed routine tool | unchanged: folds with the run (counted as failed in the fold summary) |
| Compaction card trailing the answer, usage-limit, errors | unchanged: visible |
| Completed routine tools, thinking, hooks, task_* notices, retries, agent deliveries, tool_result | fold |

Assistant mode (`foldAssistantTranscript`) uses the **same** protected set instead of folding everything that is not prose.

---

## 4. Worker cards carry their report

**Now:** `SubagentMessage` shows identity + status + "Open transcript ↗" only; the Task tool row that holds the result is suppressed when the card exists.

**Rule:** the card body (the part already inside its `<details>`) shows the **first paragraph of the worker's report** (the Task `tool_result` text, or the delivered agent message for a resumed worker) and a `Full report` disclosure for the rest, above the existing "Open transcript ↗" link. No new styling: it is the card body it already has, with content. While running, the body shows the worker's latest tool label (what `RunningTasksList` already knows). Completed cards stay in the turn, visible (§3).

---

## 5. Things addressed to you are one list

**Now:** four surfaces (QuestionTray, PermissionQueue modal, PrivateAccessPrompt, assistant_confirm) plus prose questions that fold.

**Rule:**
1. The four surfaces stay exactly as they are (they work). What is added is a **single "Needs you" count** derived from all of them, used in three existing places: the sidebar session row status (the pill already exists: Working / Needs you / Done / Failed), the `TurnNavigator` tick for that turn (an amber dot, the rail is unchanged otherwise), and the document title.
2. Prose questions are protected (§3) so they never fold, and a turn whose last prose asks a question counts as "Needs you" until the next user message.
3. "Needs you" always wins over "Working" in the sidebar.

---

## 6. State that must survive

| State | Where it lives | Survives |
|---|---|---|
| Fold open/closed per run | session store, keyed by turn id + run index | live→settled, window remount, session switch, reload |
| Per-row expansion (tool, thinking, subagent) | same component instance (one tree for live and settled) | live→settled |
| Detail level (Summary / Verbose) | `chatPrefs` (Verbose = today's `groupToolCalls=false` + thinking on) | everything |

Fold keys use the **server** message id; an optimistic id is remapped, not re-keyed, so the group does not remount when the id arrives.

---

## 7. Collapsing from the top

The existing fold row (`.wlrow.fold`) is also rendered as the **header** of an open group and is `position: sticky; top: 0` inside the transcript while the group is on screen. Clicking it collapses. "Show fewer log entries" at the bottom stays. No new control, no new style.

---

## 8. Running work

`RunningTasksList` (inline) and `BackgroundWorkStrip` (assistant mode) stay. Two fixes feed them:
- a stopped or finished resumed agent leaves the list within seconds and its elapsed counts from the current assignment (#2233);
- when an agent finishes, the list row goes away and the **card in the turn** (§4) is where its result is; the list links "Open" to that card (scroll + flash), not only to the separate transcript.

---

## 9. Mobile

Nothing new. The protected set is the same; because steers no longer create extra folds and completed workers no longer repeat per segment, a settled turn on a phone has fewer 44px rows than today, not more.

---

## 10. Order of work (each is a PR on its own, all behind the existing `groupToolCalls` pref)

1. **Turn unity**: steers stay in the turn; no fold on user send; one fold per turn. (`work-log.ts` splitTurns/segments, `message-sender.ts` optimistic turn id.) Fixes the complaint verbatim.
2. **Protected prose + worker cards visible, with report head.** (`work-log.ts` shouldHide, `SubagentMessage` body content.)
3. **Progressive live folding** using the existing live mode; one component tree for live and settled; fold state in the store.
4. **Needs-you count** in sidebar pill, TurnNavigator dot, title.
5. **Sticky fold header** and assistant-mode parity.

Verification for each: the existing `work-log.test.ts` / `WorkLogGroup.test.ts` fixtures, plus one new fixture — the long turn from the mockup scenario (two steers, two workers, one question, one failed tool, one background monitor) — asserting the visible-row list in live and settled states.
