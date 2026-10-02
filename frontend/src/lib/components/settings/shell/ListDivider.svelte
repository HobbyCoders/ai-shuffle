<script lang="ts">
	/**
	 * ListDivider: the draggable line between a collection's list and detail.
	 *
	 * role=separator with aria-valuenow. Pointer drag (captured); Left/Right
	 * move 16px (Shift 64px); Home/End jump to min/max; Enter or double-click
	 * resets to 384px. The list stays at least 300px and the detail at least
	 * 440px. The width is saved to localStorage when a change ends. Hidden
	 * below 900px of shell width and in stacked mode (settings.css).
	 */
	import {
		clampListWidth,
		maxListWidth,
		writeListWidth,
		LIST_WIDTH_DEFAULT,
		LIST_WIDTH_MIN,
		LIST_WIDTH_STEP,
		LIST_WIDTH_STEP_LARGE
	} from '$lib/settings/list-width';

	interface Props {
		width: number;
		/** Width of the collection (list + detail), for the detail minimum. */
		containerWidth: number;
		onchange?: (width: number) => void;
	}

	let { width = $bindable(LIST_WIDTH_DEFAULT), containerWidth, onchange }: Props = $props();

	let dragging = $state(false);
	let origin = 0;
	const max = $derived(maxListWidth(containerWidth));

	function set(next: number, save: boolean) {
		width = clampListWidth(next, containerWidth);
		onchange?.(width);
		if (save) writeListWidth(width);
	}

	function onpointerdown(event: PointerEvent) {
		if (event.button !== 0) return;
		event.preventDefault();
		const el = event.currentTarget as HTMLElement;
		try {
			el.setPointerCapture(event.pointerId);
		} catch {
			// Synthetic events in tests have no active pointer.
		}
		origin = el.parentElement?.getBoundingClientRect().left ?? 0;
		dragging = true;
		document.body.classList.add('st-resizing');
	}

	function onpointermove(event: PointerEvent) {
		if (!dragging) return;
		set(event.clientX - origin, false);
	}

	function end(event: PointerEvent) {
		if (!dragging) return;
		dragging = false;
		document.body.classList.remove('st-resizing');
		try {
			(event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
		} catch {
			// Already released.
		}
		set(width, true);
	}

	function onkeydown(event: KeyboardEvent) {
		const step = event.shiftKey ? LIST_WIDTH_STEP_LARGE : LIST_WIDTH_STEP;
		let next: number | null = null;
		if (event.key === 'ArrowLeft') next = width - step;
		else if (event.key === 'ArrowRight') next = width + step;
		else if (event.key === 'Home') next = LIST_WIDTH_MIN;
		else if (event.key === 'End') next = Number.MAX_SAFE_INTEGER;
		else if (event.key === 'Enter') next = LIST_WIDTH_DEFAULT;
		if (next === null) return;
		event.preventDefault();
		set(next, true);
	}
</script>

<!-- A focusable separator is an ARIA widget (window splitter). -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
	class="st-divider"
	class:drag={dragging}
	role="separator"
	tabindex="0"
	aria-orientation="vertical"
	aria-label="Resize list"
	aria-valuemin={LIST_WIDTH_MIN}
	aria-valuemax={Number.isFinite(max) ? max : undefined}
	aria-valuenow={width}
	title="Drag to resize, double-click to reset"
	style:left="calc({width}px - 4px)"
	{onpointerdown}
	{onpointermove}
	onpointerup={end}
	onpointercancel={end}
	ondblclick={() => set(LIST_WIDTH_DEFAULT, true)}
	{onkeydown}
></div>

<style>
	.st-divider {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 8px;
		cursor: col-resize;
		z-index: 4;
		touch-action: none;
	}
	.st-divider::after {
		content: '';
		position: absolute;
		top: 0;
		bottom: 0;
		left: 3px;
		width: 2px;
		background: transparent;
		transition: background 0.15s;
	}
	.st-divider:hover::after,
	.st-divider.drag::after {
		background: var(--st-border-strong);
	}
	.st-divider:focus-visible {
		outline: none;
	}
	.st-divider:focus-visible::after {
		background: var(--st-primary);
	}
	:global(body.st-resizing) {
		cursor: col-resize;
		user-select: none;
	}
</style>
