# Handoff: Plugins collection

Plugins is its own Settings category (`plugins`, lucide `Puzzle`, group AI, right after Integrations).
Everyone sees it; a read-only device sees packages only (apps, sources and installs are owner-only,
as in main's plugin hub).

## Deep links

```ts
openSettings({ category: 'plugins' });                                  // the list
openSettings({ category: 'plugins', item: 'plugin:<plugin id>' });      // a package, e.g. plugin:toolkit@mp
openSettings({ category: 'plugins', item: 'plugin:<name>@authored' });  // a draft: opens on its Review tab
openSettings({ category: 'plugins', item: 'connection:<type>' });       // an app, e.g. connection:gmail
openSettings({ category: 'plugins', item: 'connection:gmail', tab: 'Runtime' }); // tabs: Accounts, Runtime
openSettings({ category: 'plugins', item: 'explore' });                 // catalog
openSettings({ category: 'plugins', item: 'sources' });                 // sources, updates, auto-update
```

Package tabs: `Review` (pending drafts only), `Overview`, `Contents` (when it has commands, agents or
skills). The collection also accepts main's hub ids (a bare plugin id, connector type or connector
account id) and rewrites them to the item above, so `SettingsCard`'s `requestedItemId` can be passed
straight through. `LEGACY_TAB_MAP.plugins` maps the old tab name to the category.

## Files

- `components/settings/plugins/`: `PluginsCollection`, `PluginDetail`, `ConnectionDetail` (reuses
  `ConnectorManager`, which carries the Google and Plaid flows), `ExploreDetail`, `SourcesDetail`,
  `plugins-model.svelte.ts` (load, install, enable, approve, runtime actions, references, updates,
  sources, auto-update), `readme.ts`, `icons.ts`.
- `settings/entries/plugins.ts`: entries (Sources item rows), `searchSource()` (every plugin and app
  from the last load, known connector types before a visit, Explore and Sources), `attention()`
  (drafts needing review; asks for the installed list once before the first visit).
  `plugins-live.svelte.ts` holds what the collection last loaded.

## Integration (done)

- Main merged into the integration branch: Build lost its Plugins sub-tab, and
  `layout.openFeaturePage('plugins', id)` now opens the Settings view at
  `{ category: 'plugins', item }` (`pluginsSettingsTarget` in `stores/layout.svelte.ts`), so the
  Assistant's "Connect Gmail" / "Manage connections" and any "Manage plugins" link land here, on
  desktop and in the mobile shell (which reuses MainPanel).
- `PluginsHub.svelte`, `PluginDetails.svelte` and `BuildPluginList.svelte` and their tests are
  deleted; their assertions live in `PluginsCollection.test.ts` and `plugins-model.test.ts`.
- Batch install is back: Explore rows for uninstalled packages have a select box, and the Browse
  group shows "Install (N)" (`PluginsModel.installMany`, one `/plugins/install/batch` request).
