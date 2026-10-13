# Settings redesign (October 2026)

Plan of record for the Settings rebuild. The approved Revision 4 mockup and
the first-round options are kept in the private archive (`ai-shuffle-private`).

## Why

The old Settings page grew by accretion: long description blocks, Claude configured in four
places, every extra credential rendered as another full panel, Devices and GitHub filed under
Providers, voice split across two tabs, and a text-to-speech voice six levels deep. Each feature
added another clump. The new structure has to absorb growth without another redesign.

## Rules

- Helper text is at most 5 words. If a control needs more, change the control: a better label, a
  visible value, a unit, a status badge. Longer detail only behind a "?" tooltip, used sparingly.
- Two page types only:
  - **Preference page:** one column of grouped rows. Label left, control right, optional hint.
  - **Collection page:** a list beside a detail pane, with a draggable divider (default list width
    about 384px, width remembered). Every object's detail has the same anatomy: header with status
    and one primary action, up to 4 facts, tabs only where needed, then a danger zone.
- One switch component, one inline confirm for destructive actions. Settings auto-save with a
  "Saved" tick; long text (prompts, instructions) keeps an explicit Save.
- Search covers every setting and jumps to the exact row (Ctrl+K or /). Settings are data-driven:
  adding a setting means adding one registry entry and one row.
- Colors come from theme tokens only. The Providers connector line uses `--primary`.

## Navigation

| Group | Category | Page type | Contents |
|---|---|---|---|
| Personal | General | Preference | Appearance (theme, theme style swatches), Conversation, Thinking and tools, Region (time zone) |
| Personal | Voice | Preference | Dictation (global input, hotkey, mic mode, silence, cues), Voice calls |
| AI | Providers | Collection | Providers and their credentials |
| AI | Default models | Collection | The model each task uses: text tasks, image, video, 3D, audio, dictation, voice calls (read-only), fal.ai catalog |
| AI | Integrations | Collection | GitHub, API users |
| This worker | Devices | Collection | Shuffle account and remote access, approval requests, pairing, workers, phones and browsers |
| This worker | Security | Preference | Owner password, trusted LAN, agents run as you, computer use, code sandbox, security instructions |
| This worker | System | Preference | Workspace folder, import from Claude Code and Codex, updates, license |
| This worker | Logs | Collection | Log entries |

## Providers

- A provider lists only the credentials the user connected. The "+" on a provider offers the
  credential kinds it supports that are not added yet. Providers with nothing connected sit in a
  compact "Not connected" group. "Add provider" creates a custom gateway (name, endpoint, dialect,
  token).
- Credentials are independent, not a priority list. OpenAI and Grok subscriptions and API keys can
  all be on at once; each enabled credential is its own group in the model picker, and a session
  binds to the credential whose model was picked. There is no fallback.
- Every connected credential row has its own switch. There is no provider-level switch: turning
  off one credential is the common case, and turning off all of them is just turning off each.
- Claude is the exception: one credential per worker, chosen by Claude Code mode. Its header has a
  three-way control, "Subscription | API key | Off". Off removes Claude models from the picker.
- Claude's loop guard is the Policy tab of the Claude subscription. Banked resets and weekly usage
  are the Usage tab of subscription credentials.
- fal.ai has one credential, the media key, which also reads the balance (the separate billing key
  is removed by PR #1629).

## One user, no admin split

AI Shuffle has one user. Integration API keys never reach the web UI. The only web viewer without
owner rights is the owner's own paired device that has not been approved yet, so Settings shows
everything read-only with a single "This device needs approval" banner. There are no per-row
locks, hidden categories, or "admin" labels. The backend keeps `require_admin` on writes.
