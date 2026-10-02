<script lang="ts">
	/**
	 * ToolModelList: a tool's Model tab. "Recommended" lists the curated
	 * models with price and capabilities; "More from fal.ai" lists the
	 * catalog models fal.ai files under this tool, auto-imported. Picking one
	 * runs the full model-change sequence (see media-tools.svelte.ts).
	 */
	import type { ToolDefinition } from '$lib/types/ai-tool-registry';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import LinkValue from '../rows/LinkValue.svelte';
	import ModelOption from './ModelOption.svelte';
	import SearchBox from './SearchBox.svelte';
	import { getModelsState } from './tasks.svelte';

	interface Props {
		tool: ToolDefinition;
	}

	let { tool }: Props = $props();

	const { tools } = getModelsState();
	const PAGE = 40;

	let query = $state('');
	let shown = $state(PAGE);

	const curated = $derived(tool.getModels());
	const more = $derived(tools.moreModels(tool));
	const selected = $derived(tools.modelId(tool));
	const busy = $derived(tools.saving === tool.id);
	const q = $derived(query.trim().toLowerCase());

	const curatedShown = $derived(
		q
			? curated.filter(
					(m) =>
						m.displayName.toLowerCase().includes(q) ||
						m.description.toLowerCase().includes(q) ||
						m.id.toLowerCase().includes(q)
				)
			: curated
	);
	const moreFiltered = $derived(
		q
			? more.filter(
					(m) =>
						m.display_name.toLowerCase().includes(q) ||
						m.description.toLowerCase().includes(q) ||
						m.endpoint_id.toLowerCase().includes(q) ||
						m.tags.some((t) => t.toLowerCase().includes(q))
				)
			: more
	);
	// Keep the selected catalog model visible even past the first page.
	const moreShown = $derived.by(() => {
		const page = moreFiltered.slice(0, shown);
		const pick = moreFiltered.find((m) => m.endpoint_id === selected);
		return pick && !page.includes(pick) ? [pick, ...page] : page;
	});

	function pick(id: string) {
		void tools.selectModel(tool, id);
	}
</script>

{#if curated.length + more.length > 5}
	<div class="search">
		<SearchBox bind:value={query} placeholder="Search models" />
	</div>
{/if}

{#if curatedShown.length}
	<RowGroup title="Recommended" count={curatedShown.length} bare>
		<div class="st-mopts">
			{#each curatedShown as model (model.id)}
				<ModelOption
					name={model.displayName}
					id={model.id}
					badges={tools.capabilities(tool, model).map((c) => c.label)}
					meta={tool.formatPrice(model)}
					pressed={model.id === selected}
					disabled={busy}
					onpick={() => pick(model.id)}
				/>
			{/each}
		</div>
	</RowGroup>
{/if}

{#if moreFiltered.length || (!q && !curated.length)}
	<RowGroup title="More from fal.ai" count={moreFiltered.length || undefined} bare>
		{#snippet actions()}
			<LinkValue label="Full catalog" onclick={() => settingsNav.selectItem('catalog', 'models')} />
		{/snippet}
		{#if moreFiltered.length}
			<div class="st-mopts">
				{#each moreShown as model (model.endpoint_id)}
					<ModelOption
						name={model.display_name}
						id={model.endpoint_id}
						meta="Auto-imported"
						pressed={model.endpoint_id === selected}
						disabled={busy}
						title={model.description || model.endpoint_id}
						onpick={() => pick(model.endpoint_id)}
					/>
				{/each}
			</div>
			{#if moreFiltered.length > shown}
				<button type="button" class="st-btn ghost sm more" onclick={() => (shown += PAGE)}>
					Show {Math.min(PAGE, moreFiltered.length - shown)} more
				</button>
			{/if}
		{:else}
			<div class="st-mopts empty">No catalog models yet. Refresh the catalog.</div>
		{/if}
	</RowGroup>
{/if}

{#if q && !curatedShown.length && !moreFiltered.length}
	<p class="none">No models match “{query.trim()}”.</p>
{/if}

<style>
	.search {
		margin-top: 22px;
	}
	.st-mopts {
		background: var(--st-card);
		border: 1px solid var(--st-border);
		border-radius: var(--st-radius);
		overflow: hidden;
	}
	.st-mopts.empty,
	.none {
		padding: 16px;
		font-size: 13px;
		color: var(--st-mfg);
	}
	.none {
		margin: 22px 0 0;
		padding: 0 2px;
	}
	.more {
		margin-top: 6px;
	}
</style>
