<script lang="ts">
	/**
	 * BrowserPicker — the panel's own popup for a page control whose real popup
	 * Chrome draws outside the screencast (a `<select>` list, date and time
	 * pickers). It opens next to the field inside the live view (a bottom sheet
	 * when the view is small), rings the page field it stands in for, and hands
	 * the choice back to be set in the page. It stays open until the page has
	 * the value; a refusal (the list changed, the page rejected the value) is
	 * shown here with a way to re-open the list or try again. Escape, a click
	 * outside or the close button cancel without touching the page.
	 */
	import { onMount } from 'svelte';
	import { X, RotateCw } from 'lucide-svelte';
	import BrowserSelectList from './BrowserSelectList.svelte';
	import BrowserDateTimePicker from './BrowserDateTimePicker.svelte';
	import type { BrowserEditor } from '$lib/browser-panel/workspace';
	import { isPickerKind, limitsFor, placePopup, wallClock, type BrowserChoices, type DateKind, type PickerChoice } from '$lib/browser-panel/pickers';

	let {
		field,
		anchor,
		viewWidth,
		viewHeight,
		boundary,
		loadChoices,
		onApply,
		onCancel,
	}: {
		field: BrowserEditor;
		/** The page field's box in live-view px. */
		anchor: { left: number; top: number; width: number; height: number };
		viewWidth: number;
		viewHeight: number;
		/** The live view: a press outside it cancels and still reaches its target. */
		boundary?: HTMLElement;
		/** Reads a select's options from the page. */
		loadChoices?: (field: BrowserEditor) => Promise<BrowserChoices>;
		/** Sets the choice in the page; a rejection is shown in the popup. */
		onApply: (choice: PickerChoice) => void | Promise<void>;
		onCancel: () => void;
	} = $props();

	// svelte-ignore state_referenced_locally
	const kind = isPickerKind(field.type) ? field.type : 'select';
	const KIND_LABEL: Record<string, string> = {
		select: 'List',
		date: 'Date',
		time: 'Time',
		'datetime-local': 'Date & time',
		month: 'Month',
		week: 'Week',
	};

	let choices = $state<BrowserChoices | null>(null);
	let loadError = $state<string | null>(null);
	let loading = $state(false);
	let applying = $state(false);
	let applyError = $state<string | null>(null);

	async function load() {
		if (kind !== 'select') return;
		if (!loadChoices) {
			loadError = "This list can't be opened here.";
			return;
		}
		loading = true;
		loadError = null;
		applyError = null;
		choices = null;
		try {
			choices = await loadChoices(field);
		} catch (error) {
			loadError = error instanceof Error && error.message ? error.message : "Couldn't read this list. Try again.";
		} finally {
			loading = false;
		}
	}

	async function apply(choice: PickerChoice) {
		if (applying) return;
		applying = true;
		applyError = null;
		try {
			await onApply(choice);
		} catch (error) {
			applyError = error instanceof Error && error.message ? error.message : "The page didn't take that choice. Try again.";
		} finally {
			applying = false;
		}
	}

	function chooseOption(index: number) {
		const option = choices?.options[index];
		if (option) void apply({ index, label: option.label });
	}

	// The panel's limits come from the page's own attributes.
	const limits = $derived(kind === 'select' ? null : limitsFor(kind as DateKind, field.min, field.max, field.step, field.base));
	// Today and Now follow the page's clock, which may be in another time zone.
	// svelte-ignore state_referenced_locally
	const now = wallClock(field.zone);
	// svelte-ignore state_referenced_locally
	const zoneNote = field.zone && field.zone !== Intl.DateTimeFormat().resolvedOptions().timeZone ? field.zone : null;

	const sheet = $derived(viewWidth < 480 || viewHeight < 380);
	const preferred = $derived.by(() => {
		if (kind === 'select') {
			const rows = choices ? choices.options.length + choices.groups.length : 6;
			return { width: 280, height: Math.min(392, 64 + rows * 34 + (choices && choices.options.length > 12 ? 44 : 0)) };
		}
		if (kind === 'month') return { width: 296, height: 300 };
		if (kind === 'time') return { width: 272, height: 384 };
		if (kind === 'datetime-local') return viewWidth >= 620 ? { width: 560, height: 420 } : { width: 316, height: 640 };
		return { width: kind === 'week' ? 324 : 304, height: 404 };
	});
	const place = $derived(placePopup(anchor, { width: viewWidth, height: viewHeight }, preferred));
	const popupStyle = $derived(
		sheet
			? ''
			: `left:${place.left}px;top:${place.top}px;width:${place.width}px;max-height:${place.maxHeight}px`,
	);
	const ringStyle = $derived(`left:${anchor.left}px;top:${anchor.top}px;width:${anchor.width}px;height:${anchor.height}px`);

	let popupEl: HTMLDivElement | undefined = $state();

	onMount(() => {
		void load();
		// A press anywhere outside the live view cancels too (and still does
		// what it was meant to do there). Inside, the scrim catches it.
		const outside = (e: PointerEvent) => {
			const target = e.target as Node | null;
			if (target && boundary && !boundary.contains(target)) onCancel();
		};
		const escape = (e: KeyboardEvent) => {
			if (e.key === 'Escape' && !popupEl?.contains(e.target as Node)) {
				e.preventDefault();
				onCancel();
			}
		};
		document.addEventListener('pointerdown', outside, true);
		window.addEventListener('keydown', escape);
		return () => {
			document.removeEventListener('pointerdown', outside, true);
			window.removeEventListener('keydown', escape);
		};
	});

	function swallow(e: Event) {
		e.preventDefault();
		e.stopPropagation();
	}
</script>

<!-- A press on the screencast around the popup cancels it and never reaches the page. -->
<div
	class="bp-scrim"
	class:sheet
	aria-hidden="true"
	onpointerdown={(e) => {
		swallow(e);
		onCancel();
	}}
	onwheel={swallow}
	oncontextmenu={swallow}
></div>
{#if !sheet}<div class="bp-ring" style={ringStyle} aria-hidden="true"></div>{/if}

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
	bind:this={popupEl}
	class="bp"
	class:sheet
	class:above={!sheet && place.placement === 'above'}
	style={popupStyle}
	role="dialog"
	aria-label={`${KIND_LABEL[kind]}: ${field.label}`}
	tabindex="-1"
	onpointerdown={(e) => e.stopPropagation()}
	onpointerup={(e) => e.stopPropagation()}
	onwheel={(e) => e.stopPropagation()}
	oncontextmenu={(e) => e.stopPropagation()}
	onkeydown={(e) => {
		e.stopPropagation();
		if (e.key === 'Escape') {
			e.preventDefault();
			onCancel();
		}
	}}
	onkeyup={(e) => e.stopPropagation()}
>
	{#if sheet}<div class="bp-grip" aria-hidden="true"></div>{/if}
	<header class="bp-head">
		<span class="bp-kind">{KIND_LABEL[kind]}</span>
		<span class="bp-label" title={field.label}>{field.label}</span>
		<button type="button" class="bp-close" aria-label="Close without changing" onclick={onCancel}>
			<X size={15} strokeWidth={2} />
		</button>
	</header>

	<div class="bp-body" class:busy={applying} aria-busy={applying}>
		{#if applyError}
			<div class="bp-error" class:inline={kind !== 'select'} role="alert">
				<p>{applyError}</p>
				{#if kind === 'select'}
					<button type="button" class="bp-retry" onclick={load} disabled={loading}>
						<RotateCw size={13} strokeWidth={2} /> Open the list again
					</button>
				{/if}
			</div>
		{/if}
		{#if kind === 'select'}
			{#if applyError}
				<!-- The list on screen may no longer be the page's: no choosing from it. -->
			{:else if choices}
				<BrowserSelectList {choices} label={field.label} onChoose={chooseOption} {onCancel} />
			{:else if loadError}
				<div class="bp-error" role="alert">
					<p>{loadError}</p>
					<button type="button" class="bp-retry" onclick={load} disabled={loading}>
						<RotateCw size={13} strokeWidth={2} /> Try again
					</button>
				</div>
			{:else}
				<div class="bp-loading" role="status" aria-label="Loading options">
					{#each [72, 54, 86, 64, 48] as w, i (i)}
						<span class="bp-skel" style={`width:${w}%;animation-delay:${i * 70}ms`}></span>
					{/each}
				</div>
			{/if}
		{:else if limits}
			<BrowserDateTimePicker
				kind={kind as DateKind}
				value={field.value}
				{limits}
				{now}
				label={field.label}
				onChoose={(value) => void apply({ value })}
				{onCancel}
			/>
			{#if zoneNote}
				<p class="bp-zone" title="Today and Now follow the page's clock, not this device's">Page time zone: {zoneNote.replace(/_/g, ' ')}</p>
			{/if}
		{/if}
	</div>
</div>

<style>
	.bp-scrim {
		position: absolute;
		inset: 0;
		z-index: 10;
		background: transparent;
		touch-action: none;
	}
	.bp-scrim.sheet {
		background: color-mix(in oklch, var(--background) 45%, transparent);
		animation: bp-fade 180ms ease-out both;
	}

	/* The page field this popup stands in for: the same two-band ring as the
	   panel's text overlays, so it reads on light and dark pages alike. */
	.bp-ring {
		position: absolute;
		z-index: 11;
		border-radius: 3px;
		pointer-events: none;
		box-shadow:
			0 0 0 2px var(--background, #fff),
			0 0 0 4px var(--ring, #6750a4),
			0 0 14px 5px color-mix(in oklch, var(--ring, #6750a4) 32%, transparent);
		animation: bp-fade 160ms ease-out both;
	}

	.bp {
		--bp-shift: -6px;
		position: absolute;
		z-index: 12;
		display: flex;
		flex-direction: column;
		min-height: 0;
		box-sizing: border-box;
		padding-bottom: 8px;
		overflow: hidden;
		color: var(--popover-foreground);
		background: var(--popover);
		border: 1px solid var(--border-strong, var(--border));
		border-radius: var(--radius);
		box-shadow: var(--shadow-l, 0 14px 34px rgba(0, 0, 0, 0.3));
		outline: none;
		font-family: var(--font-sans);
		transform-origin: top left;
		animation: bp-in 150ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
	}
	/* The tether: an accent edge on the side that faces the field. */
	.bp::before {
		content: '';
		position: absolute;
		left: 0;
		right: 0;
		top: 0;
		height: 2px;
		background: linear-gradient(90deg, var(--primary), color-mix(in oklch, var(--primary) 0%, transparent) 85%);
		pointer-events: none;
	}
	.bp.above {
		--bp-shift: 6px;
		transform-origin: bottom left;
	}
	.bp.above::before {
		top: auto;
		bottom: 0;
	}

	.bp.sheet {
		left: 0;
		right: 0;
		bottom: 0;
		max-height: min(92%, 720px);
		border-width: 1px 0 0;
		border-radius: calc(var(--radius) + 6px) calc(var(--radius) + 6px) 0 0;
		padding-bottom: max(10px, env(safe-area-inset-bottom));
		animation: bp-sheet 220ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
	}
	.bp.sheet::before {
		display: none;
	}
	.bp-grip {
		width: 36px;
		height: 4px;
		margin: 8px auto 0;
		border-radius: 999px;
		background: color-mix(in oklch, var(--muted-foreground) 45%, transparent);
		flex-shrink: 0;
	}

	.bp-head {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 8px 8px 14px;
		flex-shrink: 0;
	}
	.bp-kind {
		flex-shrink: 0;
		font-family: var(--font-mono);
		font-size: 0.625rem;
		font-weight: 600;
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: var(--primary);
	}
	.bp-label {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
	}
	.bp-close {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		flex-shrink: 0;
		color: var(--muted-foreground);
		background: transparent;
		border: 0;
		border-radius: 8px;
		cursor: pointer;
		transition: background-color 120ms ease, color 120ms ease;
	}
	.bp-close:hover {
		color: var(--foreground);
		background: var(--hover-overlay, color-mix(in oklch, var(--muted) 70%, transparent));
	}
	.bp-close:focus-visible,
	.bp-retry:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.bp-body {
		display: flex;
		flex-direction: column;
		flex: 1;
		min-height: 0;
		transition: opacity 120ms ease;
	}
	/* Waiting for the page to take the choice: one at a time. */
	.bp-body.busy {
		opacity: 0.6;
		pointer-events: none;
		cursor: progress;
	}
	.bp-zone {
		margin: 2px 14px 0;
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.bp-loading {
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 8px 16px 12px;
	}
	.bp-skel {
		display: block;
		height: 12px;
		border-radius: 6px;
		background: linear-gradient(
			90deg,
			color-mix(in oklch, var(--muted) 90%, transparent) 0%,
			color-mix(in oklch, var(--muted-foreground) 22%, transparent) 50%,
			color-mix(in oklch, var(--muted) 90%, transparent) 100%
		);
		background-size: 200% 100%;
		animation: bp-shimmer 1.1s ease-in-out infinite;
	}

	.bp-error {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 10px;
		padding: 4px 14px 8px;
	}
	.bp-error.inline {
		padding-bottom: 4px;
	}
	.bp-error p {
		margin: 0;
		font-size: 0.8125rem;
		line-height: 1.4;
		color: var(--destructive);
	}
	.bp-retry {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 30px;
		padding: 0 12px;
		font: inherit;
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--foreground);
		background: transparent;
		border: 1px solid var(--border-strong, var(--border));
		border-radius: 8px;
		cursor: pointer;
	}
	.bp-retry:hover:not(:disabled) {
		background: var(--hover-overlay, color-mix(in oklch, var(--muted) 70%, transparent));
	}
	.bp-retry:disabled {
		opacity: 0.5;
		cursor: progress;
	}

	@media (pointer: coarse) {
		.bp-close {
			width: 44px;
			height: 44px;
		}
		.bp-retry {
			height: 44px;
			padding: 0 16px;
		}
	}

	@keyframes bp-in {
		from {
			opacity: 0;
			transform: translateY(var(--bp-shift)) scale(0.985);
		}
	}
	@keyframes bp-sheet {
		from {
			transform: translateY(100%);
		}
	}
	@keyframes bp-fade {
		from {
			opacity: 0;
		}
	}
	@keyframes bp-shimmer {
		from {
			background-position: 100% 0;
		}
		to {
			background-position: -100% 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.bp,
		.bp.sheet,
		.bp-scrim.sheet,
		.bp-ring {
			animation: none;
		}
		.bp-skel {
			animation: none;
		}
	}
</style>
