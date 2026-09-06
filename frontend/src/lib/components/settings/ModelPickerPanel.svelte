<script lang="ts">
	/**
	 * ModelPickerPanel — the T3 Code-style picker body shared by the profile /
	 * agent editor (`ModelPicker`), the chat composer dock (`ControlsDock`)
	 * and any other popover that needs to choose a model.
	 *
	 * Layout mirrors T3's `ModelPickerContent`:
	 *   ┌──────┬──────────────────────────────┐
	 *   │ ★    │ 🔍 Search models…            │
	 *   │ ──── ├──────────────────────────────┤
	 *   │ (C)  │ Fable                     ☆  │
	 *   │ (O)  │ (C) Claude · max             │
	 *   │ (G)  │ Opus                      ★  │
	 *   │      │ (C) Claude · max             │
	 *   └──────┴──────────────────────────────┘
	 * A narrow icon rail on the LEFT lists Favorites plus one brand glyph per
	 * provider group; the RIGHT column is a borderless search box over a flat
	 * list of two-line rows (model name / provider glyph + brand · auth). The
	 * rail is hidden while searching (results span every provider) and when
	 * there is only one group to choose from.
	 *
	 * CONTROLLED: renders from `value`, emits via `onSelect`. The panel has no
	 * trigger and no positioning — the parent owns the popover chrome and sizes
	 * the panel via `--mpp-height` / `--mpp-width`.
	 */
	import { Check, LayoutGrid, Search, Star } from 'lucide-svelte';
	import { tick } from 'svelte';
	import ProviderGlyph from './ProviderGlyph.svelte';
	import type { PickerGroup, PickerOption } from '$lib/constants/model-picker';
	import { matchesPickerQuery } from '$lib/constants/model-picker';
	import { getModelLabel } from '$lib/constants/models';
	import {
		getModelContextControl,
		type ContextWindowChoice
	} from '$lib/constants/model-context';
	import {
		getFavoriteModels,
		isFavoriteModel,
		toggleFavoriteModel
	} from '$lib/stores/modelFavorites.svelte';

	interface ExtraRow {
		value: string;
		label: string;
	}

	interface Props {
		groups: PickerGroup[];
		options: PickerOption[];
		value: string;
		onSelect: (value: string) => void;
		/** Rows pinned above the list in every view (e.g. "Unavailable as subagent"). */
		leadingRows?: ExtraRow[];
		/** Rows pinned below the list in every view (e.g. "Custom…"). */
		trailingRows?: ExtraRow[];
		/** One-line warning rendered above the list (e.g. a reroute explanation). */
		notice?: string;
		ariaLabel?: string;
		/** Optional session context control. Omit both props on profile-only pickers. */
		contextChoice?: ContextWindowChoice;
		onContextChoiceSelect?: (choice: ContextWindowChoice) => void;
		/** Escape pressed inside the search box. */
		onRequestClose?: () => void;
		/**
		 * Open on an All-providers list instead of the group that owns `value`.
		 * Subagent pickers use this so a Claude (or Grok, …) row still shows
		 * every connected provider's models without a rail click.
		 */
		startOnAll?: boolean;
	}

	let {
		groups,
		options,
		value,
		onSelect,
		leadingRows = [],
		trailingRows = [],
		notice,
		ariaLabel = 'Model',
		contextChoice,
		onContextChoiceSelect,
		onRequestClose,
		startOnAll = false
	}: Props = $props();

	const FAVORITES = '__favorites__';
	const ALL = '__all__';

	let query = $state('');
	let searchEl = $state<HTMLInputElement | null>(null);
	let listEl = $state<HTMLDivElement | null>(null);

	const isSearching = $derived(query.trim().length > 0);

	// Favorites that actually exist in this panel's catalog (a pin made in the
	// chat dock for a provider this scoped picker can't offer is simply skipped).
	const favoriteOptions = $derived(
		getFavoriteModels()
			.map((v) => options.find((o) => o.value === v))
			.filter((o): o is PickerOption => o != null)
	);
	const hasFavorites = $derived(favoriteOptions.length > 0);
	const contextControl = $derived(
		contextChoice && onContextChoiceSelect ? getModelContextControl(value) : null
	);

	// ---- rail selection -------------------------------------------------------
	// Initial rail pick: All (when requested), else favorites when any exist,
	// else the group owning the current value, else the first group.
	// Re-validated whenever the catalog changes so a vanished provider can't
	// leave the rail pointing at nothing.
	let railKey = $state<string | null>(null);
	const currentGroupKey = $derived(options.find((o) => o.value === value)?.groupKey ?? null);
	const effectiveRailKey = $derived.by(() => {
		// Favorites stays addressable even while empty, so its rail button has a
		// useful empty state instead of disappearing after the final unstar.
		if (railKey === FAVORITES) return FAVORITES;
		if (railKey === ALL) return ALL;
		if (railKey && groups.some((g) => g.key === railKey)) return railKey;
		if (startOnAll) return ALL;
		if (currentGroupKey) return currentGroupKey;
		if (hasFavorites) return FAVORITES;
		return groups[0]?.key ?? null;
	});

	// The Favorites tab is a stable first-class rail destination, including in a
	// one-provider scoped picker. Search still spans the full width.
	const showRail = $derived(!isSearching && groups.length > 0);

	// ---- visible rows ---------------------------------------------------------
	const visibleOptions = $derived.by<PickerOption[]>(() => {
		if (isSearching) return options.filter((o) => matchesPickerQuery(o, query));
		if (effectiveRailKey === FAVORITES) return favoriteOptions;
		if (effectiveRailKey === ALL) return options;
		return options.filter((o) => o.groupKey === effectiveRailKey);
	});
	const visibleLeading = $derived(isSearching ? [] : leadingRows);
	const visibleTrailing = $derived(isSearching ? [] : trailingRows);

	// Provider subline only earns its space when the list can mix providers
	// (searching / favorites) or the rail is hidden and the user has no other
	// cue; on a single-provider page it would repeat the rail's selection.
	const showProviderLine = $derived(
		isSearching || effectiveRailKey === FAVORITES || effectiveRailKey === ALL || !showRail
	);

	function groupFor(o: PickerOption): PickerGroup | undefined {
		return groups.find((g) => g.key === o.groupKey);
	}

	// ---- keyboard navigation --------------------------------------------------
	// Flat index across leading → options → trailing so ↑/↓ + Enter work from the
	// search box without moving focus off it (T3 behaviour).
	const rowValues = $derived<string[]>([
		...visibleLeading.map((r) => r.value),
		...visibleOptions.map((o) => o.value),
		...visibleTrailing.map((r) => r.value)
	]);
	let highlighted = $state<string | null>(null);
	const effectiveHighlighted = $derived(
		highlighted !== null && rowValues.includes(highlighted)
			? highlighted
			: rowValues.includes(value)
				? value
				: (rowValues[0] ?? null)
	);

	async function moveHighlight(delta: number) {
		if (rowValues.length === 0) return;
		const idx = effectiveHighlighted ? rowValues.indexOf(effectiveHighlighted) : -1;
		const next = (idx + delta + rowValues.length) % rowValues.length;
		highlighted = rowValues[next];
		await tick();
		listEl
			?.querySelector<HTMLElement>(`[data-value="${CSS.escape(highlighted)}"]`)
			?.scrollIntoView({ block: 'nearest' });
	}

	function onSearchKeydown(e: KeyboardEvent) {
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			void moveHighlight(1);
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			void moveHighlight(-1);
		} else if (e.key === 'Enter') {
			if (effectiveHighlighted !== null) {
				e.preventDefault();
				onSelect(effectiveHighlighted);
			}
		} else if (e.key === 'Escape') {
			if (query) {
				e.preventDefault();
				e.stopPropagation();
				query = '';
			} else if (onRequestClose) {
				e.preventDefault();
				e.stopPropagation();
				onRequestClose();
			}
		}
	}

	function pickRail(key: string) {
		railKey = key;
		highlighted = null;
	}

	function toggleFavorite(e: MouseEvent, optionValue: string) {
		e.stopPropagation();
		toggleFavoriteModel(optionValue);
	}

	function onContextKeydown(e: KeyboardEvent) {
		if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(e.key)) return;
		e.preventDefault();
		e.stopPropagation();
		const next: ContextWindowChoice = e.key === 'Home' ? 'standard'
			: e.key === 'End' ? '1m'
			: contextChoice === 'standard' ? '1m' : 'standard';
		onContextChoiceSelect?.(next);
		(e.currentTarget as HTMLElement)
			.querySelector<HTMLButtonElement>(`[data-context-choice="${next}"]`)?.focus();
	}

</script>

<div class="mpp" data-model-picker-panel>
	<div class="mpp-body">
	{#if showRail}
		<div class="mpp-rail" role="tablist" aria-label="Providers">
			<button
				type="button"
				role="tab"
				class="mpp-rail-btn"
				class:selected={effectiveRailKey === FAVORITES}
				aria-selected={effectiveRailKey === FAVORITES}
				aria-label={hasFavorites ? `Favorites, ${favoriteOptions.length} models` : 'Favorites, empty'}
				title="Favorites"
				onclick={() => pickRail(FAVORITES)}
			>
				<Star size={18} class="mpp-rail-star" />
			</button>
			<div class="mpp-rail-sep" aria-hidden="true"></div>
			{#if startOnAll}
				<button
					type="button"
					role="tab"
					class="mpp-rail-btn"
					class:selected={effectiveRailKey === ALL}
					aria-selected={effectiveRailKey === ALL}
					aria-label="All providers"
					title="All providers"
					onclick={() => pickRail(ALL)}
				>
					<LayoutGrid size={18} />
				</button>
			{/if}
			{#each groups as g (g.key)}
				<button
					type="button"
					role="tab"
					class="mpp-rail-btn"
					class:selected={effectiveRailKey === g.key}
					aria-selected={effectiveRailKey === g.key}
					aria-label={g.subtext ? `${g.name} · ${g.subtext}` : g.name}
					title={g.subtext ? `${g.name} · ${g.subtext}` : g.name}
					onclick={() => pickRail(g.key)}
				>
					<span class="mpp-rail-icon">
						<ProviderGlyph kind={g.glyph} size={18} />
						{#if g.isApi}<span class="mpp-api-badge" data-provider-api-badge aria-hidden="true">API</span>{/if}
					</span>
				</button>
			{/each}
		</div>
	{/if}

	<div class="mpp-main" class:with-rail={showRail}>
		<div class="mpp-search">
			<Search size={15} class="mpp-search-icon" />
			<input
				bind:this={searchEl}
				bind:value={query}
				type="text"
				class="mpp-search-inp"
				placeholder="Search models..."
				aria-label="Search models"
				spellcheck="false"
				autocapitalize="off"
				autocomplete="off"
				onkeydown={onSearchKeydown}
			/>
		</div>

		{#if notice}
			<div class="mpp-notice">{notice}</div>
		{/if}

		<div class="mpp-list" role="listbox" aria-label={ariaLabel} bind:this={listEl}>
			{#each visibleLeading as r (r.value)}
				{@render plainRow(r)}
			{/each}

			{#each visibleOptions as o (o.value)}
				{@const g = groupFor(o)}
				{@const fav = isFavoriteModel(o.value)}
				<!-- svelte-ignore a11y_click_events_have_key_events -->
				<div
					class="mpp-row"
					class:selected={value === o.value}
					class:highlighted={effectiveHighlighted === o.value}
					role="option"
					aria-selected={value === o.value}
					tabindex="-1"
					data-value={o.value}
					title={o.description}
					onclick={() => onSelect(o.value)}
					onmouseenter={() => (highlighted = o.value)}
				>
					<div class="mpp-row-copy">
						<div class="mpp-row-label">{o.label}</div>
						{#if showProviderLine && g}
							<div class="mpp-row-sub">
								<ProviderGlyph kind={g.glyph} size={12} />
								<span>{g.name}{#if g.subtext}<span class="mpp-row-subtext"> · {g.subtext}</span>{/if}</span>
							</div>
						{/if}
					</div>
					<button
						type="button"
						class="mpp-fav"
						class:on={fav}
						aria-label={fav ? `Remove ${o.label} from favorites` : `Add ${o.label} to favorites`}
						title={fav ? 'Remove from favorites' : 'Add to favorites'}
						onclick={(e) => toggleFavorite(e, o.value)}
					>
						<Star size={14} />
					</button>
				</div>
			{/each}

			{#if visibleOptions.length === 0 && visibleLeading.length === 0}
				<div class="mpp-empty">
					{#if isSearching}
						No models found
					{:else if effectiveRailKey === FAVORITES}
						No favorites yet — star a model to pin it here.
					{:else}
						No models available
					{/if}
				</div>
			{/if}

			{#each visibleTrailing as r (r.value)}
				{@render plainRow(r)}
			{/each}
		</div>
	</div>
	</div>

	{#if contextControl && contextChoice && onContextChoiceSelect}
		<div class="mpp-context" data-model-context-control>
			<div class="mpp-context-copy">
				<span>Context window</span>
				<small title={getModelLabel(value)}>{getModelLabel(value)}</small>
			</div>
			<div
				class="mpp-context-toggle"
				role="radiogroup"
				aria-label="Context window"
				tabindex="-1"
				onkeydown={onContextKeydown}
			>
				<button
					type="button"
					role="radio"
					aria-checked={contextChoice === 'standard'}
					aria-label="{contextControl.standardLabel} · Standard"
					tabindex={contextChoice === 'standard' ? 0 : -1}
					data-context-choice="standard"
					class:on={contextChoice === 'standard'}
					onclick={() => onContextChoiceSelect('standard')}
				>
					<span class="mpp-context-option-copy">
						<span class="mpp-context-value">{contextControl.standardLabel}</span>
						<span class="mpp-context-caption">Standard</span>
					</span>
					<Check size={14} class="mpp-context-check" aria-hidden="true" />
				</button>
				<button
					type="button"
					role="radio"
					aria-checked={contextChoice === '1m'}
					aria-label="{contextControl.extendedLabel} · Extended"
					tabindex={contextChoice === '1m' ? 0 : -1}
					data-context-choice="1m"
					class:on={contextChoice === '1m'}
					onclick={() => onContextChoiceSelect('1m')}
				>
					<span class="mpp-context-option-copy">
						<span class="mpp-context-value">{contextControl.extendedLabel}</span>
						<span class="mpp-context-caption">Extended</span>
					</span>
					<Check size={14} class="mpp-context-check" aria-hidden="true" />
				</button>
			</div>
		</div>
	{/if}
</div>

{#snippet plainRow(r: ExtraRow)}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div
		class="mpp-row mpp-row-plain"
		class:selected={value === r.value}
		class:highlighted={effectiveHighlighted === r.value}
		role="option"
		aria-selected={value === r.value}
		tabindex="-1"
		data-value={r.value}
		onclick={() => onSelect(r.value)}
		onmouseenter={() => (highlighted = r.value)}
	>
		<div class="mpp-row-copy">
			<div class="mpp-row-label">{r.label}</div>
		</div>
	</div>
{/snippet}

<style>
	.mpp {
		--mpp-hover: color-mix(in srgb, var(--popover) 90%, var(--foreground));
		display: flex;
		flex-direction: column;
		width: var(--mpp-width, 360px);
		max-width: 100%;
		height: var(--mpp-height, 346px);
		max-height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--popover);
		color: var(--popover-foreground);
		font-size: 12.5px;
	}

	/* Rail + list scroll together; the context footer is a sibling so it cannot
	   be clipped by this overflow box (the desktop composer popover is short). */
	.mpp-body {
		display: flex;
		flex-direction: row;
		flex: 1 1 auto;
		min-width: 0;
		min-height: 0;
		overflow: hidden;
	}

	/* ---- rail ------------------------------------------------------------- */
	.mpp-rail {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 4px;
		width: 44px;
		flex-shrink: 0;
		padding: 4px;
		overflow-y: auto;
		overscroll-behavior: contain;
		scrollbar-width: none;
		background: color-mix(in srgb, var(--muted) 30%, transparent);
	}
	.mpp-rail::-webkit-scrollbar {
		display: none;
	}
	.mpp-rail-btn {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 100%;
		aspect-ratio: 1;
		padding: 0;
		border: 0;
		border-radius: 7px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 0.12s ease, color 0.12s ease;
	}
	.mpp-rail-btn:hover,
	.mpp-rail-btn:focus-visible {
		background: var(--mpp-hover);
		color: var(--foreground);
		outline: none;
	}
	.mpp-rail-btn.selected {
		color: var(--foreground);
	}
	/* T3's selected indicator: a short primary bar hugging the rail's right edge. */
	.mpp-rail-btn.selected::after {
		content: '';
		position: absolute;
		right: -4px;
		top: 50%;
		width: 3px;
		height: 20px;
		transform: translateY(-50%);
		border-radius: 999px 0 0 999px;
		background: var(--primary);
	}
	.mpp-rail-btn :global(.mpp-rail-star) {
		fill: currentColor;
	}
	.mpp-rail-icon {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		border-radius: 999px;
		background: color-mix(in oklab, var(--muted) 60%, transparent);
	}
	.mpp-rail-btn.selected .mpp-rail-icon {
		background: var(--background);
	}
	.mpp-api-badge {
		position: absolute;
		left: 50%;
		bottom: -3px;
		transform: translateX(-50%);
		min-width: 16px;
		padding: 1px 2px;
		border: 1px solid var(--popover);
		border-radius: 999px;
		background: var(--primary);
		color: var(--primary-foreground);
		font-size: 6px;
		font-weight: 800;
		line-height: 7px;
		letter-spacing: 0.04em;
		text-align: center;
		box-shadow: 0 1px 2px color-mix(in srgb, var(--foreground) 18%, transparent);
	}
	.mpp-rail-sep {
		border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
		margin: 0 2px;
	}

	/* ---- main column ---------------------------------------------------- */
	.mpp-main {
		display: flex;
		flex: 1;
		flex-direction: column;
		min-width: 0;
		min-height: 0;
		background: color-mix(in srgb, var(--muted) 40%, transparent);
	}
	.mpp-main.with-rail {
		border-left: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
	}

	.mpp-search {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 8px 8px 0;
		padding: 0 2px 10px;
		border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
		color: var(--muted-foreground);
		flex-shrink: 0;
		transition: border-color 0.12s ease;
	}
	.mpp-search:focus-within {
		border-bottom-color: var(--ring);
	}
	.mpp-search :global(.mpp-search-icon) {
		flex-shrink: 0;
		opacity: 0.7;
	}
	.mpp-search-inp {
		flex: 1;
		min-width: 0;
		height: 26px;
		background: none;
		border: none;
		outline: none;
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
	}
	.mpp-search-inp::placeholder {
		color: var(--muted-foreground);
	}

	.mpp-notice {
		margin: 8px 8px 0;
		padding: 6px 8px;
		font-size: 11px;
		line-height: 1.3;
		font-weight: 500;
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 12%, transparent);
		border: 1px solid color-mix(in oklch, var(--warning) 40%, transparent);
		border-radius: 6px;
		flex-shrink: 0;
	}

	.mpp-list {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		overscroll-behavior: contain;
		padding: 6px 6px 6px 8px;
		display: flex;
		flex-direction: column;
		gap: 2px;
		scrollbar-gutter: stable;
	}

	/* ---- rows ------------------------------------------------------------ */
	.mpp-row {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 8px 8px;
		border-radius: 6px;
		cursor: pointer;
		user-select: none;
		transition: background-color 0.1s ease;
	}
	.mpp-row.highlighted,
	.mpp-row:hover {
		background: var(--mpp-hover);
	}
	.mpp-row.selected {
		background: color-mix(in srgb, var(--foreground) 8%, transparent);
	}
	.mpp-row.selected.highlighted,
	.mpp-row.selected:hover {
		background: var(--mpp-hover);
	}
	.mpp-row-plain {
		color: var(--muted-foreground);
	}
	.mpp-row-plain.selected {
		color: var(--foreground);
	}
	.mpp-row-copy {
		flex: 1;
		min-width: 0;
		text-align: left;
	}
	.mpp-row-label {
		font-size: 12.5px;
		font-weight: 500;
		line-height: 1.35;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.mpp-row-sub {
		display: flex;
		align-items: center;
		gap: 6px;
		margin-top: 3px;
		font-size: 11.5px;
		line-height: 1.35;
		color: color-mix(in srgb, var(--muted-foreground) 75%, transparent);
		overflow: hidden;
		white-space: nowrap;
	}
	.mpp-row-sub > span {
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.mpp-row-subtext {
		opacity: 0.85;
	}

	.mpp-fav {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		width: 28px;
		height: 28px;
		margin: -2px -4px -2px 0;
		padding: 0;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		opacity: 0.55;
		cursor: pointer;
		transition: opacity 0.12s ease, color 0.12s ease;
	}
	.mpp-row:hover .mpp-fav,
	.mpp-row.highlighted .mpp-fav {
		opacity: 1;
	}
	.mpp-fav:hover {
		color: var(--foreground);
		background: var(--mpp-hover);
	}
	.mpp-fav:focus-visible {
		opacity: 1;
		outline: 2px solid var(--ring);
		outline-offset: -1px;
	}
	.mpp-fav.on {
		opacity: 1;
		color: #eab308;
	}
	.mpp-fav.on :global(svg) {
		fill: currentColor;
	}

	/* Context selector is pinned below the independently scrolling model list. */
	.mpp-context {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 10px;
		border-top: 1px solid color-mix(in srgb, var(--border) 75%, transparent);
		background: color-mix(in srgb, var(--popover) 92%, var(--muted));
		flex-shrink: 0;
	}
	.mpp-context-copy {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		min-width: 0;
		gap: 12px;
		color: var(--foreground);
		font-size: 11px;
		font-weight: 600;
		line-height: 1.4;
	}
	.mpp-context-copy > span {
		flex-shrink: 0;
	}
	.mpp-context-copy small {
		min-width: 0;
		overflow: hidden;
		color: var(--muted-foreground);
		font-size: 10.5px;
		font-weight: 400;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.mpp-context-toggle {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 4px;
		padding: 3px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: color-mix(in srgb, var(--muted) 45%, transparent);
	}
	.mpp-context-toggle button {
		display: inline-flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		min-width: 0;
		min-height: 44px;
		padding: 6px 10px;
		border: 1px solid transparent;
		border-radius: 7px;
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 12px;
		font-weight: 600;
		text-align: left;
		cursor: pointer;
		transition: background-color 0.12s ease, border-color 0.12s ease, color 0.12s ease;
	}
	.mpp-context-option-copy {
		display: flex;
		flex-direction: column;
		gap: 2px;
		line-height: 1.2;
	}
	.mpp-context-value {
		font-size: 13px;
		font-variant-numeric: tabular-nums;
	}
	.mpp-context-caption {
		color: var(--muted-foreground);
		font-size: 10.5px;
		font-weight: 400;
	}
	.mpp-context-toggle :global(.mpp-context-check) {
		flex-shrink: 0;
		color: var(--primary);
		opacity: 0;
	}
	.mpp-context-toggle button:hover {
		background: var(--mpp-hover);
		color: var(--foreground);
	}
	.mpp-context-toggle button.on {
		border-color: color-mix(in srgb, var(--primary) 35%, var(--border));
		background: color-mix(in srgb, var(--primary) 8%, var(--popover));
		color: var(--foreground);
		box-shadow: 0 1px 3px color-mix(in srgb, var(--foreground) 6%, transparent);
	}
	.mpp-context-toggle button.on :global(.mpp-context-check) {
		opacity: 1;
	}
	.mpp-context-toggle button:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.mpp-empty {
		padding: 24px 8px;
		text-align: center;
		font-size: 12px;
		color: var(--muted-foreground);
	}
</style>
