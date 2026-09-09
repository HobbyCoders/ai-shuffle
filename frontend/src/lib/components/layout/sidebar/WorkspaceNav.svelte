<script lang="ts">
	/**
	 * WorkspaceNav — the customizable list of Workspace destinations.
	 *
	 * Renders the rows the user chose to keep inline, then a "More" row that
	 * opens a flyout holding everything else plus the Customize entry. The
	 * split, the order and the persistence all live in `sidebar-prefs`; this
	 * component only decides where the flyout is drawn and how it is driven.
	 *
	 * ANCHORING. The nav sits inside the sidebar's scroll region, so an
	 * absolutely-positioned flyout would be clipped by that container the moment
	 * it grew past the visible rows. The flyout is therefore `position: fixed`
	 * with coordinates measured from the More row at open time: to the RIGHT of
	 * the sidebar plane on desktop (where there is room beside the panel) and
	 * BELOW the row, spanning the panel, when the panel is fullscreen (the
	 * mobile shell's drawer).
	 * Because the coordinates are a snapshot, the host closes the flyout on
	 * scroll rather than tracking it — which is also what the mockup does.
	 *
	 * `open` is bindable so the host (Sidebar) can close it from the single
	 * window click / Escape listener it already owns, instead of this component
	 * adding a second pair.
	 */
	import { Ellipsis, ChevronRight, SlidersHorizontal } from 'lucide-svelte';
	import {
		splitWorkspaceItems,
		workspacePrefs,
		type WorkspaceItemId
	} from '$lib/stores/sidebar-prefs.svelte';
	import type { WorkspaceNavItem } from './workspace-items';

	interface Props {
		/** Destinations the current role may open, in canonical order. */
		items: WorkspaceNavItem[];
		onPick: (id: WorkspaceItemId) => void;
		/** Intent prefetch for the lazy panel chunk behind a destination. */
		onPreload: (id: WorkspaceItemId) => void;
		onCustomize: () => void;
		/** Panel is fullscreen — the mobile shell's drawer: drop the flyout
		 *  below the row instead of beside the panel. */
		fullscreen?: boolean;
		open?: boolean;
	}

	let {
		items,
		onPick,
		onPreload,
		onCustomize,
		fullscreen = false,
		open = $bindable(false)
	}: Props = $props();

	const split = $derived(splitWorkspaceItems(items, $workspacePrefs));

	let moreRef = $state<HTMLButtonElement | null>(null);
	let flyout = $state({ top: 0, left: 0, width: 208 });

	/** Measure once, at open: see the ANCHORING note above. */
	function place() {
		if (!moreRef) return;
		const row = moreRef.getBoundingClientRect();
		const panel = moreRef.closest('aside')?.getBoundingClientRect() ?? row;
		if (fullscreen) {
			flyout = {
				top: row.bottom + 4,
				left: panel.left + 8,
				width: Math.max(160, panel.width - 16)
			};
		} else {
			flyout = {
				top: Math.max(8, row.top - 6),
				left: panel.right - 4,
				width: 208
			};
		}
	}

	function toggle() {
		if (open) {
			open = false;
			return;
		}
		place();
		open = true;
	}

	function pick(id: WorkspaceItemId) {
		open = false;
		onPick(id);
	}

	function customize() {
		open = false;
		onCustomize();
	}
</script>

<nav class="ws-list" aria-label="Workspace">
	<!--
		Intent prefetch: `pointerenter` fires once per genuine entry into the row
		(not per render, not per scroll tick, and — unlike `pointermove` or
		`pointerover` — not repeatedly while the pointer sits inside), and `focus`
		gives keyboard and screen-reader users the identical warm-up.
		`preloadPanelForView` dedupes against the resolved/in-flight chunk, so
		re-entering a row costs nothing beyond the call itself. Flyout items get
		the same treatment: a destination folded into More is no less likely to be
		the one you are reaching for.
	-->
	{#each split.shown as item (item.id)}
		<button
			class="ghostrow wsrow"
			class:active={item.on}
			aria-current={item.on ? 'page' : undefined}
			onclick={() => onPick(item.id)}
			onpointerenter={() => onPreload(item.id)}
			onfocus={() => onPreload(item.id)}
		>
			<item.icon size={16} strokeWidth={1.85} />
			<span class="label">{item.label}</span>
		</button>
	{/each}

	<button
		class="ghostrow wsrow morerow"
		bind:this={moreRef}
		aria-haspopup="menu"
		aria-expanded={open}
		onclick={toggle}
	>
		<Ellipsis size={16} strokeWidth={1.85} />
		<span class="label">More</span>
		<span class="trail" aria-hidden="true"><ChevronRight size={13} strokeWidth={2} /></span>
	</button>
</nav>

{#if open}
	<div
		class="flyout pop"
		class:below={fullscreen}
		role="menu"
		aria-label="More workspace destinations"
		style="top:{flyout.top}px; left:{flyout.left}px; width:{flyout.width}px;"
	>
		{#each split.hidden as item (item.id)}
			<button
				class="mi"
				role="menuitem"
				onclick={() => pick(item.id)}
				onpointerenter={() => onPreload(item.id)}
				onfocus={() => onPreload(item.id)}
			>
				<item.icon size={15} strokeWidth={1.85} />
				<span class="mi-label">{item.label}</span>
			</button>
		{/each}
		{#if split.hidden.length > 0}
			<div class="sep" role="separator"></div>
		{/if}
		<button class="mi" role="menuitem" onclick={customize}>
			<SlidersHorizontal size={15} strokeWidth={1.85} />
			<span class="mi-label">Customize</span>
		</button>
	</div>
{/if}

<style>
	.ws-list {
		display: flex;
		flex-direction: column;
		gap: 1px;
	}

	/* Same ghost-row language as the New session row above the scroll region, so
	   the sticky block and the list beneath it read as one rhythm. */
	.ghostrow {
		width: 100%;
		display: flex;
		align-items: center;
		gap: 9px;
		height: 32px;
		padding: 0 8px;
		border: none;
		background: transparent;
		border-radius: 8px;
		color: var(--sidebar-muted);
		cursor: pointer;
		text-align: left;
		transition: background-color 0.13s ease, color 0.13s ease;
	}
	.ghostrow:hover { background: var(--sidebar-accent); color: var(--foreground); }
	.ghostrow :global(svg) { color: var(--subtle-foreground); transition: color 0.13s ease; flex: none; }
	.ghostrow:hover :global(svg) { color: var(--foreground); }
	.ghostrow .label {
		font-size: 13px;
		margin-right: auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.wsrow.active,
	.wsrow.active:hover { background: var(--sidebar-accent-active); color: var(--foreground); }
	.wsrow.active :global(svg) { color: var(--primary); }

	.morerow[aria-expanded='true'] { background: var(--sidebar-accent-active); color: var(--foreground); }
	.trail { display: inline-flex; }

	/* One popover recipe: lit top edge, blur, cast shadow, short spring in. */
	.pop {
		position: fixed;
		z-index: 120;
		padding: 6px;
		border-radius: 12px;
		background: color-mix(in oklch, var(--popover) 90%, transparent);
		backdrop-filter: blur(16px) saturate(1.2);
		-webkit-backdrop-filter: blur(16px) saturate(1.2);
		border: 1px solid var(--border);
		box-shadow:
			inset 0 1px 0 oklch(1 0 0 / 0.08),
			var(--shadow-l);
		transform-origin: 0 12px;
		animation: pop-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
	}
	.pop.below { transform-origin: 24px 0; }

	.mi {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		height: 32px;
		padding: 0 8px;
		border: none;
		background: transparent;
		border-radius: 7px;
		color: var(--popover-foreground);
		font-size: 12.5px;
		text-align: left;
		cursor: pointer;
		transition: background-color 0.12s ease;
	}
	.mi:hover { background: var(--accent); }
	.mi :global(svg) { flex: none; color: var(--muted-foreground); }
	.mi:hover :global(svg) { color: var(--foreground); }
	.mi-label { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.sep { height: 1px; margin: 5px 4px; background: var(--border); }

	@keyframes pop-in {
		from { opacity: 0; transform: scale(0.94) translateY(-4px); }
		to { opacity: 1; transform: none; }
	}

	:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }

	/* The same rows serve the desktop sidebar and the mobile drawer; on the
	   mobile shell — and on any touch-only pointer — enlarge them toward the
	   44px touch minimum. */
	@media (hover: none) {
		.ghostrow { height: 40px; }
		.mi { height: 40px; }
	}

	@media (prefers-reduced-motion: reduce) {
		.ghostrow, .mi { transition: none; }
		.pop { animation: none; }
	}
</style>
