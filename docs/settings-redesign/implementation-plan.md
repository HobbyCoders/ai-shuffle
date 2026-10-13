# Settings rebuild: implementation plan

Spec: the Revision 4 mockup in the private archive and `docs/settings-redesign.md`.
Every current setting is listed in `docs/settings-redesign/inventory.md`.
Integration branch: `claude/settings-ui-redesign-2106ec`. Slices branch from it and merge back; one PR
to main at the end.

## 0. Facts that shape the plan (verified in code)

| Fact | Evidence | Consequence |
|---|---|---|
| Android renders the same Settings component | `docs/mobile-desktop-ui-split.md` (mobile/ uses `kit.files.lib = '../frontend/src/lib'`); `MainPanel.svelte:972-973`; `mobile/src/shell/AppTopBar.svelte:66` | The phone stack (index, page, detail) lives in the shared components. `platform.isMobile` switches are fine. Mobile-only CSS goes in `mobile/src/mobile.css` (`html:root[data-platform='mobile']`); `frontend/scripts/check-no-mobile-ui.mjs` fails on `data-platform='mobile'` under `frontend/src`. |
| Android Back is WebView history | `android/.../ui/ChatActivity.kt:225-227` | Each phone-stack step pushes a history entry (SvelteKit `pushState` from `$app/navigation`; stub at `lib/test-stubs/app-navigation.ts`). |
| The web UI is desktop-only; narrow windows only reflow | split doc | Stack by container width (ResizeObserver, ≤600px) on the web too, and always when `platform.isMobile`. |
| Feature-page deep links exist | `layout.openFeaturePage(type, itemId)`, `layout.clearFeatureItemId()`; Notes/Docs receive `initialItemId` (`MainPanel.svelte:977,989`) | `openSettings(target)` encodes the target into `itemId`. `openSettingsTab` (`api/account.ts:72-75`) becomes a shim. |
| Ctrl+K is the global Spotlight shortcut | `AppPage.svelte:271-279`; `keyboard.ts:56` (last registered wins) | Settings registers its own Ctrl+K while mounted and unregisters on unmount. `/` is a keydown handler on the shell root that ignores inputs. |
| Custom gateway creation exists in the backend | `app/api/model_providers.py:54`, `create_provider` 744-785, `api_dialect` | Frontend only: create (`provider_type:'custom'`, name, base_url, api_dialect, auth_token), fetch models, curate. |
| Idempotent enable exists | `update_provider` handles `enabled` and closes billing (PATCH); `/toggle` flips | Switches call `PATCH {enabled: bool}`, never `/toggle`. |
| Check for updates is on main | `app/api/settings_updates.py` (`POST /application-updates {action}`, `GET /application-updates/workers`) | Build it now. PR #1626 only edits `ApplicationUpdates.svelte` (deleted here). |
| Unapproved paired devices get `is_admin:false` | `app/api/auth.py:311-316`; many GETs are `require_admin` | Read-only mode turns 403s into a quiet "Available after approval" state. |
| No i18n system | `package.json`, `lib` | Copy lives in registry entries and component constants. |
| Backend messages point at old Settings paths | `credential_gate.py:36-57`, `auth.py:122`, `claude_relogin_notice.py:36`, `mobile.py:145,203`, `settings_audio.py:311,404,439,900,908,946,1050`, `stt_realtime.py:141,156`, `voice.py:419,608,622,782`, `license.py:288,297,312`, `cluster.py:427`, `desktop/src/main/source-updates.ts:41`; asserted in `tests/core/test_delegated_error_status.py:49`, `test_license.py:113`, `test_hub_tunnel_limit.py:172-176`, `tests/api/test_settings_audio_routes.py:317` | Slice E updates them. |

## 1. Architecture

### 1.1 Layout

```
frontend/src/lib/settings/                 data and state, no markup
  types.ts              CategoryId, SettingEntry, SettingsTarget, SearchItem, CategoryModule
  categories.ts         9 categories: id, label, lucide icon, group (personal|ai|worker), type (preference|collection)
  registry.ts           imports entries/*.ts, builds byId, validates in dev
  entries/{general,voice,providers,models,integrations,devices,security,system,logs}.ts
                        each exports { entries, searchSource?, attention? }
  search.ts             buildIndex(), query(q): all words must match title+where+keywords, prefix boost, dedupe, cap 12
  navigation.svelte.ts  category, selected item per category, tab per item, phone view (index|page|detail),
                        pendingAnchor, pendingOpen; openSettings(target), encodeTarget/decodeTarget,
                        LEGACY_TAB_MAP, sessionStorage persistence, pushState for the phone stack
  access.svelte.ts      readOnly = !$isAdmin; loadState() -> {status:'ok'|'forbidden'|'error'}
  save-status.svelte.ts flashSaved() for the Saved tick
  list-width.ts         default 384, min list 300, min detail 440, localStorage 'settings.listWidth'

frontend/src/lib/components/settings/
  shell/   SettingsShell SettingsIndex SettingsSearch ApprovalBanner PreferencePage CollectionPage ListDivider
           DetailPane FactsStrip DetailTabs DangerZone EmptyDetail settings.css
           (settings.css: scoped aliases built with color-mix from app.css tokens, no hardcoded oklch,
           so every theme preset keeps working)
  rows/    SettingRow RowGroup Switch Segmented SelectField NumberField TextField PathField ValueText LinkValue
           ExpandToggle InlineConfirm HelpTip StatusDot Meter Tile ListItem ListGroupLabel InlineBanner
           CodeBlock HotkeyField (wraps HotkeyCapture)
  pages/   GeneralPage VoicePage SecurityPage SystemPage, pages/index.ts (category -> component)
  providers/ models/ integrations/ devices/ logs/   one collection each
  (existing ModelPicker, ModelPickerPanel, ProviderGlyph, ClaudeCliPanel stay in components/settings/)
```

`deck/cards/SettingsCard.svelte` stays the lazy entry (so `deck/lazy/panels.ts` is unchanged) and becomes
about 20 lines: `<SettingsShell initialItemId={…} />`. Settings stops using `StudioEditorShell`; other editors
keep it.

### 1.2 Shell and navigation

- SettingsShell: 224px index plus content. Content is a PreferencePage or CollectionPage from `pages/index.ts`.
  Collections load lazily with dynamic `import()`.
- Stacked mode when `platform.isMobile` or the shell is ≤600px wide. The shell sets `data-view=index|page|detail`
  and shows one pane; back buttons only in stacked mode. On mobile the index hides its own "Settings" title
  (the app bar shows it), in `mobile/src/mobile.css`.
- Android Back: index→page and page→detail call `pushState('', { settingsView })`; the shell follows
  `page.state` (`$app/state`). The in-UI back button calls `history.back()`.
- Deep links: `SettingsTarget { category; item?; tab?; anchor?; open? }`. `openSettings(target)` calls
  `layout.openFeaturePage('settings', encodeTarget(target))`, or updates the store directly when Settings is
  mounted. The shell applies `initialItemId` then calls `layout.clearFeatureItemId()`, scrolls
  `[data-setting-anchor=…]` into view and flashes it for 2.2s (`behavior:'auto'` under reduced motion), and opens
  `open` if set. Last place persists in sessionStorage.
- Legacy map for `openSettingsTab`: account→general, chat→general, voice→voice, providers→providers,
  license→{system, anchor 'system.license'}, api→integrations, system→security, logs→logs.
- Search: the box replaces the nav with results (combobox/listbox, arrows, Enter, Esc clears). Ctrl+K and `/`
  focus it; in stacked mode they go to the index first.
- Read-only: `setContext('settings-access', {readOnly})`. Primitives disable themselves, Add buttons disable,
  DetailPane wraps its body in `<fieldset disabled>`, ApprovalBanner ("This device needs approval" / "Approve
  from an approved device" / Devices link) sits above the content. No per-row locks.
- Saved tick: `<span class="saved" aria-live="polite">` in each page or detail header, driven by `flashSaved()`.
- Remote changes: keep the `watchResources(['settings'], …)` pattern. Long-text editors show "Changed
  elsewhere, your edits kept" with "Load latest".

### 1.3 Row primitives (one of each)

- SettingRow `{ id, value?, defaultValue?, onReset?, dim?, children, expanded? }`: label, hint and help come
  from the registry; renders `data-setting-anchor={id}`; min height 52px; teal modified edge and hover/focus
  reset when `value !== defaultValue` (always visible on phone); disables its control when read-only.
- Switch: `role="switch"`, `aria-checked`, sizes md|sm. The only switch in Settings.
- InlineConfirm `{ label, question, confirmLabel, tone:'danger'|'primary', onConfirm }`: swaps the control for
  "question [Confirm] [Cancel]", Esc cancels, focus moves to Cancel. Replaces `window.confirm`, ConfirmDialog and
  ad-hoc arm buttons (Trusted LAN, license deactivate, security instructions revert, removes, revokes,
  banked-reset Use, sign-outs, GitHub disconnect).
- Save model: auto-save with the Saved tick, except long text and secrets (call prompts, security instructions,
  owner password, license key, API keys) which use explicit Save/Cancel. Number fields commit on blur or Enter.
- HelpTip: `?` button, tooltip on hover/focus/click (`role="tooltip"`, `aria-describedby`).
- ListItem / Tile / StatusDot: tile with status dot, name, one-line summary, optional meta, `aria-current` when
  selected, Up/Down keyboard movement.

### 1.4 Collection layout and divider

- `CollectionPage {category, title, count, addLabel?, onAdd?, toolbar?, filterPlaceholder, list, detail}`;
  selection lives in the navigation store per category.
- ListDivider: `role="separator"` with `aria-valuenow`; pointer drag with capture; Left/Right 16px (Shift 64),
  Home/End min/max, Enter or double-click resets to 384; clamp list ≥300 and detail ≥440, re-clamp on resize;
  `localStorage['settings.listWidth']` in try/catch; hidden below 900px; list fixed at 280px from 601 to 900px.

### 1.5 Detail anatomy (DetailPane)

1. Header: 42px tile with status dot, h2 (focused on select in stacked mode), kind badge, status pill,
   subtitle, one primary action.
2. Facts strip, at most 4 facts (2×2 on phone). A fact with no data is omitted.
3. Tabs only when the type has more than one (`role=tablist`).
4. Body as grouped rows.
5. DangerZone last, with InlineConfirm rows.

Tab and item state live in the navigation store so search can jump to a tab.

### 1.6 Reuse, rewrite, delete

| Existing | Fate | Slice |
|---|---|---|
| Stores: providers, chatPreferences, hotkeyPreferences, theme, timezonePreference, computerUse, owner-access, github, catalog, modelSchema, modelFavorites | Reuse. providers store gains `setProviderEnabled(id, bool)` (PATCH), `createGateway`, `falEnabled`/`setFalEnabled` | B |
| `lib/api/*` clients | Reuse. `claude-auth.ts` and `agent-credentials.ts` gain `'off'` (`isClaudeAuthMode` must accept it) | B |
| ModelPicker, ModelPickerPanel, ProviderGlyph | Reuse unchanged | — |
| ClaudeCliPanel | Extract the terminal state machine to `components/settings/providers/claude-cli-login.svelte.ts`; ClaudeCliPanel (used by setup CredentialStep) and the new subscription detail both consume it | B |
| ClaudeAuthModePanel, LoopGuardPanel | Delete (Claude header control and Policy tab replace them) | B |
| `deck/cards/settings/providers/*` (ProviderList, ProviderRow, ProviderPanel, AddProviderModal, ProvidersSection, ClaudeApiKeyRow, OpenAICredits, OpenRouterCredits, ManageMediaModelsDrawer, MediaToolsSection) | Delete; rewritten as `components/settings/providers/*` | B |
| ChatGptDeviceFlow, GrokDeviceFlow | Extract one generic `device-enrollment.svelte.ts` ({kind:'codex'\|'xai', existingProviderId}), same draft create/delete rules; new DeviceCodeSetup; delete old | B |
| ApiKeyProviderFlow, OpenAi/Grok/OpenRouterKeyFlow, ClaudeApiKeyFlow, FalAiKeyFlow | Delete; logic moves into KeySetup | B |
| ProviderModelsFlow + ProviderPanel models block | Merge into ModelCuration (setup and Models tab); keep `context_window` and `copyReasoningMetadata`/`mergeReasoningMetadata` | B |
| BankedResetsPanel | Extract `banked-resets.svelte.ts`; new Usage-tab rows | B |
| SettingsImportDialog | Rewrite as inline ImportWizard in System > Workspace using `api/settings-import.ts`; delete dialog | A |
| GeneralSection, ChatBehaviorSection, HotkeysSection, ApplicationUpdates, OwnerAccessSection, LicenseSection, SystemTab, CodeSandboxRow, VoiceSection, VoiceModelOptions | Delete; rebuilt as General, Voice, Security, System pages. HotkeyCapture is kept and wrapped | A |
| AIToolsSettings, ToolCategoryCard, ModelPickerGrid, ModelBrowser | Extract state and save semantics (model PATCH, clear model-params, reset non-slider settings, TTS first voice, STT capability invalidation) into `components/settings/models/media-tools.svelte.ts`; new ToolDetail, ToolModelList, CatalogDetail; delete old | C |
| DynamicModelParams | Reuse, restyled to the row grammar | C |
| ApiUserManager, ApiTab | Extract `integrations/api-users.svelte.ts`; new ApiUserDetail (Overview/Run/Access/Sessions); delete both | D |
| GitHubConnection | Keep for setup; GitHubDetail uses `stores/github.svelte` directly | D |
| DevicesSection, AddDevicePanel, WorkerDeviceConfirmations | Extract `devices/devices-model.svelte.ts` (reuse `account-devices.ts`); new per-type details; delete old | D |
| AccountPanel, JoinNetworkPanel | Keep (setup uses both); extract account sign-in and join flows into `.svelte.ts` modules shared with the new details | D |
| LogsTab | Rewrite as LogsCollection + LogDetail; 10s polling only while visible | D |
| settings-layout.css, settings-shared.css | Delete once nothing imports them | final step |

Never delete or move `GitHubConnection`, `AccountPanel`, `JoinNetworkPanel`, `ClaudeCliPanel`
(`routes/setup/+page.svelte:32-34`, `setup/CredentialStep.svelte:27`).

## 2. Settings registry

```ts
export type CategoryId = 'general'|'voice'|'providers'|'models'|'integrations'|'devices'|'security'|'system'|'logs';
export interface SettingsTarget { category: CategoryId; item?: string; tab?: string; anchor?: string; open?: string }
export interface SettingEntry {
  id: string;                  // 'voice.micMode': unique, also the DOM anchor
  category: CategoryId;
  section: string;             // group title on the page and in search
  label: string;
  hint?: string;               // ≤ 5 words, test-enforced
  help?: string;               // ? tooltip text
  keywords?: string[];         // search synonyms
  item?: string; tab?: string; // location inside a collection
  platform?: 'all'|'desktop-app';
  default?: unknown;
}
export interface SearchItem { title: string; where: string; keywords?: string; target: SettingsTarget }
export interface CategoryModule {
  entries: SettingEntry[];
  searchSource?: () => SearchItem[];
  attention?: () => { count: number; tone: 'warn'|'err' } | null;
}
```

- Dynamic items (credentials, providers, API users, devices, tools, log levels) come from each category's
  `searchSource()` reading live stores, memoized by store version.
- Index attention counts: providers (credentials needing reconnect), devices (pending requests), logs (errors).
- Adding a setting = one entry in `entries/<category>.ts` + one `<SettingRow id="<category>.<key>">`.
- Guard tests written once in F0: `registry.test.ts` (unique ids, hint ≤5 words, label, valid category),
  `coverage.test.ts` (exported `assertCategoryCoverage(category, renderFn)`: every entry has an anchor on its
  rendered page/tab, every anchor has an entry), `search.test.ts`.

## 3. Backend (slice E)

### Required

Claude `off` mode:

| File | Change |
|---|---|
| `app/core/claude_auth_mode.py` | `MODE_OFF="off"` in MODES, label "Off"; `set_mode` accepts it; never chosen automatically |
| `app/core/credential_gate.py` | Off → `(False, OFF_MESSAGE)`: "Claude is off on this worker. Turn it on in Settings > Providers > Claude, or pick a model from another provider." `record_startup` logs info, not a blocked warning |
| `app/core/environment.py` (`inject_claude_code_auth`) | Off strips ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN |
| `app/core/model_registry.py` (`_claude_payload`) | Off → `{"auth_kind": None, "plan_type": None, "enabled": False}` (pickers already hide Claude when auth_kind is null) |
| `app/core/account_failover.py:118` | `!= MODE_API_KEY` → `== MODE_SUBSCRIPTION` |
| `app/api/generation.py` / `generation_options` | Refuse via `credential_gate.turn_refusal(leg)` when text generation would run on the delegated Claude leg |
| `app/api/settings.py` | ClaudeAuthModeRequest accepts 'off' |

Tests: `test_credential_gate.py`, `test_claude_code_auth.py`, `test_model_registry.py`,
`test_account_failover.py`, `test_loop_guard.py` (off ⇒ inactive), `tests/api/test_claude_auth_mode_api.py`, and a
generation refusal test.

fal.ai enabled flag:

| File | Change |
|---|---|
| `app/core/fal_credential.py` (new) | `KEY="fal_enabled"` (unset = on), `is_enabled()`, `set_enabled(bool)`, `api_key(api_user_id=None)` returning None when off |
| Readers routed through it | `app/mcp/ai_tools_sdk.py:254-264` (off also blocks the FAL_KEY/FAL_API_KEY env fallback), `app/core/environment.py:779-781`, `app/api/settings_audio.py:329-330`, `app/api/voice.py:131,150,600`, `app/api/catalog.py:39,71,101` |
| `app/api/settings.py` | `GET /settings/integrations` adds `fal_enabled`; new `PUT /settings/integrations/fal-ai/enabled {enabled}` (require_admin) returns `{fal_enabled, fal_api_key_set}` and fires the settings resource-sync event |
| Error copy | "fal.ai is off. Turn it on in Settings > Providers > fal.ai." |

Tests: new `tests/core/test_fal_credential.py`, `tests/api/test_fal_enabled_api.py`; update
`test_tool_catalog.py` and `test_settings_audio_routes.py`.

Copy sweep: `grep -rnE "Settings ?(›|>|→)" app desktop`; point every hit at the new homes (Providers > Claude,
Providers > fal.ai, Default models > Dictation, Devices, System > License, Security > Computer use) and update
the asserting tests listed in section 0.

Contract for slice B: `ClaudeAuthMode = 'subscription'|'api_key'|'off'` (off ⇒ `gate.claude_credential_ready=false`,
`gate.reason=OFF_MESSAGE`); `IntegrationsResponse.fal_enabled: boolean`; `PUT /settings/integrations/fal-ai/enabled`;
registry `claude.enabled?: boolean`.

### Deferred (the UI omits the fact when data is missing)

ChatGPT/Grok account email and plan on ModelProviderResponse; per-credential last used; monthly spend per key
(needs PR #1635); on-demand fal balance refresh; a "same request" log filter (v1 uses "Same logger").

### Open PRs

- #1629 removes the fal billing key: if it merges first, rebase E's fal work on it; otherwise leave the
  `fal_billing` fields alone. Never build the billing key UI.
- #1626 edits ApplicationUpdates.svelte (deleted here); port its result labels into A's UpdateCheckResults.
- #1640, #1643: no overlap.

## 4. Slices

F0 lands first. A, B, C, D then run in parallel worktrees from the integration branch. E is backend-only and
can start immediately. Only F0 edits the shell, rows, registry core, SettingsCard.svelte, MainPanel.svelte,
api/account.ts, pages/index.ts and mobile/src/mobile.css; a slice needing a primitive change reports it rather
than editing those files.

### F0 Foundation
Owns: `lib/settings/{types,categories,registry,search,navigation.svelte,access.svelte,save-status.svelte,list-width}.ts`,
stub `entries/*.ts`, `components/settings/shell/**`, `components/settings/rows/**`, stub pages and stub
`<X>Collection.svelte` per collection, `pages/index.ts`, `deck/cards/SettingsCard.svelte`,
`layout/MainPanel.svelte` (pass initialItemId to Settings like Notes), `api/account.ts` (shim),
`mobile/src/mobile.css` (Settings section), `lib/api/search.ts:29` description.
Acceptance: 9 categories in 3 groups with icons, attention slots, keyboard nav; search via Ctrl+K (only while
mounted) and `/`, results replace nav, Enter jumps and flashes; both page types render; divider per 1.4; stacked
mode with back steps and pushState; read-only banner and disabled controls when `!isAdmin`; `openSettings` and
the legacy shim land correctly; Saved tick and InlineConfirm work; light, dark and a non-default preset look right.
Tests: registry, coverage harness, search, navigation (encode/decode, legacy map, initialItemId consumption,
pushState), SettingsShell (index, search, banner, stacked, Ctrl+K register/unregister), ListDivider, SettingRow,
InlineConfirm, Switch. Delete `deck/cards/SettingsCard.account.test.ts` (move its assertion to navigation.test.ts).

### A Preference pages
Owns: `pages/{General,Voice,Security,System}Page.svelte`, `pages/editors/{ThemeStyleGrid,CallPromptsEditor,
OwnerPasswordForm,SecurityInstructionsEditor,UpdateCheckResults,LicenseSeats,LicenseKeyForm,ImportWizard}.svelte`,
`pages/voice-state.svelte.ts`, `entries/{general,voice,security,system}.ts`; deletes the sections listed in 1.6;
copy edits in `profiles/toolSurface.ts:100`, `ProfileForm.svelte:288`, `LicenseBanner.svelte:34`,
`setup/LicenseStep.svelte`.
Acceptance: every mockup row on these pages, including time zone in General > Region, the Dictation link row in
Voice, voice as select + play, "Calls run on" (link or select by `voice_provider_id`), Trusted LAN via
InlineConfirm, computer use disabled with reason as hint, "Agents run as you" only with `desktop?.agentAccess`,
Check for updates polling `/workers` every 2s up to 90s with per-worker results (disabled when updates are
off), License status/seats/key/Check now/Deactivate, inline import wizard (Source, Preview, Confirm, Result).
Tests: page coverage via `assertCategoryCoverage`, voice-state sync, import wizard; port OwnerAccessSection,
ApplicationUpdates, LicenseSection, CodeSandboxRow, VoiceModelOptions, VoiceSection.sync,
SettingsImportDialog tests and delete originals.

### B Providers
Owns: `components/settings/providers/**` (ProvidersCollection, ProviderList, ProviderGroupHeader,
CredentialRow, NotConnectedRow, AddKindMenu, ProviderDetail, NotConnectedDetail, CredentialDetail, tabs,
setup/{KeySetup,DeviceCodeSetup,ClaudeCliSetup,GatewaySetup,FalKeySetup}, ModelCuration, providers-model.ts,
device-enrollment.svelte.ts, claude-cli-login.svelte.ts, banked-resets.svelte.ts), `entries/providers.ts`,
`settings/ClaudeCliPanel.svelte`, `stores/providers.svelte.ts`, `api/claude-auth.ts`, `api/agent-credentials.ts`;
deletes per 1.6; copy in `AutomationForm.svelte:148`, `setup/CredentialStep.svelte:189`.
Acceptance (Revision 4): only connected credentials listed; "+" menu of missing kinds (disabled when complete);
"Not connected" group with setup in the detail; "Add provider" = custom gateway (name, endpoint, dialect,
token, Connect, curate) ending as its own group. Switches only on credential rows, via PATCH {enabled} for
OpenAI/Grok/OpenRouter/gateways and the new fal endpoint; never on provider headers. Claude header: exclusive
"Subscription | API key | Off". Connector line `var(--primary)`, teal into credentials that are on (Claude: the
active one). Claude subscription tabs Overview (CLI facts; sign-in terminal on desktop, host command on phone),
Models, Usage (`/usage/limits` windows + unattended turns), Policy (loop guard, held turns Confirm/Dismiss).
ChatGPT/SuperGrok Usage: weekly allowance and banked resets with Use via InlineConfirm. API keys: Overview
connection form, Models via ModelCuration (metadata kept), Usage with OpenRouter credits or "Not available for
this key". Native image generation is one row with `?`. Danger zone: Remove; Sign out of the CLI.
Tests: providers-model (grouping, on-count, line state, add-kind menu, not-connected, Claude modes incl. off),
ProvidersCollection, GatewaySetup, ModelCuration, device-enrollment, claude-cli-login; port then delete the old
provider/flow/ClaudeAuthModePanel/LoopGuardPanel tests. Must stay green: ClaudeCliPanel.test.ts,
setup/CredentialStep.test.ts, routes/setup/setup.test.ts.

### C Default models
Owns: `components/settings/models/**` (ModelsCollection, TextTasksDetail, DictationDetail, VoiceCallsDetail,
ToolDetail, ToolModelList, CatalogDetail, media-tools.svelte.ts), `entries/models.ts`, DynamicModelParams;
deletes AIToolsSettings, ToolCategoryCard, ModelPickerGrid, ModelBrowser.
Acceptance: groups Text / Image / Video / 3D / Audio (Dictation, TTS, STT, Text to audio, Audio to audio, Voice
calls) / Catalog; hollow dot only for "No model chosen"; model change clears params, resets non-slider
settings, TTS takes the new model's first voice, STT invalidates the capability cache; catalog search, chips,
refresh, unusable greyed; tool subtitle "Runs on fal.ai media key" linking to Providers, warning row when fal is
off; Text tasks warns when its model is Claude and Claude is off.
Tests: media-tools, ModelsCollection, TextTasksDetail; port then delete AIToolsSettings, ModelBrowser,
setting-editor-ownership tests.

### D Integrations, Devices, Logs
Owns: `components/settings/integrations/**`, `components/settings/devices/**`, `components/settings/logs/**`,
`entries/{integrations,devices,logs}.ts`, edits to AccountPanel and JoinNetworkPanel (consume extracted flows),
copy in `ConnectorManager.svelte:1188`; deletes ApiTab, ApiUserManager, DevicesSection, AddDevicePanel,
WorkerDeviceConfirmations, LogsTab.
Acceptance: API users ("Add API user" creates one draft row, one-time key banner after Create, tabs Overview / Run
(ModelPicker with "Agent default") / Access / Sessions, danger Rotate key and Delete); "Add device" creates a
pairing-code item (kind, address, QR, countdown); requests Approve via InlineConfirm ("IDs match?") and Deny; this
worker rename and Network tab (join, manual address, keep separate settings, leave and join); worker revoke
shows a rotate-keys toast with Open providers; Logs level select, search, refresh, export, live count, detail
facts Time / Level / Logger, polling paused while hidden.
Tests: devices-model, api-users, LogsCollection; port then delete ApiUserManager (+a11y), DevicesSection,
AddDevicePanel, WorkerDeviceConfirmations, LogsTab.visibility tests. Must stay green: AccountPanel,
JoinNetworkPanel, GitHubConnection, routes/setup/setup tests.

### E Backend
Owns `app/**`, `tests/**`, `desktop/src/main/source-updates.ts`. Section 3, required items.
Run `PYTHONPATH=fluid pytest tests/core tests/api -q`.

### Final integration
Delete settings-layout.css and settings-shared.css if unused; no stale `Settings ›/>/→` paths in frontend/src;
full verification below. Deferred: `docs/content/**` path updates.

## 5. Risks

- Android: test stacked mode under `setPlatformForMobileApp()`; Back pops one level; CLI terminal stays hidden
  on mobile (host command instead); divider off in stacked mode; `npm run build:mobile` must pass.
- Deep links: the only external caller of the old event is `AccountPanel.svelte:290`
  (`openSettingsTab('license')`), covered by the shim. Turn actionable text references (license banner,
  automation notice, computer-use hint) into `openSettings(...)` buttons. Clear `initialItemId` after use.
- Unapproved device: 403s become "Available after approval" with no console errors.
- Claude off: agents defaulting to Claude get a clear refusal at turn start; `isClaudeAuthMode` must accept 'off'.
- fal off must also block the env-key fallback.
- Ctrl+K must unregister cleanly; a leaked registration breaks Spotlight. Test it.
- Theme presets: tokens only.
- Accessibility: `nav aria-label="Settings"` with `aria-current`, combobox search, `role=switch`,
  `role=separator`, tablists, focus-reachable tooltips, `aria-live` Saved tick, reduced motion, focus the page
  or detail heading on navigation.
- Bundle: collections lazy-loaded; watch the Settings chunk size.

## 6. Verification

1. Per slice: `cd frontend && npm test`, `npm run check`, `npm run build`; backend
   `PYTHONPATH=fluid pytest tests/core tests/api -q`.
2. Mobile: `npm run build:mobile` and the `mobile/` vitest suite.
3. Browser pass against `AI_SHUFFLE_DEV_API=http://10.0.0.20:8000 npm run dev` (port 5173). The user signs in.
   ServerWorker runs main's backend and real data, so the pass is read-only: every category at 1440x900 dark and
   light plus one preset; search "loop guard", "timezone", "rotate", "dictation"; divider drag, keys, reset,
   persistence; ≤600px stack; hint word-count script; clean console; keyboard-only pass. Claude Off and the fal
   switch are checked against a local backend or after deploy.
4. Android Back behaviour via the mobile build.
5. Coverage: every mockup coverage item has a registry entry or is explicitly out of scope (agent avatar,
   assistant setup).
