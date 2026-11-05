<script lang="ts">
	/**
	 * CollectionPage: a list beside a detail pane, split by a draggable
	 * divider (list 384px by default, width remembered).
	 *
	 *   <CollectionPage category="logs" count="42 of 12,408" filterPlaceholder="Search logs" bind:filter>
	 *     {#snippet list()}<ListItem id="l1" name="..." />{/snippet}
	 *     {#snippet detail()}<DetailPane name="...">...</DetailPane>{/snippet}
	 *   </CollectionPage>
	 *
	 * Selection lives in the navigation store per category (ListItem reads and
	 * sets it). In stacked mode the list and the detail are separate steps.
	 */
	import { tick, untrack, type Snippet } from 'svelte';
	import { ListFilter, Plus, Search } from 'lucide-svelte';
	import BackRow from './BackRow.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { categoryById } from '$lib/settings/categories';
	import { clampListWidth, readListWidth } from '$lib/settings/list-width';
	import type { CategoryId } from '$lib/settings/types';
	import ListDivider from './ListDivider.svelte';
	import PageTitle from './PageTitle.svelte';
	import ListEmptyScope from './ListEmptyScope.svelte';

	interface Props {
		/** Settings category; pages outside Settings pass `title` instead. */
		category?: CategoryId;
		/** Defaults to the category label. */
		title?: string;
		/** Shown after the title ("4 connected"). */
		count?: string | number;
		addLabel?: string;
		onAdd?: () => void;
		addDisabled?: boolean;
		/** Extra controls under the title (log level, refresh, export). */
		toolbar?: Snippet;
		/** null hides the filter box. */
		filterPlaceholder?: string | null;
		/** `search` icon instead of the filter icon (Logs). */
		filterIcon?: 'filter' | 'search';
		filter?: string;
		/** Controls under the filter box (segmented filters, a folder select). */
		filters?: Snippet;
		/** Back button in stacked mode; null hides it (the list is the first step). */
		onBack?: (() => void) | null;
		/** Accessible name of the list region. */
		listLabel?: string;
		/**
		 * Opening an item replaces the list at every width, with the detail's back
		 * control returning to it (what stacked mode does on a phone).
		 */
		replaceOnSelect?: boolean;
		list: Snippet;
		detail: Snippet;
	}

	let {
		category,
		title,
		count,
		addLabel,
		onAdd,
		addDisabled = false,
		toolbar,
		filterPlaceholder,
		filterIcon = 'filter',
		filter = $bindable(''),
		filters,
		onBack,
		listLabel,
		replaceOnSelect = false,
		list,
		detail
	}: Props = $props();

	const access = getSettingsAccess();
	const label = $derived(title ?? (category ? categoryById(category).label : ''));
	const placeholder = $derived(
		filterPlaceholder === undefined ? `Filter ${label.toLowerCase()}` : filterPlaceholder
	);

	let el: HTMLDivElement | undefined = $state();
	const openItem = $derived(category ? settingsNav.selectedItem(category) : null);

	// The list and the detail swap in place, so focus has to move with them:
	// into the detail's title, or back to the row it was opened from.
	let lastOpen: string | null | undefined;
	$effect(() => {
		const current = openItem ?? null;
		const previous = lastOpen;
		lastOpen = current;
		if (!replaceOnSelect || settingsNav.stacked || previous === undefined || previous === current) return;
		void tick().then(() => {
			const row = [...(el?.querySelectorAll<HTMLElement>('[data-settings-item]') ?? [])].find(
				(item) => item.dataset.settingsItem === previous
			);
			const target = current
				? el?.querySelector<HTMLElement>('[data-settings-heading="detail"]')
				: row?.matches('[data-settings-nav-item]')
					? row
					: row?.querySelector<HTMLElement>('[data-settings-nav-item]');
			target?.focus({ preventScroll: !current });
		});
	});
	let width = $state(readListWidth());
	let containerWidth = $state(0);

	$effect(() => {
		if (!el || typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver((entries) => {
			containerWidth = entries[0]?.contentRect.width ?? 0;
		});
		ro.observe(el);
		return () => ro.disconnect();
	});

	// Re-clamp (without saving) when the space changes.
	$effect(() => {
		const cw = containerWidth;
		if (!cw) return;
		untrack(() => {
			const next = clampListWidth(width, cw);
			if (next !== width) width = next;
		});
	});
</script>

<div
	class="st-coll"
	bind:this={el}
	style:--st-list-width="{width}px"
	data-replace={replaceOnSelect ? (openItem ? 'detail' : 'list') : undefined}
>
	<ListDivider bind:width {containerWidth} />
	<div class="st-lcol">
		<div class="st-lhead">
			{#if onBack !== null}<BackRow label="Settings" onclick={() => (onBack ? onBack() : settingsNav.back())} />{/if}
			<PageTitle title={label} {count}>
				{#snippet actions()}
				{#if addLabel}
					<button
						type="button"
						class="st-btn primary sm st-add"
						title={addLabel}
						disabled={addDisabled || access.readOnly}
						onclick={() => onAdd?.()}
					>
						<Plus /><span class="st-add-l">{addLabel}</span>
					</button>
				{/if}
				{/snippet}
			</PageTitle>
			{#if toolbar}<div class="st-ltools">{@render toolbar()}</div>{/if}
			{#if placeholder !== null}
				<label class="st-filter">
					{#if filterIcon === 'search'}<Search />{:else}<ListFilter />{/if}
					<span class="st-vh">{placeholder}</span>
					<input type="search" bind:value={filter} {placeholder} autocomplete="off" />
				</label>
			{/if}
			{#if filters}<div class="st-lfilters">{@render filters()}</div>{/if}
		</div>
		<div class="st-lscroll st-scroll" data-settings-list role={listLabel ? 'region' : undefined} aria-label={listLabel}>
			<ListEmptyScope>{@render list()}</ListEmptyScope>
		</div>
	</div>
	<section class="st-dpane st-scroll" aria-label="Details">
		{@render detail()}
	</section>
</div>

<style>
	.st-coll {
		display: grid;
		grid-template-columns: var(--st-list-width, 384px) minmax(0, 1fr);
		flex: 1;
		min-height: 0;
		position: relative;
	}
	.st-lcol {
		/* The list is a container so its header and rows can fit a narrow
		   column (280px when a wide sidebar sits beside Settings). */
		container: st-list / inline-size;
		border-right: 1px solid var(--st-border);
		display: flex;
		flex-direction: column;
		min-height: 0;
		min-width: 0;
	}
	.st-lhead {
		position: relative;
		padding: var(--st-page-top) 8px 12px;
		display: flex;
		flex-direction: column;
	}
	.st-lhead > :global(.st-page-title) { padding-left: 12px; }
	.st-filter + .st-lfilters, .st-ltools + .st-filter { margin-top: 12px; }

	.st-add {
		flex-shrink: 0;
	}
	/* Too narrow for title, count and a labelled button on one line: the
	   button keeps its plus and its accessible name, the label is hidden. */
	@container st-list (max-width: 359px) {
		.st-add {
			width: 28px;
			padding: 0;
			justify-content: center;
		}
		.st-add-l {
			position: absolute;
			width: 1px;
			height: 1px;
			overflow: hidden;
			clip: rect(0 0 0 0);
			white-space: nowrap;
		}
	}
	.st-ltools {
		display: flex;
		gap: 6px;
		align-items: center;
	}
	.st-lfilters {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
		min-width: 0;
	}
	.st-filter {
		display: flex;
		align-items: center;
		gap: 8px;
		height: 32px;
		border: 1px solid var(--st-input);
		background: var(--st-field);
		border-radius: var(--st-radius-md);
		padding: 0 10px 0 11px;
		color: var(--st-mfg);
		min-width: 0;
	}
	.st-filter :global(svg.lucide) {
		width: 15px;
		height: 15px;
	}
	.st-filter input {
		flex: 1;
		border: 0;
		background: none;
		outline: none;
		font-size: 13px;
		min-width: 0;
		width: 100%;
		color: var(--st-fg);
	}
	.st-filter input::-webkit-search-cancel-button {
		-webkit-appearance: none;
	}
	.st-filter input::placeholder {
		color: var(--st-subtle);
	}
	.st-filter:focus-within { outline: 1.5px solid color-mix(in srgb, var(--ring) 60%, transparent); outline-offset: 1px; }
	.st-filter input, .st-filter input:focus, .st-filter input:focus-visible { border: 0; outline: none; box-shadow: none; }
	.st-lscroll {
		flex: 1;
		overflow-y: auto;
		padding: 0 8px 56px;
		min-height: 0;
	}
	.st-dpane {
		/* Rows reflow when the pane is narrow (see settings.css). */
		container: st-detail / inline-size;
		overflow-y: auto;
		min-width: 0;
		min-height: 0;
	}
</style>
