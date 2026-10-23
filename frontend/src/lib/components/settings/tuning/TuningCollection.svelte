<script lang="ts">
	/**
	 * Settings > Model tuning: every model in the picker, grouped like the
	 * picker, with what a new chat on it starts with. The detail edits the
	 * same per-model records the composer remembers (tuning-model.ts), and,
	 * for a model on a local gateway, its sampling and thinking switch.
	 */
	import '../providers/providers.css';
	import { onMount } from 'svelte';
	import { Gauge } from 'lucide-svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import ListGroupLabel from '../rows/ListGroupLabel.svelte';
	import ProviderTile from '../providers/ProviderTile.svelte';
	import TuningDetail from './TuningDetail.svelte';
	import { tuningRevision } from './revision.svelte';
	import { collectTuningModels, hasSavedTuning, parseTuningItem, tuningReady, tuningSummary, type TuningModel } from './tuning-model';

	const CATEGORY = 'tuning' as const;
	let filter = $state('');

	onMount(() => {
		void providers.refreshAll();
		return tuningRevision.follow();
	});
	watchResources(['settings'], () => providers.refreshAll());

	const rows = $derived(providers.current.providers);
	const models = $derived(collectTuningModels());
	const ready = $derived(tuningReady());
	const q = $derived(filter.trim().toLowerCase());
	const shown = $derived(
		models.filter(
			(m) =>
				!q ||
				[m.label, m.key, ...m.routes.map((r) => `${r.group} ${r.groupSubtext ?? ''} ${r.modelId}`)]
					.join(' ')
					.toLowerCase()
					.includes(q)
		)
	);
	const groups = $derived.by(() => {
		const out = new Map<string, TuningModel[]>();
		for (const m of shown) out.set(m.group, [...(out.get(m.group) ?? []), m]);
		return [...out.entries()];
	});
	const tunedCount = $derived.by(() => {
		void tuningRevision.value;
		return models.filter((m) => hasSavedTuning(m, rows)).length;
	});

	const selected = $derived(settingsNav.selectedItem(CATEGORY));
	const current = $derived(models.find((m) => m.item === selected) ?? null);

	function summary(m: TuningModel): string {
		void tuningRevision.value;
		return tuningSummary(m, rows);
	}
	function tuned(m: TuningModel): boolean {
		void tuningRevision.value;
		return hasSavedTuning(m, rows);
	}
	const meta = (m: TuningModel) => (m.routes.length > 1 ? `${m.routes.length} routes` : undefined);
</script>

<CollectionPage
	category={CATEGORY}
	count={models.length ? `${tunedCount} of ${models.length} changed` : undefined}
	filterPlaceholder="Filter models"
	listLabel="Models"
	bind:filter
>
	{#snippet list()}
		<div data-setting-anchor="tuning.models">
			{#if !ready && models.length === 0}
				<p class="st-empty" role="status">Loading models…</p>
			{:else if models.length === 0}
				<p class="st-empty">No models in the picker yet. Connect a provider, then tune its models here.</p>
			{:else if shown.length === 0}
				<p class="st-empty">Nothing matches.</p>
			{:else}
				{#each groups as [group, list] (group)}
					<ListGroupLabel label={group} />
					{#each list as m (m.item)}
						<ListItem
							id={m.item}
							name={m.label}
							category={CATEGORY}
							summary={summary(m)}
							meta={meta(m)}
						>
							{#snippet lead()}
								<ProviderTile
									provider={{ glyph: m.glyph, letter: m.group.slice(0, 1).toUpperCase() }}
									status={m.routes.every((r) => !r.usable) ? 'warn' : undefined}
									ring={selected === m.item ? 'accent' : 'bg'}
								/>
							{/snippet}
							{#snippet badges()}
								{#if tuned(m)}<span class="tn-chip">Changed</span>{/if}
							{/snippet}
						</ListItem>
					{/each}
				{/each}
			{/if}
		</div>
	{/snippet}
	{#snippet detail()}
		{#if current}
			{#key current.item}<TuningDetail model={current} {rows} />{/key}
		{:else if selected && !ready}
			<EmptyDetail icon={Gauge} message="Loading models" />
		{:else if selected && parseTuningItem(selected)}
			<EmptyDetail icon={Gauge} title="Not in the picker" message="This model is no longer offered. Its saved choices stay on your account." />
		{:else}
			<EmptyDetail icon={Gauge} message="Select a model to see what new chats on it start with." />
		{/if}
	{/snippet}
</CollectionPage>

<style>
	/* The list's echo of a row's teal "modified" edge. */
	.tn-chip {
		display: inline-flex;
		align-items: center;
		margin-left: 6px;
		padding: 0 7px;
		border-radius: 999px;
		font-size: 11px;
		font-weight: 500;
		line-height: 18px;
		white-space: nowrap;
		color: var(--st-primary);
		background: var(--st-primary-soft);
		box-shadow: inset 0 0 0 1px var(--st-primary-line);
	}
</style>
