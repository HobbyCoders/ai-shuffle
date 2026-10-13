# Porting main into the settings rebuild

The integration branch was cut from main at `1ed21b192`. Main then gained 168 commits; these touch the
old Settings UI that the rebuild replaces, so their behaviour has to land in the new pages. Tick each
item when it is ported and covered by a test.

## Commits

- [x] `b7372fe`, `84e70ab`, `85a830a`, `be6196f` Plugins move into Settings, authored drafts with
      review approval. Rebuilt as the `plugins` collection (own category after Integrations).
      Main's "Manage plugins" links (the `plugins` view type, desktop and mobile shell) must open
      `openSettings({ category: 'plugins', item })`. Build page loses its Plugins tab.
- [x] `73f4343` Assistant: `openFeaturePage('settings', '<tab>')` opens a Settings tab
      (`initialTab`, cleared with `layout.clearFeatureItemId()`). Map old tab ids to new categories
      (account→general, chat→general, voice→voice, providers→providers, plugins→plugins,
      license→system item license, api→integrations, system→system, logs→logs). Assistant cards
      link to Plugins (Connect Gmail) and Settings > Providers (Sign in to GitHub; GitHub now lives
      in Integrations). Done: legacy tab names decode through `LEGACY_TAB_MAP` (plugins included);
      Connect Gmail opens Plugins > Gmail, Sign in to GitHub opens Integrations > GitHub.
- [x] `b671d04` Check for updates in Application updates. Slice A's System page has it; compare
      polling, "two quiet readings", 90 s cap, per-worker result text (`describe()`), the
      `application-update-policy` event before and after, and the scope hint.
- [x] `f692c99` then `9b8540b` Loop guard explainer added, then dropped: net no UI change. Make sure
      the rebuilt Claude Policy tab has no explainer paragraph.
- [x] `2a5e2c2` Held-turn "Confirm starts the turn now…" hint only for admins (viewers with the
      buttons). The Policy tab has no hint; its Confirm/Dismiss buttons are hidden read-only, as on main.
- [x] `8fc5f1b` Usage rows and fal credits:
  - no separate fal billing key anywhere in Settings (removed from MediaToolsSection);
  - [x] no fal billing key in the rebuilt fal.ai detail or Default models;
  - OpenAI "Credit balance not available for this key" and the dropped billing link are in the chat
    usage bar (`ContextUsageBar`), which the rebuild does not touch; main's Settings OpenAI panel
    keeps its billing link, as slice B's `tabs/KeyUsage.svelte` does;
  - Claude usage header "Last reported <age>", no duplicate "Updated";
  - ChatGPT extra credits only in the expanded rows;
  - [x] `frontend/tests/UsageRowsFixture.svelte` now opens Settings > Providers > fal.ai; the
    verifier passes (42 checks, desktop and phone).
  - [x] Claude header and ChatGPT credits are chat usage bar changes; they came in with the merge
    unchanged.

## After the merge

- Resolve every modify/delete conflict by porting the main side into the new component, then take
  the deletion.
- Grep for imports of deleted components (build and check catch most).
- Full verification from `handoff.md` step 5, plus `node tests/verify-usage-rows.mjs` if it runs here.

## Result (main merged at its head of 2026-10-02)

- Conflicts resolved: SettingsCard (ours: the shell), MainPanel (one encoded target; `plugins` views
  open Settings > Plugins), and the deleted ApplicationUpdates, MediaToolsSection and
  SettingsCard.account test, each ported first.
- Main's real-browser verifiers run against the new pages: settings-plugins (27 checks),
  usage-rows (42), assistant-setup (94).
