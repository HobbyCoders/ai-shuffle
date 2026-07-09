<script lang="ts" module>
	import type { Snippet } from 'svelte';

	export interface ContextMenuItem {
		id: string;
		label: string;
		/** Optional leading icon snippet (rendered at 15px, muted). */
		icon?: Snippet;
		/** Destructive styling (red) — e.g. Clear. */
		danger?: boolean;
		/** Reflects an on/off state (Favorite toggle) — shows a trailing check. */
		active?: boolean;
		disabled?: boolean;
		/** Small right-aligned secondary text (e.g. a device name). */
		hint?: string;
	}
</script>

<script lang="ts">
	import { browser } from '$app/environment';

	interface Props {
		open: boolean;
		/** Anchor position in viewport (clientX / clientY of the gesture). */
		x: number;
		y: number;
		items: ContextMenuItem[];
		ariaLabel?: string;
		/** Optional small header label above the items. */
		heading?: string;
		onSelect: (id: string) => void;
		onClose: () => void;
	}

	let { open, x, y, items, ariaLabel = 'Session actions', heading, onSelect, onClose }: Props = $props();

	// Measured menu box so we can keep it fully inside the viewport. bind:client*
	// updates once the element is laid out; the fade-in masks the initial snap.
	let menuW = $state(0);
	let menuH = $state(0);
	let menuEl = $state<HTMLDivElement | null>(null);
	let focusedIndex = $state(-1);
	const GAP = 8;

	const vw = $derived(browser ? window.innerWidth : 1920);
	const vh = $derived(browser ? window.innerHeight : 1080);

	// Clamp within [GAP, viewport - size - GAP]. If the menu is wider/taller than
	// the anchor allows, it shifts left/up so its far edge stays on-screen.
	const left = $derived(Math.max(GAP, Math.min(x, vw - menuW - GAP)));
	const top = $derived(Math.max(GAP, Math.min(y, vh - menuH - GAP)));

	function firstEnabled(): number {
		return items.findIndex((it) => !it.disabled);
	}

	// On each open, focus the first actionable item for keyboard users.
	$effect(() => {
		if (open) {
			focusedIndex = firstEnabled();
			// Focus after the element exists.
			queueMicrotask(() => {
				const btns = menuEl?.querySelectorAll<HTMLButtonElement>('[data-menuitem]');
				if (btns && focusedIndex >= 0) btns[focusedIndex]?.focus();
			});
		} else {
			focusedIndex = -1;
		}
	});

	function moveFocus(dir: 1 | -1) {
		if (!items.length) return;
		let next = focusedIndex;
		for (let i = 0; i < items.length; i++) {
			next = (next + dir + items.length) % items.length;
			if (!items[next].disabled) break;
		}
		focusedIndex = next;
		const btns = menuEl?.querySelectorAll<HTMLButtonElement>('[data-menuitem]');
		btns?.[next]?.focus();
	}

	function onMenuKeydown(e: KeyboardEvent) {
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			moveFocus(1);
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			moveFocus(-1);
		} else if (e.key === 'Home') {
			e.preventDefault();
			focusedIndex = firstEnabled();
			menuEl?.querySelectorAll<HTMLButtonElement>('[data-menuitem]')[focusedIndex]?.focus();
		} else if (e.key === 'End') {
			e.preventDefault();
			focusedIndex = items.length - 1;
			moveFocus(-1);
			moveFocus(1);
		} else if (e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			onClose();
		} else if (e.key === 'Tab') {
			// A context menu is a transient layer — Tab dismisses rather than
			// leaking focus into the page behind it.
			onClose();
		}
	}

	function choose(item: ContextMenuItem) {
		if (item.disabled) return;
		onSelect(item.id);
	}

	// Close on any scroll (the anchor would drift out from under the menu).
	function onScroll() {
		if (open) onClose();
	}
	function onResize() {
		if (open) onClose();
	}
</script>

<svelte:window onscroll={onScroll} onresize={onResize} />

{#if open}
	<!-- Backdrop: captures the dismiss click/press so it never falls through to a
	     session row underneath. Right-click on it also just closes. -->
	<div
		class="ctx-backdrop"
		role="presentation"
		onpointerdown={(e) => { e.preventDefault(); onClose(); }}
		oncontextmenu={(e) => { e.preventDefault(); onClose(); }}
	></div>

	<!-- height:auto is a defensive inline override: this fixed overlay renders as a
	     flow sibling of its host panel, so an ancestor layout rule such as
	     `.feature-panel > * { height: 100% }` (MainPanel) would otherwise resolve
	     against the viewport and stretch the menu to full screen height. Inline
	     declarations win the cascade over non-!important stylesheet rules, so this
	     keeps the panel hugging its items regardless of where it's mounted. -->
	<div
		bind:this={menuEl}
		bind:clientWidth={menuW}
		bind:clientHeight={menuH}
		class="ctx-menu"
		style="left: {left}px; top: {top}px; height: auto;"
		role="menu"
		aria-label={ariaLabel}
		tabindex="-1"
		onkeydown={onMenuKeydown}
		oncontextmenu={(e) => e.preventDefault()}
	>
		{#if heading}
			<div class="ctx-heading">{heading}</div>
		{/if}
		{#each items as item (item.id)}
			<button
				type="button"
				data-menuitem
				class="ctx-item"
				class:danger={item.danger}
				class:is-active={item.active}
				role="menuitem"
				tabindex="-1"
				disabled={item.disabled}
				aria-disabled={item.disabled}
				onclick={() => choose(item)}
			>
				{#if item.icon}
					<span class="ctx-icon">{@render item.icon()}</span>
				{/if}
				<span class="ctx-label">{item.label}</span>
				{#if item.hint}
					<span class="ctx-hint">{item.hint}</span>
				{/if}
				{#if item.active}
					<svg class="ctx-check" width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.4" d="M5 13l4 4L19 7" />
					</svg>
				{/if}
			</button>
		{/each}
	</div>
{/if}

<style>
	/* Backdrop sits above the board but below the menu; transparent — it only
	   exists to catch the dismiss gesture. */
	.ctx-backdrop {
		position: fixed;
		inset: 0;
		z-index: 1000;
		background: transparent;
	}

	.ctx-menu {
		position: fixed;
		z-index: 1001;
		min-width: 200px;
		max-width: min(260px, calc(100vw - 16px));
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		box-shadow: var(--shadow-l);
		padding: 5px;
		animation: ctx-menu-in 0.14s ease;
		transform-origin: top left;
	}

	@keyframes ctx-menu-in {
		from { opacity: 0; transform: scale(0.97) translateY(-3px); }
		to { opacity: 1; transform: scale(1) translateY(0); }
	}

	.ctx-heading {
		padding: 6px 10px 4px;
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.ctx-item {
		display: flex;
		align-items: center;
		gap: 9px;
		width: 100%;
		padding: 8px 10px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) - 4px);
		color: var(--popover-foreground, var(--foreground));
		font-size: 0.8125rem;
		font-weight: 500;
		text-align: left;
		cursor: pointer;
		transition: background-color 0.12s ease, color 0.12s ease;
	}
	.ctx-item:hover:not(:disabled),
	.ctx-item:focus-visible {
		background: var(--hover-overlay);
		outline: none;
	}
	.ctx-item:focus-visible {
		box-shadow: inset 0 0 0 1.5px color-mix(in oklch, var(--primary) 55%, transparent);
	}
	.ctx-item:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.ctx-item.is-active {
		color: var(--primary);
	}
	.ctx-item.danger {
		color: var(--destructive);
	}
	.ctx-item.danger:hover:not(:disabled),
	.ctx-item.danger:focus-visible {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
	}

	.ctx-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		color: var(--muted-foreground);
	}
	.ctx-item.is-active .ctx-icon { color: var(--primary); }
	.ctx-item.danger .ctx-icon { color: var(--destructive); }
	.ctx-item :global(svg) { flex-shrink: 0; }

	.ctx-label {
		flex: 1 1 auto;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.ctx-hint {
		flex-shrink: 0;
		max-width: 92px;
		font-size: 0.6875rem;
		font-weight: 500;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.ctx-check {
		flex-shrink: 0;
		color: var(--primary);
		margin-left: auto;
	}
	.ctx-item.is-active .ctx-hint + .ctx-check,
	.ctx-item.is-active .ctx-check { margin-left: 2px; }

	/* Touch: roomier hit targets. */
	@media (hover: none) {
		.ctx-item { padding: 11px 12px; font-size: 0.875rem; }
		.ctx-menu { min-width: 220px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.ctx-menu { animation: none; }
	}
</style>
