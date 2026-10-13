# Slice A handoff (preference pages)

Branch `claude/settings-slice-a`, based on `032f177a8` (F0 head of `claude/settings-ui-redesign-2106ec`).

## Done

- Registry entries for General, Voice, Security and System (`frontend/src/lib/settings/entries/{general,voice,security,system}.ts`). `system.license` exists for the legacy license tab.
- General: Appearance, Conversation, Thinking and tools, Region (`pages/GeneralPage.svelte`).
- Voice: Dictation (global input, hotkey, mic mode, stop after silence, sound cues, Dictation model link to `{category:'models', item:'dictation'}`) and Voice calls (switch, voice select + play, Calls run on, max length, opening words, call prompts editor). State in `pages/voice-state.svelte.ts`; editor `pages/editors/CallPromptsEditor.svelte`.
- Security: owner password (inline form), Trusted LAN via InlineConfirm, Agents run as you (desktop only), computer use (reason as hint, detail in `?`), code sandbox (status, reason in `?`, guide button), security instructions editor with Save and Revert via InlineConfirm and a "Changed elsewhere" banner.
- System: workspace folder (PathField; Check path saves when valid; remote-change banner), inline import wizard (`editors/ImportWizard.svelte`), updates (policy switches, Check for updates polling `/settings/application-updates/workers` every 2 s up to 90 s, per-worker results; `pages/updates-state.svelte.ts`), version, license (status, seats, key form with Check now, full-seats holder list, Deactivate via InlineConfirm).
- Shared editor styles: `pages/editors/editors.css` (`--st-*` tokens only).
- Deleted: GeneralSection, ChatBehaviorSection, HotkeysSection, ApplicationUpdates, OwnerAccessSection, LicenseSection, SystemTab, CodeSandboxRow, VoiceSection, VoiceModelOptions (and their tests). Tests ported to `pages/*.test.ts` and `pages/editors/*.test.ts`.
- Copy edits: `profiles/toolSurface.ts`, `ProfileForm.svelte`, `LicenseBanner.svelte` ("Open license" button via `openSettings`), `setup/LicenseStep.svelte`.
- `tests/ComposerSuggestionFixture.svelte` now uses the new SettingRow + Switch (it imported ChatBehaviorSection).

## Status

- `npm test`: 371 files, 4366 tests passed (before the last small fix commit; page tests re-run green after it: 8 files, 85 tests).
- `npm run check`: 0 errors, 0 warnings.
- `npm run build`: not run yet.
- Visual check done at 1440x900, dark and light, with stubbed fetch on port 5174 (General, Voice, Security, System, editors open). Server stopped.

## Remains

1. Run `npm run build` in `frontend/`.
2. Delete `deck/cards/settings/providers/SettingsImportDialog.svelte` and its test once slice B removes `ProvidersSection.svelte` (the only importer). Left in place to avoid a modify/delete conflict with B.

## Gotchas

- I edited F0's `frontend/src/lib/settings/coverage.test.ts`: the harness tests now swap in a fixed General registry (the real one grew), and the every-page loop skips rows that only render in some states (`voice.hotkey`, `security.runsAs`, `system.seats`, `system.licenseKey`, `system.deactivate`). Other slices adding entries will touch the same file.
- Switch, Segmented and SelectField keep the value they were set to when the parent's value does not change, so a failed save left them showing the unsaved value. Workaround: the pages wrap server-backed controls in `{#key state.failures}`. F0 request: make these primitives controlled (render the prop, never assign it locally unless bound).
- F0 request: HotkeyField wraps the old HotkeyCapture, which renders a bordered box with "Click to change"; the mockup shows bare key caps.
- Cross-slice ids assumed: Dictation model links to `models` item `dictation`; "Calls run on" links to `providers` item = the model_providers row id. Slices B and C must use those ids or these links need updating.
- No i18n; hints are five words or fewer (registry test enforces registered hints; dynamic hints passed as props were kept short by hand).
