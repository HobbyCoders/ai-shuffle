<script lang="ts">
	/**
	 * ExploreDetail: every app and agent plugin this worker can add, from the
	 * official connector runtimes and the plugin sources. Search, filter by
	 * Apps or Agent plugins, install a package in place, or open an item.
	 */
	import { Download, RefreshCw } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import Tile from '../rows/Tile.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { moveListFocus } from '../rows/ListItem.svelte';
	import SearchBox from '../models/SearchBox.svelte';
	import { getPluginsModel, plural, type PluginEntry } from './plugins-model.svelte';
	import { EXPLORE_ICON, PACKAGE_ICON, REVIEW_ICON, connectionIcon } from './icons';
	import type { StatusTone } from '$lib/settings/types';

	type Kind = 'all' | 'connections' | 'skills';
	const KINDS = [
		{ value: 'all', label: 'All' },
		{ value: 'connections', label: 'Apps' },
		{ value: 'skills', label: 'Agent plugins' }
	] as const;

	const model = getPluginsModel();
	const access = getSettingsAccess();
	let query = $state('');
	let kind = $state<Kind>('all');

	const q = $derived(query.trim().toLowerCase());
	const shown = $derived(
		model.entries.filter(
			(e) =>
				(kind === 'all' || (kind === 'connections' ? e.kind === 'connection' : e.kind === 'package')) &&
				`${e.name} ${e.description ?? ''}`.toLowerCase().includes(q)
		)
	);

	const facts = $derived<Fact[]>([
		{ label: 'Apps', value: model.loading ? null : model.connectionEntries.length },
		{ label: 'Agent plugins', value: model.loading ? null : model.packageEntries.length },
		{ label: 'Sources', value: model.marketplaces.length || null }
	]);

	function rowState(e: PluginEntry): { text: string; tone?: StatusTone } {
		if (e.kind === 'connection') {
			if (e.count) return { text: plural(e.count, 'account'), tone: 'ok' };
			if (e.installed) return { text: e.enabled ? 'Installed' : 'Disabled', tone: e.enabled ? 'ok' : 'off' };
			return { text: 'Official' };
		}
		if (e.review) return { text: 'Needs review', tone: 'warn' };
		if (e.installed) return { text: e.enabled ? 'Installed' : 'Disabled', tone: e.enabled ? 'ok' : 'off' };
		return { text: e.marketplace ?? 'Marketplace' };
	}

	function open(e: PluginEntry) {
		settingsNav.selectItem(e.item, 'plugins');
	}
</script>

{#snippet primary()}
	<button type="button" class="st-btn" disabled={model.loading} onclick={() => model.load()}>
		<RefreshCw />Refresh
	</button>
{/snippet}

{#snippet banner()}
	{#if model.actionError}
		<InlineBanner tone="warn"><span role="alert">{model.actionError}</span></InlineBanner>
	{/if}
{/snippet}

<DetailPane
	name="Explore plugins"
	icon={EXPLORE_ICON}
	badge="Catalog"
	subtitle="Apps and agent plugins"
	{primary}
	{facts}
	{banner}
	category="plugins"
	item="explore"
>
	<RowGroup title={q ? 'Search results' : 'Browse'} count={shown.length} bare>
		<SearchBox bind:value={query} placeholder="Search plugins" />
		<div class="filters">
			<Segmented options={KINDS} bind:value={kind} size="sm" label="Plugin kind" />
		</div>
		{#if model.loading}
			<p class="st-empty" role="status">Loading plugins…</p>
		{:else if shown.length}
			<div class="st-card rows" data-settings-list>
				{#each shown as entry (entry.item)}
					{@const s = rowState(entry)}
					<div class="row">
						<button
							type="button"
							class="main"
							data-settings-nav-item
							onclick={() => open(entry)}
							onkeydown={moveListFocus}
						>
							<Tile
								icon={entry.kind === 'connection'
									? connectionIcon(entry.id)
									: entry.review
										? REVIEW_ICON
										: PACKAGE_ICON}
								status={s.tone}
							/>
							<span class="tx">
								<span class="nm">{entry.name}</span>
								<span class="ds">{entry.description || 'No description'}</span>
							</span>
							<span class="meta">{s.text}</span>
						</button>
						{#if entry.kind === 'package' && !entry.installed && !access.readOnly}
							<button
								type="button"
								class="st-btn sm"
								aria-label="Install {entry.name}"
								disabled={!!model.busy}
								onclick={() => model.install(entry.id)}
							>
								<Download />{model.busy === entry.id ? 'Installing…' : 'Install'}
							</button>
						{:else if entry.kind === 'connection' && !entry.installed && !entry.count && !access.readOnly}
							<button type="button" class="st-btn sm" aria-label="Set up {entry.name}" onclick={() => open(entry)}>
								Set up
							</button>
						{/if}
					</div>
				{/each}
			</div>
		{:else if q}
			<p class="st-empty">No plugins match your search.</p>
		{:else}
			<p class="st-empty">Nothing here yet. Add a source.</p>
		{/if}
	</RowGroup>
</DetailPane>

<style>
	.filters {
		margin: 10px 0 12px;
	}
	.rows {
		display: grid;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 8px;
		padding-right: 12px;
		min-width: 0;
	}
	.row + .row {
		border-top: 1px solid var(--st-border);
	}
	.main {
		flex: 1;
		min-width: 0;
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 10px;
		padding: 9px 4px 9px 14px;
		border: 0;
		background: none;
		text-align: left;
		color: inherit;
	}
	.main:hover .nm {
		text-decoration: underline;
		text-underline-offset: 3px;
	}
	.tx {
		display: grid;
		min-width: 0;
	}
	.nm {
		font-size: 13.5px;
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.ds {
		font-size: 12px;
		color: var(--st-mfg);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.meta {
		font-size: 12px;
		color: var(--st-subtle);
		white-space: nowrap;
	}
	.st-empty {
		padding: 16px 2px;
	}
</style>
