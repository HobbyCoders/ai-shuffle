<script lang="ts">
	/**
	 * RowActionMenu — the collapsed-⋮ + animated slide-out action rail shared by
	 * any list row that carries 3+ per-row action buttons (session rows, file
	 * rows, …). It renders the caller's buttons inside a tray plus a single
	 * vertical ⋮ toggle.
	 *
	 * Desktop (fine pointer, wide): the toggle is hidden and the tray sits inline;
	 * the row reveals it on hover. Reveal is driven entirely by the parent through
	 * the inherited `--ram-reveal` custom property (0 hidden / 1 shown) so this
	 * component never has to reach across a scoping boundary to know the row's
	 * hover state — set `--ram-reveal: 1` on any button that should stay visible
	 * at rest (e.g. an active favourite).
	 *
	 * Touch / narrow (≤1080px, the app-wide mobile cutoff): the four-ish buttons
	 * collapse behind the ⋮. Tapping it floats the tray out to the LEFT so opening
	 * never reflows the row, and the buttons peel out staggered nearest-first. The
	 * tray stretches to the row's content height so it reads as part of the row,
	 * not a detached pill. Honors prefers-reduced-motion by snapping.
	 *
	 * Open/close state lives with the parent (one-open-at-a-time, outside-click and
	 * Escape to close), which keeps this component presentational. Built on theme
	 * tokens only.
	 */
	import { EllipsisVertical } from 'lucide-svelte';
	import type { Snippet } from 'svelte';

	interface Props {
		/** Whether the tray is expanded (touch/narrow only). */
		open: boolean;
		/** Toggle handler. The component stops propagation before calling it so a
		 *  row-level click (open the item) never also fires. */
		onToggle: (e: MouseEvent | KeyboardEvent) => void;
		/** Accessible name for the ⋮ toggle (also its tooltip). */
		label?: string;
		/** The action buttons. Each direct child animates independently. */
		children: Snippet;
	}

	let { open, onToggle, label = 'Actions', children }: Props = $props();

	function handleToggle(e: MouseEvent | KeyboardEvent) {
		e.stopPropagation();
		onToggle(e);
	}
</script>

<span class="ram" class:open>
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

	/* Desktop reveal is the parent's call, signalled through the inherited
	   `--ram-reveal` custom property; default 1 keeps buttons visible if a caller
	   opts out of hover-reveal entirely. */
	.ram-tray > :global(*) {
		opacity: var(--ram-reveal, 1);
		transition: opacity 120ms ease, color 120ms ease, background-color 120ms ease;
	}

	/* The ⋮ trigger only exists for touch / narrow layouts — desktop reveals the
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

	/* Touch / mobile-width: collapse the rail behind the ⋮. (hover: none) covers
	   real touch devices incl. unfolded foldables; 1080px matches the app-wide
	   mobile cutoff so the desktop resize-test path works too. */
	@media (hover: none), (max-width: 1080px) {
		.ram-toggle { display: flex; }

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
	}

	@media (prefers-reduced-motion: reduce) {
		/* Snap the rail in/out — no slide, no stagger. */
		.ram-tray,
		.ram-tray > :global(*) { transition: none; }
		.ram-tray { transform: none; }
		.ram.open .ram-tray > :global(*) { transform: none; transition-delay: 0s; }
	}
</style>
