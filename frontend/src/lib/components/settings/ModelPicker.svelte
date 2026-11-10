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
	import { tick } from 'svelte';
	import { ChevronDown } from 'lucide-svelte';
	import { decodeProviderModel, CUSTOM_VALUE } from '$lib/constants/models';
	import { buildModelPickerCatalog } from '$lib/constants/model-picker';
	import ModelPickerPanel from './ModelPickerPanel.svelte';
	import { focusLeftRoots, pickerHoldsFocus } from './model-picker-focus';
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
		/** Optional friendly trigger label when a saved value is not in the catalog. */
		fallbackLabel?: string;
		/**
		 * Offer the free-text `Custom…` entry. Off for callers that must end up
		 * with a real, routable model id — a typo'd custom string would only
		 * surface as a failed call much later.
		 */
		allowCustom?: boolean;
		/** List only API-key providers (see `BuildCatalogOptions.apiKeyOnly`). */
		apiKeyOnly?: boolean;
	}

	let {
		value,
		onSelect,
		id,
		providerModelsOnly = false,
		providerScope,
		allowEmpty = false,
		emptyLabel = 'Unavailable as subagent',
		fallbackLabel,
		allowCustom = true,
		apiKeyOnly = false
	}: Props = $props();

	const catalog = $derived(buildModelPickerCatalog({ providerModelsOnly, providerScope, apiKeyOnly }));

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
	const PANEL_PREFERRED_WIDTH = 360;
	// The panel stops 24px short of whatever edge it opens toward.
	const VIEWPORT_MARGIN = 24;
	// Sideways it may come closer to the clipping edge: 8px.
	const SIDE_MARGIN = 8;
	let openUpward = $state(false);
	let panelHeight = $state(PANEL_PREFERRED_HEIGHT);
	/** Grow rightward from the trigger's left edge instead of leftward from its right. */
	let alignStart = $state(false);
	/** Set only when neither side has room for the preferred width. */
	let panelWidth = $state<number | null>(null);

	interface Bounds { top: number; bottom: number; left: number; right: number }

	/**
	 * Bounds the panel can be seen in: the viewport, narrowed by every
	 * scrolling ancestor. Inside a dialog the body scrolls and ends at the
	 * footer, so measuring against the window alone let the panel open into
	 * the hidden part of the body, behind the footer (#1811). Sideways every
	 * ancestor that clips its overflow counts: a Settings detail pane cut off
	 * a right-anchored panel that grew past its left edge (#1338).
	 */
	function visibleBounds(el: HTMLElement): Bounds {
		const b: Bounds = { top: 0, bottom: window.innerHeight, left: 0, right: window.innerWidth };
		for (let node = el.parentElement; node; node = node.parentElement) {
			const { overflowX, overflowY } = getComputedStyle(node);
			const clipsY = overflowY === 'auto' || overflowY === 'scroll';
			const clipsX = overflowX !== '' && overflowX !== 'visible';
			if (!clipsX && !clipsY) continue;
			const r = node.getBoundingClientRect();
			if (clipsY) {
				b.top = Math.max(b.top, r.top);
				b.bottom = Math.min(b.bottom, r.bottom);
			}
			if (clipsX) {
				// The padding box: a vertical scrollbar is not room for the panel.
				const left = r.left + (node.clientLeft || 0);
				const width = node.clientWidth || r.width;
				b.left = Math.max(b.left, left);
				b.right = Math.min(b.right, left + width);
			}
		}
		return b;
	}

	function positionPanel() {
		if (typeof window === 'undefined' || !rootEl) return;
		const rect = rootEl.getBoundingClientRect();
		const bounds = visibleBounds(rootEl);
		const spaceBelow = bounds.bottom - rect.bottom - VIEWPORT_MARGIN;
		const spaceAbove = rect.top - bounds.top - VIEWPORT_MARGIN;
		if (spaceBelow < PANEL_MIN_HEIGHT && spaceAbove > spaceBelow) {
			openUpward = true;
			panelHeight = Math.max(PANEL_MIN_HEIGHT, Math.min(PANEL_PREFERRED_HEIGHT, spaceAbove));
		} else {
			openUpward = false;
			panelHeight = Math.max(PANEL_MIN_HEIGHT, Math.min(PANEL_PREFERRED_HEIGHT, spaceBelow));
		}
		// Sideways: keep the right-anchored default when it fits, else grow
		// rightward, else take the roomier side at the width it has.
		const width = Math.min(PANEL_PREFERRED_WIDTH, window.innerWidth * 0.92);
		const roomLeftward = rect.right - (bounds.left + SIDE_MARGIN);
		const roomRightward = bounds.right - SIDE_MARGIN - rect.left;
		if (roomLeftward >= width) {
			alignStart = false;
			panelWidth = null;
		} else if (roomRightward >= width) {
			alignStart = true;
			panelWidth = null;
		} else {
			alignStart = roomRightward > roomLeftward;
			panelWidth = Math.floor(Math.max(roomLeftward, roomRightward));
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
		if (fallbackLabel) return fallbackLabel;
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
		// Measure again once the panel is in: if opening moved the trigger, the
		// panel follows it instead of keeping the side picked for the old spot.
		if (next) void tick().then(() => { if (open) positionPanel(); });
	}

	let triggerEl = $state<HTMLButtonElement | null>(null);

	/** Close the panel; Escape also hands focus back to the trigger (#1339). */
	function close(returnFocus = false) {
		open = false;
		if (returnFocus) triggerEl?.focus({ preventScroll: true });
	}

	// Close on outside click / Escape while open.
	function onDocClick(e: MouseEvent) {
		if (rootEl && !rootEl.contains(e.target as Node)) close();
	}
	// Tab or Shift+Tab out of the picker closes it, leaving focus where the
	// user sent it (#1407).
	function onFocusOut(e: FocusEvent) {
		if (open && focusLeftRoots(e, rootEl)) close();
	}
	function onKeydown(e: KeyboardEvent) {
		if (e.key !== 'Escape') return;
		e.preventDefault();
		// Focus lost to the page still goes back to the trigger.
		close(pickerHoldsFocus(rootEl));
		// This Escape closed the panel; keep it from also closing a dialog the
		// picker sits in (those listen on window, after this document listener).
		e.stopPropagation();
	}

	$effect(() => {
		if (!open) return;
		document.addEventListener('mousedown', onDocClick);
		document.addEventListener('keydown', onKeydown);
		// A resize or a phone rotation moves the pane edges: place it again.
		window.addEventListener('resize', positionPanel);
		return () => {
			document.removeEventListener('mousedown', onDocClick);
			document.removeEventListener('keydown', onKeydown);
			window.removeEventListener('resize', positionPanel);
		};
	});

	// Close when the picker stops being shown. Settings stays mounted behind
	// the chat with `display: none`, so leaving it with the keyboard (no
	// outside mouse press) left the panel open and the trigger expanded in the
	// hidden page, and it came back open (#1345). A hidden element reports a
	// zero-size box, which the observer delivers.
	$effect(() => {
		const el = rootEl;
		if (!open || !el || typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver(() => {
			if (el.getClientRects().length === 0) close();
		});
		ro.observe(el);
		return () => ro.disconnect();
	});
</script>

<div class="mpk" bind:this={rootEl} onfocusout={onFocusOut}>
	<button
		type="button"
		{id}
		bind:this={triggerEl}
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
			class:mpk-panel-start={alignStart}
			style="--mpp-height: {panelHeight}px{panelWidth === null ? '' : `; --mpp-width: ${panelWidth}px`}"
		>
			<ModelPickerPanel
				groups={catalog.groups}
				options={catalog.options}
				{value}
				onSelect={selectOption}
				{leadingRows}
				{trailingRows}
				onRequestClose={() => close(true)}
				focusSearchOnOpen
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
		/* Right-anchored by default: most pickers sit near the clipped right edge
		   of their `.set-group` (overflow: hidden), so extra width grows LEFTWARD.
		   positionPanel() switches to `.mpk-panel-start` or narrows the panel when
		   that would cross the left edge of what clips it (#1338). */
		right: 0;
		--mpp-width: min(360px, 92vw);
		border: 1px solid var(--menu-border);
		border-radius: var(--menu-radius);
		box-shadow: var(--menu-shadow);
		overflow: hidden;
	}

	/* Flip above the trigger when there isn't enough room below (short viewport
	   or a trigger near the bottom of the page) — set alongside the inline
	   height by positionPanel(). */
	.mpk-panel.mpk-panel-up {
		top: auto;
		bottom: calc(100% + 4px);
	}

	/* Grow rightward from the trigger's left edge when leftward has no room. */
	.mpk-panel.mpk-panel-start {
		right: auto;
		left: 0;
	}
</style>
