<script lang="ts">
	/**
	 * FalCatalog: every fal.ai model the catalog knows. Search across name,
	 * description, id and tags; category chips. Picking an image, video, audio
	 * or 3D generation model makes it the remembered choice for that kind; any
	 * other model is pinned for the tool fal.ai files it under (editing, image
	 * to video...). A model no tool uses is greyed out.
	 */
	import { catalogStore } from '$lib/stores/catalog';
	import { CATEGORY_LABELS, type CatalogModel } from '$lib/types/ai-models';
	import RowGroup from '../rows/RowGroup.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import ModelOption from './ModelOption.svelte';
	import SearchBox from './SearchBox.svelte';
	import { GENERATION_TOOL_KIND, toolMeta } from './media-tools.svelte';
	import { KIND_LABEL } from '$lib/utils/generation-choice';
	import { getModelsState } from './tasks.svelte';

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
		const tool = tools.toolForCatalogModel(model);
		if (tools.isDefault(model.endpoint_id)) {
			return { text: tool && GENERATION_TOOL_KIND[tool.id] ? 'Remembered' : 'Pinned', badge: true };
		}
		if (!tool) return { text: 'No tool uses this', badge: false };
		return { badge: false };
	}

	function rowTitle(model: CatalogModel): string {
		const tool = tools.toolForCatalogModel(model);
		if (!tool) return `${model.endpoint_id}: ${NO_TOOL}`;
		const kind = GENERATION_TOOL_KIND[tool.id];
		if (kind) return `${model.endpoint_id}: remember for ${KIND_LABEL[kind].plural}`;
		return `${model.endpoint_id}: pin for ${toolMeta(tool).name.toLowerCase()}`;
	}
</script>

{#if error}
	<InlineBanner tone="warn">
		{error}
		{#snippet actions()}
			<button type="button" class="st-btn sm" onclick={() => catalogStore.load(true)}>Retry</button>
		{/snippet}
	</InlineBanner>
{/if}
<RowGroup title="Browse fal.ai" count={category || q ? `${filtered.length.toLocaleString()} shown` : undefined} bare>
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
</style>
