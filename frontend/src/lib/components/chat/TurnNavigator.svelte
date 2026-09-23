<script lang="ts">
	/** Left-rail user-turn minimap. Each tick is a prompt; hover reveals the
	 *  message. Stays in the transcript gutter and never touches the composer. */
	import type { UserTurnNavigationItem } from '$lib/utils/transcript-navigation';
	import { resolveTurnNavigationIndex } from '$lib/utils/transcript-navigation';
	import { tick } from 'svelte';
	import { ChevronUp, ChevronDown, ChevronsUp, ChevronsDown, Search, X } from 'lucide-svelte';

	interface Props {
		items: UserTurnNavigationItem[];
		currentIndex: number;
		onSelect: (index: number) => void;
	}

	let { items, currentIndex, onSelect }: Props = $props();
	let focused = $state(false);
	let hoveredIndex = $state<number | null>(null);
	const resolvedIndex = $derived(Math.max(0, Math.min(items.length - 1, currentIndex)));
	const windowStart = $derived(Math.max(0, Math.min(items.length - 9, resolvedIndex - 4)));
	let pickerOpen = $state(false);
	let query = $state('');
	let resultLimit = $state(60);
	let browseStart = $state(0);
	let nav = $state<HTMLElement>();
	let trigger = $state<HTMLButtonElement>();
	let searchInput = $state<HTMLInputElement>();
	const pickerId = $props.id();
	const searchTerm = $derived(query.trim().toLowerCase());
	const matches = $derived(items.map((item, index) => ({ item, index })).filter(({ item, index }) => {
		if (/^\d+$/.test(searchTerm)) return index + 1 === Number(searchTerm);
		return !searchTerm || `${item.title} ${item.preview}`.toLowerCase().includes(searchTerm);
	}));
	// Start the unfiltered list near the reading position; search covers the whole session.
	const resultStart = $derived(query.trim() ? 0 : browseStart);
	const visibleMatches = $derived(matches.slice(resultStart, resultStart + resultLimit));

	async function openPicker() {
		query = '';
		resultLimit = 60;
		browseStart = Math.max(0, resolvedIndex - 2);
		pickerOpen = true;
		await tick();
		searchInput?.focus();
	}

	function closePicker(restoreFocus = false) {
		pickerOpen = false;
		if (restoreFocus) trigger?.focus();
	}

	function selectFromPicker(index: number) {
		closePicker(true);
		onSelect(index);
	}

	function onWindowKeydown(event: KeyboardEvent) {
		if (pickerOpen && event.key === 'Escape') {
			event.preventDefault();
			closePicker(true);
		}
	}

	function onKeydown(event: KeyboardEvent) {
		if (event.key === 'Home' || event.key === 'End') {
			event.preventDefault();
			onSelect(event.key === 'Home' ? 0 : items.length - 1);
			return;
		}
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

<svelte:window onkeydown={onWindowKeydown} onpointerdown={(event) => {
	if (pickerOpen && !nav?.contains(event.target as Node)) closePicker();
}} />

<nav
	bind:this={nav}
	class="turn-nav"
	class:is-focused={focused}
	class:long-history={items.length > 30}
	style="--count: {items.length}"
	aria-label="Navigate user turns"
	onfocusin={() => (focused = true)}
	onfocusout={(event) => {
		focused = false;
		if (event.relatedTarget && !nav?.contains(event.relatedTarget as Node)) closePicker();
	}}
	onmouseleave={() => (hoveredIndex = null)}
>
	{#if items.length > 30}
		<button class="rail-control endpoint" type="button" title="First message" aria-label="First user turn" disabled={resolvedIndex === 0} onclick={() => onSelect(0)}><ChevronsUp size={14} /></button>
		<button class="rail-control" type="button" title="Previous message" aria-label="Previous user turn" disabled={resolvedIndex === 0} onclick={() => onSelect(resolvedIndex - 1)} onkeydown={onKeydown}><ChevronUp size={14} /></button>
		<div class="nearby-turns">
			{#each items.slice(windowStart, windowStart + 9) as item, offset (item.id)}
				{@const index = windowStart + offset}
				<button type="button" class="tick" class:is-current={index === resolvedIndex}
					aria-label="User message {index + 1} of {items.length}: {item.label}"
					aria-current={index === resolvedIndex ? 'true' : undefined}
					onclick={() => onSelect(index)} onkeydown={onKeydown}>
					<span class="bar" aria-hidden="true"></span>
					<span class="preview" aria-hidden="true">
						<span class="preview-position">Message {index + 1} of {items.length}</span>
						<span class="preview-title">{item.title}</span>
						{#if item.preview}<span class="preview-body">{item.preview}</span>{/if}
					</span>
				</button>
			{/each}
		</div>
		<button bind:this={trigger} class="position-button" type="button" title="Browse session timeline"
			aria-label="Browse session timeline, message {resolvedIndex + 1} of {items.length}"
			aria-expanded={pickerOpen} aria-controls={pickerId}
			onclick={() => pickerOpen ? closePicker() : openPicker()} onkeydown={onKeydown}>
			<span class="position-current">{resolvedIndex + 1}</span><span class="position-total">/ {items.length}</span>
		</button>
		<button class="rail-control" type="button" title="Next message" aria-label="Next user turn" disabled={resolvedIndex === items.length - 1} onclick={() => onSelect(resolvedIndex + 1)} onkeydown={onKeydown}><ChevronDown size={14} /></button>
		<button class="rail-control endpoint" type="button" title="Last message" aria-label="Last user turn" disabled={resolvedIndex === items.length - 1} onclick={() => onSelect(items.length - 1)}><ChevronsDown size={14} /></button>
		{#if pickerOpen}
			<section id={pickerId} class="timeline-picker" aria-label="Session timeline">
				<header><span>Session timeline</span><button class="rail-control" type="button" aria-label="Close session timeline" onclick={() => closePicker(true)}><X size={15} /></button></header>
				<label class="timeline-search"><Search size={15} /><input bind:this={searchInput} bind:value={query} oninput={() => (resultLimit = 60)} type="search" placeholder="Find a message or number…" aria-label="Find a message or number" /></label>
				<div class="timeline-results">
					{#if resultStart > 0}<button class="show-more" type="button" onclick={() => { browseStart = Math.max(0, browseStart - 60); resultLimit += 60; }}>Show earlier messages</button>{/if}
					{#each visibleMatches as { item, index } (item.id)}
						<button class="timeline-result" class:is-current={index === resolvedIndex} type="button" aria-current={index === resolvedIndex ? 'true' : undefined} onclick={() => selectFromPicker(index)}>
							<span class="result-number">{index + 1}</span>
							<span class="result-copy">
								<span class="result-title">{item.title}</span>
								{#if item.preview}<span class="result-preview">{item.preview}</span>{/if}
							</span>
						</button>
					{:else}<p class="empty-results">No messages found.</p>{/each}
					{#if resultStart + resultLimit < matches.length}<button class="show-more" type="button" onclick={() => (resultLimit += 60)}>Show more messages</button>{/if}
				</div>
				<footer>{items.length} messages<span>Esc to close</span></footer>
			</section>
		{/if}
	{:else}
	{#each items as item, index (item.id)}
		<button
			type="button"
			class="tick"
			class:is-current={index === resolvedIndex}
			class:is-hovered={hoveredIndex === index}
			style="--proximity: {hoveredIndex === null ? 0 : Math.max(0, 1 - Math.abs(index - hoveredIndex) / 3)}"
			aria-label="User message {index + 1} of {items.length}: {item.label}"
			aria-current={index === resolvedIndex ? 'true' : undefined}
			onclick={() => onSelect(index)}
			onkeydown={onKeydown}
			onmouseenter={() => (hoveredIndex = index)}
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
	{/if}
</nav>

<style>
	.turn-nav {
		position: absolute;
		left: 2px;
		top: 50%;
		z-index: 12;
		display: flex;
		flex-direction: column;
		justify-content: center;
		align-items: flex-start;
		gap: clamp(3px, calc(42vh / var(--count, 4) - 2px), 9px);
		max-height: 56vh;
		padding: 10px 12px 10px 0;
		transform: translateY(-50%);
		pointer-events: auto;
	}

	.long-history {
		left: 8px;
		width: 48px;
		gap: 2px;
		padding: 6px 0;
		align-items: center;
		border: 1px solid color-mix(in oklch, var(--border) 55%, transparent);
		border-radius: 16px;
		background: color-mix(in oklch, var(--card) 90%, transparent);
		color: var(--muted-foreground);
	}

	.rail-control {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 30px;
		height: 26px;
		flex-shrink: 0;
		padding: 0;
		border: 0;
		border-radius: 7px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.rail-control:hover:not(:disabled), .position-button:hover { background: var(--accent); color: var(--foreground); }
	.rail-control:disabled { opacity: 0.25; cursor: default; }
	.rail-control:focus-visible, .position-button:focus-visible, .timeline-result:focus-visible, .show-more:focus-visible {
		outline: 2px solid var(--ring); outline-offset: -2px;
	}
	.nearby-turns { display: flex; flex-direction: column; padding: 4px 0; }
	.long-history .tick { height: 16px; width: 32px; justify-content: center; flex-shrink: 0; }
	.long-history .bar { width: 12px; transform-origin: center; }
	.long-history .tick.is-current .bar { width: 24px; height: 3px; background: var(--foreground); }
	.long-history .tick:hover .bar, .long-history .tick:focus-visible .bar { transform: scaleX(1.2); }
	.position-button {
		display: flex; flex-direction: column; align-items: center; gap: 2px;
		width: 100%; padding: 7px 2px; border: 0; border-radius: 8px;
		background: transparent; cursor: pointer; font-variant-numeric: tabular-nums;
	}
	.position-current { color: var(--foreground); font-size: 12px; font-weight: 600; line-height: 1.2; }
	.position-total { color: var(--muted-foreground); font-size: 10px; line-height: 1.2; }
	.preview-position { display: block; margin-bottom: 5px; color: var(--muted-foreground); font-size: 10px; }
	.timeline-picker {
		position: absolute; left: calc(100% + 10px); top: 50%; transform: translateY(-50%);
		display: flex; flex-direction: column; width: min(340px, calc(100vw - 88px));
		max-height: min(480px, 70vh); overflow: hidden;
		border: 1px solid var(--border); border-radius: 14px;
		background: var(--popover); color: var(--foreground); box-shadow: var(--shadow-m);
	}
	.timeline-picker header { display: flex; align-items: center; justify-content: space-between; padding: 12px 12px 8px 16px; font-size: 13px; font-weight: 600; }
	.timeline-search { display: flex; align-items: center; gap: 8px; margin: 0 12px 10px; padding: 9px 10px; border: 1px solid var(--border); border-radius: 8px; color: var(--muted-foreground); }
	.timeline-search:focus-within { border-color: var(--ring); }
	.timeline-search input { width: 100%; min-width: 0; border: 0; outline: 0; padding: 0; background: transparent; color: var(--foreground); font: inherit; font-size: 12px; }
	.timeline-results { overflow-y: auto; overscroll-behavior: contain; min-height: 0; padding: 0 6px 6px; }
	.timeline-result { display: flex; gap: 12px; width: 100%; padding: 11px 10px; border: 0; border-radius: 8px; text-align: left; background: transparent; color: var(--muted-foreground); cursor: pointer; }
	.timeline-result:hover, .timeline-result.is-current { background: var(--accent); color: var(--foreground); }
	.result-number { min-width: 26px; padding-top: 1px; font-size: 11px; font-variant-numeric: tabular-nums; }
	.result-copy { min-width: 0; }
	.result-title { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; line-height: 1.5; }
	.result-preview { display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin-top: 3px; color: var(--muted-foreground); font-size: 11px; line-height: 1.5; }
	.timeline-picker footer { display: flex; justify-content: space-between; padding: 10px 16px; border-top: 1px solid var(--border); color: var(--muted-foreground); font-size: 10px; }
	.show-more { width: 100%; padding: 10px; border: 0; border-radius: 8px; background: transparent; color: var(--muted-foreground); font-size: 11px; cursor: pointer; }
	.show-more:hover { background: var(--accent); color: var(--foreground); }
	.empty-results { padding: 22px 12px; text-align: center; font-size: 12px; color: var(--muted-foreground); }
	.long-history:has(.timeline-picker) .preview { display: none; }
	@media (max-height: 600px) and (pointer: fine) { .long-history .nearby-turns { display: none; } }

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
		width: 14px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--subtle-foreground) 72%, transparent);
		transform: translateX(calc(var(--proximity, 0) * 4px)) scaleX(calc(1 + var(--proximity, 0) * 0.8));
		transform-origin: left center;
		transition:
			background-color 180ms ease,
			transform 240ms cubic-bezier(0.22, 1, 0.36, 1),
			opacity 160ms ease;
	}

	.turn-nav:hover .tick:not(:hover):not(.is-current) .bar,
	.turn-nav.is-focused .tick:not(:focus-visible):not(.is-current) .bar {
		opacity: calc(0.42 + var(--proximity, 0) * 0.58);
	}

	.tick.is-current .bar {
		background: color-mix(in oklch, var(--foreground) 82%, transparent);
	}

	.tick:hover .bar,
	.tick.is-hovered .bar,
	.tick:focus-visible .bar {
		background: var(--foreground);
		height: 3px;
		transform: translateX(0) scaleX(2.2);
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
		backdrop-filter: var(--fx-rail-backdrop);
		-webkit-backdrop-filter: var(--fx-rail-backdrop);
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

	/* Desktop reading aid. On compact/coarse-pointer surfaces it competes with
	   transcript scrolling and the composer island. */
	@media (max-width: 639px), (pointer: coarse) {
		.turn-nav:not(.long-history) { display: none; }
		.long-history {
			left: 6px;
			top: 6px;
			transform: none;
			width: auto;
			flex-direction: row;
			padding: 3px;
			border-radius: 12px;
		}
		.long-history .nearby-turns, .long-history .endpoint { display: none; }
		.long-history > .rail-control { width: 36px; height: 36px; }
		.position-button { flex-direction: row; justify-content: center; width: auto; min-width: 60px; height: 36px; gap: 4px; padding: 0 8px; }
		.timeline-picker { top: calc(100% + 8px); left: 0; transform: none; width: min(340px, calc(100vw - 24px)); max-height: min(480px, 60dvh); }
	}

	@media (prefers-reduced-motion: reduce) {
		.bar,
		.preview { transition: none; }
		.bar { transform: none; }
	}
</style>
