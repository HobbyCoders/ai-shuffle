<script lang="ts">
	/**
	 * GenerationModelBrowser: provider first, then any model that provider
	 * offers for this call's operation (text-to-video, image-to-video, edit...).
	 * Search and paging run on the server, so fal's hundreds of endpoints are
	 * never sent whole. Picking a row hands it to the caller, which loads the
	 * model's declared options and quotes them. Shared by the chat choice card,
	 * Settings > Default models and the automation picker.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { Search } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { generationChoices } from '$lib/stores/generation-choices.svelte';
	import { workerInstalls } from '$lib/stores/worker-presence';
	import {
		modelRowTitle,
		priceView,
		providerKey,
		sourceLabel,
		workerLabel,
		type GenerationKind,
		type GenerationProvider,
		type ModelRow
	} from '$lib/utils/generation-choice';

	interface Props {
		kind: GenerationKind;
		operation?: string;
		/** The model currently picked on the card, marked in the list. */
		selectedModel?: string;
		/** Open on this account's provider when it can make the kind. */
		preferAccount?: string;
		onpick: (row: ModelRow) => void;
	}

	let { kind, operation, selectedModel = '', preferAccount = '', onpick }: Props = $props();

	const PAGE = 20;
	const SEARCH_DEBOUNCE_MS = 250;
	const uid = `gmb-${Math.random().toString(36).slice(2, 9)}`;

	let providers = $state<GenerationProvider[] | null>(null);
	let operationLabel = $state('');
	let providerError = $state('');
	let active = $state('');
	let query = $state('');
	let rows = $state<ModelRow[]>([]);
	let total = $state(0);
	let hasMore = $state(false);
	let loading = $state(false);
	let listError = $state('');
	let notice = $state('');
	// The provider and search the shown rows belong to. Rows and "Show more"
	// act only while that matches what is asked and no page is on its way.
	let shownFor = $state('');
	// The worker that served the first fal page; later pages come from it.
	let pageWorker: string | null = null;
	let seq = 0;
	let searchTimer: ReturnType<typeof setTimeout> | null = null;
	const ready = $derived(!loading && shownFor === `${active}\n${query}`);

	const here = $derived(generationChoices.worker);
	const current = $derived(providers?.find((p) => providerKey(p) === active));
	const unavailable = $derived((providers ?? []).filter((p) => !p.available));

	function providerName(p: GenerationProvider): string {
		const name = sourceLabel(p.source);
		if (p.source === 'fal') return name;
		const remote = p.workers.find((w) => w && w !== here && w !== 'local');
		return remote ? `${name} · ${workerLabel(remote, $workerInstalls, here)}` : name;
	}

	async function loadProviders() {
		providerError = '';
		try {
			const data = await generationChoices.providers(kind, operation);
			providers = data.providers ?? [];
			operationLabel = data.operation_label ?? '';
			const usable = providers.filter((p) => p.available);
			const preferred = usable.find((p) => p.account === preferAccount || (p.source === 'fal' && preferAccount === 'fal-ai'));
			const first = preferred ?? usable[0];
			if (first) choose(providerKey(first));
		} catch (e) {
			providerError = getErrorMessage(e, 'Could not list the providers.');
			providers = [];
		}
	}

	function cancelSearch() {
		if (searchTimer) clearTimeout(searchTimer);
		searchTimer = null;
	}

	function choose(key: string) {
		if (key === active) return;
		cancelSearch();
		active = key;
		query = '';
		rows = [];
		total = 0;
		hasMore = false;
		shownFor = '';
		notice = '';
		pageWorker = null;
		void loadPage(0);
	}

	async function loadPage(offset: number) {
		if (!active) return;
		const key = `${active}\n${query}`;
		// "Show more" pages only the list on screen, never a newer search.
		if (offset && shownFor !== key) return;
		const mine = ++seq;
		loading = true;
		listError = '';
		try {
			const page = await generationChoices.models(kind, {
				source: active, operation, query, offset, limit: PAGE, worker: offset ? pageWorker : null
			});
			if (mine !== seq) return;
			rows = offset ? [...rows, ...page.models] : page.models;
			total = page.total;
			hasMore = page.has_more;
			if (!offset) {
				pageWorker = page.worker ?? null;
				notice = page.notice ?? '';
			}
			shownFor = key;
		} catch (e) {
			if (mine === seq) listError = getErrorMessage(e, 'Could not list the models.');
		} finally {
			if (mine === seq) loading = false;
		}
	}

	function onSearch(value: string) {
		query = value;
		cancelSearch();
		// A page for the earlier search no longer lands; rows wait for this one.
		seq++;
		loading = true;
		searchTimer = setTimeout(() => {
			searchTimer = null;
			void loadPage(0);
		}, SEARCH_DEBOUNCE_MS);
	}

	function onProviderKey(event: KeyboardEvent, index: number) {
		const usable = (providers ?? []).filter((p) => p.available);
		if (!['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'].includes(event.key) || usable.length < 2) return;
		event.preventDefault();
		const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1;
		const next = usable[(index + step + usable.length) % usable.length];
		choose(providerKey(next));
		document.getElementById(`${uid}-p-${providerKey(next)}`)?.focus();
	}

	onMount(() => void loadProviders());
	onDestroy(cancelSearch);
</script>

<div class="gmb" data-testid="generation-model-browser">
	{#if providerError}
		<p class="gmb-msg" role="alert">{providerError}</p>
	{:else if providers === null}
		<div class="gmb-skeleton" aria-busy="true" aria-label="Loading providers"><span></span><span></span></div>
	{:else if !providers.some((p) => p.available)}
		<p class="gmb-msg">
			No connected account can do {operationLabel ? operationLabel.toLowerCase() : 'this'} yet. Connect fal.ai or a plan in Providers.
		</p>
	{:else}
		<div class="gmb-providers" role="radiogroup" aria-label="Provider">
			{#each providers.filter((p) => p.available) as provider, index (providerKey(provider))}
				{@const key = providerKey(provider)}
				<button
					type="button"
					role="radio"
					id="{uid}-p-{key}"
					class="gmb-provider"
					class:selected={key === active}
					aria-checked={key === active}
					tabindex={key === active ? 0 : -1}
					onclick={() => choose(key)}
					onkeydown={(e) => onProviderKey(e, index)}
				>
					<span class="gmb-provider-name">{providerName(provider)}</span>
					{#if provider.model_count !== undefined}<span class="gmb-count">{provider.model_count}</span>{/if}
				</button>
			{/each}
		</div>
	{/if}
	{#if unavailable.length}
		<ul class="gmb-unavailable" aria-label="Unavailable providers">
			{#each unavailable as provider (providerKey(provider) + provider.workers.join())}
				<li><span class="gmb-provider-name">{providerName(provider)}</span>: {provider.reason || 'Unavailable'}</li>
			{/each}
		</ul>
	{/if}

	{#if current}
		{#if current.source === 'fal' || total > 6 || query}
			<label class="gmb-search">
				<Search aria-hidden="true" />
				<span class="sr-only">Search models</span>
				<input
					type="search"
					placeholder={current.model_count ? `Search ${current.model_count} models` : 'Search models'}
					value={query}
					oninput={(e) => onSearch(e.currentTarget.value)}
					autocomplete="off"
					spellcheck="false"
				/>
			</label>
		{/if}
		{#if notice}<p class="gmb-msg" role="note">{notice}</p>{/if}
		{#if listError}
			<p class="gmb-msg" role="alert">{listError}</p>
		{:else if !rows.length && loading}
			<div class="gmb-skeleton" aria-busy="true" aria-label="Loading models"><span></span><span></span></div>
		{:else if !rows.length}
			<p class="gmb-msg">{query ? `No model matches “${query}”.` : 'This provider has no models for this.'}</p>
		{:else}
			<ul class="gmb-list" aria-label="Models" aria-busy={loading}>
				{#each rows as row (row.worker + row.account + row.model)}
					{@const view = priceView(row.price, kind)}
					{@const title = modelRowTitle(row)}
					{@const unpriced = !view.priced && row.price?.reason !== 'not_quoted'}
					<li>
						<button
							type="button"
							class="gmb-row"
							class:selected={row.model === selectedModel}
							aria-pressed={row.model === selectedModel}
							disabled={!row.available || !ready}
							title={row.available ? (unpriced && view.detail) || row.description || row.model : row.reason || 'Unavailable'}
							onclick={() => ready && onpick(row)}
						>
							<span class="gmb-row-main">
								<span class="gmb-row-title">{title}</span>
								<span class="gmb-row-id">{row.available ? row.model : row.reason || 'Unavailable'}</span>
							</span>
							<span class="gmb-row-price" class:unknown={!view.priced}>
								{#if view.priced}
									<span class="gmb-figure">{view.figure}</span>
								{:else}
									{#if unpriced}<span class="gmb-figure">Unknown</span>{/if}
									<span class={unpriced ? 'gmb-rate' : 'gmb-figure'}>{view.rate ?? (unpriced ? '' : 'Unpriced')}</span>
								{/if}
							</span>
						</button>
					</li>
				{/each}
			</ul>
			<div class="gmb-foot">
				<span class="gmb-shown">{rows.length} of {total}</span>
				{#if hasMore}
					<button type="button" class="gmb-more" disabled={!ready} onclick={() => loadPage(rows.length)}>
						{loading ? 'Loading' : 'Show more'}
					</button>
				{/if}
			</div>
		{/if}
	{/if}
</div>

<style>
	.gmb {
		display: grid;
		gap: 0.5rem;
		min-width: 0;
	}
	.gmb-msg {
		margin: 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}
	.gmb-providers {
		display: flex;
		flex-wrap: wrap;
		gap: 0.375rem;
	}
	.gmb-provider {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		min-height: 32px;
		max-width: 100%;
		padding: 0 0.625rem;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: transparent;
		color: inherit;
		font: inherit;
		font-size: 0.75rem;
		font-weight: 500;
		cursor: pointer;
	}
	.gmb-provider:hover {
		background: var(--muted);
	}
	.gmb-provider.selected {
		border-color: var(--border-strong, var(--foreground));
		background: var(--muted);
	}
	.gmb-provider-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.gmb-count {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
	}
	.gmb-unavailable {
		margin: 0;
		padding: 0;
		list-style: none;
		font-size: 0.6875rem;
		line-height: 1.45;
		color: var(--muted-foreground);
	}
	.gmb-search {
		position: relative;
		display: flex;
		align-items: center;
	}
	.gmb-search :global(svg) {
		position: absolute;
		left: 0.625rem;
		width: 14px;
		height: 14px;
		color: var(--muted-foreground);
		pointer-events: none;
	}
	.gmb-search input {
		width: 100%;
		min-height: 36px;
		padding: 0 0.625rem 0 2rem;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 10px) - 4px);
		background: var(--background);
		color: var(--foreground);
		/* 16px keeps iOS from zooming the page on focus. */
		font: inherit;
		font-size: max(16px, 0.8125rem);
	}
	@media (min-width: 640px) {
		.gmb-search input {
			font-size: 0.8125rem;
		}
	}
	.gmb-search input:focus-visible {
		outline: 2px solid var(--ring, var(--foreground));
		outline-offset: 1px;
	}
	.gmb-list {
		display: grid;
		gap: 2px;
		max-height: 17rem;
		margin: 0;
		padding: 2px;
		overflow-y: auto;
		overscroll-behavior: contain;
		list-style: none;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 10px) - 4px);
	}
	.gmb-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		width: 100%;
		min-height: 44px;
		padding: 0.375rem 0.5rem;
		border: 1px solid transparent;
		border-radius: calc(var(--radius, 10px) - 6px);
		background: transparent;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.gmb-row:hover:not(:disabled) {
		background: var(--muted);
	}
	.gmb-row.selected {
		border-color: var(--border-strong, var(--foreground));
		background: var(--muted);
	}
	.gmb-row:disabled {
		cursor: not-allowed;
		opacity: 0.6;
	}
	.gmb-row-main {
		display: grid;
		flex: 1;
		min-width: 0;
	}
	.gmb-row-title {
		font-size: 0.8125rem;
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.gmb-row-id {
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.gmb-row-price {
		flex: none;
		max-width: 45%;
		text-align: right;
	}
	.gmb-figure {
		display: block;
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.gmb-rate {
		display: block;
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.gmb-row-price.unknown .gmb-figure {
		font-weight: 500;
		color: var(--muted-foreground);
	}
	.gmb-foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
	}
	.gmb-more {
		min-height: 32px;
		padding: 0 0.625rem;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 10px) - 4px);
		background: transparent;
		color: var(--foreground);
		font: inherit;
		font-size: 0.75rem;
		cursor: pointer;
	}
	.gmb-more:hover:not(:disabled) {
		background: var(--muted);
	}
	.gmb-skeleton {
		display: flex;
		gap: 0.375rem;
	}
	.gmb-skeleton span {
		width: 6rem;
		height: 32px;
		border-radius: 999px;
		background: var(--muted);
		animation: gmb-pulse 1.2s ease-in-out infinite;
	}
	@keyframes gmb-pulse {
		50% {
			opacity: 0.5;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.gmb-skeleton span {
			animation: none;
		}
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
	}
</style>
