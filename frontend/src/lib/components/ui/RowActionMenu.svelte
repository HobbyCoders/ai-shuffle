<script lang="ts">
	/**
	 * RowActionMenu — the collapsed-⋮ + animated slide-out action rail shared by
	 * any list row that carries 3+ per-row action buttons (session rows, file
	 * rows, …). It renders the caller's buttons inside a tray plus a single
	 * vertical ⋮ toggle.
	 *
	 * Desktop shell with a fine pointer: the toggle is hidden and the tray sits inline;
	 * the row reveals it on hover. Reveal is driven entirely by the parent through
	 * the inherited `--ram-reveal` custom property (0 hidden / 1 shown) so this
	 * component never has to reach across a scoping boundary to know the row's
	 * hover state — set `--ram-reveal: 1` on any button that should stay visible
	 * at rest (e.g. an armed delete confirm).
	 *
	 * Touch devices and the mobile shell: the four-ish buttons
	 * collapse behind the ⋮. Tapping it floats the tray out to the LEFT so opening
	 * never reflows the row, and the buttons peel out staggered nearest-first. The
	 * tray stretches to the row's content height so it reads as part of the row,
	 * not a detached pill. Honors prefers-reduced-motion by snapping.
	 *
	 * Open/close state lives with the parent (one-open-at-a-time, outside-click and
	 * Escape to close), which keeps this component presentational. Built on theme
	 * tokens only.
	 *
	 * Optional `persistent` snippet — action(s) that must stay visible on the row
	 * even where `children` collapse behind the ⋮. On desktop it renders inline at
	 * the end of the rail with the same hover-reveal as `children` (via the shared
	 * `--ram-reveal`), visually indistinguishable from a `children` button. On
	 * touch / mobile it sits OUTSIDE the collapsing tray, always visible, just left
	 * of the ⋮ toggle and sized to match it. Omit it and every existing consumer is
	 * untouched. Use it for the one action a row can't afford to bury (e.g. a
	 * session's Complete).
	 */
	import { EllipsisVertical } from 'lucide-svelte';
	import type { Snippet } from 'svelte';

	interface Props {
		/** Whether the tray is expanded (touch / mobile only). */
		open: boolean;
		/** Toggle handler. The component stops propagation before calling it so a
		 *  row-level click (open the item) never also fires. */
		onToggle: (e: MouseEvent | KeyboardEvent) => void;
		/** Accessible name for the ⋮ toggle (also its tooltip). */
		label?: string;
		/** The action buttons. Each direct child animates independently. */
		children: Snippet;
		/** Optional always-visible action(s): inline in the rail on desktop, but
		 *  kept OUT of the collapsing tray (always shown) on touch / mobile. */
		persistent?: Snippet;
	}

	let { open, onToggle, label = 'Actions', children, persistent }: Props = $props();

	function handleToggle(e: MouseEvent | KeyboardEvent) {
		e.stopPropagation();
		onToggle(e);
	}
</script>

<span class="ram" class:open>
	{#if persistent}
		<span class="ram-persistent">{@render persistent()}</span>
	{/if}
	<span class="ram-tray">{@render children()}</span>
	<button
		class="ram-toggle"
		aria-label={label}
		aria-expanded={open}
		title={label}
		onclick={handleToggle}
	>
		<EllipsisVertical size={16} strokeWidth={1.6} />
	</button>
</span>

<style>
	.ram {
		position: relative;
		display: inline-flex;
		align-items: center;
		align-self: stretch;
		justify-content: flex-end;
		gap: 2px;
		flex-shrink: 0;
	}
	.ram-tray {
		display: flex;
		align-items: center;
		gap: 2px;
	}
	/* The always-visible slot lives beside the tray. On desktop it's just another
	   inline segment of the rail (same reveal below); on touch / mobile it's the one
	   thing that stays put while the tray collapses (blocks further down). */
	.ram-persistent {
		display: inline-flex;
		align-items: center;
		gap: 2px;
	}

	/* Desktop reveal is the parent's call, signalled through the inherited
	   `--ram-reveal` custom property; default 1 keeps buttons visible if a caller
	   opts out of hover-reveal entirely. The persistent slot rides the same signal
	   so it's indistinguishable from a tray button at rest and on hover. */
	.ram-tray > :global(*),
	.ram-persistent > :global(*) {
		opacity: var(--ram-reveal, 1);
		transition: opacity 120ms ease, color 120ms ease, background-color 120ms ease;
	}

	/* The ⋮ trigger only exists for touch / mobile layouts — desktop reveals the
	   whole rail on row hover, so keep it out of the flow entirely. */
	.ram-toggle {
		display: none;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		flex-shrink: 0;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 120ms ease, background-color 120ms ease;
	}
	.ram-toggle:hover {
		color: var(--foreground);
		background: color-mix(in oklch, var(--muted) 60%, transparent);
	}
	.ram-toggle:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	/* Touch devices: collapse the rail behind the ⋮. (hover: none) covers real
	   touch hardware, including touch laptops. */
	@media (hover: none) {
		.ram-toggle { display: flex; }

		/* The persistent slot is NOT part of the collapsing tray: it stays in normal
		   flow just left of the ⋮ and is always shown. There's no row hover on touch,
		   so the desktop `--ram-reveal` (0 at rest) would otherwise hide it — pin it. */
		.ram-persistent > :global(*) {
			opacity: 1;
			transform: none;
		}

		/* Float the tray out to the LEFT of the ⋮ so opening never reflows the
		   row. top/bottom:0 against the stretched .ram makes the tray match the
		   row's content height — it reads as part of the row, not a loose pill. */
		.ram-tray {
			position: absolute;
			right: calc(100% + 6px);
			top: 0;
			bottom: 0;
			padding: 0 6px;
			border-radius: 10px;
			background: var(--popover);
			border: 1px solid var(--border);
			box-shadow: var(--shadow-l);
			z-index: 6;

			/* Closed: collapsed toward the toggle, anchored at its right edge. */
			opacity: 0;
			transform: translateX(6px) scale(0.98);
			transform-origin: right center;
			pointer-events: none;
			transition: opacity 150ms ease, transform 210ms cubic-bezier(0.22, 1, 0.36, 1);
		}
		.ram.open .ram-tray {
			opacity: 1;
			transform: none;
			pointer-events: auto;
		}

		/* Buttons peel out from behind the ⋮, staggered nearest-first, on one
		   smooth ease (no overshoot bounce — that read as jittery before). */
		.ram-tray > :global(*) {
			opacity: 0;
			transform: translateX(10px);
			transition: opacity 170ms ease, transform 260ms cubic-bezier(0.22, 1, 0.36, 1);
		}
		.ram.open .ram-tray > :global(*) {
			opacity: 1;
			transform: translateX(0);
		}
		.ram.open .ram-tray > :global(*:nth-last-child(1)) { transition-delay: 30ms; }
		.ram.open .ram-tray > :global(*:nth-last-child(2)) { transition-delay: 62ms; }
		.ram.open .ram-tray > :global(*:nth-last-child(3)) { transition-delay: 94ms; }
		.ram.open .ram-tray > :global(*:nth-last-child(4)) { transition-delay: 126ms; }
		.ram.open .ram-tray > :global(*:nth-last-child(5)) { transition-delay: 158ms; }

		/* Toggle reads as pressed while its rail is open. */
		.ram.open .ram-toggle {
			color: var(--foreground);
			background: color-mix(in oklch, var(--muted) 60%, transparent);
		}
	}

	/* Roomier touch target on phones (matches the sessions rail's 38px buttons). */
	@media (max-width: 639px) {
		.ram-toggle { width: 38px; height: 38px; }

		/* Enlarge EVERY tray button to the sessions rail's 38px touch target so
		   all consumers render at one size on phones regardless of their smaller
		   desktop dimensions (26–36px). Icon glyphs keep their px; only the hit
		   area grows (buttons centre their icon via grid/flex place-items). It's
		   an idempotent min-size, so SessionsBoard's own 38px buttons here stay
		   pixel-identical. */
		.ram-tray > :global(button),
		.ram-persistent > :global(button) {
			min-width: 38px;
			min-height: 38px;
		}

		/* The floating tray is bounded to the row's content height (top/bottom:0
		   against the stretched .ram). Guard a floor so the 38px buttons never
		   poke out of a short row's tray — instead the tray grows downward past
		   the row, the way the taller sessions rows already contain it. Sessions'
		   own ≥64px rows sit above this floor, so it's a no-op there. */
		.ram-tray { min-height: 44px; }
	}

	@media (prefers-reduced-motion: reduce) {
		/* Snap the rail in/out — no slide, no stagger. */
		.ram-tray,
		.ram-tray > :global(*),
		.ram-persistent > :global(*) { transition: none; }
		.ram-tray { transform: none; }
		.ram.open .ram-tray > :global(*) { transform: none; transition-delay: 0s; }
	}
</style>
