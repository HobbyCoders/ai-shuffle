<script lang="ts">
	/**
	 * Settings > Memory: everything agents have saved to remember, with search
	 * and filters. Each memory is edited with an explicit Save. Unsaved edits are
	 * kept when another row or page opens, and a tab close asks first.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { Brain, Pin, TriangleAlert } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import MemoryDetail from './MemoryDetail.svelte';
	import {
		FILTER_OPTIONS,
		acquireMemoryModel,
		memoryMeta,
		releaseMemoryModel,
		type MemoryFilter,
		type MemoryItem
	} from './memory-model.svelte';
	import '../pages/editors/editors.css';

	const CATEGORY = 'memory' as const;
	const model = acquireMemoryModel();

	onMount(() => void model.load());
	onDestroy(() => releaseMemoryModel(model));

	$effect(() => {
		if (!model.anyDirty) return;
		const protectDraft = (event: BeforeUnloadEvent) => {
			event.preventDefault();
			event.returnValue = '';
		};
		window.addEventListener('beforeunload', protectDraft);
		return () => window.removeEventListener('beforeunload', protectDraft);
	});

	const selected = $derived(settingsNav.selectedItem(CATEGORY));
	const known = $derived(!!selected && (!!model.item(selected) || !!model.forms[selected]));
	const total = $derived(model.items.length);
	const count = $derived(model.visible.length === total ? total : `${model.visible.length} of ${total}`);
	const scopeOptions = $derived([{ value: '', label: 'All places' }, ...model.scopes.map((s) => ({ value: s, label: s }))]);

	const EMPTY_FILTER: Record<MemoryFilter, string> = {
		active: 'No active memories.',
		archived: 'No archived memories.',
		review: 'Nothing needs review.',
		all: 'No memories yet.'
	};

	const summary = (m: MemoryItem) => (model.dirty(m.id) ? 'Unsaved changes' : memoryMeta(m));
</script>

<CollectionPage
	category={CATEGORY}
	count={model.loaded ? count : undefined}
	filterPlaceholder="Search memories"
	filterIcon="search"
	listLabel="Saved memories"
	bind:filter={model.query}
>
	{#snippet filters()}
		<span class="mem-filters">
			<SelectField
				label="Show"
				value={model.filter}
				options={FILTER_OPTIONS}
				width={160}
				onchange={(v) => (model.filter = v as MemoryFilter)}
			/>
			{#if model.scopes.length > 1}
				<SelectField label="Where it applies" value={model.scope} options={scopeOptions} width={180} onchange={(v) => (model.scope = v)} />
			{/if}
		</span>
	{/snippet}
	{#snippet list()}
		<div class="mem-list" data-setting-anchor="memory.list">
			{#if model.loadError}
				<InlineBanner tone="warn">
					<span role="alert">{model.loadError}</span>
					{#snippet actions()}
						<button type="button" class="st-btn sm" disabled={model.loading} onclick={() => model.load()}>Retry</button>
					{/snippet}
				</InlineBanner>
			{/if}
			{#if model.forbidden}
				<div class="st-card"><p class="st-empty">{FORBIDDEN_TEXT}</p></div>
			{:else if model.loading && !model.loaded}
				<div class="st-card"><p class="st-empty" role="status">Loading memories…</p></div>
			{:else if model.loaded}
				{#each model.shown as m (m.id)}
					<ListItem id={m.id} name={m.text} category={CATEGORY} summary={summary(m)}>
						{#snippet lead()}
							<span class="mark" class:warn={m.needs_review}>
								{#if m.needs_review}
									<TriangleAlert aria-hidden="true" /><span class="st-vh">Needs review.</span>
								{:else if m.always}
									<span class="glyph" title="Applies to every chat"><Pin aria-hidden="true" /></span><span class="st-vh">Applies to every chat.</span>
								{/if}
							</span>
						{/snippet}
					</ListItem>
				{/each}
				{#if model.hiddenCount > 0}
					<button type="button" class="st-btn mem-more" onclick={() => model.showMore()}>
						Show more ({model.hiddenCount.toLocaleString()} left)
					</button>
				{/if}
				{#if total === 0}
					<EmptyDetail icon={Brain} message="No memories yet. When you or an agent saves something to remember, it appears here." idle />
				{:else if model.visible.length === 0}
					{#if model.narrowed}
						<EmptyDetail message="Nothing matches." actionLabel="Clear" onAction={() => model.clearSearch()} />
					{:else}
						<EmptyDetail message={EMPTY_FILTER[model.filter]} idle />
					{/if}
				{/if}
			{/if}
		</div>
	{/snippet}
	{#snippet detail()}
		{#if selected && known}
			{#key selected}
				<MemoryDetail {model} id={selected} />
			{/key}
		{:else if selected && model.loaded}
			<EmptyDetail icon={Brain} title="Not found" message="This memory was deleted, or isn't on this worker." />
		{:else}
			<EmptyDetail icon={Brain} message="Select a memory to read or change it." idle />
		{/if}
	{/snippet}
</CollectionPage>

<style>
	.mem-filters {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 8px;
		min-width: 0;
	}
	.mem-list :global(.st-it) {
		align-items: start;
		padding-block: 8px;
		/* Off-screen rows skip layout, which keeps long lists smooth. */
		content-visibility: auto;
		contain-intrinsic-size: auto 62px;
	}
	.mem-list :global(.st-it .nm) {
		display: -webkit-box;
		-webkit-box-orient: vertical;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		white-space: normal;
		overflow: hidden;
		overflow-wrap: anywhere;
		font-weight: 400;
		line-height: 1.4;
	}
	.mem-list :global(.st-it .sm) {
		margin-top: 2px;
	}
	.mark {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 14px;
		height: 19px;
		color: var(--st-subtle);
	}
	.mark.warn {
		color: var(--st-warning);
	}
	.glyph {
		display: inline-flex;
	}
	.mark :global(svg.lucide) {
		width: 13px;
		height: 13px;
	}
	.mem-more {
		width: 100%;
		margin-top: 8px;
		justify-content: center;
	}
</style>
