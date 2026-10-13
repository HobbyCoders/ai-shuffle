# Settings rebuild: status (2026-10-02)

Branch `claude/settings-ui-redesign-2106ec-1tb9id` (pushed) holds the whole rebuild, merged with main:

- Plan of record, inventory, implementation plan, F0 API sheet (this folder) and the mockups.
- F0 foundation (shell, rows, registry, search, navigation, phone stack).
- Slice E backend (Claude off mode, fal.ai switch, Settings path copy).
- Slice A General, Voice, Security, System. Slice B Providers. Slice C Default models.
  Slice D Integrations, Devices, Logs. Plugins (its own category after Integrations; see
  `handoff-plugins.md`). Each slice's `handoff-slice-<x>.md` records its details.
- `main` merged at its 2026-10-02 head. Every main change that touched the old Settings UI is ported;
  `main-port.md` lists them one by one.

## Done in the integration pass

- F0 primitive requests from the slices: Switch, Segmented and SelectField revert when an `onchange`
  promise resolves `false` or rejects (no more `{#key failures}`); RowGroup's card is the global
  `.st-card` and no longer clips popovers (Text tasks and the provider cards use it); RangeField is in
  `rows/`; TextField takes `inputmode`; InlineConfirm has `variant="primary"`; an empty danger zone
  stays hidden; HotkeyField draws key caps and replaces HotkeyCapture; LinkValue stays usable in
  read-only details.
- Cross-slice ids: Voice links to Default models `dict` and Providers `mp:<rowId>`.
- Claude off: `AgentGateStatus.mode` admits `'off'`; the chat banner already shows the gate's own
  "Claude is off" reason.
- Deep links: `layout.openFeaturePage('plugins', id)` opens Settings > Plugins; legacy tab names
  (including `plugins`) decode through `LEGACY_TAB_MAP`; the Assistant's cards open Plugins > Gmail and
  Integrations > GitHub.
- Copy: setup wizard, Assistant, AccountPanel, code comments, MCP notes and the docs (with their in-app
  and site copies) name the new homes. The changelog is left as shipped.
- Removed: old settings components and tests replaced by the slices, SettingsImportDialog,
  HotkeyCapture, PluginsHub, PluginDetails, BuildPluginList, settings-layout.css,
  settings-shared.css.

## Verification on the final head

- `frontend`: `npm test` (379 files, 4658 passed, 1 skipped: the providers coverage loop, covered with
  data in `ProvidersCollection.test.ts`), `npm run check` clean, `npm run build` and
  `npm run build:mobile` pass.
- Main's real-browser verifiers, run against the new pages with Chromium: `verify-settings-plugins.mjs`
  (27 checks), `verify-usage-rows.mjs` (42), `verify-assistant-setup.mjs` (94).
- Backend: `pytest tests/core tests/api -n 4 --timeout=120`: 16362 passed, 75 skipped, 13 failed. The
  same 13 fail on a clean checkout of main in the same container (container runtime, file logging,
  owner .env, chat access invalidation, global invalidation relay, rate-limit breaker hook, chat image
  upload), so they are environmental, not this branch. Without `--timeout` one of them hangs the run
  at about 87%.
- Docs: `diff -r docs/content frontend/src/lib/docs/content` is clean and
  the website `render-docs.mjs --check` passed at the review revision. The
  current check lives in [hobbycoders-cloud/site](https://github.com/HobbyCoders/hobbycoders-cloud/tree/main/site);
  use that repository's documented source/snapshot options.

## Known gaps

- Explore's search and filter, and Default models' catalog search, are disabled on read-only devices
  (DetailPane disables its whole body there).
- `ConnectorManager` keeps its own styling inside the Plugins app detail.
- Integrations' count leaves out an unsaved API user; the device address dropdown truncates long
  entries (slice D notes).
- GitHubConnection (setup wizard) still has its own copy of the gh sign-in logic; slice D's
  `github-auth.svelte.ts` could replace it.

## Next

Open a PR from this branch to main when the owner asks; the owner reviews and merges.
