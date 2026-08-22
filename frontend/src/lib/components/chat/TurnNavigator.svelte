<script lang="ts">
	/** Left-rail user-turn minimap. Each tick is a prompt; hover reveals the
	 *  message. Stays in the transcript gutter and never touches the composer. */
	import type { UserTurnNavigationItem } from '$lib/utils/transcript-navigation';
	import { resolveTurnNavigationIndex } from '$lib/utils/transcript-navigation';

	interface Props {
		items: UserTurnNavigationItem[];
		currentIndex: number;
		onSelect: (index: number) => void;
	}

	let { items, currentIndex, onSelect }: Props = $props();
	let focused = $state(false);
	let hoveredIndex = $state<number | null>(null);
	const resolvedIndex = $derived(Math.max(0, Math.min(items.length - 1, currentIndex)));

	function onKeydown(event: KeyboardEvent) {
		if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
		event.preventDefault();
		const destination = resolveTurnNavigationIndex(
			resolvedIndex,
			items.length,
			event.key === 'ArrowUp' ? -1 : 1
		);
		if (destination !== resolvedIndex) onSelect(destination);
	}
</script>

<nav
	class="turn-nav"
	class:is-focused={focused}
	style="--count: {items.length}"
	aria-label="Navigate user turns"
	onfocusin={() => (focused = true)}
	onfocusout={() => (focused = false)}
>
	{#each items as item, index (item.id)}
		<button
			type="button"
			class="tick"
			class:is-current={index === resolvedIndex}
			class:is-hovered={hoveredIndex === index}
			style="--w: {item.weight}"
			aria-label="User message {index + 1} of {items.length}: {item.label}"
			aria-current={index === resolvedIndex ? 'true' : undefined}
			onclick={() => onSelect(index)}
			onkeydown={onKeydown}
			onmouseenter={() => (hoveredIndex = index)}
			onmouseleave={() => {
				if (hoveredIndex === index) hoveredIndex = null;
			}}
			onfocus={() => (hoveredIndex = index)}
			onblur={() => {
				if (hoveredIndex === index) hoveredIndex = null;
			}}
		>
			<span class="bar" aria-hidden="true"></span>
			<span class="preview" aria-hidden="true">
				<span class="preview-title">{item.title}</span>
				{#if item.preview}
					<span class="preview-body">{item.preview}</span>
				{/if}
			</span>
		</button>
	{/each}
</nav>

<style>
	.turn-nav {
		position: absolute;
		left: max(8px, calc((100% - 768px) / 2 - 34px));
		top: 50%;
		z-index: 12;
		display: flex;
		flex-direction: column;
		justify-content: center;
		align-items: flex-start;
		gap: clamp(3px, calc(42vh / var(--count, 4) - 2px), 9px);
		max-height: 56vh;
		padding: 10px 12px 10px 2px;
		transform: translateY(-50%);
		pointer-events: none;
	}

	.tick {
		position: relative;
		pointer-events: auto;
		display: flex;
		align-items: center;
		width: 28px;
		height: 10px;
		padding: 0;
		border: 0;
		background: transparent;
		cursor: pointer;
	}

	.bar {
		display: block;
		height: 2px;
		width: calc(7px + var(--w, 0.5) * 15px);
		border-radius: 999px;
		background: color-mix(in oklch, var(--subtle-foreground) 72%, transparent);
		transform-origin: left center;
		transition:
			width 180ms ease,
			background-color 180ms ease,
			transform 180ms cubic-bezier(0.22, 1, 0.36, 1),
			opacity 160ms ease;
	}

	.turn-nav:hover .tick:not(:hover):not(.is-current) .bar,
	.turn-nav.is-focused .tick:not(:focus-visible):not(.is-current) .bar {
		opacity: 0.42;
	}

	.tick.is-current .bar {
		width: calc(10px + var(--w, 0.5) * 15px);
		background: color-mix(in oklch, var(--foreground) 82%, transparent);
	}

	.tick:hover .bar,
	.tick.is-hovered .bar,
	.tick:focus-visible .bar {
		width: calc(14px + var(--w, 0.5) * 14px);
		background: var(--foreground);
		transform: scaleY(1.65);
	}

	.tick:focus-visible {
		outline: none;
	}

	.tick:focus-visible .bar {
		box-shadow: 0 0 0 2px color-mix(in oklch, var(--ring) 70%, transparent);
	}

	.preview {
		position: absolute;
		left: calc(100% + 10px);
		top: 50%;
		width: min(320px, calc(100vw - 96px));
		padding: 10px 12px;
		border: 1px solid color-mix(in oklch, var(--border) 88%, transparent);
		border-radius: 12px;
		background: color-mix(in oklch, var(--popover) 96%, transparent);
		box-shadow: var(--shadow-m);
		backdrop-filter: blur(12px) saturate(140%);
		-webkit-backdrop-filter: blur(12px) saturate(140%);
		opacity: 0;
		pointer-events: none;
		transform: translate(-8px, -50%) scale(0.98);
		transform-origin: left center;
		transition:
			opacity 160ms ease,
			transform 180ms cubic-bezier(0.22, 1, 0.36, 1);
		z-index: 2;
		text-align: left;
	}

	.tick:hover .preview,
	.tick.is-hovered .preview,
	.tick:focus-visible .preview {
		opacity: 1;
		transform: translate(0, -50%) scale(1);
	}

	.preview-title {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 13px;
		font-weight: 600;
		letter-spacing: -0.015em;
		line-height: 1.3;
		color: var(--foreground);
	}

	.preview-body {
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
		margin-top: 4px;
		font-size: 12px;
		line-height: 1.45;
		color: var(--muted-foreground);
	}

	@media (max-width: 880px) {
		.turn-nav { left: 6px; }
	}

	/* Desktop reading aid. On compact/coarse-pointer surfaces it competes with
	   transcript scrolling and the composer island. */
	@media (max-width: 639px), (pointer: coarse) {
		.turn-nav { display: none; }
	}

	@media (prefers-reduced-motion: reduce) {
		.bar,
		.preview { transition: none; }
	}
</style>
