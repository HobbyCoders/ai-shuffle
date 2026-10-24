<script lang="ts">
	/**
	 * AccountCatalog: the "All accounts" tab of Settings → Default models →
	 * Catalog. Every connected account on every worker for one generation kind,
	 * verified or not, with its status (ready, no price, unavailable and why),
	 * its live price and the worker that holds it. One optional favourite per
	 * kind is a bookmark: it is shown apart from the remembered choice and
	 * never runs anything by itself.
	 */
	import { untrack } from 'svelte';
	import { Star, ShieldCheck, ArrowUpRight } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { workerInstalls } from '$lib/stores/worker-presence';
	import { GENERATION_KINDS, generationChoices } from '$lib/stores/generation-choices.svelte';
	import {
		KIND_LABEL,
		choiceOf,
		modelLabel,
		priceView,
		sameChoice,
		sourceLabel,
		workerLabel,
		type CatalogEntry,
		type CatalogStatus,
		type GenerationChoice,
		type GenerationKind
	} from '$lib/utils/generation-choice';
	import type { StatusTone } from '$lib/settings/types';
	import RowGroup from '../rows/RowGroup.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import SearchBox from './SearchBox.svelte';
	import { GENERATION_ICONS } from './icons';
	import { groupByWorker, type AccountCatalog } from './account-catalog.svelte';

	let { catalog }: { catalog: AccountCatalog } = $props();

	const KIND_SHORT: Record<GenerationKind, string> = { image: 'Images', video: 'Video', audio: 'Audio', '3d': '3D' };
	const STATUS: Record<CatalogStatus, { text: string; tone: StatusTone }> = {
		available: { text: 'Ready', tone: 'ok' },
		unpriced: { text: 'No price', tone: 'warn' },
		unavailable: { text: 'Unavailable', tone: 'off' }
	};

	let search = $state(untrack(() => catalog.query));
	$effect(() => catalog.setQuery(search));

	let busyKey = $state<string | null>(null);
	let actionError = $state('');

	const kind = $derived(catalog.kind);
	const label = $derived(KIND_LABEL[kind]);
	const self = $derived(generationChoices.worker);
	const installs = $derived($workerInstalls);
	const entries = $derived(catalog.entries ?? []);
	const groups = $derived(groupByWorker(entries, self));
	const favourite = $derived(generationChoices.favourites[kind] ?? null);
	const favouriteEntry = $derived(favourite ? (entries.find((e) => sameChoice(favourite, e)) ?? null) : null);
	const remembered = $derived(generationChoices.remembered[kind] ?? null);

	function keyOf(e: Pick<CatalogEntry, 'source' | 'account' | 'worker' | 'model'>): string {
		return `${e.source}|${e.account}|${e.worker}|${e.model}`;
	}

	function presence(worker: string): { text: string; online: boolean | null } {
		const install = installs?.find((i) => i.device_id === worker || i.id === worker);
		if (worker === self || worker === 'local' || install?.is_self) return { text: 'This worker', online: true };
		if (!install) return { text: 'Peer', online: null };
		return { text: install.online ? 'Online' : 'Offline', online: install.online };
	}

	function accountText(e: Pick<CatalogEntry, 'source' | 'account' | 'worker'>): string {
		const local = e.worker === self || e.worker === 'local';
		const name = local ? catalog.accountName(e) : null;
		return name ? `${sourceLabel(e.source)} · ${name}` : sourceLabel(e.source);
	}

	function shownModel(model: string): string {
		return model === 'unavailable' ? 'No verified model' : modelLabel(model);
	}

	function why(e: CatalogEntry): string {
		if (e.status === 'unpriced') return e.reason || 'No known price, so it cannot run.';
		return e.reason || 'This route cannot run right now.';
	}

	/** A placeholder row for an account with no verified model cannot be bookmarked. */
	function starrable(e: CatalogEntry): boolean {
		return e.model !== 'unavailable';
	}

	async function toggleFavourite(e: CatalogEntry) {
		const key = keyOf(e);
		busyKey = key;
		actionError = '';
		try {
			if (favourite && sameChoice(favourite, e)) await generationChoices.clearFavourite(kind);
			else await generationChoices.setFavourite(kind, choiceOf(e));
		} catch (err) {
			actionError = getErrorMessage(err, 'Could not change the favourite.');
		} finally {
			busyKey = null;
		}
	}

	async function removeFavourite() {
		busyKey = 'favourite';
		actionError = '';
		try {
			await generationChoices.clearFavourite(kind);
		} catch (err) {
			actionError = getErrorMessage(err, 'Could not remove the favourite.');
		} finally {
			busyKey = null;
		}
	}

	function describeChoice(choice: GenerationChoice): string {
		return `${sourceLabel(choice.source)} · ${shownModel(choice.model)}`;
	}
</script>

<div class="acat" data-kind={kind}>
	<div class="kinds" role="group" aria-label="Generation kind">
		{#each GENERATION_KINDS as k (k)}
			{@const Icon = GENERATION_ICONS[k]}
			<button type="button" class="kind" aria-pressed={kind === k} onclick={() => catalog.setKind(k)}>
				<Icon aria-hidden="true" />{KIND_SHORT[k]}
			</button>
		{/each}
	</div>

	<SearchBox bind:value={search} placeholder="Search accounts and models" label="Search accounts and models" />

	<!-- The favourite: a bookmark, deliberately apart from the remembered choice. -->
	<section class="fav" class:set={!!favourite} aria-labelledby="acat-fav-title">
		<span class="fav-mark" aria-hidden="true"><Star /></span>
		<div class="fav-body">
			<h4 id="acat-fav-title">Favourite for {label.plural}</h4>
			{#if favourite}
				<p class="fav-pick">{describeChoice(favourite)}</p>
				<p class="fav-meta">
					on {workerLabel(favourite.worker, installs, self)}
					{#if favouriteEntry}
						<span aria-hidden="true">·</span> {priceView(favouriteEntry.price, kind).figure}
						<span aria-hidden="true">·</span> {STATUS[favouriteEntry.status].text}
					{:else if catalog.entries && !catalog.query}
						<span aria-hidden="true">·</span> not offered right now
					{/if}
				</p>
			{:else}
				<p class="fav-pick none">None. Star a row below to keep one at hand.</p>
			{/if}
			<p class="fav-note">
				A bookmark only: it never runs by itself. When you don't name a source, the agent uses your remembered choice
				{#if remembered}
					(<button type="button" class="fav-link" onclick={() => settingsNav.go({ category: 'models', item: `gen-${kind}` })}>
						{describeChoice(remembered)}<ArrowUpRight aria-hidden="true" />
					</button>)
				{:else}
					or <button type="button" class="fav-link" onclick={() => settingsNav.go({ category: 'models', item: `gen-${kind}` })}>
						asks you<ArrowUpRight aria-hidden="true" />
					</button>
				{/if}.
			</p>
		</div>
		{#if favourite}
			<button type="button" class="st-btn sm ghost fav-remove" disabled={busyKey !== null} onclick={removeFavourite}>Remove</button>
		{/if}
	</section>

	{#if actionError}
		<InlineBanner tone="warn">{actionError}</InlineBanner>
	{/if}
	{#if catalog.error}
		<InlineBanner tone="warn">
			{catalog.error}
			{#snippet actions()}
				<button type="button" class="st-btn sm" onclick={() => catalog.load()}>Retry</button>
			{/snippet}
		</InlineBanner>
	{/if}

	{#if catalog.entries === null}
		{#if !catalog.error}
			<div class="skeleton" aria-busy="true" aria-label="Loading accounts">
				<span></span><span></span><span></span>
			</div>
		{/if}
	{:else if !entries.length}
		{#if catalog.query}
			<p class="empty">Nothing matches “{catalog.query}” for {label.plural}.</p>
		{:else}
			<div class="empty">
				<p>No connected account can make {label.plural} yet.</p>
				<button type="button" class="st-btn sm" onclick={() => settingsNav.go({ category: 'providers' })}>Open providers</button>
			</div>
		{/if}
	{:else}
		<div class="groups" class:stale={catalog.loading}>
			{#each groups as group (group.worker)}
				{@const where = presence(group.worker)}
				<RowGroup title={workerLabel(group.worker, installs, self)} count="{group.entries.length} {group.entries.length === 1 ? 'route' : 'routes'}">
					{#snippet actions()}
						<span class="where" data-online={where.online}>
							{#if where.online !== null}<StatusDot tone={where.online ? 'ok' : 'off'} />{/if}{where.text}
						</span>
					{/snippet}
					{#each group.entries as entry (keyOf(entry))}
						{@const status = STATUS[entry.status] ?? STATUS.unavailable}
						{@const price = priceView(entry.price, kind)}
						{@const isFav = !!favourite && sameChoice(favourite, entry)}
						{@const isRemembered = !!remembered && sameChoice(remembered, entry)}
						<div class="arow" data-status={entry.status} data-catalog-row={keyOf(entry)} class:fav-row={isFav}>
							<StatusDot tone={status.tone} />
							<div class="main">
								<div class="top">
									<span class="src">{accountText(entry)}</span>
									<span class="pill {entry.status}">{status.text}</span>
									{#if entry.verified}<span class="pill quiet" title="A verified route and option set"><ShieldCheck aria-hidden="true" />Verified</span>{/if}
									{#if isRemembered}<span class="pill accent">Remembered</span>{/if}
								</div>
								<div class="model">
									<span class="mname">{shownModel(entry.model)}</span>
									{#if entry.model !== 'unavailable'}<span class="st-mono mid">{entry.model}</span>{/if}
								</div>
								{#if entry.status !== 'available'}<p class="why">{why(entry)}</p>{/if}
							</div>
							<div class="price" class:unknown={!price.priced}>
								{#if entry.status === 'unavailable'}
									<span class="fig">—</span><span class="note">cannot run</span>
								{:else}
									<span class="fig">{price.figure}</span><span class="note">{price.note}</span>
								{/if}
							</div>
							<button
								type="button"
								class="star"
								class:on={isFav}
								aria-pressed={isFav}
								aria-label={isFav
									? `Remove favourite: ${accountText(entry)} · ${shownModel(entry.model)}`
									: `Favourite: ${accountText(entry)} · ${shownModel(entry.model)}`}
								title={starrable(entry) ? (isFav ? 'Remove favourite' : `Favourite for ${label.plural}`) : 'No model to bookmark'}
								disabled={!starrable(entry) || busyKey !== null}
								onclick={() => toggleFavourite(entry)}
							>
								<Star aria-hidden="true" />
							</button>
						</div>
					{/each}
				</RowGroup>
			{/each}
		</div>
	{/if}

	<p class="foot">
		Prices are live estimates for each model's default options; a run is priced again before it spends. Up to 15 models
		per source on each worker; search to find more fal.ai models.
	</p>
</div>

<style>
	.acat {
		display: grid;
		gap: 12px;
		margin-top: 18px;
		container-type: inline-size;
	}

	/* ── Kind switch ─────────────────────────────────────────────── */
	.kinds {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.kind {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		min-height: 32px;
		padding: 0 12px;
		border-radius: 999px;
		border: 1px solid var(--st-border-strong);
		background: none;
		color: var(--st-mfg);
		font-size: 12.5px;
		font-weight: 500;
		transition:
			color 0.15s ease,
			background-color 0.15s ease;
	}
	.kind :global(svg) {
		width: 14px;
		height: 14px;
	}
	.kind:hover:not([aria-pressed='true']) {
		color: var(--st-fg);
		background: var(--st-hover);
	}
	.kind[aria-pressed='true'] {
		background: var(--st-fg);
		color: var(--st-bg);
		border-color: transparent;
	}

	/* ── Favourite: a bookmark ribbon, apart from the remembered choice ── */
	.fav {
		position: relative;
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		gap: 12px;
		align-items: start;
		padding: 14px 16px 14px 14px;
		border: 1px dashed var(--st-border-strong);
		border-radius: var(--st-radius);
		background: var(--st-card);
	}
	.fav.set {
		border-style: solid;
		border-color: var(--st-warning-line);
		background:
			linear-gradient(100deg, var(--st-warning-soft), transparent 55%),
			var(--st-card);
	}
	.fav-mark {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: var(--st-radius-sm);
		color: var(--st-subtle);
		background: var(--st-muted);
	}
	.fav.set .fav-mark {
		color: var(--st-warning);
		background: var(--st-warning-soft);
	}
	.fav-mark :global(svg) {
		width: 16px;
		height: 16px;
	}
	.fav.set .fav-mark :global(svg) {
		fill: currentColor;
	}
	.fav-body {
		min-width: 0;
	}
	h4 {
		margin: 0;
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--st-mfg);
	}
	.fav-pick {
		margin: 3px 0 0;
		font-size: 14px;
		font-weight: 600;
		overflow-wrap: anywhere;
	}
	.fav-pick.none {
		font-weight: 400;
		font-size: 13px;
		color: var(--st-mfg);
	}
	.fav-meta {
		margin: 2px 0 0;
		font-size: 12px;
		color: var(--st-mfg);
		font-variant-numeric: tabular-nums;
	}
	.fav-note {
		margin: 8px 0 0;
		font-size: 12px;
		line-height: 1.5;
		color: var(--st-mfg);
	}
	.fav-link {
		display: inline-flex;
		align-items: center;
		gap: 2px;
		padding: 0;
		border: 0;
		background: none;
		color: var(--st-fg);
		font: inherit;
		text-decoration: underline;
		text-decoration-color: color-mix(in oklch, currentColor 35%, transparent);
		text-underline-offset: 3px;
	}
	.fav-link :global(svg) {
		width: 12px;
		height: 12px;
	}
	.fav-remove {
		align-self: center;
	}

	/* ── Worker groups and rows ──────────────────────────────────── */
	.groups {
		transition: opacity 0.2s ease;
	}
	.groups.stale {
		opacity: 0.6;
	}
	.groups :global(.st-grp.detail:first-child) {
		margin-top: 6px;
	}
	.where {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		color: var(--st-mfg);
	}
	.arow {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto auto;
		align-items: center;
		gap: 4px 12px;
		min-height: 56px;
		padding: 10px 8px 10px 16px;
		animation: acat-rise 0.28s cubic-bezier(0.2, 0.7, 0.2, 1) both;
	}
	.arow + .arow {
		border-top: 1px solid var(--st-border);
	}
	.arow > :global(.st-dot) {
		align-self: start;
		margin-top: 6px;
	}
	.arow[data-status='unavailable'] .main {
		opacity: 0.62;
	}
	.arow.fav-row {
		background: linear-gradient(90deg, var(--st-warning-soft), transparent 40%);
	}
	.main {
		min-width: 0;
	}
	.top {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 4px 6px;
		min-width: 0;
	}
	.src {
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--st-mfg);
		overflow-wrap: anywhere;
	}
	.pill {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		padding: 1px 7px;
		border-radius: 999px;
		font-size: 11px;
		font-weight: 500;
		white-space: nowrap;
		background: var(--st-muted);
		color: var(--st-mfg);
	}
	.pill :global(svg) {
		width: 11px;
		height: 11px;
	}
	.pill.available {
		color: var(--st-success);
		background: color-mix(in oklch, var(--st-success) 12%, transparent);
	}
	.pill.unpriced {
		color: var(--st-warning);
		background: var(--st-warning-soft);
	}
	.pill.accent {
		color: var(--st-primary);
		background: var(--st-primary-soft);
	}
	.model {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 2px 8px;
		margin-top: 2px;
		min-width: 0;
	}
	.mname {
		font-size: 13.5px;
		font-weight: 500;
	}
	.mid {
		font-size: 11.5px;
		color: var(--st-subtle);
		overflow-wrap: anywhere;
		min-width: 0;
	}
	.why {
		margin: 3px 0 0;
		font-size: 12px;
		line-height: 1.4;
		color: var(--st-mfg);
	}
	.price {
		display: grid;
		justify-items: end;
		text-align: right;
		min-width: 64px;
	}
	.fig {
		font-family: var(--st-mono);
		font-size: 14px;
		font-weight: 600;
		letter-spacing: -0.02em;
		font-variant-numeric: tabular-nums;
	}
	.note {
		font-size: 11px;
		color: var(--st-mfg);
		white-space: nowrap;
	}
	.price.unknown .fig {
		color: var(--st-mfg);
	}
	.star {
		display: grid;
		place-items: center;
		width: 40px;
		height: 40px;
		border: 0;
		border-radius: var(--st-radius-sm);
		background: none;
		color: var(--st-subtle);
		transition:
			color 0.15s ease,
			background-color 0.15s ease,
			transform 0.12s ease;
	}
	.star :global(svg) {
		width: 17px;
		height: 17px;
		transition: transform 0.2s cubic-bezier(0.3, 1.4, 0.5, 1);
	}
	.star:hover:not(:disabled) {
		color: var(--st-fg);
		background: var(--st-hover);
	}
	.star:active:not(:disabled) {
		transform: scale(0.92);
	}
	.star.on {
		color: var(--st-warning);
	}
	.star.on :global(svg) {
		fill: currentColor;
		transform: scale(1.08);
	}
	.star:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}

	/* ── States ──────────────────────────────────────────────────── */
	.skeleton {
		display: grid;
		gap: 8px;
		margin-top: 6px;
	}
	.skeleton span {
		height: 56px;
		border-radius: var(--st-radius-md);
		background: linear-gradient(90deg, var(--st-muted), color-mix(in oklch, var(--st-muted) 40%, transparent), var(--st-muted));
		background-size: 200% 100%;
		animation: acat-shimmer 1.2s ease-in-out infinite;
	}
	.empty {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 10px;
		margin: 0;
		padding: 12px 2px;
		font-size: 13px;
		color: var(--st-mfg);
	}
	.empty p {
		margin: 0;
	}
	.foot {
		margin: 4px 0 0;
		font-size: 12px;
		line-height: 1.5;
		color: var(--st-subtle);
	}

	/* Narrow panes (phone, split view): the price joins the status line. */
	@container (max-width: 440px) {
		.arow {
			grid-template-columns: auto minmax(0, 1fr) auto;
			grid-template-areas: 'dot main star' 'dot price star';
			padding-left: 12px;
		}
		.arow > :global(.st-dot) {
			grid-area: dot;
		}
		.main {
			grid-area: main;
		}
		.price {
			grid-area: price;
			display: flex;
			align-items: baseline;
			gap: 6px;
			justify-items: start;
			text-align: left;
		}
		.star {
			grid-area: star;
			width: 44px;
			height: 44px;
		}
		.fav {
			grid-template-columns: auto minmax(0, 1fr);
		}
		.fav-remove {
			grid-column: 2;
			justify-self: start;
		}
	}

	@keyframes acat-rise {
		from {
			opacity: 0;
			transform: translateY(4px);
		}
	}
	@keyframes acat-shimmer {
		to {
			background-position: -200% 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.arow,
		.skeleton span {
			animation: none;
		}
		.star,
		.star :global(svg),
		.kind,
		.groups {
			transition: none;
		}
	}
</style>
