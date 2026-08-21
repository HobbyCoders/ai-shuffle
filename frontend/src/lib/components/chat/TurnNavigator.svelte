<script lang="ts">
	/** Previous/next user-turn navigation, adapted from T3 Code's transcript minimap.
	 * The compact rail stays inside the transcript viewport and never touches the
	 * composer island. */
	import type { UserTurnNavigationItem } from '$lib/utils/transcript-navigation';
	import { resolveTurnNavigationIndex } from '$lib/utils/transcript-navigation';

	interface Props {
		items: UserTurnNavigationItem[];
		currentIndex: number;
		onSelect: (index: number) => void;
	}

	let { items, currentIndex, onSelect }: Props = $props();
	let focused = $state(false);
	const resolvedIndex = $derived(Math.max(0, Math.min(items.length - 1, currentIndex)));
	const current = $derived(items[resolvedIndex] ?? items[0]);
	const previous = $derived(items[resolvedIndex - 1]);
	const next = $derived(items[resolvedIndex + 1]);

	function move(delta: -1 | 1) {
		const destination = resolveTurnNavigationIndex(resolvedIndex, items.length, delta);
		if (destination !== resolvedIndex) onSelect(destination);
	}
</script>

<nav
	class="turn-nav"
	class:is-focused={focused}
	aria-label="Navigate user turns"
	onfocusin={() => (focused = true)}
	onfocusout={() => (focused = false)}
>
	<div class="turn-preview" aria-hidden="true">
		<span>{current?.label ?? 'User message'}</span>
	</div>
	<button
		type="button"
		class="turn-button"
		onclick={() => move(-1)}
		disabled={resolvedIndex <= 0}
		aria-label={previous ? `Previous user message: ${previous.label}` : 'No previous user message'}
		title="Previous user message"
	>
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
			<path d="m18 15-6-6-6 6" />
		</svg>
	</button>
	<span class="turn-count" aria-label={`User message ${resolvedIndex + 1} of ${items.length}`}>{resolvedIndex + 1}<span aria-hidden="true">/</span><span class="sr-only"> of </span>{items.length}</span>
	<button
		type="button"
		class="turn-button"
		onclick={() => move(1)}
		disabled={resolvedIndex >= items.length - 1}
		aria-label={next ? `Next user message: ${next.label}` : 'No next user message'}
		title="Next user message"
	>
		<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
			<path d="m6 9 6 6 6-6" />
		</svg>
	</button>
</nav>

<style>
	.turn-nav {
		position: absolute;
		right: max(10px, calc((100% - 768px) / 2 - 48px));
		top: 50%;
		z-index: 12;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 2px;
		padding: 4px;
		border: 1px solid color-mix(in oklch, var(--border) 78%, transparent);
		border-radius: 10px;
		background: color-mix(in oklch, var(--background) 86%, transparent);
		box-shadow: var(--shadow-s);
		backdrop-filter: blur(10px);
		transform: translateY(-50%);
		color: var(--subtle-foreground);
		transition: border-color 150ms ease, color 150ms ease, opacity 150ms ease;
	}

	.turn-nav:hover,
	.turn-nav.is-focused {
		border-color: var(--border);
		color: var(--muted-foreground);
	}

	.turn-button {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border: 0;
		border-radius: 7px;
		background: transparent;
		color: inherit;
		cursor: pointer;
		transition: background-color 130ms ease, color 130ms ease, transform 130ms ease;
	}

	.turn-button svg { width: 14px; height: 14px; }
	.turn-button:hover:not(:disabled) { background: var(--scrim); color: var(--foreground); }
	.turn-button:active:not(:disabled) { transform: scale(0.92); }
	.turn-button:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.turn-button:disabled { opacity: 0.28; cursor: default; }

	.turn-count {
		font-family: var(--font-mono);
		font-size: 9.5px;
		font-variant-numeric: tabular-nums;
		letter-spacing: -0.03em;
		color: var(--subtle-foreground);
	}

	.turn-preview {
		position: absolute;
		right: calc(100% + 8px);
		top: 50%;
		width: min(260px, calc(100vw - 96px));
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--popover);
		box-shadow: var(--shadow-m);
		font-size: 11.5px;
		line-height: 1.45;
		color: var(--muted-foreground);
		opacity: 0;
		pointer-events: none;
		transform: translate(5px, -50%);
		transition: opacity 150ms ease, transform 150ms ease;
	}

	.turn-preview span {
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.turn-nav:hover .turn-preview,
	.turn-nav.is-focused .turn-preview {
		opacity: 1;
		transform: translate(0, -50%);
	}

	@media (max-width: 880px) {
		.turn-nav { right: 8px; opacity: 0.72; }
		.turn-nav:hover,
		.turn-nav.is-focused { opacity: 1; }
		.turn-preview { display: none; }
	}

	@media (max-width: 639px), (pointer: coarse) {
		.turn-nav {
			top: auto;
			bottom: 12px;
			flex-direction: row;
			transform: none;
		}
		.turn-button { width: 44px; height: 44px; }
		.turn-preview { display: none; }
	}

	@media (prefers-reduced-motion: reduce) {
		.turn-nav,
		.turn-button,
		.turn-preview { transition: none; }
	}
</style>
