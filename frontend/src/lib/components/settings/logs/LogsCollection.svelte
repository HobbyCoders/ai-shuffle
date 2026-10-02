<script lang="ts">
	/**
	 * Logs: the worker's recent lines, newest first, with level filter,
	 * debounced search, refresh, export and a live count. Polls every 10s
	 * only while Settings is the visible panel and the tab is visible.
	 */
	import { onMount, untrack } from 'svelte';
	import { Download, RefreshCw, ScrollText, X } from 'lucide-svelte';
	import { activeView } from '$lib/stores/layout.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import LogDetail from './LogDetail.svelte';
	import { LOG_POLL_MS, formatLogTime, levelTone, logsModel, type LogsModel } from './logs-model.svelte';

	interface Props {
		/** Tests pass their own model. */
		model?: LogsModel;
	}

	let { model = logsModel }: Props = $props();

	const CATEGORY = 'logs' as const;
	let filter = $state(untrack(() => model.search));
	let pageVisible = $state(true);

	onMount(() => {
		const update = () => (pageVisible = !document.hidden);
		update();
		document.addEventListener('visibilitychange', update);
		return () => {
			document.removeEventListener('visibilitychange', update);
			model.dispose();
		};
	});

	$effect(() => {
		if (filter !== model.search) model.setSearch(filter);
	});

	// Fetch now and every 10s while Settings and the tab are visible; a
	// filter change restarts the cycle.
	$effect(() => {
		void model.level;
		void model.appliedSearch;
		if ($activeView?.type !== 'settings' || !pageVisible) return;
		void model.fetch();
		const timer = setInterval(() => void model.fetch(), LOG_POLL_MS);
		return () => clearInterval(timer);
	});

	const selected = $derived(settingsNav.selectedItem(CATEGORY));
	const entry = $derived(model.find(selected));
	const levelOptions = $derived([
		{ value: '', label: 'All levels' },
		...model.levelOptions.map((l) => ({ value: l, label: l }))
	]);
	const count = $derived(model.loaded ? `${model.returned.toLocaleString()} of ${model.total.toLocaleString()}` : '');

	function select(id: string) {
		model.pin(id);
		settingsNav.selectItem(id, CATEGORY);
	}

	function sameLogger(logger: string) {
		model.sameLogger(logger);
		settingsNav.back();
	}
</script>

<CollectionPage category={CATEGORY} {count} filterPlaceholder="Search logs" filterIcon="search" bind:filter>
	{#snippet toolbar()}
		<span class="anchor" data-setting-anchor="logs.level">
			<SelectField
				label="Log level"
				value={model.level}
				options={levelOptions}
				width={150}
				onchange={(v) => model.setLevel(v)}
			/>
		</span>
		{#if model.logger}
			<button
				type="button"
				class="chip"
				aria-label="Show every logger"
				title="Show every logger"
				onclick={() => model.clearLogger()}
			>
				<span>{model.logger}</span><X />
			</button>
		{/if}
		<span class="sp"></span>
		<button
			type="button"
			class="st-btn icon sm"
			class:spin={model.loading && model.loaded}
			aria-label="Refresh logs"
			title="Refresh logs"
			disabled={model.loading}
			onclick={() => model.refresh()}
		>
			<RefreshCw />
		</button>
		<span class="anchor" data-setting-anchor="logs.export">
			<button type="button" class="st-btn sm" disabled={model.exporting} onclick={() => model.exportLogs()}>
				<Download />{model.exporting ? 'Exporting…' : 'Export'}
			</button>
		</span>
	{/snippet}
	{#snippet list()}
		{#if model.error}
			<InlineBanner tone="warn"><span role="alert">{model.error}</span></InlineBanner>
		{/if}
		{#if model.forbidden}
			<p class="st-empty">{FORBIDDEN_TEXT}</p>
		{:else if model.loading && !model.loaded}
			<p class="st-empty" role="status">Loading logs…</p>
		{:else if model.shown.length === 0}
			<p class="st-empty">
				No logs found.{#if model.level || model.appliedSearch || model.logger}
					Try other filters.{/if}
			</p>
		{:else}
			{#each model.shown as line (line.id)}
				<ListItem
					id={line.id}
					category={CATEGORY}
					variant="log"
					mono
					name={line.message}
					summary={line.logger_name}
					meta={formatLogTime(line.timestamp).slice(0, 8)}
					status={levelTone(line.level)}
					onselect={select}
				/>
			{/each}
		{/if}
	{/snippet}
	{#snippet detail()}
		{#if entry}
			<LogDetail {entry} onSameLogger={sameLogger} />
		{:else}
			<EmptyDetail icon={ScrollText} message="Select a log entry." />
		{/if}
	{/snippet}
</CollectionPage>

<style>
	.anchor {
		display: inline-flex;
		align-items: center;
		border-radius: var(--st-radius-md);
	}
	.sp {
		flex: 1;
	}
	.chip {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		height: 26px;
		max-width: 150px;
		padding: 0 6px 0 9px;
		border: 1px solid var(--st-border-strong);
		border-radius: 999px;
		background: var(--st-muted);
		font-family: var(--st-mono);
		font-size: 11.5px;
		color: var(--st-fg);
	}
	.chip span {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.chip :global(svg.lucide) {
		width: 13px;
		height: 13px;
		color: var(--st-mfg);
	}
	.spin :global(svg.lucide) {
		animation: st-logs-spin 0.9s linear infinite;
	}
	@keyframes st-logs-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spin :global(svg.lucide) {
			animation: none;
		}
	}
</style>
