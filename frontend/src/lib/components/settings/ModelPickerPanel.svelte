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
	import { Check, CircleDashed, LayoutGrid, Lock, Search, Star } from 'lucide-svelte';
	import { onDestroy, tick } from 'svelte';
	import ProviderGlyph from './ProviderGlyph.svelte';
	import type { PickerGroup, PickerOption } from '$lib/constants/model-picker';
	import { matchesPickerQuery } from '$lib/constants/model-picker';
	import { getModelLabel } from '$lib/constants/models';
	import {
		CONTEXT_CAP_STEP,
		CONTEXT_CAP_UNIT,
		clampStandardCap,
		formatContextTokens,
		getModelContextControl,
		standardCapPresets,
		type ContextWindowChoice
	} from '$lib/constants/model-context';
	import {
		getFavoriteModels,
		isFavoriteModel,
		toggleFavoriteModel
	} from '$lib/stores/modelFavorites.svelte';
	import {
		MODEL_WINDOW_MAX,
		MODEL_WINDOW_MIN,
		getModelWindowInfo,
		saveModelWindow
	} from '$lib/stores/model-window';

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
		/**
		 * The Standard cap in effect (tokens). With `onContextStandardChange` it
		 * makes Standard adjustable, from the family default up to just below the
		 * full window. Extended is always the model's full window and not editable.
		 */
		contextStandardTokens?: number | null;
		onContextStandardChange?: (tokens: number) => void | Promise<void>;
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
		contextStandardTokens = null,
		onContextStandardChange,
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
	/** The host opted into the context section and the value is one of its models. */
	const contextHosted = $derived(
		onContextChoiceSelect != null && options.some(option => option.value === value)
	);
	const contextControl = $derived(
		contextHosted && contextChoice ? getModelContextControl(value) : null
	);
	/**
	 * Every other hosted model still gets the section: one known window, or an
	 * unknown one (never guessed). Its window is the provider entry's own
	 * `context_window`, which the owner can set from here.
	 */
	const modelWindow = $derived.by(() => {
		if (!contextHosted || contextControl) return null;
		const info = getModelWindowInfo(value);
		return info && info.kind !== 'choice' ? info : null;
	});

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

	/** Rail tooltip: brand and auth subtext. */
	function railLabel(g: PickerGroup): string {
		return g.subtext ? `${g.name} · ${g.subtext}` : g.name;
	}

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
		// A group of one: arrows keep focus on the only radio and change nothing.
		if (!contextControl) return;
		const next: ContextWindowChoice = e.key === 'Home' ? 'standard'
			: e.key === 'End' ? '1m'
			: contextChoice === 'standard' ? '1m' : 'standard';
		onContextChoiceSelect?.(next);
		(e.currentTarget as HTMLElement)
			.querySelector<HTMLButtonElement>(`[data-context-choice="${next}"]`)?.focus();
	}

	// ---- Standard cap editor --------------------------------------------------
	// Standard is a cap the owner can raise: the slider snaps to a 10K grid,
	// the field takes any whole-K value in range, and Extended stays the full
	// window. A local draft keeps the control responsive while the chat's
	// run-settings save is in flight; it drops back to the prop once saved.
	const editorId = $props.id();
	/** Arrow keys arrive in bursts; save the settled value, not every step. */
	const KEY_COMMIT_DELAY_MS = 300;
	const PAGE_STEP = 100_000;

	const capEditable = $derived(
		contextControl != null && onContextStandardChange != null
			&& contextControl.standardMaxTokens > contextControl.standardTokens
	);
	const savedCap = $derived(
		contextControl ? clampStandardCap(contextControl, contextStandardTokens ?? contextControl.standardTokens) : 0
	);
	let capDraft = $state<number | null>(null);
	const shownCap = $derived(contextControl ? (capDraft ?? savedCap) : 0);
	const capRaised = $derived(contextControl != null && shownCap > contextControl.standardTokens);
	const capPresets = $derived(contextControl ? standardCapPresets(contextControl) : []);
	const capFill = $derived.by(() => {
		if (!contextControl) return 0;
		const span = contextControl.standardMaxTokens - contextControl.standardTokens;
		return span > 0 ? ((shownCap - contextControl.standardTokens) / span) * 100 : 0;
	});

	let capEditorOpen = $state(false);
	let capText = $state('');
	let capTextFocused = $state(false);
	let capError = $state<string | null>(null);
	let capTimer: ReturnType<typeof setTimeout> | null = null;
	let pendingCap: number | null = null;

	// The field mirrors the cap in effect unless the owner is typing in it.
	$effect(() => {
		const whole = String(Math.round(shownCap / CONTEXT_CAP_UNIT));
		if (!capTextFocused) capText = whole;
	});

	async function commitCap(tokens: number) {
		if (!contextControl || !onContextStandardChange) return;
		const next = clampStandardCap(contextControl, tokens);
		// A direct pick supersedes any arrow-key burst still waiting to save.
		if (capTimer) clearTimeout(capTimer);
		capTimer = null;
		pendingCap = null;
		capDraft = next;
		capError = null;
		try {
			await onContextStandardChange(next);
		} finally {
			// A newer drag may have started meanwhile; only clear our own draft.
			if (capDraft === next) capDraft = null;
		}
	}

	function flushCapCommit() {
		if (capTimer) clearTimeout(capTimer);
		capTimer = null;
		const tokens = pendingCap;
		pendingCap = null;
		if (tokens != null) void commitCap(tokens);
	}

	function scheduleCapCommit(tokens: number) {
		capDraft = tokens;
		pendingCap = tokens;
		if (capTimer) clearTimeout(capTimer);
		capTimer = setTimeout(flushCapCommit, KEY_COMMIT_DELAY_MS);
	}

	onDestroy(flushCapCommit);

	/** Pointer positions snap to the 10K grid; the range ends stay reachable. */
	function snapCap(tokens: number): number {
		if (!contextControl) return tokens;
		const { standardTokens: min, standardMaxTokens: max } = contextControl;
		if (tokens <= min) return min;
		if (tokens >= max) return max;
		return clampStandardCap(contextControl, Math.round(tokens / CONTEXT_CAP_STEP) * CONTEXT_CAP_STEP);
	}

	/** One grid step up or down from `from`, landing on the grid (272K → 280K). */
	function stepCap(from: number, direction: 1 | -1, size: number): number {
		if (!contextControl) return from;
		const target = direction > 0
			? Math.floor(from / size) * size + size
			: Math.ceil(from / size) * size - size;
		return clampStandardCap(contextControl, target);
	}

	function onCapSliderKeydown(e: KeyboardEvent) {
		if (!contextControl) return;
		let next: number | null = null;
		if (e.key === 'ArrowRight' || e.key === 'ArrowUp') next = stepCap(shownCap, 1, CONTEXT_CAP_STEP);
		else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') next = stepCap(shownCap, -1, CONTEXT_CAP_STEP);
		else if (e.key === 'PageUp') next = stepCap(shownCap, 1, PAGE_STEP);
		else if (e.key === 'PageDown') next = stepCap(shownCap, -1, PAGE_STEP);
		else if (e.key === 'Home') next = contextControl.standardTokens;
		else if (e.key === 'End') next = contextControl.standardMaxTokens;
		if (next == null) return;
		e.preventDefault();
		e.stopPropagation();
		if (next !== shownCap) scheduleCapCommit(next);
	}

	function onCapSliderInput(e: Event) {
		capDraft = snapCap(Number((e.currentTarget as HTMLInputElement).value));
	}

	function onCapSliderChange(e: Event) {
		void commitCap(snapCap(Number((e.currentTarget as HTMLInputElement).value)));
	}

	/**
	 * Free entry in K tokens: `450`, `450k`, `0.5m`, `1.2M`. A bare number of
	 * 10,000 or more is read as raw tokens, so pasting `450000` works too.
	 */
	function parseCapText(text: string): number | null {
		const match = text.trim().toLowerCase().replace(/[,_\s]/g, '')
			.match(/^(\d+(?:\.\d+)?)(k|m)?(?:tokens?)?$/);
		if (!match) return null;
		const amount = Number(match[1]);
		const tokens = match[2] === 'm' ? amount * 1_000_000
			: match[2] === 'k' || amount < 10_000 ? amount * CONTEXT_CAP_UNIT
			: amount;
		return Number.isFinite(tokens) && tokens > 0 ? Math.round(tokens) : null;
	}

	function capProblem(tokens: number | null): string | null {
		if (!contextControl) return null;
		if (tokens == null) return 'Enter a size in K tokens, like 450.';
		if (tokens < contextControl.standardTokens) {
			return `Standard can't go below ${contextControl.standardLabel}.`;
		}
		if (tokens > contextControl.standardMaxTokens) {
			return `Up to ${formatContextTokens(contextControl.standardMaxTokens)}. `
				+ `For the full ${contextControl.extendedLabel}, choose Extended.`;
		}
		return null;
	}

	function onCapTextInput() {
		capError = capText.trim() ? capProblem(parseCapText(capText)) : null;
	}

	function submitCapText(): boolean {
		const tokens = parseCapText(capText);
		const problem = capProblem(tokens);
		capError = problem;
		if (problem || tokens == null) return false;
		if (!contextControl) return false;
		const next = clampStandardCap(contextControl, tokens);
		if (next !== shownCap) void commitCap(next);
		capText = String(Math.round(next / CONTEXT_CAP_UNIT));
		return true;
	}

	function onCapTextKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			e.preventDefault();
			e.stopPropagation();
			submitCapText();
		} else if (e.key === 'Escape' && (capError || capText !== String(Math.round(shownCap / CONTEXT_CAP_UNIT)))) {
			// Revert the field first; a second Escape closes the popover as usual.
			e.preventDefault();
			e.stopPropagation();
			capText = String(Math.round(shownCap / CONTEXT_CAP_UNIT));
			capError = null;
		}
	}

	function onCapTextBlur() {
		capTextFocused = false;
		// An invalid entry is dropped on blur: the field shows the cap in effect.
		if (!submitCapText()) {
			capError = null;
			capText = String(Math.round(shownCap / CONTEXT_CAP_UNIT));
		}
	}

	function pickCapPreset(tokens: number) {
		capError = null;
		if (tokens !== shownCap) void commitCap(tokens);
	}

	// ---- Model limit editor (one known window, or none) ------------------------
	// The window is the provider entry's own `context_window`: the turn runs and
	// compacts against it, the usage meter divides by it. Saving writes that
	// entry (as Settings → Providers does) and re-reads the registry.
	let winEditorOpen = $state(false);
	let winText = $state('');
	let winTextFocused = $state(false);
	/** True once the owner typed in the field since it last showed the saved window. */
	let winDirty = $state(false);
	let winError = $state<string | null>(null);
	let winSaving = $state(false);
	let winDraft = $state<number | null>(null);
	const shownWindow = $derived(winDraft ?? modelWindow?.window ?? null);
	/** Hosts that let Standard be adjusted let a single window be set too. */
	const winEditable = $derived(!!modelWindow?.editable && onContextStandardChange != null);
	const winPresets = $derived.by(() => {
		const sizes = [64_000, 128_000, 200_000, 256_000, 400_000, 1_000_000, 2_000_000];
		const current = modelWindow?.window;
		if (!current) return [128_000, 200_000, 256_000, 1_000_000];
		return [...sizes.filter((size) => size < current).slice(-3), current];
	});

	// A different model starts with the editor closed and no stale draft.
	let winModel = '';
	$effect(() => {
		if (value === winModel) return;
		winModel = value;
		winEditorOpen = false;
		winError = null;
		winDraft = null;
		winDirty = false;
	});

	$effect(() => {
		const whole = shownWindow ? String(Math.round(shownWindow / CONTEXT_CAP_UNIT)) : '';
		if (!winTextFocused) winText = whole;
	});

	function windowProblem(tokens: number | null): string | null {
		if (tokens == null) return 'Enter a size in K tokens, like 128.';
		if (tokens < MODEL_WINDOW_MIN) return `At least ${formatContextTokens(MODEL_WINDOW_MIN)}.`;
		if (tokens > MODEL_WINDOW_MAX) return `Up to ${formatContextTokens(MODEL_WINDOW_MAX)}.`;
		return null;
	}

	/**
	 * Save `tokens` exactly as given: a quick size keeps its exact value and
	 * the field's whole-K entry is already whole. The window in effect is a
	 * no-op, so re-picking 1.05M never rewrites 1,048,576 as 1,049,000.
	 */
	async function commitWindow(tokens: number) {
		const info = modelWindow;
		if (!winEditable || !info?.providerId) return;
		const next = Math.round(tokens);
		if (next === (winDraft ?? info.window)) return;
		winDraft = next;
		winError = null;
		winSaving = true;
		try {
			await saveModelWindow(info.providerId, info.modelId, next);
		} catch (error) {
			winError = `Could not save: ${error instanceof Error ? error.message : String(error)}`;
		} finally {
			winSaving = false;
			if (winDraft === next) winDraft = null;
		}
	}

	function submitWinText(): boolean {
		// Only an edit is saved: an untouched field (1049 shown for 1,048,576)
		// must never be written back as a rounded, slightly different window.
		if (!winDirty) {
			winError = null;
			return true;
		}
		if (!winText.trim()) return false;
		const tokens = parseCapText(winText);
		const problem = windowProblem(tokens);
		winError = problem;
		if (problem || tokens == null) return false;
		winDirty = false;
		void commitWindow(tokens);
		return true;
	}

	function onWinTextInput() {
		winDirty = true;
		winError = winText.trim() ? windowProblem(parseCapText(winText)) : null;
	}

	function onWinTextKeydown(e: KeyboardEvent) {
		const whole = shownWindow ? String(Math.round(shownWindow / CONTEXT_CAP_UNIT)) : '';
		if (e.key === 'Enter') {
			e.preventDefault();
			e.stopPropagation();
			submitWinText();
		} else if (e.key === 'Escape' && (winError || winText !== whole)) {
			e.preventDefault();
			e.stopPropagation();
			winText = whole;
			winError = null;
			winDirty = false;
		}
	}

	function onWinTextBlur() {
		winTextFocused = false;
		if (!submitWinText()) {
			if (!winError?.startsWith('Could not save')) winError = null;
			winText = shownWindow ? String(Math.round(shownWindow / CONTEXT_CAP_UNIT)) : '';
			winDirty = false;
		}
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
					aria-label={railLabel(g)}
					title={railLabel(g)}
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
					aria-label="{formatContextTokens(shownCap)} · Standard"
					tabindex={contextChoice === 'standard' ? 0 : -1}
					data-context-choice="standard"
					class:on={contextChoice === 'standard'}
					onclick={() => onContextChoiceSelect('standard')}
				>
					<span class="mpp-context-option-copy">
						<span class="mpp-context-value">{formatContextTokens(shownCap)}</span>
						<span class="mpp-context-caption">
							Standard{#if capRaised}<span class="mpp-context-raised">{' · raised'}</span>{/if}
						</span>
					</span>
					<Check size={14} class="mpp-context-check" aria-hidden="true" />
				</button>
				<button
					type="button"
					role="radio"
					aria-checked={contextChoice === '1m'}
					aria-label="{contextControl.extendedLabel} · Extended"
					title="The model's full window. Not adjustable."
					tabindex={contextChoice === '1m' ? 0 : -1}
					data-context-choice="1m"
					class:on={contextChoice === '1m'}
					onclick={() => onContextChoiceSelect('1m')}
				>
					<span class="mpp-context-option-copy">
						<span class="mpp-context-value">{contextControl.extendedLabel}</span>
						<span class="mpp-context-caption">
							<Lock size={9} class="mpp-context-lock" aria-hidden="true" />Extended · full
						</span>
					</span>
					<Check size={14} class="mpp-context-check" aria-hidden="true" />
				</button>
			</div>

			<div class="mpp-cap-summary" data-context-summary>
				<p aria-live="polite">
					{#if contextChoice === '1m'}
						Uses the full <strong>{contextControl.extendedLabel}</strong> window before compacting
					{:else}
						Compacts near <strong>~{formatContextTokens(shownCap)}</strong> tokens{#if capRaised}<span class="mpp-cap-default">{` · default ${contextControl.standardLabel}`}</span>{/if}
					{/if}
				</p>
				{#if capEditable && contextChoice === 'standard'}
					<button
						type="button"
						class="mpp-cap-adjust"
						class:open={capEditorOpen}
						aria-expanded={capEditorOpen}
						aria-controls="{editorId}-cap"
						onclick={() => (capEditorOpen = !capEditorOpen)}
					>
						{capEditorOpen ? 'Done' : 'Adjust'}
					</button>
				{/if}
			</div>

			{#if capEditable && contextChoice === 'standard' && capEditorOpen}
				<div
					class="mpp-cap"
					id="{editorId}-cap"
					role="group"
					aria-label="Standard context cap"
					data-context-cap-editor
				>
					<div class="mpp-cap-track">
						<input
							type="range"
							class="mpp-cap-slider"
							min={contextControl.standardTokens}
							max={contextControl.standardMaxTokens}
							step={CONTEXT_CAP_UNIT}
							value={shownCap}
							style="--fill: {capFill}%"
							aria-label="Standard context cap"
							aria-valuetext="{formatContextTokens(shownCap)} tokens"
							oninput={onCapSliderInput}
							onchange={onCapSliderChange}
							onkeydown={onCapSliderKeydown}
						/>
						<span
							class="mpp-cap-locked"
							title="{contextControl.extendedLabel} is Extended, the model's full window"
							aria-hidden="true"
						>
							<Lock size={9} />{contextControl.extendedLabel}
						</span>
						<div class="mpp-cap-scale" aria-hidden="true">
							<span>{contextControl.standardLabel}</span>
							<span>{formatContextTokens(contextControl.standardMaxTokens)}</span>
						</div>
					</div>

					<div class="mpp-cap-row">
						<div class="mpp-cap-presets" role="group" aria-label="Quick sizes">
							{#each capPresets as preset (preset)}
								<button
									type="button"
									class="mpp-cap-chip"
									class:on={preset === shownCap}
									aria-pressed={preset === shownCap}
									aria-label={preset === contextControl.standardTokens
										? `${formatContextTokens(preset)}, default`
										: formatContextTokens(preset)}
									onclick={() => pickCapPreset(preset)}
								>
									{formatContextTokens(preset)}
								</button>
							{/each}
						</div>
						<label class="mpp-cap-field" class:invalid={capError}>
							<span class="sr-only">Custom Standard cap in K tokens</span>
							<input
								type="text"
								inputmode="decimal"
								autocomplete="off"
								spellcheck="false"
								bind:value={capText}
								aria-invalid={capError ? 'true' : undefined}
								aria-describedby={capError ? `${editorId}-cap-error` : undefined}
								onfocus={() => (capTextFocused = true)}
								oninput={onCapTextInput}
								onkeydown={onCapTextKeydown}
								onblur={onCapTextBlur}
							/>
							<span class="mpp-cap-unit" aria-hidden="true">K</span>
						</label>
					</div>
					{#if capError}
						<p class="mpp-cap-error" id="{editorId}-cap-error" role="alert">{capError}</p>
					{/if}
				</div>
			{/if}
		</div>
	{:else if modelWindow}
		{@const known = shownWindow != null}
		{@const windowText = shownWindow != null ? formatContextTokens(shownWindow) : 'Unknown'}
		<div class="mpp-context" data-model-context-control data-context-kind={modelWindow.kind}>
			<div class="mpp-context-copy">
				<span>Context window</span>
				<small title={getModelLabel(value)}>{getModelLabel(value)}</small>
			</div>
			<div
				class="mpp-context-toggle single"
				role="radiogroup"
				aria-label="Context window"
				tabindex="-1"
				onkeydown={onContextKeydown}
			>
				<button
					type="button"
					role="radio"
					aria-checked="true"
					aria-label={known ? `${windowText} · Model limit` : 'Unknown · Not advertised'}
					title={known ? "This model's one window." : 'The provider does not advertise this model\'s window.'}
					tabindex="0"
					data-context-choice="model"
					class="on"
					class:unknown={!known}
					class:saving={winSaving}
				>
					<span class="mpp-context-option-copy">
						<span class="mpp-context-value">{windowText}</span>
						<span class="mpp-context-caption">
							{#if known}
								<Lock size={9} class="mpp-context-lock" aria-hidden="true" />Model limit
							{:else}
								Not advertised
							{/if}
						</span>
					</span>
					{#if known}
						<Check size={14} class="mpp-context-check" aria-hidden="true" />
					{:else}
						<CircleDashed size={14} class="mpp-context-check" aria-hidden="true" />
					{/if}
				</button>
			</div>

			<div class="mpp-cap-summary" data-context-summary>
				<p aria-live="polite">
					{#if known}
						Compacts near <strong>~{windowText}</strong> tokens<span class="mpp-cap-default">{' · model limit'}</span>{#if !winEditable}<span class="mpp-win-pointer">{' Change it in Settings → Providers.'}</span>{/if}
					{:else}
						No known limit, so no usage percentage.{#if !winEditable}{' Set one in Settings → Providers.'}{/if}
					{/if}
				</p>
				{#if winEditable}
					<button
						type="button"
						class="mpp-cap-adjust"
						class:open={winEditorOpen}
						aria-expanded={winEditorOpen}
						aria-controls="{editorId}-win"
						onclick={() => (winEditorOpen = !winEditorOpen)}
					>
						{winEditorOpen ? 'Done' : known ? 'Adjust' : 'Set limit'}
					</button>
				{/if}
			</div>

			{#if winEditable && winEditorOpen}
				<div
					class="mpp-cap"
					id="{editorId}-win"
					role="group"
					aria-label="Model context limit"
					aria-busy={winSaving}
					data-model-window-editor
				>
					<p class="mpp-win-note">
						{known ? 'Lower it to compact sooner.' : 'Set the size this model accepts.'}
						Saved on the model in Settings → Providers, so every chat on it uses this.
					</p>
					<div class="mpp-cap-row">
						<div class="mpp-cap-presets" role="group" aria-label="Quick sizes">
							{#each winPresets as preset (preset)}
								<button
									type="button"
									class="mpp-cap-chip"
									class:on={preset === shownWindow}
									aria-pressed={preset === shownWindow}
									disabled={winSaving}
									onclick={() => { winError = null; void commitWindow(preset); }}
								>
									{formatContextTokens(preset)}
								</button>
							{/each}
						</div>
						<label class="mpp-cap-field" class:invalid={winError}>
							<span class="sr-only">Model context limit in K tokens</span>
							<input
								type="text"
								inputmode="decimal"
								autocomplete="off"
								spellcheck="false"
								placeholder="—"
								bind:value={winText}
								disabled={winSaving}
								aria-invalid={winError ? 'true' : undefined}
								aria-describedby={winError ? `${editorId}-win-error` : undefined}
								onfocus={() => (winTextFocused = true)}
								oninput={onWinTextInput}
								onkeydown={onWinTextKeydown}
								onblur={onWinTextBlur}
							/>
							<span class="mpp-cap-unit" aria-hidden="true">K</span>
						</label>
					</div>
					{#if winError}
						<p class="mpp-cap-error" id="{editorId}-win-error" role="alert">{winError}</p>
					{/if}
				</div>
			{/if}
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
	.mpp-context-caption {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		white-space: nowrap;
	}
	.mpp-context-raised {
		color: var(--primary);
		font-weight: 500;
	}
	.mpp-context-caption :global(.mpp-context-lock) {
		flex-shrink: 0;
		opacity: 0.75;
	}

	/* A group of one: the model's only window, selected and not a toggle. */
	.mpp-context-toggle.single {
		grid-template-columns: minmax(0, 1fr);
	}
	.mpp-context-toggle.single button,
	.mpp-context-toggle.single button:hover {
		cursor: default;
		border-color: color-mix(in srgb, var(--primary) 35%, var(--border));
		background: color-mix(in srgb, var(--primary) 8%, var(--popover));
		color: var(--foreground);
	}
	.mpp-context-toggle.single button.unknown,
	.mpp-context-toggle.single button.unknown:hover {
		border-style: dashed;
		border-color: color-mix(in srgb, var(--muted-foreground) 45%, transparent);
		background: repeating-linear-gradient(
			-45deg,
			transparent 0 4px,
			color-mix(in srgb, var(--muted-foreground) 7%, transparent) 4px 6px
		);
		box-shadow: none;
	}
	.mpp-context-toggle.single button.unknown .mpp-context-value {
		color: var(--muted-foreground);
	}
	.mpp-context-toggle.single button.unknown :global(.mpp-context-check) {
		color: var(--muted-foreground);
	}
	.mpp-context-toggle.single button.saving .mpp-context-value {
		animation: mpp-win-saving 0.9s ease-in-out infinite alternate;
	}
	@keyframes mpp-win-saving {
		to {
			opacity: 0.45;
		}
	}
	.mpp-win-note {
		margin: 0;
		color: var(--muted-foreground);
		font-size: 10.5px;
		line-height: 1.4;
	}
	.mpp-cap-chip:disabled,
	.mpp-cap-field input:disabled {
		opacity: 0.55;
		cursor: progress;
	}

	/* ---- Standard cap: summary line + inline editor ---------------------- */
	.mpp-cap-summary {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		min-height: 24px;
		padding: 0 2px;
	}
	.mpp-cap-summary p {
		min-width: 0;
		margin: 0;
		color: var(--muted-foreground);
		font-size: 11px;
		line-height: 1.35;
	}
	.mpp-cap-summary strong {
		color: var(--foreground);
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.mpp-cap-default {
		white-space: nowrap;
	}
	.mpp-cap-adjust {
		flex-shrink: 0;
		height: 24px;
		padding: 0 9px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--popover);
		color: var(--foreground);
		font: inherit;
		font-size: 11px;
		font-weight: 600;
		cursor: pointer;
		transition: background-color 0.12s ease, border-color 0.12s ease, color 0.12s ease;
	}
	.mpp-cap-adjust:hover {
		background: var(--mpp-hover);
	}
	.mpp-cap-adjust.open {
		border-color: color-mix(in srgb, var(--primary) 45%, var(--border));
		color: var(--primary);
	}
	.mpp-cap-adjust:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.mpp-cap {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 10px 10px 8px;
		border: 1px solid color-mix(in srgb, var(--border) 85%, transparent);
		border-radius: 10px;
		background: var(--popover);
		animation: mpp-cap-in 0.16s ease-out;
		container: mpp-cap / inline-size;
	}
	@keyframes mpp-cap-in {
		from {
			opacity: 0;
			transform: translateY(-3px);
		}
	}

	/* The slider covers Standard's range; the hatched stub after it is the
	   Extended window — visibly past the end and locked. */
	.mpp-cap-track {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: center;
		column-gap: 8px;
	}
	.mpp-cap-slider {
		--thumb: 16px;
		--rail: color-mix(in srgb, var(--muted-foreground) 22%, transparent);
		width: 100%;
		min-width: 0;
		height: 24px;
		margin: 0;
		background: transparent;
		cursor: pointer;
		appearance: none;
		-webkit-appearance: none;
	}
	.mpp-cap-slider::-webkit-slider-runnable-track {
		height: 6px;
		border-radius: 999px;
		background: linear-gradient(to right, var(--primary) var(--fill), var(--rail) var(--fill));
	}
	.mpp-cap-slider::-moz-range-track {
		height: 6px;
		border-radius: 999px;
		background: linear-gradient(to right, var(--primary) var(--fill), var(--rail) var(--fill));
	}
	.mpp-cap-slider::-webkit-slider-thumb {
		width: var(--thumb);
		height: var(--thumb);
		margin-top: calc((6px - var(--thumb)) / 2);
		border: 2px solid var(--primary);
		border-radius: 50%;
		background: var(--popover);
		box-shadow: 0 1px 3px color-mix(in srgb, var(--foreground) 22%, transparent);
		appearance: none;
		-webkit-appearance: none;
		transition: transform 0.12s ease;
	}
	.mpp-cap-slider::-moz-range-thumb {
		width: var(--thumb);
		height: var(--thumb);
		box-sizing: border-box;
		border: 2px solid var(--primary);
		border-radius: 50%;
		background: var(--popover);
		box-shadow: 0 1px 3px color-mix(in srgb, var(--foreground) 22%, transparent);
	}
	.mpp-cap-slider:active::-webkit-slider-thumb {
		transform: scale(1.12);
	}
	.mpp-cap-slider:focus {
		outline: none;
	}
	.mpp-cap-slider:focus-visible::-webkit-slider-thumb {
		box-shadow: 0 0 0 3px color-mix(in srgb, var(--ring) 55%, transparent);
	}
	.mpp-cap-slider:focus-visible::-moz-range-thumb {
		box-shadow: 0 0 0 3px color-mix(in srgb, var(--ring) 55%, transparent);
	}
	.mpp-cap-locked {
		display: inline-flex;
		flex-shrink: 0;
		align-items: center;
		gap: 3px;
		height: 18px;
		padding: 0 6px;
		border: 1px dashed color-mix(in srgb, var(--muted-foreground) 45%, transparent);
		border-radius: 999px;
		background: repeating-linear-gradient(
			-45deg,
			transparent 0 3px,
			color-mix(in srgb, var(--muted-foreground) 14%, transparent) 3px 5px
		);
		color: var(--muted-foreground);
		font-size: 9.5px;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.mpp-cap-scale {
		display: flex;
		justify-content: space-between;
		/* Under the slider column only, not under the locked stub. */
		grid-column: 1;
		margin-top: -3px;
		padding: 0 2px;
		color: var(--muted-foreground);
		font-size: 10px;
		font-variant-numeric: tabular-nums;
	}

	.mpp-cap-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-top: 2px;
	}
	.mpp-cap-presets {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		min-width: 0;
	}
	.mpp-cap-chip {
		min-width: 44px;
		height: 28px;
		padding: 0 8px;
		border: 1px solid var(--border);
		border-radius: 7px;
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 11.5px;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		cursor: pointer;
		transition: background-color 0.12s ease, border-color 0.12s ease, color 0.12s ease;
	}
	.mpp-cap-chip:hover {
		background: var(--mpp-hover);
		color: var(--foreground);
	}
	.mpp-cap-chip.on {
		border-color: color-mix(in srgb, var(--primary) 50%, var(--border));
		background: color-mix(in srgb, var(--primary) 12%, var(--popover));
		color: var(--foreground);
	}
	.mpp-cap-chip:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}
	/* Too narrow for chips and the field on one line (phones): the chips share
	   a full line evenly and the field sits under them at the right. */
	@container mpp-cap (max-width: 330px) {
		.mpp-cap-row {
			flex-wrap: wrap;
		}
		.mpp-cap-presets {
			flex: 1 1 100%;
			flex-wrap: nowrap;
		}
		.mpp-cap-chip {
			flex: 1 1 0;
			min-width: 0;
		}
		.mpp-cap-field {
			margin-left: auto;
		}
	}

	.mpp-cap-field {
		display: inline-flex;
		flex-shrink: 0;
		align-items: center;
		height: 28px;
		padding: 0 8px 0 2px;
		border: 1px solid var(--border);
		border-radius: 7px;
		background: color-mix(in srgb, var(--muted) 35%, transparent);
		cursor: text;
		transition: border-color 0.12s ease, box-shadow 0.12s ease;
	}
	.mpp-cap-field:focus-within {
		border-color: var(--ring);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--ring) 30%, transparent);
	}
	.mpp-cap-field.invalid {
		border-color: var(--destructive);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--destructive) 22%, transparent);
	}
	.mpp-cap-field input {
		width: 5ch;
		height: 100%;
		padding: 0 2px 0 6px;
		border: 0;
		outline: none;
		background: transparent;
		color: var(--foreground);
		font: inherit;
		font-size: 12.5px;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		text-align: right;
	}
	.mpp-cap-unit {
		color: var(--muted-foreground);
		font-size: 11.5px;
		font-weight: 600;
	}
	.mpp-cap-error {
		margin: 0;
		color: var(--destructive);
		font-size: 10.5px;
		line-height: 1.35;
	}

	/* Touch: full-size targets on phones and tablets. */
	@media (pointer: coarse) {
		.mpp-cap-adjust {
			height: 32px;
			padding: 0 12px;
		}
		.mpp-cap-slider {
			--thumb: 22px;
			height: 40px;
		}
		.mpp-cap-chip,
		.mpp-cap-field {
			height: 40px;
		}
		.mpp-cap-field input {
			font-size: 16px; /* no iOS zoom on focus */
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.mpp-cap {
			animation: none;
		}
		.mpp-cap-slider::-webkit-slider-thumb {
			transition: none;
		}
		.mpp-context-toggle.single button.saving .mpp-context-value {
			animation: none;
		}
	}

	.mpp-empty {
		padding: 24px 8px;
		text-align: center;
		font-size: 12px;
		color: var(--muted-foreground);
	}
</style>
