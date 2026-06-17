<script lang="ts">
	/**
	 * ContextMenu — a reusable positioned popup menu.
	 *
	 * Owns the menu container, viewport-clamped positioning, and dismissal
	 * (Escape + outside pointer-down). Menu items are supplied as a snippet so
	 * each caller keeps its own item markup/styling (e.g. `.ctx-btn` buttons).
	 *
	 * Usage:
	 *   <ContextMenu open={!!menu} x={menu?.x ?? 0} y={menu?.y ?? 0} onClose={() => (menu = null)}>
	 *     <button class="ctx-btn" onclick={...}>Rename</button>
	 *   </ContextMenu>
	 */
	import type { Snippet } from 'svelte';

	interface Props {
		open: boolean;
		/** Requested anchor point (e.g. the cursor position). */
		x: number;
		y: number;
		onClose: () => void;
		minWidth?: number;
		children: Snippet;
	}

	let { open, x, y, onClose, minWidth = 180, children }: Props = $props();

	let menuEl: HTMLDivElement | undefined = $state();
	// Clamped position, recomputed once the menu is measured. Null until then,
	// so the first paint uses the raw anchor (x, y) — no (0,0) flicker.
	let clamped = $state<{ x: number; y: number } | null>(null);

	// Recompute whenever the menu opens, the anchor moves, or the element binds.
	$effect(() => {
		if (!open) {
			clamped = null;
			return;
		}
		if (!menuEl) return;
		const rect = menuEl.getBoundingClientRect();
		const pad = 8;
		let nx = x;
		let ny = y;
		if (nx + rect.width > window.innerWidth - pad) nx = window.innerWidth - rect.width - pad;
		if (ny + rect.height > window.innerHeight - pad) ny = window.innerHeight - rect.height - pad;
		if (nx < pad) nx = pad;
		if (ny < pad) ny = pad;
		clamped = { x: nx, y: ny };
	});

	const left = $derived(clamped?.x ?? x);
	const top = $derived(clamped?.y ?? y);

	function onWindowKeydown(e: KeyboardEvent) {
		if (open && e.key === 'Escape') {
			e.stopPropagation();
			onClose();
		}
	}

	function onWindowPointerdown(e: PointerEvent) {
		// A pointer-down anywhere outside the menu dismisses it. Pointer-down
		// (not click) so the menu closes before a click lands elsewhere.
		if (open && menuEl && !menuEl.contains(e.target as Node)) {
			onClose();
		}
	}
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerdown} />

{#if open}
	<div
		bind:this={menuEl}
		class="context-menu"
		style="left: {left}px; top: {top}px; min-width: {minWidth}px;"
		onclick={(e) => e.stopPropagation()}
		onkeydown={(e) => e.stopPropagation()}
		role="menu"
		tabindex="-1"
	>
		{@render children()}
	</div>
{/if}

<style>
	.context-menu {
		position: fixed;
		background: oklch(0.13 0.01 260);
		border: 1px solid oklch(0.25 0.015 260);
		border-radius: 0.75rem;
		box-shadow:
			0 8px 32px rgba(0, 0, 0, 0.5),
			0 0 0 1px oklch(0.2 0.01 260);
		z-index: 100;
		overflow: hidden;
		padding: 0.375rem;
		animation: contextIn 0.12s ease;
	}

	@keyframes contextIn {
		from {
			opacity: 0;
			transform: scale(0.95);
		}
		to {
			opacity: 1;
			transform: scale(1);
		}
	}
</style>
