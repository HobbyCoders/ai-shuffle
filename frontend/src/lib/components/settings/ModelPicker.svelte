<script lang="ts">
	/**
	 * ModelPicker — searchable, grouped, pinnable model selector (phase-2 Chunk D.4).
	 *
	 * Replaces the phase-1 optgroup `<select>` for choosing a profile's model. A
	 * flat grid fails at OpenRouter scale (T3 Chat lesson, F5), so this ships
	 * search + provider grouping + favorites/pinning from day one.
	 *
	 * CONTROLLED component: it renders from `value` (the composite selection
	 * string used by the phase-1 picker) and emits the newly-chosen value via
	 * `onSelect`. It owns NO save logic — the parent keeps its exact decode/save
	 * contract. Composite value encoding (unchanged from phase-1; the encode/
	 * decode pair now lives in `$lib/constants/models` so the chat composer
	 * shares it — PA13):
	 *   - native Claude model  → the base model id (e.g. `sonnet`)
	 *   - provider model       → `encodeProviderModel(providerId, modelId)`
	 *   - custom free-text     → CUSTOM_VALUE
	 *
	 * Pins are frontend-only, persisted to localStorage (no backend store, no
	 * schema change) — a per-install convenience across the pinned models.
	 */
	import { Search, Star, Check, ChevronDown } from 'lucide-svelte';
	import {
		getModels,
		getProviders,
		encodeProviderModel,
		getProviderDisplay,
		getClaudeDisplay,
		decodeProviderModel,
		CUSTOM_VALUE
	} from '$lib/constants/models';

	interface Props {
		/** Current composite selection value (see module comment). */
		value: string;
		/** Emitted with the new composite value when the user picks a row. */
		onSelect: (value: string) => void;
		/** Optional id for the trigger button (a11y labelling). */
		id?: string;
		/**
		 * When true, list only provider-curated models — suppress the native
		 * Claude group and the `Custom…` entry. Additive, default false.
		 */
		providerModelsOnly?: boolean;
	}

	let { value, onSelect, id, providerModelsOnly = false }: Props = $props();

	const PIN_STORAGE_KEY = 'aihub.pinnedModels';
	// Groups are keyed by provider ID (the native catalog gets this sentinel) so
	// two rows sharing a brand — both OpenAI rows display as "OpenAI" — stay
	// separate groups; the auth-kind subtext is what tells them apart.
	const NATIVE_GROUP_KEY = '__claude__';

	interface PickerOption {
		value: string; // composite value emitted on select
		modelId: string; // raw model id (for search + display)
		label: string;
		/** Stable bucket identity (provider id, or the native sentinel). */
		groupKey: string;
		/** Brand name shown on the group heading. */
		group: string;
		/** Muted auth-kind qualifier on the heading ("API" / "prolite" / …). */
		groupSubtext: string | null;
	}

	// ---- pinned set (localStorage-backed) -----------------------------------
	function loadPins(): string[] {
		if (typeof window === 'undefined') return [];
		try {
			const raw = window.localStorage.getItem(PIN_STORAGE_KEY);
			const parsed = raw ? JSON.parse(raw) : [];
			return Array.isArray(parsed) ? parsed.filter((v) => typeof v === 'string') : [];
		} catch {
			return [];
		}
	}

	let pinned = $state<string[]>(loadPins());

	function persistPins() {
		if (typeof window === 'undefined') return;
		try {
			window.localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify(pinned));
		} catch {
			// Best-effort — a full/blocked store just means pins don't persist.
		}
	}

	function togglePin(optionValue: string, e: MouseEvent) {
		e.stopPropagation();
		pinned = pinned.includes(optionValue)
			? pinned.filter((v) => v !== optionValue)
			: [...pinned, optionValue];
		persistPins();
	}

	// ---- option set (native Claude first, then each provider) ----------------
	const options = $derived.by<PickerOption[]>(() => {
		const out: PickerOption[] = [];
		if (!providerModelsOnly) {
			const claude = getClaudeDisplay();
			for (const m of getModels()) {
				out.push({
					value: m.value,
					modelId: m.value,
					label: m.label,
					groupKey: NATIVE_GROUP_KEY,
					group: claude.name,
					groupSubtext: claude.subtext
				});
			}
		}
		for (const p of getProviders()) {
			const display = getProviderDisplay(p);
			for (const pm of p.models) {
				out.push({
					value: encodeProviderModel(p.id, pm.id),
					modelId: pm.id,
					label: pm.label,
					groupKey: p.id,
					group: display.name,
					groupSubtext: display.subtext
				});
			}
		}
		return out;
	});

	// ---- open/close + search state ------------------------------------------
	let open = $state(false);
	let query = $state('');
	let rootEl = $state<HTMLDivElement | null>(null);

	// ---- flip-or-constrain positioning --------------------------------------
	// The panel defaults to opening below the trigger with a 340px cap, which
	// clips on short viewports / triggers near the bottom of the page (#ModelPicker
	// clipping fix). Measure available space each time the panel opens and either
	// flip it above the trigger or shrink its max-height to fit, whichever side
	// has room — mirrors the flip-or-constrain approach other poppers in this
	// codebase use (see ControlsDock's upward-opening dropdowns).
	const PANEL_PREFERRED_HEIGHT = 340;
	const PANEL_MIN_HEIGHT = 160;
	const VIEWPORT_MARGIN = 12;
	let openUpward = $state(false);
	let panelMaxHeight = $state(PANEL_PREFERRED_HEIGHT);

	function positionPanel() {
		if (typeof window === 'undefined' || !rootEl) return;
		const rect = rootEl.getBoundingClientRect();
		const spaceBelow = window.innerHeight - rect.bottom - VIEWPORT_MARGIN;
		const spaceAbove = rect.top - VIEWPORT_MARGIN;
		if (spaceBelow < PANEL_MIN_HEIGHT && spaceAbove > spaceBelow) {
			openUpward = true;
			panelMaxHeight = Math.max(PANEL_MIN_HEIGHT, Math.min(PANEL_PREFERRED_HEIGHT, spaceAbove));
		} else {
			openUpward = false;
			panelMaxHeight = Math.max(PANEL_MIN_HEIGHT, Math.min(PANEL_PREFERRED_HEIGHT, spaceBelow));
		}
	}

	const filtered = $derived.by<PickerOption[]>(() => {
		const q = query.trim().toLowerCase();
		if (!q) return options;
		return options.filter(
			(o) => o.label.toLowerCase().includes(q) || o.modelId.toLowerCase().includes(q)
		);
	});

	// Pinned rows shown on top (order = pin order), filtered by the same query.
	const pinnedOptions = $derived(
		pinned
			.map((v) => filtered.find((o) => o.value === v))
			.filter((o): o is PickerOption => o != null)
	);

	// Remaining options grouped by their group KEY (never the brand — see above),
	// native group first.
	interface OptionGroup {
		key: string;
		group: string;
		subtext: string | null;
		items: PickerOption[];
	}
	const groupedOptions = $derived.by<OptionGroup[]>(() => {
		const groups: OptionGroup[] = [];
		for (const o of filtered) {
			let bucket = groups.find((g) => g.key === o.groupKey);
			if (!bucket) {
				bucket = { key: o.groupKey, group: o.group, subtext: o.groupSubtext, items: [] };
				groups.push(bucket);
			}
			bucket.items.push(o);
		}
		return groups;
	});

	// Label shown on the collapsed trigger.
	const currentLabel = $derived.by(() => {
		if (value === CUSTOM_VALUE) return 'Custom…';
		const match = options.find((o) => o.value === value);
		if (match) return match.label;
		// No matching option (registry not hydrated yet, or the provider/model was
		// removed) — show the bare model id rather than the raw `prov:…` composite.
		return decodeProviderModel(value)?.modelId ?? (value || 'Select a model');
	});

	function selectOption(optionValue: string) {
		onSelect(optionValue);
		open = false;
		query = '';
	}

	function toggleOpen() {
		const next = !open;
		if (next) {
			query = '';
			// Measure before the panel mounts — rootEl still equals the trigger's
			// own box at this point, which is exactly the anchor we need.
			positionPanel();
		}
		open = next;
	}

	// Close on outside click / Escape while open.
	function onDocClick(e: MouseEvent) {
		if (rootEl && !rootEl.contains(e.target as Node)) open = false;
	}
	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') open = false;
	}

	$effect(() => {
		if (!open) return;
		document.addEventListener('mousedown', onDocClick);
		document.addEventListener('keydown', onKeydown);
		return () => {
			document.removeEventListener('mousedown', onDocClick);
			document.removeEventListener('keydown', onKeydown);
		};
	});

	function isPinned(optionValue: string): boolean {
		return pinned.includes(optionValue);
	}
</script>

<div class="mpk" bind:this={rootEl}>
	<button
		type="button"
		{id}
		class="mpk-trigger"
		class:open
		aria-haspopup="listbox"
		aria-expanded={open}
		onclick={toggleOpen}
	>
		<span class="mpk-current">{currentLabel}</span>
		<ChevronDown size={15} class="mpk-chev" />
	</button>

	{#if open}
		<div
			class="mpk-panel"
			class:mpk-panel-up={openUpward}
			style="max-height: {panelMaxHeight}px"
			role="listbox"
			aria-label="Model"
		>
			<div class="mpk-search">
				<Search size={14} />
				<!-- svelte-ignore a11y_autofocus -->
				<input
					type="text"
					class="mpk-search-inp"
					placeholder="Search models…"
					bind:value={query}
					autofocus
					spellcheck="false"
					autocapitalize="off"
					autocomplete="off"
				/>
			</div>

			<div class="mpk-list">
				{#if pinnedOptions.length > 0}
					<div class="mpk-group">
						<div class="mpk-grouphead">Pinned</div>
						{#each pinnedOptions as o (o.value)}
							{@render row(o)}
						{/each}
					</div>
				{/if}

				{#each groupedOptions as g (g.key)}
					<div class="mpk-group">
						<div class="mpk-grouphead">
							{g.group}{#if g.subtext}<span class="mpk-groupsub">&nbsp;· {g.subtext}</span>{/if}
						</div>
						{#each g.items as o (o.value)}
							{@render row(o)}
						{/each}
					</div>
				{/each}

				{#if filtered.length === 0}
					<div class="mpk-empty">No models match “{query}”.</div>
				{/if}

				{#if !providerModelsOnly}
					<div class="mpk-group mpk-customgroup">
						<button
							type="button"
							class="mpk-row"
							class:selected={value === CUSTOM_VALUE}
							role="option"
							aria-selected={value === CUSTOM_VALUE}
							onclick={() => selectOption(CUSTOM_VALUE)}
						>
							<span class="mpk-check">{#if value === CUSTOM_VALUE}<Check size={14} />{/if}</span>
							<span class="mpk-label">Custom…</span>
						</button>
					</div>
				{/if}
			</div>
		</div>
	{/if}
</div>

{#snippet row(o: PickerOption)}
	<button
		type="button"
		class="mpk-row"
		class:selected={value === o.value}
		role="option"
		aria-selected={value === o.value}
		onclick={() => selectOption(o.value)}
	>
		<span class="mpk-check">{#if value === o.value}<Check size={14} />{/if}</span>
		<span class="mpk-label" title={o.modelId}>{o.label}</span>
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<span
			class="mpk-pin"
			class:on={isPinned(o.value)}
			role="button"
			tabindex="-1"
			aria-label={isPinned(o.value) ? `Unpin ${o.label}` : `Pin ${o.label}`}
			title={isPinned(o.value) ? 'Unpin' : 'Pin'}
			onclick={(e) => togglePin(o.value, e)}
		>
			<Star size={13} />
		</span>
	</button>
{/snippet}

<style>
	.mpk {
		position: relative;
		width: 100%;
		/* The profile form's control column shrinks to content — without a floor
		   the trigger collapses to the current label's width (~76px). */
		min-width: 220px;
	}

	/* Trigger mirrors the app's `.sel` control so it drops into a `.grp-row`. */
	.mpk-trigger {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		width: 100%;
		min-height: 36px;
		padding: 0 10px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 9px;
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
		cursor: pointer;
		text-align: left;
	}
	.mpk-trigger:hover {
		background: var(--muted);
	}
	.mpk-trigger.open {
		border-color: var(--primary);
	}
	.mpk-current {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.mpk-trigger :global(.mpk-chev) {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}

	.mpk-panel {
		position: absolute;
		z-index: 40;
		top: calc(100% + 4px);
		/* Right-anchored: the picker sits near the clipped right edge of its
		   `.set-group` (overflow: hidden), so extra width must grow LEFTWARD.
		   Sized to content with a floor of the trigger width, capped so long
		   model labels still ellipsize instead of spanning the card. */
		right: 0;
		min-width: 100%;
		width: max-content;
		max-width: min(340px, 85vw);
		display: flex;
		flex-direction: column;
		/* Ceiling only — the actual cap is the inline `max-height` computed in
		   positionPanel() each time the panel opens, so it always fits between
		   the trigger and whichever viewport edge it's anchored to. */
		max-height: 340px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
		box-shadow: 0 12px 32px -12px rgba(0, 0, 0, 0.4);
		overflow: hidden;
	}

	/* Flip above the trigger when there isn't enough room below (short viewport
	   or a trigger near the bottom of the page) — set alongside the inline
	   max-height by positionPanel(). */
	.mpk-panel.mpk-panel-up {
		top: auto;
		bottom: calc(100% + 4px);
	}

	.mpk-search {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 10px;
		border-bottom: 1px solid var(--border);
		color: var(--muted-foreground);
		flex-shrink: 0;
	}
	.mpk-search-inp {
		flex: 1;
		min-width: 0;
		background: none;
		border: none;
		outline: none;
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
	}

	.mpk-list {
		overflow-y: auto;
		padding: 4px;
	}

	.mpk-group + .mpk-group {
		margin-top: 2px;
	}
	.mpk-grouphead {
		padding: 6px 8px 4px;
		font-size: 11px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted-foreground);
	}

	/* Auth-kind qualifier on a group heading ("· API" / "· PROLITE") — lighter
	   and dimmer than the brand it follows, so the brand still reads first. */
	.mpk-groupsub {
		font-weight: 500;
		opacity: 0.7;
	}

	.mpk-row {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 8px;
		background: none;
		border: none;
		border-radius: 7px;
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
		cursor: pointer;
		text-align: left;
	}
	.mpk-row:hover {
		background: var(--muted);
	}
	.mpk-row.selected {
		background: color-mix(in oklch, var(--primary) 14%, transparent);
	}
	.mpk-check {
		display: inline-flex;
		width: 14px;
		flex-shrink: 0;
		color: var(--primary);
	}
	.mpk-label {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.mpk-pin {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		padding: 3px;
		border-radius: 6px;
		color: var(--muted-foreground);
		opacity: 0.45;
		cursor: pointer;
	}
	.mpk-row:hover .mpk-pin {
		opacity: 0.8;
	}
	.mpk-pin:hover {
		background: var(--background);
		color: var(--foreground);
		opacity: 1;
	}
	.mpk-pin.on {
		color: var(--primary);
		opacity: 1;
	}
	.mpk-pin.on :global(svg) {
		fill: currentColor;
	}

	.mpk-empty {
		padding: 10px 8px;
		font-size: 12.5px;
		color: var(--muted-foreground);
	}

	.mpk-customgroup {
		margin-top: 4px;
		border-top: 1px solid var(--border);
		padding-top: 4px;
	}
</style>
