# Usage panel: behavior spec

The usage panel is the popover behind the composer's context ring
(`frontend/src/lib/components/chat/ContextUsageBar.svelte`). About 12 past
sessions changed it one detail at a time with no written target. This file is
that target. **Where each figure comes from** (endpoints, credentials, polling
intervals, failure caching) is specified in
[`docs/provider-usage-sources.md`](../provider-usage-sources.md) and not
repeated here.

Change this spec first, then the code. A section marked **Known gap** is a
place where the code currently differs.

## Owner rules (apply to every row)

| Rule | Why |
| --- | --- |
| Headline numbers are **remaining** ("62% left", "$14.20 left"), never used or spent. | A used figure next to remaining figures was read wrong (Fable at 83% used looked like 83% left). |
| Usage stays visible in the app; the owner should never need a provider dashboard to know what is left. | Explicit owner requirement. Unofficial ChatGPT/Codex and SuperGrok polling stays on. |
| One account per provider. No multi-account switching UI. | The owner runs one account each. |
| Expanded rows show the windows/balances plus **only** a "Compare on the provider dashboard" link, no explanatory text blocks. | Long explanations were removed on request (2026-09-24). |
| A stale reading is shown with "Last reported <age>", never silently as current. | Prevents trusting an old number. |
| Nothing is estimated from this app's token costs. | Estimates live on the Stats card as "Estimated spend". |

## Layout

| Part | Collapsed | Expanded |
| --- | --- | --- |
| Context window (top) | One severity-colored bar with the total % | Segmented bar per category, rows with tokens and %, trailing model row |
| Claude subscription row | The 5-hour window's **remaining %** | Every reported window (5-hour, weekly, weekly Opus/Sonnet, Fable) as "<n>% left" with reset countdowns |
| ChatGPT row | Weekly subscription window remaining | Windows plus extra credits only when the reported balance is > 0 (credits are not USD) |
| SuperGrok row | Subscription window remaining | Windows and plan |
| OpenRouter row | Account credit left (key spend limit only when there is no account balance) | Same, plus the dashboard link |
| fal.ai row | Credit balance left | Same, plus the dashboard link |
| OpenAI API row | "Credit balance not available for this key" | Same; no Platform billing link |

The collapsed value of a multi-window row is the window a session most often
hits first (5-hour for Claude), **not** the minimum of all windows (fixed in
#1688).

## Interaction

| Control | Desktop | Mobile |
| --- | --- | --- |
| Open panel | Click the ring | Tap the ring; tap outside closes |
| Expand/collapse a row | Click the row header (chevron) | Tap the row header |
| Hide a provider row | Eye-off icon, shown on row hover | Eye-off icon always visible |
| Restore hidden rows | "Hidden providers (n)" disclosure at the bottom | Same |
| Refresh | Opening the panel requests a refresh, rate-limited by the cooldowns in the sources doc | Same |
| Number changes | Remembered last value animates to the new value on open | Same, reduced motion respected |

Row expand state and hidden providers are account-synced preferences
(`expansion-preferences`), so every device shows the same set.

## States

| State | What the row shows |
| --- | --- |
| Fresh reading | Values, no age |
| Stale reading | Values + "Last reported <age>" |
| Reset time has passed without a new report | Last value + "awaits a new report" (never assumes 100%) |
| Source refused the credential | "Not available for this key" + dashboard link; retried only after the credential changes |
| No data yet (Claude before the first run) | "No usage data yet" |
| Provider not configured | Row absent |

## Known gaps

- **Claude usage between sessions**: Claude figures update only from a running
  CLI's rate-limit events, so usage spent in claude.ai or another tool shows up
  after the next agent turn. There is no polled Claude source by design.
- **Anthropic API key headers** are normalized but not wired into the panel yet
  (`anthropic_headers` in the sources doc).
- **Visual regression coverage**: `frontend/tests/visual-regression.mjs
  usage-rows` renders the provider rows fixture at desktop, phone and Z Fold 6
  widths; run it before and after any change to this panel.
