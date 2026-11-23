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
| A plan without a 5-hour window (Claude Ultra, every ChatGPT Pro tier: `pro`, `prolite`) shows **no 5-hour row**: no "not reported yet" placeholder, and a reported 5-hour window whose reset has passed is hidden too. A **live** 5-hour window (reset still ahead) is still shown, because provider data beats the plan rule. | Anthropic's Ultra has "No 5-hour limit for verified accounts" (newer accounts keep a temporary one until verified) and OpenAI says "Pro plans currently have no five-hour limit" ([claude.com/pricing](https://claude.com/pricing), [learn.chatgpt.com/docs/pricing](https://learn.chatgpt.com/docs/pricing), 2026-11-01). A permanent empty or stale 5-hour row was wrong for those plans. The Settings > Providers usage tabs (whose clock ticks while open, so a window that resets drops out) and the GetUsage tool follow the same rule; the tool applies the Claude plan only when the CLI status cache already holds it (no probe), and never deletes the cached reading. |

## Layout

| Part | Collapsed | Expanded |
| --- | --- | --- |
| Context window (top) | One severity-colored bar with the total % | Segmented bar per category, rows with tokens and %, trailing model row |
| Claude subscription row | The 5-hour window's **remaining %** (weekly on a plan without a 5-hour window) | Every reported window (5-hour, weekly, weekly Opus/Sonnet, Fable) as "<n>% left" with reset countdowns |
| ChatGPT row | Weekly subscription window remaining | Windows plus extra credits only when the reported balance is > 0 (credits are not USD) |
| SuperGrok row | Subscription window remaining | Windows and plan |
| OpenRouter row | Account credit left (key spend limit only when there is no account balance) | Same, plus the dashboard link |
| fal.ai row | Credit balance left | Same, plus the dashboard link |
| OpenAI API key | "—" (no meter: credit needs a Platform session or Admin key) | "Credit balance is shown only on the OpenAI Platform" and the link to its Usage page |

### Subscriptions and API keys groups

Below the context window the rows sit in two collapsible groups,
**Subscriptions** (plans: Claude, ChatGPT, SuperGrok, Kimi Code, ...) then
**API keys** (pay as you go: Claude API, OpenAI, OpenRouter, fal.ai, ...).
Each group header shows its name and how many accounts it holds. A group
starts collapsed, and its open state is remembered like a row's.

A collapsed group shows only what **the current chat** uses, so the panel stays
short:

| Group | Collapsed shows | Empty when |
| --- | --- | --- |
| Subscriptions | The plan the chat runs on, named, with its **weekly** window's remaining % (the row's own headline when the plan has no weekly window) | The chat runs on an API key |
| API keys | The API key the chat runs on; otherwise the paid API its tools last billed (fal.ai after generating an image, an OpenAI or xAI key after an image on that key), named, with that row's headline | The chat used no paid API |

The paid API a chat's tools billed is read from the chat's own tool results:
the generation tools' result names the source and account it ran on; plan
sources (ChatGPT, SuperGrok) are not paid APIs. Inside an open group the
chat's own account carries a neutral "in use" tag.

The collapsed value of a multi-window row is the window a session most often
hits first (5-hour for Claude), **not** the minimum of all windows (fixed in
#1688). On a plan without a 5-hour window (above) the collapsed value never
uses a hidden 5-hour window: Claude falls back to the weekly (all models)
window, then the smallest remaining one; ChatGPT to its least remaining
window (the weekly one).

Which plan an install is on: Claude's comes from this install's own CLI
sign-in (`claude auth status`, see the sources doc), so the plan rule applies
to the self install's Claude section and to another device's section when that
device reports the same Claude account key (the same subscription, whose
windows the panel merges). A device on another account, or one with no account
key, keeps every row. ChatGPT's comes from the provider's own `plan_type`. An
unknown plan keeps every row.

## Interaction

| Control | Desktop | Mobile |
| --- | --- | --- |
| Open panel | Click the ring | Tap the ring; tap outside closes |
| Expand/collapse a group | Click the group header (chevron) | Tap the group header |
| Expand/collapse a row | Click the row header (chevron) | Tap the row header |
| Hide a provider row | Eye-off icon, shown on row hover | Eye-off icon always visible |
| Restore hidden rows | "Hidden providers (n)" disclosure at the bottom | Same |
| Refresh | Opening the panel requests a refresh, rate-limited by the cooldowns in the sources doc | Same |
| Number changes | Remembered last value animates to the new value on open | Same, reduced motion respected |

Group and row expand state and hidden providers are account-synced preferences
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
