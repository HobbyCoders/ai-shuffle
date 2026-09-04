<script lang="ts">
	/**
	 * ModelPicker — trigger + popover for choosing a profile's / agent's model.
	 *
	 * The popover body is the shared T3-style `ModelPickerPanel` (provider icon
	 * rail on the left, search + two-line model rows on the right, shared
	 * favorites). This wrapper owns only the trigger button and the
	 * flip-or-constrain positioning.
	 *
	 * CONTROLLED component: it renders from `value` (the composite selection
	 * string) and emits the newly-chosen value via `onSelect`. It owns NO save
	 * logic — the parent keeps its exact decode/save contract. Composite value
	 * encoding (the encode/decode pair lives in `$lib/constants/models` so the
	 * chat composer shares it — PA13):
	 *   - native Claude model  → the base model id (e.g. `sonnet`)
	 *   - provider model       → `encodeProviderModel(providerId, modelId)`
	 *   - custom free-text     → CUSTOM_VALUE
	 */
	import { ChevronDown } from 'lucide-svelte';
	import { decodeProviderModel, CUSTOM_VALUE } from '$lib/constants/models';
	import { buildModelPickerCatalog } from '$lib/constants/model-picker';
	import ModelPickerPanel from './ModelPickerPanel.svelte';
	import ProviderGlyph from './ProviderGlyph.svelte';

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
		/** Limit choices to one provider. Use NATIVE_PROVIDER_KEY for native Claude. */
		providerScope?: string;
		/** Offer an empty assignment for provider-scoped subagent configuration. */
		allowEmpty?: boolean;
		emptyLabel?: string;
		/**
		 * Offer the free-text `Custom…` entry. Off for callers that must end up
		 * with a real, routable model id — a typo'd custom string would only
		 * surface as a failed call much later.
		 */
		allowCustom?: boolean;
		/**
		 * Open the panel on an All-providers list (every catalog row, with
		 * provider sublines). Used by subagent assignment pickers.
		 */
		startOnAll?: boolean;
	}

	let {
		value,
		onSelect,
		id,
		providerModelsOnly = false,
		providerScope,
		allowEmpty = false,
		emptyLabel = 'Unavailable as subagent',
		allowCustom = true,
		startOnAll = false
	}: Props = $props();

	const catalog = $derived(buildModelPickerCatalog({ providerModelsOnly, providerScope }));

	const leadingRows = $derived(allowEmpty ? [{ value: '', label: emptyLabel }] : []);
	const trailingRows = $derived(
		allowCustom && !providerModelsOnly && !providerScope
			? [{ value: CUSTOM_VALUE, label: 'Custom…' }]
			: []
	);

	// ---- open/close state ------------------------------------------------------
	let open = $state(false);
	let rootEl = $state<HTMLDivElement | null>(null);

	// ---- flip-or-constrain positioning --------------------------------------
	// The panel defaults to opening below the trigger with a 346px cap, which
	// clips on short viewports / triggers near the bottom of the page. Measure
	// available space each time the panel opens and either flip it above the
	// trigger or shrink its height to fit, whichever side has room.
	const PANEL_PREFERRED_HEIGHT = 346;
	const PANEL_MIN_HEIGHT = 180;
	const VIEWPORT_MARGIN = 12;
	let openUpward = $state(false);
	let panelHeight = $state(PANEL_PREFERRED_HEIGHT);

	function positionPanel() {
		if (typeof window === 'undefined' || !rootEl) return;
		const rect = rootEl.getBoundingClientRect();
		const spaceBelow = window.innerHeight - rect.bottom - VIEWPORT_MARGIN;
		const spaceAbove = rect.top - VIEWPORT_MARGIN;
		if (spaceBelow < PANEL_MIN_HEIGHT && spaceAbove > spaceBelow) {
			openUpward = true;
			panelHeight = Math.max(PANEL_MIN_HEIGHT, Math.min(PANEL_PREFERRED_HEIGHT, spaceAbove));
		} else {
			openUpward = false;
			panelHeight = Math.max(PANEL_MIN_HEIGHT, Math.min(PANEL_PREFERRED_HEIGHT, spaceBelow));
		}
	}

	// ---- trigger label -----------------------------------------------------
	const currentOption = $derived(catalog.options.find((o) => o.value === value));
	const currentGroup = $derived(
		currentOption ? catalog.groups.find((g) => g.key === currentOption.groupKey) : undefined
	);
	const currentLabel = $derived.by(() => {
		if (allowEmpty && !value) return emptyLabel;
		if (value === CUSTOM_VALUE) return 'Custom…';
		if (currentOption) return currentOption.label;
		// No matching option (registry not hydrated yet, or the provider/model was
		// removed) — show the bare model id rather than the raw `prov:…` composite.
		return decodeProviderModel(value)?.modelId ?? (value || 'Select a model');
	});

	function selectOption(optionValue: string) {
		// Keep the panel open so the user can still tweak options like context
		// window size; outside click / Escape still dismiss via onRequestClose.
		onSelect(optionValue);
	}

	function toggleOpen() {
		const next = !open;
		if (next) positionPanel();
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
		{#if currentGroup}
			<span class="mpk-glyph"><ProviderGlyph kind={currentGroup.glyph} size={15} /></span>
		{/if}
		<span class="mpk-current">{currentLabel}</span>
		<ChevronDown size={15} class="mpk-chev" />
	</button>

	{#if open}
		<div
			class="mpk-panel"
			class:mpk-panel-up={openUpward}
			style="--mpp-height: {panelHeight}px"
		>
			<ModelPickerPanel
				groups={catalog.groups}
				options={catalog.options}
				{value}
				onSelect={selectOption}
				{leadingRows}
				{trailingRows}
				{startOnAll}
				onRequestClose={() => (open = false)}
			/>
		</div>
	{/if}
</div>

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
	.mpk-glyph {
		display: inline-flex;
		flex-shrink: 0;
		color: var(--muted-foreground);
	}
	.mpk-current {
		flex: 1;
		min-width: 0;
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
		   `.set-group` (overflow: hidden), so extra width must grow LEFTWARD. */
		right: 0;
		--mpp-width: min(360px, 92vw);
		border: 1px solid var(--border);
		border-radius: 10px;
		box-shadow: 0 16px 40px -18px rgba(0, 0, 0, 0.55);
		overflow: hidden;
	}

	/* Flip above the trigger when there isn't enough room below (short viewport
	   or a trigger near the bottom of the page) — set alongside the inline
	   height by positionPanel(). */
	.mpk-panel.mpk-panel-up {
		top: auto;
		bottom: calc(100% + 4px);
	}
</style>
