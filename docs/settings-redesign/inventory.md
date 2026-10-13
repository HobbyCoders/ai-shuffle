# Settings UI inventory (current state, pre-redesign)

> Billing update (2026-10-12): the trial policy and trial-specific launch
> steps below are historical, superseded by [open-source plan D4](../release/open-source-plan.md).
> A free account includes 1 remote worker; the paid plan covers 3. No trial.
> Use the [billing runbook](../release/billing-and-accounts.md) for current rollout steps.

Repo: `D:\Development\ai-shuffle\.claude\worktrees\friendly-sammet-fe39a7`. All paths below are relative to `frontend/src/` unless they start with `app/` or `desktop/`.
Abbreviation: `S/` = `lib/components/deck/cards/settings/`, `P/` = `S/providers/`, `F/` = `P/flows/`, `C/` = `lib/components/settings/`.

---

## 1. Navigation structure

Entry: `lib/components/deck/cards/SettingsCard.svelte`, hosted in `StudioEditorShell` (`lib/components/layout/StudioEditorShell.svelte`), title "Settings", rail label "Settings sections" (SettingsCard.svelte:386-396). The same component serves web, desktop app, and the Android WebView mobile shell (opened via `MainPanel.svelte:972` / `deck/lazy/panels.ts:15`).

Rail, in order (SettingsCard.svelte:57-71):

| # | id | Label | Icon | Gate | Content (SettingsCard.svelte) |
|---|----|-------|------|------|-------------------------------|
| 1 | account | Account | User | everyone | `GeneralSection` + `OwnerAccessSection` (admin) (298-314) |
| 2 | chat | Chat & Input | MessageSquare | everyone | `ChatBehaviorSection` + `HotkeysSection` (317-322) |
| 3 | voice | Voice | AudioLines | everyone (but PUT is admin-only server-side, see pain points) | `VoiceSection` (325-329) |
| 4 | providers | Providers | KeyRound | everyone | `ProvidersSection` + `DevicesSection` (admin) (332-341) |
| 5 | license | License | BadgeCheck | everyone (edit controls admin-only) | `LicenseSection` (344-348) |
| — | admin-divider | "Admin" (non-clickable heading) | — | admin | — |
| 6 | api | API | Code2 | admin | `ApiTab` → `ApiUserManager` (351-355) |
| 7 | system | System | SlidersHorizontal | admin | `SystemTab` (Code sandbox + System prompt) (358-375) |
| 8 | logs | Logs | ScrollText | admin | `LogsTab` (378-382) |

Other navigation facts
- Default tab: `account` (SettingsCard.svelte:47). Tabs remount on every switch (`{#if activeTab === …}`), so each section refetches on open.
- External deep-link: `openSettingsTab(tab)` dispatches `shuffle-open-settings-tab` (`lib/api/account.ts:72-75`); only used by AccountPanel's "Open Settings > License" button (S/AccountPanel.svelte:290). Handler: SettingsCard.svelte:120-124.
- Remote-change banner at the top: "Workspace settings changed elsewhere. Your unsaved path is kept here." / "Security instructions changed elsewhere…" (SettingsCard.svelte:159, 179, 285).
- Search index description for the Settings page: "Providers, accounts, models, voice, appearance and devices" (`lib/api/search.ts:29`).
- The file's header comment is stale (lists Account / Connections / AI / API / System, SettingsCard.svelte:3-12).

Desktop vs mobile layout
- Wide: sticky left rail + content column, max width 1240px (StudioEditorShell.svelte:116-121, rail markup 82-104).
- At `max-width: 720px` the rail turns into a wrapping row of chips above the content; the "Admin" heading becomes a full-width label line; counts hidden (StudioEditorShell.svelte:285-315). There is no separate mobile settings screen.
- Modals/drawers go fullscreen when `platform.isMobile` (AddProviderModal, reconnect modal, ManageMediaModelsDrawer, SettingsImportDialog): P/AddProviderModal.svelte:72, P/ProviderList.svelte:76, P/ManageMediaModelsDrawer.svelte:28.
- Claude CLI in-app sign-in terminal is hidden on mobile (`canUseTerminal = terminal && isAdmin && !platform.isMobile`, C/ClaudeCliPanel.svelte:69); mobile gets the "run this command" instructions instead.

Platform gating
- Desktop app only: "Agents run as you" disclosure group at the top of Account (S/GeneralSection.svelte:37, 40-55; text from `desktop/src/main/disclosure.ts:21-31`).
- Native host only: Computer use toggle is greyed out with a reason in Docker/headless (S/ChatBehaviorSection.svelte:21-28, 60-79).
- Docker vs local: Claude CLI instructions differ by `status.login.surface === 'docker'` (C/ClaudeCliPanel.svelte:311-325).
- Code sandbox (bubblewrap) is Linux-only; the row explains that (S/CodeSandboxRow.svelte:55-58).

Admin/owner gating summary
- Admin-only groups: Application updates (GeneralSection:90-95), Owner access (SettingsCard:312), Devices (SettingsCard:337-339), Worker device confirmations (WorkerDeviceConfirmations:61), API/System/Logs tabs.
- Admin-only controls that render disabled for non-admins: Claude Code mode radios, Loop guard inputs, held-turn Confirm/Dismiss, CLI Sign in/Sign out, License key/Activate/Check/Deactivate.
- Controls shown enabled to non-admins but rejected by the server (`require_admin`): provider add/edit/remove/toggle/model curation (`app/api/model_providers.py:746`), Text generation model (`app/api/settings.py:887-890`), every Voice setting (`app/api/settings.py` PUT /voice "Admin only").

---

## 2. Every setting and control, by tab

Notation: **Label** — control type — options — default — visibility — helper text (verbatim).

### 2.1 Account tab

#### 2.1.0 "Agents run as you" (desktop app only) — S/GeneralSection.svelte:40-55
- Read-only disclosure. Title "Agents run as you", message "Agents in AI Shuffle Desktop run as you, on this computer.", four bullets, footer. Verbatim (desktop/src/main/disclosure.ts:22-31):
  - "They use your own accounts: the Claude Code CLI with your own `claude` login, and any other CLI, API key or GitHub login you set up."
  - "They can read, change and delete any file your user account can, run programs, and reach your network, not only the workspace folder."
  - "Computer use, when you turn it on, controls your real screen, mouse and keyboard."
  - "Nothing isolates an agent from the rest of your account. Review tool approvals, and do not leave Bypass on for work you would not run yourself."
  - Footer: "Want a boundary? Run the worker in Docker or on another machine and connect to it instead. You can read this again under Help > Agent Access."

#### 2.1.1 Appearance — S/GeneralSection.svelte:57-88
- **Theme** — segmented (3 buttons) — Light / Dark / System — default: system preference on first load (`lib/stores/theme.svelte.ts`) — no helper.
- **Theme style** — select — "Default" + 43 presets from `lib/themes/presets.json`: Amber Minimal, Amethyst Haze, Bold Tech, Bubblegum, Caffeine, Candyland, Catppuccin, Claude, Claymorphism, Clean Slate, Cosmic Night, Cyberpunk, Darkmatter, Default Sharp, Doom 64, Elegant Luxury, Graphite, Kodama Grove, Midnight Bloom, Mocha Mousse, Modern Minimal, Mono, Nature, Neo Brutalism, Northern Lights, Notebook, Ocean Breeze, Pastel Dreams, Perpetuity, Quantum Rose, Retro Arcade, Sage Garden, Soft Pop, Solar Dusk, Starry Night, Sunset Horizon, Supabase, T3 Chat, Tangerine, Twitter, Vercel, Vintage Paper, Violet Bloom — default "Default" — no helper.

#### 2.1.2 Application updates (admin) — S/GeneralSection.svelte:90-95 → S/ApplicationUpdates.svelte
Polls `GET /settings/application-updates` every 5s (ApplicationUpdates.svelte:10-20).
- **Allow updates** — checkbox switch — default ON — helper (line 34): "Receive signed application updates on desktop and Docker without installing a new release. Enabled by default and shared across your account's workers."
- **Update all workers** — checkbox switch — default OFF — disabled unless Allow updates is on — helper (line 38): "Use the existing update button for all online workers. When off, it updates only the worker you are connected to. Off by default."
- Footer helper (line 41): "Download and restart using the update button in the sidebar. Offline, busy, or unsupported workers are reported, not queued for a later restart. Runtime and system security changes still require a new release; native Android changes still need a Play update."
- Status when off (line 42): "Application updates are paused. Your current version will keep running."
- Note: there is no "Check for updates" / "Restart" button in Settings; that lives in the sidebar update button.

#### 2.1.3 Workspace — S/GeneralSection.svelte:97-173 (state in SettingsCard.svelte:150-271)
- **Workspace directory** — text input (mono) + **Validate** button + **Save** (primary) button — placeholder "/path/to/workspace" — feedback "✓ Valid path" / "✗ {error}" / "Workspace saved". No helper.
  - Note: workspace config is loaded only for admins (`loadAllData` is admin-only, SettingsCard.svelte:105-107) but the input renders for everyone.
- **Timezone** — select with optgroups — default "local" (label "Browser Default"). Options: Browser Default, UTC; Americas: Eastern (New York), Central (Chicago), Mountain (Denver), Pacific (Los Angeles), Alaska, Hawaii, São Paulo; Europe & Africa: London, Paris / Berlin, Moscow, Cairo; Asia & Pacific: Dubai, India (Kolkata), China (Shanghai), Tokyo, Seoul, Sydney, Auckland. No helper. (Lives under "Workspace", not Appearance/General.)

#### 2.1.4 About — S/GeneralSection.svelte:175-184
- **Version** — read-only text "AI Shuffle {version}".

#### 2.1.5 Owner access (admin) — S/OwnerAccessSection.svelte
- States: unsupported worker ("This worker does not support owner passwords. Update the worker to manage owner access here.", :85), load error with **Try again** (:89-90), Loading.
- **Set an owner password / Change the owner password** — form: Current password (only when one is set), New password (placeholder "New password (at least {min_length} characters)"), Repeat the new password, **Save password** (:95-108). Helper (:98): "Browsers sign in with it once per device. Phones and browsers linked through your HobbyCoders account use their own device sign-in." Success: "Password changed. Every other browser was signed out." / "Owner password set."
- **Trusted LAN, no login** — checkbox — default OFF — turning on shows a native `window.confirm`: "Trust every device on your network? Any browser that can reach this address becomes the owner without a password: guests on your Wi-Fi, other people on your tailnet, and any compromised device. Leave this off unless your network is only yours." (:64). Helper (:114): "Off by default. When on, every device on your local network or tailnet is the owner without a password."

### 2.2 Chat & Input tab

#### 2.2.1 Computer use — S/ChatBehaviorSection.svelte:56-79 (offsets corrected)
- **Enable computer use** — switch — default from server (`computerUse.enabled`) — inline status hint after the label: "Controlling {display}" / "On — available to every chat" / "Off". Disabled when the host can't do it, with reason text (:21-28):
  - docker: "AI Shuffle is running in Docker, which cannot control the host desktop. Run it natively to use this."
  - no-display: "No desktop display detected on this host."
  - no-monitors: "No monitors detected on this host."
  - missing-deps: "The mss/pynput packages are not installed on the server."
  - env-disabled: "Disabled by the COMPUTER_USE_DISABLED environment variable."
  - screenshot-probe-failed: "The screen-capture probe failed on this host."
  - fallback: "Unavailable ({reason})."

#### 2.2.2 Reply suggestions — ChatBehaviorSection.svelte:82-91
- **Suggest next reply** — switch — default OFF (`lib/stores/chatPreferences.svelte.ts:45`) — no helper (store comment: extra model call).

#### 2.2.3 Reasoning & tools — ChatBehaviorSection.svelte:94-148
- **Show thinking blocks** — switch — default ON.
- **Expand thinking by default** — switch — default OFF — disabled when thinking blocks hidden.
- **Show thinking summary** — switch — default ON — disabled when thinking blocks hidden.
- **Expand tools by default** — switch — default OFF.
- **Group tool calls** — switch — default ON.
- **Expand todo list by default** — switch — default ON.
(No helper text on any. Defaults: chatPreferences.svelte.ts:45-54.)

#### 2.2.4 Messages — ChatBehaviorSection.svelte:151-179
- **Auto-scroll on new messages** — switch — default ON.
- **Show message timestamps** — switch — default OFF.
- **Compact message view** — switch — default OFF.

#### 2.2.5 Voice & hotkeys — S/HotkeysSection.svelte
- **Global voice input** — switch — default OFF (`lib/stores/hotkeyPreferences.svelte.ts:55`).
- **Voice input hotkey** — key-capture button (S/HotkeyCapture.svelte) — shown only when Global voice input is on — default Ctrl/Cmd+Shift+S (hotkeyPreferences.svelte.ts:46-51). Capture rules: Esc cancels, needs a modifier unless F1-F12/Insert/Delete/Home/End/PageUp/PageDown (HotkeyCapture.svelte:34-66). Idle shows `<kbd>` + "Click to change"; capturing shows "Press a key combination...".
- **Sound cues** — switch — default ON.

### 2.3 Voice tab — S/VoiceSection.svelte (+ S/VoiceModelOptions.svelte)
Backed by `GET/PUT /settings/voice` (`lib/api/voice.ts`; server `app/api/settings.py:605-780`).

#### 2.3.1 Voice transcription (VoiceSection.svelte:171-230)
- Intro (:183): "Dictate into your chat draft. Voice calls and the global shortcut use separate controls."
- **Transcription model** — select grouped by provider — helper (:186): "Used by the chat microphone and global voice input. Separate from Fal.ai Audio to Text." Default `fal-ai/whisper`. Options (`app/api/settings_audio.py:183-240`):
  - OpenAI: GPT Live Transcribe, GPT-4o Transcribe, GPT-4o mini Transcribe, OpenAI Whisper
  - Fal.ai: Whisper (Large v3), ElevenLabs Scribe v1, ElevenLabs Scribe v2, fal Speech-to-Text
- **Composer microphone mode** — select — Auto / Toggle — default Auto — helper (:208): "Auto finishes after silence. Toggle keeps recording until you press the microphone again."
- **Silence timeout (seconds)** — number, 1-60, commit on blur/Enter — default 5 — shown only in Auto mode — helper (:219): "Wait 1–60 seconds after speech before finishing."

#### 2.3.2 Voice calls (VoiceSection.svelte:232-308)
- **Enable voice mode** — switch — default OFF.
- Sub-header "GPT Live" + text (:255): "Uses your OpenAI API provider. Tasks use the model selected in your chat composer." (The model is fixed to `gpt-live-1` server-side; there is no model picker, `app/api/settings.py` get_voice_settings.)
- **Choose a voice** — radio card grid, each card has a Play/Stop preview button (:257-271). Default `marin`. GPT Live voices (`app/core/voice_models.py:6-7`): Marin, Cedar, Alloy, Ash, Ballad, Coral, Echo, Sage, Shimmer, Verse, Quartz, Ripple, Vesper, Willow, Stone, Gleam, Meridian, Bossa, Tempo, Beacon, Delta, Cinder. Preview status "Generating sample…" / "Playing voice preview"; previews bill the OpenAI API account (server `preview_note`, not rendered).
- **Max call minutes** — number ≥1, commit on blur/Enter — default 240 — no helper.
- Note (:296): "Changes apply to new calls."
- **Conversation prompts** — `<details>` disclosure → VoiceModelOptions form (VoiceModelOptions.svelte):
  - **Opening words (optional)** — text, max 2000 — placeholder "Wait for me to speak" — helper: "Leave blank to listen first. New calls use these prompts; the chat composer controls the model, effort and permissions for tasks."
  - **Voice conversation prompt** — textarea (7 rows, max 16000) + **Restore default** button.
  - **Chat agent prompt** — textarea + **Restore default**.
  - Server note under prompts (`app/core/voice_models.py:44`): "Task requests run in the existing chat session with its selected agent instructions, conversation, tools and approval rules. The voice prompt controls direct replies and when to ask the chat agent for help."
  - **Save prompts** / **Discard changes**.
- Not exposed although the API has them: voice provider pin (`voice_provider_id`), runtime settings list (`model_details.runtime_settings`), Realtime knobs (turn detection, eagerness, etc. are only for legacy models).

### 2.4 Providers tab — P/ProvidersSection.svelte (group order 127-204)
Group order on the page: Claude Code mode → Loop guard → Claude Code CLI → Providers (list + Import settings) → Generated text → Media & tools → Devices (admin, from SettingsCard).

#### 2.4.1 Claude Code mode — C/ClaudeAuthModePanel.svelte
- **Radio cards** (admin-only enabled):
  - "Claude subscription" — detail: "Claude Code CLI not installed" / "CLI not signed in" / "{email} ({plan})" (:38-43).
  - "Anthropic API key" — detail: "No key stored" / "Key ending in {last4}" / "Key stored" (:45-48).
- Helper (:113-120): "Active: **{mode_label}**." + subscription: "Agents run on the plan of the account the CLI is signed in to. A stored API key is not used in this mode." / API: "Agents run on your API key, billed per use by Anthropic. The CLI's own sign-in is not used in this mode." + "AI Shuffle never switches modes on its own."
- Conditional warnings: license gate reason (:122-124); not-ready: "The Claude Code CLI is not signed in, so Claude agents cannot start. Sign in below, or choose API key mode." / "No API key is stored, so Claude agents cannot start. Add a key under Providers, or choose subscription mode." (:125-133); non-admin: "Only the owner can change the mode." (:135).

#### 2.4.2 Loop guard — C/LoopGuardPanel.svelte (offsets corrected)
- Status line (:97-105): "The loop guard is off." / "Active: Claude Code runs on your subscription. Unattended turns this hour: {n} of {cap}." / "Not active in API key mode; your Anthropic Console spend limit is the control there." + "It never stops a turn you started yourself."
- **Loop guard** — checkbox — default ON — helper: "Hold unattended turns past the budget until you confirm them."
- **Per session, per hour** — number ≥1 (server range 1-1000) — default 12 — helper "Default 12".
- **Per worker, per hour** — number ≥1 (1-10000) — default 60 — helper "Default 60".
- **Background worker time limit (minutes)** — number ≥0 — default 60 — helper "Default 60; 0 turns it off".
- **Waiting for confirmation** list (only when held turns exist): row "{Automation | Scheduled wake-up | Background worker | Connector} · {time}" + truncated prompt; per-row **Confirm** / **Dismiss** (admin) (:145-162).
- Defaults/limits: `app/core/loop_guard.py:63-71`.

#### 2.4.3 Claude Code CLI — C/ClaudeCliPanel.svelte
- Facts list (:222-245): Status (CLI not installed / Signed in / Not signed in / Unknown), Account, Signed in with, Plan (Max/Pro/Team/Enterprise), Organization, CLI version, Binary path.
- Notices: CLI error, relogin notice, and when signed in (:254): "Agents in subscription mode run on this account's plan. To switch accounts, sign out (the CLI's `{logout_command}`), then sign in again."
- Buttons (:257-269): **Sign in** (admin, CLI installed, not signed in, terminal available, not mobile), **Sign out** (admin, signed in), **Check again** (always).
- Sign-in terminal (modal-less inline flow, :271-305): helper "Running `claude auth login` on this worker. Open the link, sign in to your Claude account on Anthropic's page, then paste the code it shows below."; live output `<pre>`; **Open Anthropic's sign-in page** link; code input "Paste the code here" + **Send** + **Stop**; after exit **Try again** / **Close**; "The CLI reports a successful sign-in."
- How-to when not signed in and no terminal (:309-327): Docker host command (`docker exec … claude auth login`) with container-name hint "`ai-shuffle` with Docker Compose"; or "Open a terminal on the computer running AI Shuffle and run `claude auth login`"; not-installed variants mention `/home/appuser/.local` mapping.
- Footer (:329-334): "Prefer to pay per use? An Anthropic API key works instead of a subscription and suits scheduled and unattended runs." (+ "Use an API key" link only in the add-provider modal variant).

#### 2.4.4 Providers list — P/ProvidersSection.svelte:151-163 → P/ProviderList.svelte
- Group header button: **Import settings** (opens SettingsImportDialog, see 2.4.8).
- Fixed catalog of slots, always rendered in this order (ProviderList.svelte:380-497). Each slot is a `ProviderRow` (P/ProviderRow.svelte): glyph tile with status dot (ok/warn/off), title, mono badge, one-line subtitle, chevron; optional enable switch on the right. Clicking the header expands a `ProviderPanel`; one panel open at a time (:90-96).

| Slot | Title | Badge | Toggle | Subtitle states |
|------|-------|-------|--------|-----------------|
| 1 | Claude | Subscription | none | "Not signed in — sign in to the Claude Code CLI above." / "Not in use: Claude Code mode is API key" / "Signed in as {email} · Claude {Plan} subscription" (:161-170) |
| 2 | Anthropic API key | API key | none | "Not connected — paste an Anthropic key to use it instead of the subscription." / "Key set · {masked} · not in use: Claude Code mode is subscription" / "Key set · {masked}" (:177-188) |
| 3 | ChatGPT | Subscription | yes (per row) | "Not connected — sign in with ChatGPT." / "Connected · N models" / "Reconnect needed" / prefixed "Disabled · …" (:191-207) |
| 4 | OpenAI API key | API key | yes | "Not connected — paste an OpenAI API key." / "Active · {base_url}" (+ " · no token") / "Disabled" (:209-213) |
| 5 | Grok | Subscription | yes | "Not connected — sign in with SuperGrok." / same sub states |
| 6 | Grok API key | API key | yes | "Not connected — paste a Grok API key." / gateway states |
| 7 | OpenRouter | Gateway | yes | "Not connected — paste an OpenRouter API key." / gateway states |
| 8+ | {row name} (custom gateways, only if rows exist) | Gateway | yes | gateway states |

Row classification (:110-118): `auth_mode='chatgpt'` → ChatGPT; `auth_mode='grok'` or type grok → Grok; type openrouter/openai/xai; everything else → custom.

Expanded panel — P/ProviderPanel.svelte (same structure on every row):
1. **Action row** (:384-401): primary **Sign in** (subscription rows) or **Add key** (key rows) + **Remove** (only if configured) with inline "Remove? [Remove] [Cancel]".
   - Claude row: Sign in scrolls up to the Claude Code CLI group (ProviderList.svelte:173-175); no Remove.
   - Anthropic key row: Add key opens the add modal on the `claude-key` step when no key; when a key exists it toggles an inline **Replace API key** form (P/ClaudeApiKeyRow.svelte:48-75: password input "sk-ant-…", **Save**/**Cancel**, helper "Setting a new key keeps Claude Code authenticating with the key, overriding the connected account. Remove it from the row to revert to the account."). Remove clears the key.
   - Empty slots: primary opens AddProviderModal pinned to that provider's step.
   - Filled ChatGPT/Grok: Sign in opens a "Reconnect ChatGPT"/"Reconnect Grok" modal running the device flow against the existing row (ProviderList.svelte:503-562).
   - Filled key rows: Add key just focuses the Auth token field (:213-219).
2. **Settings** block (key/gateway rows with a stored row only, :405-479): Name (text), Endpoint (text, mono), Auth token (password; placeholder "{masked} — leave blank to keep" or "Provider API key"), API dialect (select, custom rows only: "Anthropic Messages" / "OpenAI chat/completions"), Context window (number, "Optional"), Max output tokens (number, "Optional"), **Save** (enabled only when dirty). Error "Name and endpoint are both required."
3. **Models** block:
   - Stored rows (:481-543): header "Models · {checked} of {total}" + refresh icon; filter input "Filter models…"; checkbox list (model id + label), each toggle saves immediately. Empty "No models returned."; filter miss "No models match “{q}”."; hidden-media hint "{id} is a {voice|image|video|speech} model — pick it under {Settings › Voice | Settings › AI tools}." (:533-535).
   - Claude / Anthropic key rows (:544-558): read-only "Models · built in" list from the registry.
4. **OpenRouter credits** (OpenRouter rows on https://openrouter.ai, P/OpenRouterCredits.svelte): "Account credits"; "Usage reads your remaining account credits with the API key already saved for this provider. No separate key is needed."; "${balance} left · last reported by OpenRouter"; legacy-key note; "Refreshes on demand, at most once every five minutes. If this key cannot read account credits, Usage shows its remaining key limit or an unavailable balance—not spending as credit left."; link "View credits in OpenRouter".
5. **Native image generation** (enabled openai/chatgpt/xai/grok rows, ProviderPanel.svelte:564-570): two paragraphs: "GenerateNativeImage uses this session’s Responses provider and credential. Model and account support are checked when an image is requested. It is separate from fal.ai and never switches providers automatically." / "Generated images are saved with the session. The provider may charge for generation; session cost estimates do not include native image charges."
6. **OpenAI API credit balance** (OpenAI rows on api.openai.com, P/OpenAICredits.svelte): three paragraphs ("OpenAI does not expose a prepaid credit balance through the model API key. No additional key is needed or requested here." / "An Admin API key reports organization spending, not credit left. Any previously configured usage access is preserved. Usage keeps that spending separate from balances." / "For weekly subscription allowance and available extra credits, use the ChatGPT sign-in provider. ChatGPT credits are separate from OpenAI API billing.") + link "View balance in OpenAI Platform billing". No data shown, text only.
7. **Banked resets** (ChatGPT/Grok rows with a live token, P/BankedResetsPanel.svelte): header "Banked resets · N available" + refresh; list rows: title ("Codex rate-limit reset" / "Usage limit reset"), "Expires {date} · {Available|Being used|Used} · Granted {date}", description (Grok default: "A reset clears your weekly usage once. Resets don't stack and expire on their date."); per-row **Use** → "Use this reset?" + "Eligible Codex windows may reset. This action cannot be undone." → **Confirm** / **Cancel**; outcome messages (:145-161).

Row-level toggle: the enable switch on ChatGPT/OpenAI/Grok/Grok key/OpenRouter/custom rows (ProviderRow.svelte:99-117).

Not shown anywhere on provider rows: subscription usage/limits (ChatGPT weekly allowance, Grok usage), auth mode label, default model / small-fast model (they are passed through on save but not editable, ProviderPanel.svelte:186-188).

#### 2.4.5 Add-provider modal — P/AddProviderModal.svelte
Steps (lines 1-12 of module script): choose, openai-key, openai-sub, grok-key, grok-sub, claude-key, claude-sub, openrouter-key, fal-key. Titles and chooser cards (:82-103):
- OpenAI API key — "Paste a key and choose your models."
- OpenAI subscription — "Sign in with ChatGPT. No key needed."
- Grok API key — "Paste a key and choose your models."
- Grok subscription — "Sign in with SuperGrok. No key needed."
- Anthropic API key — "Paste an Anthropic key for Claude Code."
- Claude Code subscription — "Sign in to the Claude Code CLI with your own account."
- OpenRouter API key — "One key, hundreds of models to choose from."
- fal.ai API key — "Choose models for image, video, audio and 3D tools."

In Settings the modal is only ever opened pinned to a step from an empty slot (`initialStep`, ProviderList.svelte:499-501), so the chooser is never shown in Settings, and the fal-key and claude-sub steps are unreachable from Settings (fal has its own inline card; Claude sign-in is the CLI group).

Flow steps
- API key (OpenAI / Grok / OpenRouter) — F/ApiKeyProviderFlow.svelte: (1) "Paste your {OpenAI|Grok|OpenRouter} API key. Next, choose which models to offer." + password field (placeholders sk-…, xai-…, sk-or-…) + Back / **Continue**; creates the row with fixed name and base URL (`https://api.openai.com/v1`, `https://api.x.ai/v1`, `https://openrouter.ai/api`) (:15-19, 29-44). (2) Model picker (F/ProviderModelsFlow.svelte): "Preset catalog — check the models to offer." or "Fetched N models. Check the ones to offer.", Refresh, filter, checkboxes, **Choose later** / **Use selected (N)**.
- ChatGPT / Grok subscription — F/ChatGptDeviceFlow.svelte, F/GrokDeviceFlow.svelte: auto-creates a draft row on mount (ChatGPT base `https://chatgpt.com/backend-api/codex`; Grok `https://api.x.ai/v1`), shows "Sign in with your ChatGPT account to connect Codex. Enter the code below on the sign-in page." / "Sign in with SuperGrok to connect Grok. Enter the code below on the sign-in page.", big user code + **Copy**, **Open sign-in page**, "Waiting for authorization…", **Cancel** (deletes the draft). Error: **Remove draft** / **Try again**. On success continues to the same model picker. Reconnect mode reuses the row and never deletes it.
- Anthropic key — F/ClaudeApiKeyFlow.svelte: "Paste an Anthropic API key. Claude Code authenticates with it and it overrides the connected account until you remove it." + field + Back / **Use this key**. Not a model_providers row (POST /settings/integrations/anthropic).
- fal.ai key — F/FalAiKeyFlow.svelte: key → "API key saved and verified." → embeds the full AIToolsSettings media picker → Done.
- Claude Code subscription — embeds ClaudeCliPanel (setup variant) + Done.

#### 2.4.6 Generated text — P/ProvidersSection.svelte:165-197
- **Text generation model** — ModelPicker popover (allowCustom=false) + **effort** select ("Provider default" + the model's supported levels from None/Minimal/Low/Medium/High/XHigh/Max/Ultra, `lib/constants/model-effort.ts:7-16`). Default: registry default model, effort "". No helper on screen (code comment: used for session titles, commit messages, PR descriptions, branch names, agent generation, :58-62).

#### 2.4.7 Media & tools — P/MediaToolsSection.svelte
- **fal.ai API key** keycard (:139-175): badge "Set"/"Optional", masked key; when unset: password input "Enter your fal.ai API key" + **Save** ("Saved & verified" / "Saved (could not verify)"); when set: **Remove** (no confirm).
- **fal.ai billing access (optional)** (:177-189): helper "Uses the official billing API with a separate Admin API key, stored encrypted on this worker only. Your media key is never used to probe billing permissions. Alpha billing endpoints are no longer used."; when saved: "Admin billing key saved. Open Usage to request a reading; requests respect a five-minute cooldown."; input "fal.ai Admin API key"; **Save billing key** / **Replace billing key**; **Disconnect billing** → "Remove the billing key and saved balance? Your media key will not change." **Remove billing key** / Cancel.
- **GitHub** (`lib/components/GitHubConnection.svelte`, embedded): connected "@user" + **Disconnect**; otherwise segmented **Web sign-in** | **Personal access token**. Web: **Sign in with GitHub** → one-time code + **Copy code**, "Open GitHub to sign in", "Waiting for approval on GitHub…", **Cancel sign-in** (15 min expiry). Token: "GitHub personal access token" input (placeholder "ghp_… or github_pat_…") + **Connect token** + "Create a token" link.
- **AI media models** keycard → **Manage** opens a right-side drawer "AI media models — Image, video, audio and 3D generation models" (P/ManageMediaModelsDrawer.svelte) hosting AIToolsSettings (section 4).

#### 2.4.8 Import settings dialog — P/SettingsImportDialog.svelte
Four-step wizard: Source → Preview & select → Apply → Result (:327-334).
- Source: "Fluid stays authoritative." note; **Providers to scan** radio: "Claude + Codex" ("Scan both; exact duplicates collapse and differing instructions become review sidecars."), "Claude" ("Scan ~/.claude and the Claude project registry."), "Codex" ("Scan ~/.codex and Codex-configured projects."); **Settings home (optional)** text ("Use the detected server home"; helper about Docker read-only mount); "MCP details are redacted." note.
- Preview: per-category checkbox lists with select-all. Categories (`app/core/settings_import_service.py:181-191`): Global instructions, Project instructions, Skills, Agents, Configured projects, MCP servers, Settings and plugins.
- Confirm: required acknowledgement checkbox "I understand the provider sources stay untouched, existing Fluid settings win, and destination files will not be overwritten." → Apply.
- Result: counts + "Projects refreshed." → Done.
- Note: despite sitting on the Providers list header, this imports instructions/skills/agents/MCP/projects, not model providers or credentials.

#### 2.4.9 Devices (admin) — S/DevicesSection.svelte
Header (:308-319): "Devices", count ("N devices"), state pill (Ready / Syncing / Joining / Needs local setup / Offline, hints in `lib/api/network-status.ts:16-30`), **Add device** / **Close** toggle.

Sub-blocks, top to bottom:
1. **Shuffle account** — S/AccountPanel.svelte: status "Signed in as @user" / "Not signed in" / etc.; remote access line ("Remote access is on", "…reconnecting", "…off: connection limit reached", "…unavailable on this worker"); actions **Sign in** (opens browser tab), **Sign out**, refresh icon, edit-username pencil; browser flow card ("Sign in in browser", "Finish in the browser tab that opens, then return here. This worker joins your account as soon as you do.", **Use my phone instead** → QR + code + **Copy**, "The code expires after ten minutes and links exactly one worker.", **Cancel**); username form ("Choose your username" / "Username", **Save**); tunnel-limit refusal ("{used} of {limit} remote connections in use on this account. This worker keeps working here and on your local network. It asks again every hour, and right away when you add a license key." + **Open Settings > License**); **Sign this browser out** when the browser authenticated via the account.
2. **Account device access to this worker** — S/WorkerDeviceConfirmations.svelte: **Refresh requests**; two paragraphs ("Signing in to a Shuffle account does not grant this worker’s admin access. Approve here from the worker’s own browser session or an already-confirmed device. A LAN connection alone cannot approve." / warning "Only approve a request you initiated. Compare the device ID below with the ID shown on your device; account-supplied names are not proof of identity."); per request: name, status (Awaiting worker approval / Approved / Denied / Revoked), Device ID, Platform, Key expires; actions **Approve {name}**, **Deny request** / **Revoke worker access**, each with a confirm step (**Confirm approval** / **Confirm denial** / Cancel).
3. **Add device panel** (hidden until Add device) — S/AddDevicePanel.svelte: kind tabs **Worker** (enrolled only), **Phone or browser**, **Shared-database worker** (legacy only); per-kind helper (the Worker one is ~70 words, :157-158 offset-corrected); **Address the device will use** select (endpoint label · URL, "(this computer only)" for loopback); **Create worker invite / Create pairing code / Create enrollment code**. Result: code + copy, QR, "Expires in mm:ss · works once", **Copy invite link / Copy pairing link** or copy-command (`python run_local.py --join CODE`), **Cancel code**.
4. **Situation notices** (DevicesSection.svelte:326-378): join in progress (JoinNetworkPanel waiting state), "Joining your account's network", "Set up this worker" (JoinNetworkPanel + **Keep separate settings**), "Switching to your account's settings" (+ Retry), stale/offline warnings, "Sync directly between workers" explainer, key-rotation warning after a revoke (+ **Open providers** / Dismiss).
   - JoinNetworkPanel (S/JoinNetworkPanel.svelte:61-109): helper "Invite codes are for a worker on your home network or a VPN such as Tailscale; …"; **Invite link or code** input; or manual **Worker address** + **Code**; **Join** / **Join another network** → **Leave and join** / **Keep current network**; waiting card with **Cancel**.
5. **Device list** (:384-532), per row type:
   - This worker: name + "This worker" tag, platform, version, network name · N workers; **Rename** (pencil → inline input, ✓/✕).
   - Network member workers: online dot, name, platform, version, Online/Seen X ago; **Rename**, **Revoke** (confirm text mentions rotating provider keys).
   - Joining devices: spinner, "Joining through your account…" / "with an invite…"; no actions.
   - Legacy shared-database installs: "Shared database" tag; **Allow** (if not approved) or **Rename** + **Revoke**; **Remove** (trash) with confirm.
   - Paired clients (phones/browsers): kind, platform, Seen…; **Revoke**.
   - Account-only registrations: "On your account" tag, "Availability unknown"; **Revoke from account** (not for the current device).
   - Feedback flashes: "Device renamed", "{name} access revoked", "Worker approved", etc.

### 2.5 License tab — S/LicenseSection.svelte
- Beta mode: "Beta: no license needed." + "Every feature works while the beta runs. When paid licenses start, you'll add your key here." (:96-99).
- Licensed mode: optional banner; facts: Status (Beta/Active/Trial/Payment grace period/Renewal overdue/Ending/Expired/Not activated/Needs attention), Plan, Workers "{used} of {seats} in use", Lease valid until / Lease ended, Payment grace until, Key "…{hint}", Last checked; "Last check failed: …"; reason when not allowed.
- Admin: **License key / Use a different key** — text "ASH-XXXXX-XXXXX-XXXXX-XXXXX" + **Activate** (Enter submits). Seat-limit list "Free a seat on a worker you no longer use, then activate again." with per-worker **Deactivate**. With a lease: **Check now**, **Deactivate on this worker** (native confirm: "Deactivate the license on this worker? Its seat is freed for another worker. New agent runs pause here until you activate again; your sessions and files are not affected.").
- Footer: "One license covers up to 3 workers. Without an active license, new agent runs pause; your sessions, files and settings always stay available."

### 2.6 API tab (admin) — S/ApiTab.svelte → `lib/components/ApiUserManager.svelte`
- Group "API users"; inner header "API Users" + **Add API User** (≈:354-360). The heading appears twice (group head and inner h3).
- One-time key banner: "API Key Generated" / "This key will only be shown once. Copy it now and store it securely." + **Copy Key** / **Dismiss**.
- Create/Edit form ("New API User" / "Edit API User"):
  - Identity: **Name*** ("e.g., Discord Bot, Mobile App"), **Description** ("Optional description"), **Project** select (None + projects; "Restrict this user to a specific project workspace").
  - Run configuration: **Model** (ModelPicker, empty = "Agent default"; "Preconfigured for every connector session of this user"), **Effort** select ("Default — Use the agent default" + levels with descriptions); note "Permissions are always Ask — connector sessions can never inherit a bypass setting. Tool permission requests are sent to the connected platform (e.g. Discord) for the user to approve or deny."
  - Access: **Agents** checkbox list ("Which agents this user can access"); **Active** checkbox (edit only).
  - **Create/Update** / **Cancel**.
- List rows: avatar initials, active dot, name, (id), description, badges (model, effort, project, N agents, "ask", "disabled"), Created / Last used; actions **Edit**, **Rotate API key**, **Delete** (Confirm/Cancel); collapsible **Sessions (N)** with refresh and clickable session rows (status, agent, project).
- Empty state: "No API users yet. Create one to use with connectors or external integrations."

### 2.7 System tab (admin) — S/SystemTab.svelte
- **Code sandbox** (S/CodeSandboxRow.svelte, read-only): "Agents' Python code" → "Code sandbox: checking… / active / off / unavailable" + reason; three helper paragraphs ("The OS sandbox isolates agents' Python code from this worker's files and network. Tool calls still follow the session's permission mode." / "When the sandbox is unavailable or off, Ask mode prompts before each Python cell. Bypass skips ordinary tool approvals; private access and other explicit consent requests still require your approval." / "The current OS sandbox uses bubblewrap on Linux; it is not available on Windows or macOS.") + docs link "The code-mode sandbox in the install guide".
- **System prompt** → "Custom instructions" textarea (12 rows, placeholder "Enter system prompt instructions…"); badges "Customized"/"Using default", "Unsaved changes"; **Revert to default** (ConfirmDialog "Are you sure you want to revert to the default security instructions? This will discard any customizations.", SettingsCard.svelte:399-407) and **Save changes**. Backend endpoint is `/settings/security-instructions`; empty save blocked ("Instructions cannot be empty. Use Revert to restore defaults.").

### 2.8 Logs tab (admin) — S/LogsTab.svelte
- Toolbar: **Log level** select ("All Levels", DEBUG, INFO, WARNING, ERROR, CRITICAL + any server levels), **Search logs...** text (300ms debounce), **Refresh**, **Export** (downloads `ai-shuffle-logs.log`).
- Status "Showing {returned} of {total} logs"; viewer rows: HH:MM:SS.mmm, level chip, message; newest first; 500-line limit; auto-refresh every 10s while Settings is visible.

### 2.9 Not in the Settings card but imported by it / related
- `lib/components/assistant/AgentIdentitySettings.svelte` is NOT rendered in Settings. It lives in the agent/profile editor (General tab, admin only) at `lib/components/deck/cards/profiles/ProfileForm.svelte:205`. It offers: "Put a face to the name" avatar picker (robot, spark, orbit, sprout, pixel — `lib/api/assistant.ts:4`), helper "Shared everywhere this agent appears. Instructions and permissions stay unchanged.", protected-builtin hint, and a "Personal assistant" block with **Assistant settings** / **Set up as assistant** (opens the Assistant feature page).
- Setup wizard reuses AccountPanel, JoinNetworkPanel, ClaudeCliPanel (`routes/setup/+page.svelte`, `lib/components/setup/CredentialStep.svelte`).

---

## 3. Providers in detail

### 3.1 Provider × credential matrix

| Provider | Credential kinds | Where it is stored | How added from Settings | Row(s) in the list |
|----------|------------------|--------------------|-------------------------|--------------------|
| Claude (Anthropic) | (a) Subscription via the stock Claude Code CLI login (`claude auth login`, in-app terminal on Docker/local admin, or host command); (b) Anthropic API key | (a) CLI's own credentials; (b) `/settings/integrations/anthropic` (not a model_providers row) | (a) Claude Code CLI group → Sign in; (b) Anthropic API key slot → Add key → ClaudeApiKeyFlow, or inline Replace form | Two fixed rows: "Claude" (Subscription) and "Anthropic API key" (API key). Which one is used is chosen in a third place: "Claude Code mode" radios. |
| OpenAI | (a) ChatGPT/Codex subscription via OAuth device code; (b) OpenAI API key | model_providers rows: (a) `provider_type='chatgpt'` / `auth_mode='chatgpt'`, base `https://chatgpt.com/backend-api/codex`; (b) `provider_type='openai'`, base `https://api.openai.com/v1` | Empty "ChatGPT" slot → Sign in → device flow → model picker; empty "OpenAI API key" slot → Add key → key → model picker | Two separate fixed rows: "ChatGPT" and "OpenAI API key" |
| Grok (xAI) | (a) SuperGrok subscription via device code; (b) Grok API key | (a) `provider_type='grok'` / `auth_mode='grok'`; (b) `provider_type='xai'`, base `https://api.x.ai/v1` | Same pattern as OpenAI | Two separate rows: "Grok" and "Grok API key" |
| OpenRouter | API key only | `provider_type='openrouter'`, base `https://openrouter.ai/api` | Empty slot → Add key → model picker | One row; shows Account credits block |
| Custom gateway | API key + endpoint + dialect (Anthropic Messages or OpenAI chat/completions) | `provider_type='custom'` | No UI path creates one any more (only existing/legacy rows render) | One row per custom row |
| fal.ai | Media API key + optional separate Admin billing key | `/settings/integrations/fal-ai`, `/settings/integrations/fal-billing` | Media & tools group inline keycard | Not in the provider list; a keycard under Media & tools |
| GitHub | Web device sign-in or personal access token | `gh` auth on the worker | Media & tools group | Not in the provider list |

Status/usage per row today: status dot + subtitle only. Model count for subscriptions; endpoint URL for key rows. Credits: OpenRouter balance (live), OpenAI (static text, no number), fal (in Usage page, not here). Banked resets: ChatGPT and Grok only. Subscription usage windows/limits: not shown in Settings (Usage page). Default model per provider: not exposed.

### 3.2 How a second credential for the same provider is represented

- Each credential kind for a provider is a separate fixed slot, so a provider that supports both a subscription and an API key already appears as two sibling rows with their own glyph, badge, toggle, action row, settings form, models list, and (for subs) banked resets. Example: OpenAI = "ChatGPT" row + "OpenAI API key" row, each with its own model curation; Grok likewise; Claude = "Claude" + "Anthropic API key" + the separate "Claude Code mode" chooser + the separate "Claude Code CLI" group (four places for one provider).
- If more than one model_providers row lands in the same slot (e.g. two ChatGPT accounts or two OpenAI keys), the first row takes the slot title and every extra row renders directly beneath it as another full row titled with `p.name`, same glyph and badge, expanded independently (`{#each rows as p, i}`, ProviderList.svelte:278-305 and 332-358). Each extra row carries its own complete panel, so N credentials = N clumps.
- Settings has no way to add that second row: once a slot is filled, the subscription "Sign in" becomes Reconnect (re-auths the existing row, ProviderList.svelte:250-252) and the key row's "Add key" only focuses the existing token field (ProviderPanel.svelte:213-219). The generic chooser (AddProviderModal "choose") is never shown in Settings. Extra rows today come from older data or the API.
- Model curation, enable toggles, and removal are per row, not per provider, and there is no "which credential wins" control for OpenAI/Grok (unlike Claude's mode radios).

### 3.3 Media tools and models (fal.ai) — S/AIToolsSettings.svelte, `lib/types/ai-tool-registry.ts`
Two views via a toggle: **Tools** (category cards) and **Browse All (N)** (catalog browser).

Categories and tools (ai-tool-registry.ts:150-395), each tool = model picker + per-model settings, auto-saved:

| Category | Tool | Setting key | Default model | Per-model settings |
|----------|------|-------------|---------------|--------------------|
| Image ("Generate and edit images with AI") | Text to Image | image_model | fal-ai/flux-pro/v1.1-ultra | Aspect Ratio (pills), Resolution (pills) |
| | Image Editing | image_edit_model | fal-ai/flux-pro/kontext | — |
| Video ("Generate and animate videos with AI") | Text to Video | video_model | fal-ai/kling-video/o3/pro/text-to-video | Aspect Ratio, Duration (s), Resolution (pills) |
| | Image to Video | i2v_model | fal-ai/kling-video/o3/pro/image-to-video | — |
| 3D ("Generate 3D models and objects") | Text to 3D | model3d_model | fal-ai/meshy/text-to-3d | — |
| | Image to 3D | image_to_3d_model | fal-ai/hunyuan-3d/v2/turbo | — |
| Audio ("Text-to-speech and speech-to-text") | Text to Speech | tts_model | fal-ai/kokoro/american-english | Voice (select, default af_heart), Speed (slider 0.5-2.0 step 0.05, default 1.0), Output Format (select, default wav) |
| | Speech to Text | stt_model | fal-ai/whisper | — |
| Audio Generation ("Generate music & sound, and transform audio") | Text to Audio | audio_model | fal-ai/lyria2 | — |
| | Audio to Audio | audio_to_audio_model | (none) | — |

- Category card (S/ToolCategoryCard.svelte:259-409): header icon, label, description, "N models"; feedback "Updated"/error; each tool is an accordion row showing current model name + up to 2 capability badges; expanded: description, ModelPickerGrid, full capability badges, then either curated per-model settings or DynamicModelParams (for non-curated catalog models).
- Changing a model clears schema params and non-slider settings; TTS auto-picks the first voice (ToolCategoryCard.svelte:128-197).
- Capability badges (ai-tool-registry.ts:96-145): Editing, Ref Images (n), Text: x, Inpainting, Native Audio, PBR Materials, Formats: …, Streaming, Emotions, Multi-Speaker, Realtime, Diarization, Translation.
- TTS model list for reference (`app/api/settings_audio.py:42-180`): Kokoro (American English), MiniMax Speech 2.8 HD, MiniMax Speech 02 HD, Chatterbox, Dia TTS, ElevenLabs v3, ElevenLabs Multilingual v2, ElevenLabs Turbo v2.5, F5 TTS, Orpheus TTS, VibeVoice 1.5B, GPT-4o mini TTS, OpenAI TTS-1, OpenAI TTS-1 HD.
- Speech to Text here (fal STT tool, `stt_model`) is a different setting from Voice → Transcription model (`voice_input_model`); the Voice tab helper says so.

---

## 4. Models

### 4.1 ModelPicker / ModelPickerPanel (chat models) — C/ModelPicker.svelte, C/ModelPickerPanel.svelte
- Used in Settings by: Generated text → Text generation model (ProvidersSection.svelte:176-182) and API user Model. Also used by the profile editor and the chat composer dock.
- Trigger: provider glyph + current label + chevron; popover flips up or shrinks to fit (346px preferred, 180px min) (ModelPicker.svelte:85-103).
- Panel: left icon rail (Favorites ★, All providers, one glyph per provider group with "name · auth subtext" tooltip); right: "Search models..." box over two-line rows (model name / provider glyph + brand · auth), per-row favorite star; rail hidden while searching or with one group (ModelPickerPanel.svelte:1-24, 233-345).
- Optional leading row ("Unavailable as subagent" / "Agent default"), trailing "Custom…" row (off for Text generation), optional context-window toggle Standard vs 1M (only when the caller passes it; not used in Settings) (:364-400).
- Effort list (model-effort.ts:7-16): None, Minimal, Low, Medium, High, XHigh, Max, Ultra, filtered per model's supported levels.

### 4.2 Provider model curation (per provider row) — see 2.4.4 item 3 and ProviderModelsFlow (2.4.5).

### 4.3 ModelPickerGrid (media) — S/ModelPickerGrid.svelte
- Collapsed: selected model button (name, price like "$0.040/img", "+N" auto-imported count, chevron) + description.
- Expanded: search (only if >5 models) "Search models...", tier 1 "Recommended" (curated cards: name, check, description, price, capability dots), tier 2 "More from fal.ai · auto-imported" (catalog cards: name, description, endpoint id); "No models match …".

### 4.4 ModelBrowser (fal catalog) — S/ModelBrowser.svelte
- Search "Search {N} models...", refresh (re-pulls catalog from fal.ai), category chips "All (N)" + per category with counts, results count "(filtered)", grid of cards (thumbnail or output-type emoji, name, category, description, endpoint id), badges "✓ Default" or "No tool uses this model" (greyed, unpickable). Clicking a pickable card sets it as the default for its tool type.

### 4.5 DynamicModelParams — S/DynamicModelParams.svelte
- Shown inside a tool when the selected model is a non-curated catalog model (ToolCategoryCard.svelte:339-344).
- Fetches the model's normalized OpenAPI input schema; renders every field except the primary prompt (`is_primary_text`) (`lib/stores/modelSchema.ts:83-86`). Title "Model parameters".
- Control per field type: boolean → checkbox; enum → select (with "Default" when not required); number/integer with min+max → range slider + value (step 1 or 0.05); number without range → number input; file → text "https://… (file URL)"; anything else → text. Field description shown under each. Values persist under the tool's setting key via `PATCH /settings/integrations/model-params` with 350ms debounce; "Loading model options…"; remote-change note "Model options changed elsewhere. Your unsaved options are kept here."

### 4.6 Voice models
- Voice calls: fixed GPT Live (`gpt-live-1`); 22 voices (see 2.3.2). Legacy labels kept for pinned calls: GPT Realtime 2.1 / 2.1 Mini / 2 / Realtime (legacy), PersonaPlex (`lib/api/voice.ts:34-41`). PersonaPlex voices NATF0-3, NATM0-3, VARF0-4, VARM0-4 (`app/core/voice_models.py:8`) are not selectable for new calls.
- Voice transcription models: see 2.3.1.
- Media TTS/STT: see 3.3.

---

## 5. Pain points observed

1. **One provider, many places.** Claude is configured in four separate groups on one tab: "Claude Code mode" radios, "Loop guard", "Claude Code CLI" sign-in, and two rows ("Claude", "Anthropic API key") in the list. The Claude row's "Sign in" just scrolls back up to the CLI group. OpenAI and Grok each get two unrelated rows (sub + key) with duplicated panels.
2. **Every extra credential is a full clump.** Additional rows for a slot render as full sibling rows with their own toggle, Remove, settings form, model curation, credits text, and banked resets (ProviderList.svelte:278-305, 332-358). There is no provider-level grouping or "active credential" choice outside Claude.
3. **No way to add a second credential or a custom gateway from Settings.** The chooser step is unreachable; filled slots turn "Sign in" into Reconnect and "Add key" into "focus the token field". Custom gateways render but cannot be created.
4. **Wordy static text blocks**, mostly on provider panels: OpenAICredits (3 paragraphs, no data), Native image generation (2 paragraphs, repeated on every OpenAI/ChatGPT/Grok/xAI row), OpenRouterCredits (3 paragraphs around one number), fal billing access paragraph, CodeSandboxRow (3 paragraphs), WorkerDeviceConfirmations (2 paragraphs + warning), AddDevicePanel "Worker" helper (~70 words), JoinNetworkPanel intro, Devices "Sync directly between workers", Application updates footer, ClaudeCliPanel Docker how-to.
5. **Providers tab is overloaded.** It holds Claude mode, loop guard, CLI, providers, generated-text model, fal + billing, GitHub, media model drawer, settings import, and the entire Devices/account/network/pairing area (admin). The Devices section is styled differently (its own bordered card) from the other groups.
6. **Misplaced items.**
   - Devices, Shuffle account, remote access, pairing: under Providers, not Account.
   - GitHub sign-in: under "Media & tools".
   - "Import settings" (imports instructions/skills/agents/MCP/projects) sits on the Providers list header though it imports no providers.
   - Timezone sits under "Workspace".
   - Text generation model sits between Providers and Media.
   - Loop guard is a Claude-subscription billing control but has its own top-level group.
   - Computer use (a host capability with security weight) sits under Chat & Input.
7. **Voice is split.** Global voice input + hotkey + sound cues are in Chat & Input ("Voice & hotkeys"), while transcription model, mic mode and voice calls are in the Voice tab. There are also three different STT settings: Voice → Transcription model, Media → Speech to Text, and the dictation hotkey.
8. **Stale/inconsistent references.** Hidden-media hint says "pick it under Settings › AI tools" (ProviderPanel.svelte:261), but no such tab exists; the drawer is "Providers › Media & tools › AI media models". SettingsCard and SystemTab header comments mention removed Connections/AI tabs. The "System prompt" UI is backed by "security instructions" and its revert dialog says "security instructions".
9. **Admin gating is inconsistent.** Voice tab, provider add/edit/remove/toggle/curation, and Text generation model are enabled for non-admins but the server rejects the writes (`require_admin`). Claude mode, loop guard and license controls are disabled correctly. The workspace path input renders for non-admins though its data loads only for admins.
10. **Inconsistent confirmation patterns.** Native `window.confirm` (Trusted LAN, License deactivate), ConfirmDialog (system prompt revert), inline arm/confirm (providers, devices, banked resets, API users), and none at all (fal.ai key Remove, GitHub Disconnect, CLI Sign out).
11. **Inconsistent controls for the same thing.** Switches are a mix of `.switch` buttons, native checkboxes styled as switches (Application updates), and plain checkboxes (Loop guard, Trusted LAN). Save models vary: auto-save (chat prefs, media), save-on-blur (voice numbers), dirty-form Save (provider settings, prompts), explicit Validate+Save (workspace).
12. **Deep nesting for media models.** Providers tab → Media & tools → AI media models → Manage → drawer → Tools/Browse toggle → category card → tool accordion → model grid expand → per-model settings / Model parameters. That is 5-6 levels to change a TTS voice.
13. **Heading/icon duplication.** "API users" group head plus inner "API Users" heading; "Reply suggestions" and "Messages" both use the MessageSquare icon; the add-provider modal reuses the Sparkles icon for three choices.
14. **Missing affordances worth noting for the redesign.** No subscription usage/limits on provider rows (only in Usage), no default/small-fast model per provider, no visible provider for voice calls (`voice_provider_id` is API-only), no "Check for updates" in Settings, no explicit Add-provider entry point.
