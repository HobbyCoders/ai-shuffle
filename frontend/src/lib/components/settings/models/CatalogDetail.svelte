<script lang="ts">
	/**
	 * CatalogDetail: every fal.ai model the catalog knows. Search across name,
	 * description, id and tags; category chips; Refresh re-pulls the catalog
	 * from fal.ai. Picking a model makes it the default of the tool fal.ai
	 * files it under; a model no tool uses is greyed out.
	 */
	import { RefreshCw } from 'lucide-svelte';
	import { catalogStore } from '$lib/stores/catalog';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { CATEGORY_LABELS, type CatalogModel } from '$lib/types/ai-models';
	import DetailPane from '../shell/DetailPane.svelte';
	import type { Fact } from '../shell/FactsStrip.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import ModelOption from './ModelOption.svelte';
	import SearchBox from './SearchBox.svelte';
	import { ALL_TOOLS, toolMeta } from './media-tools.svelte';
	import { getModelsState } from './tasks.svelte';
	import { CATALOG_ICON } from './icons';

	const NO_TOOL = 'No tool uses this model';
	const PAGE = 60;

	const { tools } = getModelsState();

	let query = $state('');
	let category = $state<string | null>(null);
	let shown = $state(PAGE);

	// catalogStore is a plain store; catalogVersion makes these reactive.
	const all = $derived.by(() => {
		void tools.catalogVersion;
		return catalogStore.getAll();
	});
	const categories = $derived.by(() => {
		void tools.catalogVersion;
		return catalogStore.categories;
	});
	const size = $derived.by(() => {
		void tools.catalogVersion;
		return catalogStore.catalogSize;
	});
	const loading = $derived.by(() => {
		void tools.catalogVersion;
		return catalogStore.loading;
	});
	const error = $derived.by(() => {
		void tools.catalogVersion;
		return catalogStore.error;
	});

	const q = $derived(query.trim().toLowerCase());
	const filtered = $derived.by(() => {
		let result = all;
		if (category) result = result.filter((m) => m.category === category);
		if (q) {
			result = result.filter(
				(m) =>
					m.display_name.toLowerCase().includes(q) ||
					m.description.toLowerCase().includes(q) ||
					m.endpoint_id.toLowerCase().includes(q) ||
					m.tags.some((t) => t.toLowerCase().includes(q))
			);
		}
		return result;
	});
	const page = $derived(filtered.slice(0, shown));
	const toolsWithModel = $derived(ALL_TOOLS.filter((t) => tools.modelId(t)).length);

	const facts = $derived<Fact[]>([
		{ label: 'Models', value: size ? size.toLocaleString() : null },
		{ label: 'Used by tools', value: toolsWithModel },
		{ label: 'Categories', value: categories.length || null }
	]);

	// A new filter starts from the first page again.
	$effect(() => {
		void q;
		void category;
		shown = PAGE;
	});

	function categoryLabel(value: string): string {
		return CATEGORY_LABELS[value] ?? value;
	}

	function rowMeta(model: CatalogModel): { text?: string; badge: boolean } {
		if (tools.isDefault(model.endpoint_id)) return { text: 'Default', badge: true };
		if (!tools.toolForCatalogModel(model)) return { text: 'No tool uses this', badge: false };
		return { badge: false };
	}

	function rowTitle(model: CatalogModel): string {
		const tool = tools.toolForCatalogModel(model);
		if (!tool) return `${model.endpoint_id}: ${NO_TOOL}`;
		return `${model.endpoint_id}: set as the ${toolMeta(tool).name.toLowerCase()} default`;
	}
</script>

{#snippet primary()}
	<button type="button" class="st-btn" disabled={loading} onclick={() => catalogStore.refresh()}>
		<RefreshCw class={loading ? 'spin' : ''} />Refresh
	</button>
{/snippet}

{#snippet banner()}
	{#if tools.falEnabled === false}
		<InlineBanner tone="warn">
			fal.ai is off, so these models cannot run.
			{#snippet actions()}
				<button type="button" class="st-btn sm" onclick={() => settingsNav.go({ category: 'providers' })}>
					Open providers
				</button>
			{/snippet}
		</InlineBanner>
	{/if}
{/snippet}

<DetailPane
	name="All fal.ai models"
	icon={CATALOG_ICON}
	status={loading ? 'busy' : error ? 'err' : 'ok'}
	statusText={loading ? 'Refreshing' : error ? 'Could not load' : 'Synced'}
	badge="Catalog"
	subtitle="Pick to set a default"
	{primary}
	{facts}
	{banner}
	category="models"
	item="catalog"
>
	{#if error}
		<InlineBanner tone="warn">
			{error}
			{#snippet actions()}
				<button type="button" class="st-btn sm" onclick={() => catalogStore.load(true)}>Retry</button>
			{/snippet}
		</InlineBanner>
	{/if}
	<RowGroup title="Browse" count={category || q ? `${filtered.length.toLocaleString()} shown` : undefined} bare>
		<SearchBox bind:value={query} placeholder="Search {size.toLocaleString()} models" label="Search catalog" />
		<div class="chips" role="group" aria-label="Category">
			<button type="button" class="chip" aria-pressed={category === null} onclick={() => (category = null)}>
				All {size.toLocaleString()}
			</button>
			{#each categories as cat (cat.category)}
				<button
					type="button"
					class="chip"
					aria-pressed={category === cat.category}
					onclick={() => (category = category === cat.category ? null : cat.category)}
				>
					{categoryLabel(cat.category)} {cat.count.toLocaleString()}
				</button>
			{/each}
		</div>
		{#if page.length}
			<div class="st-mopts">
				{#each page as model (model.endpoint_id)}
					{@const meta = rowMeta(model)}
					<ModelOption
						name={model.display_name}
						id={model.endpoint_id}
						kind={categoryLabel(model.category)}
						meta={meta.text}
						metaBadge={meta.badge}
						pressed={tools.isDefault(model.endpoint_id)}
						unusable={!tools.toolForCatalogModel(model)}
						disabled={tools.saving !== null}
						title={rowTitle(model)}
						onpick={() => tools.selectFromCatalog(model)}
					/>
				{/each}
			</div>
			{#if filtered.length > shown}
				<button type="button" class="st-btn ghost sm more" onclick={() => (shown += PAGE)}>
					Show {Math.min(PAGE, filtered.length - shown)} more of {filtered.length.toLocaleString()}
				</button>
			{/if}
		{:else if loading}
			<p class="empty">Loading the catalog…</p>
		{:else if q || category}
			<p class="empty">No models match. Clear the search or pick another category.</p>
		{:else}
			<p class="empty">The catalog is empty. Refresh to fetch it from fal.ai.</p>
		{/if}
	</RowGroup>
</DetailPane>

<style>
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin: 10px 0 12px;
	}
	.chip {
		font-size: 12.5px;
		padding: 3px 10px;
		border-radius: 999px;
		border: 1px solid var(--st-border-strong);
		color: var(--st-mfg);
		background: none;
		font-variant-numeric: tabular-nums;
	}
	.chip:hover:not([aria-pressed='true']) {
		color: var(--st-fg);
	}
	.chip[aria-pressed='true'] {
		background: var(--st-fg);
		color: var(--st-bg);
		border-color: transparent;
	}
	.st-mopts {
		background: var(--st-card);
		border: 1px solid var(--st-border);
		border-radius: var(--st-radius);
		overflow: hidden;
	}
	.empty {
		margin: 0;
		padding: 16px 2px;
		font-size: 13px;
		color: var(--st-mfg);
	}
	.more {
		margin-top: 6px;
	}
	:global(.settings-shell svg.lucide.spin) {
		animation: st-models-spin 0.9s linear infinite;
	}
	@keyframes -global-st-models-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		:global(.settings-shell svg.lucide.spin) {
			animation: none;
		}
	}
</style>
