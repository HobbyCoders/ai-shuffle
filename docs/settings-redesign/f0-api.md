# Settings foundation API (slice F0)

Reference for slices A–D. Read the source when in doubt; this sheet lists what exists.

## `$lib/settings/`

- `types.ts`: `CategoryId`, `CATEGORY_IDS`, `PAGE_TYPES`, `isCategoryId`, `SettingsTarget {category, item?, tab?, anchor?, open?}`, `SettingEntry`, `SearchItem`, `CategoryModule {entries, searchSource?, attention?}`, `Attention {count, tone:'warn'|'err'}`, `SettingsView`, `StatusTone = 'ok'|'warn'|'err'|'off'|'standby'|'busy'`.
- `categories.ts`: `CATEGORIES` (id, label, lucide icon, group, type), `CATEGORY_GROUPS`, `categoryById`.
- `registry.ts`: `MODULES`, `ENTRIES`, `getEntry(id)`, `entriesFor(category, {item?, tab?})`, `validateEntries`, `wordCount`, `HINT_MAX_WORDS = 5`.
- `entries/<cat>.ts`: exports `entries` and optionally `searchSource()` / `attention()`. `general.ts` has real entries; the rest are stubs for the slices to fill.
- Categories (index order): general, voice, providers, models, integrations, plugins, devices, security, system, logs. Plugins items: `plugin:<id>`, `connection:<type>`, `explore`, `sources` (see `handoff-plugins.md`).
- `search.ts`: `buildIndex({desktopApp?})`, `query(q, index?)`, `MAX_RESULTS = 12`.
- `navigation.svelte.ts`: singleton `settingsNav` with `go(target)`, `selectCategory(id)`, `selectItem(item, category?)`, `clearSelection(category?)`, `selectedItem(category?)`, `tabOf(category, item, tabs)`, `setTab(category, item, tab)`, `back()`, `consumeOpen(anchor)`, `requestSearchFocus()`, `reset()` (tests), fields `category`, `view`, `stacked`, `pendingAnchor`, `pendingOpen`. Also `openSettings(target)`, `encodeTarget`, `decodeTarget`, `LEGACY_TAB_MAP`, `openSettingsForLegacyTab`.
- `access.svelte.ts`: `getSettingsAccess().readOnly`, `loadState(fn)` → `{status:'ok', data} | {status:'forbidden'} | {status:'error', message}`, `FORBIDDEN_TEXT = 'Available after approval'`, `isForbidden`, `SETTINGS_ACCESS_CONTEXT`.
- `save-status.svelte.ts`: `flashSaved()`.
- `list-width.ts`: `clampListWidth`, `readListWidth`, `writeListWidth` and constants.
- `testing/coverage.ts`: `assertCategoryCoverage(category, renderFn, {skip?, settle?, cleanup?})`, `coverageLocations(category)`.

## `components/settings/rows/` (re-exported from `rows/index.ts`)

- `SettingRow {id?, label?, hint?, help?, hintMono?, value?, defaultValue?, onReset?, dim?, children, expanded?}`. Copy and default come from the registry by `id`; props override. Without `id` it is an unregistered per-item row (pass `label`). Controls inside a row take their accessible label from it; don't pass `label` to them.
- `RowGroup {title, count?, actions?, tone?:'default'|'danger', bare?, id?, children}`: h2 on a page, h3 inside a DetailPane.
- `Switch {checked (bindable), onchange?, size?:'md'|'sm', disabled?, label?}`
- `Segmented<T> {options, value, onchange?, size?, disabled?, label?}`
- `SelectField {value, options? | groups?, onchange?, disabled?, label?, mono?, width?}`
- `NumberField {value, onchange?, min?, max?, step?, unit?, disabled?, label?}` (commits on blur/Enter, clamped)
- `TextField {value, onchange?, oninput?, placeholder?, type?, mono?, width?, disabled?, label?, autocomplete?, maxlength?}`
- `PathField {value, onCheck(path): Promise<{ok, message}>, oninput?, placeholder?, checkLabel?, width?, disabled?, label?}`
- `ValueText {value?, mono?, tone?:'muted'|'fg', status?, children?}`
- `LinkValue {label, target?: SettingsTarget, onclick?, mono?, ariaLabel?}`
- `ExpandToggle {value, open (bindable), onToggle?, mono?, label?}` (opens itself when a target's `open` equals its row id)
- `InlineConfirm {label?, question, confirmLabel?, tone?, variant?, size?, onConfirm, onCancel?, disabled?, armed (bindable), trigger?}`
- `HelpTip {text, about?}`, `StatusDot {tone, label?, ring?, size?}`, `Meter {used, cap, unit?, warnAt?, label?}`
- `Tile {icon?, text?, status?, size?:'sm'|'lg', ring?, dashed?, children?}`
- `ListItem {id, name, summary?, meta?, icon?, glyph?, status?, category?, selected?, onselect?, mono?, variant?:'default'|'log', lead?}` and `moveListFocus`. Custom list rows need `data-settings-nav-item` to join arrow-key movement.
- `ListGroupLabel {label, children?}`, `InlineBanner {tone?:'warn'|'key'|'info', icon?, children, actions?}`, `CodeBlock {code, prompt?, copy?, label?}`, `HotkeyField {value, onchange, disabled?}`

## `components/settings/shell/` (re-exported from `shell/index.ts`)

- `SettingsShell {initialItemId?}`
- `PreferencePage {category, title?, children}`
- `CollectionPage {category, title?, count?, addLabel?, onAdd?, addDisabled?, toolbar?, filterPlaceholder? (null hides), filterIcon?, filter (bindable), list, detail}`
- `DetailPane {name, icon?, glyph?, tile?, status?, statusText?, badge?, subtitle?, primary?, facts?: {label, value, mono?, muted?}[], banner?, tabs?, category?, item?, danger?: {id?, label, hint?, button, question, onConfirm, disabled?}[], dangerRows?, children: Snippet<[tab]>}`
- `FactsStrip`, `DetailTabs`, `DangerZone`, `EmptyDetail {title?, message?, icon?, children?}`, `ListDivider`, `SavedTick`
- `pages/index.ts`: `PREFERENCE_PAGES`, `COLLECTION_LOADERS`, `loadCollection`, `isPreferenceCategory`

## Conventions

- Adding a setting: an entry in `entries/<cat>.ts` with id `<cat>.<key>`, then `<SettingRow id="<cat>.<key>">` with the control inside.
- Coverage: `assertCategoryCoverage('<cat>', renderFn)`. For collections `renderFn` gets `{item?, tab?}` per place that has entries plus once with `{}`; call `settingsNav.go({category, item, tab})` before rendering. Use `skip` with a reason for rows hidden on purpose.
- Auto-save then `flashSaved()`. Long text and secrets keep explicit Save.
- Read-only: primitives, DetailPane body and primary action, and Add buttons disable themselves. Wrap loads in `loadState`; show `FORBIDDEN_TEXT` on 403.
- Styling: only the `--st-*` aliases in `shell/settings.css` and the shared `.st-btn` classes (primary, danger, solid, ghost, warn, sm, icon). No `data-platform='mobile'` selectors under `frontend/src`; phone tweaks go in the Settings section of `mobile/src/mobile.css`.
- Ctrl+K is registered only while Settings is the visible panel.
- Test stubs: `$app/state` stub (aliased in `vitest.config.ts`; set `page.state` to simulate Back), `test-stubs/SettingRowHarness.svelte`.
- `system.license` must exist (slice A): the legacy `license` tab and AccountPanel land on it.
- Do not run `npm run build:mobile` in a slice worktree: it creates a `mobile/node_modules` junction. The integration step runs it.
