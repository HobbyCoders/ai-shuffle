<script lang="ts">
	/**
	 * The panel's stand-in for a page `<select>` popup: the page's options with
	 * their groups and disabled state. Arrows, Home/End and PageUp/PageDown move
	 * through enabled options, typing jumps to a label (type-ahead), Enter or
	 * Space chooses, Escape cancels. Long lists also get a filter box.
	 */
	import { onMount, tick } from 'svelte';
	import { Check, Search } from 'lucide-svelte';
	import {
		edgeChoice,
		matchingChoices,
		moveChoice,
		revealIn,
		TypeAhead,
		typeAheadMatch,
		type BrowserChoices,
	} from '$lib/browser-panel/pickers';

	let {
		choices,
		label,
		autofocus = true,
		onChoose,
		onCancel,
	}: {
		choices: BrowserChoices;
		label: string;
		/** Focus the list on mount (off for touch, so no keyboard pops up). */
		autofocus?: boolean;
		onChoose: (index: number) => void;
		onCancel: () => void;
	} = $props();

	/** Lists longer than this get a filter box. */
	const FILTER_FROM = 12;
	const PAGE = 8;
	const uid = `bsl-${Math.random().toString(36).slice(2, 9)}`;

	let query = $state('');
	let listEl: HTMLDivElement | undefined = $state();
	const visible = $derived(matchingChoices(choices, query));
	const visibleOptions = $derived(visible.map((i) => choices.options[i]));
	const initialActive = () => {
		const selected = choices.options[choices.selected];
		return selected && !selected.disabled ? choices.selected : edgeChoice(choices.options, 'first');
	};
	let active = $state(initialActive());
	const typed = new TypeAhead();

	// Rows in order, with a heading before each run of one optgroup.
	const rows = $derived.by(() => {
		const out: ({ kind: 'group'; key: string; label: string; off: boolean } | { kind: 'option'; index: number })[] = [];
		let group: number | undefined;
		for (const index of visible) {
			const g = choices.options[index].group;
			// A disabled optgroup (all its options off) reads as off too.
			const off = g !== undefined && choices.options.every((o) => o.group !== g || o.disabled);
			if (g !== undefined && g !== group) out.push({ kind: 'group', key: `g${g}-${index}`, label: choices.groups[g] ?? '', off });
			group = g;
			out.push({ kind: 'option', index });
		}
		return out;
	});

	// A filter that hides the active option moves to the first visible one.
	$effect(() => {
		if (!visible.includes(active)) active = visible[edgeChoice(visibleOptions, 'first')] ?? -1;
	});

	function scrollActive(center = false) {
		void tick().then(() => revealIn(listEl, document.getElementById(`${uid}-${active}`), center));
	}

	function move(delta: number | 'first' | 'last') {
		const pos = visible.indexOf(active);
		const next = typeof delta === 'number' ? moveChoice(visibleOptions, pos, delta) : edgeChoice(visibleOptions, delta);
		if (next >= 0) active = visible[next];
		scrollActive();
	}

	function choose(index: number) {
		const option = choices.options[index];
		if (!option || option.disabled) return;
		onChoose(index);
	}

	function keydown(e: KeyboardEvent) {
		e.stopPropagation();
		const inFilter = e.target instanceof HTMLInputElement;
		const nav: Record<string, () => void> = {
			ArrowDown: () => move(1),
			ArrowUp: () => move(-1),
			PageDown: () => move(PAGE),
			PageUp: () => move(-PAGE),
			Home: () => move('first'),
			End: () => move('last'),
		};
		if (inFilter && (e.key === 'Home' || e.key === 'End')) return; // caret keys in the filter box
		if (nav[e.key]) {
			e.preventDefault();
			nav[e.key]();
		} else if (e.key === 'Enter') {
			e.preventDefault();
			choose(active);
		} else if (e.key === 'Escape' || e.key === 'Tab') {
			e.preventDefault();
			onCancel();
		} else if (!inFilter && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
			// Space chooses unless it continues a type-ahead ("new y…").
			const q = typed.push(e.key, Date.now());
			e.preventDefault();
			if (e.key === ' ' && q === ' ') {
				typed.reset();
				choose(active);
				return;
			}
			const hit = typeAheadMatch(visibleOptions, q, visible.indexOf(active));
			if (hit >= 0) {
				active = visible[hit];
				scrollActive();
			}
		}
	}

	onMount(() => {
		if (autofocus) listEl?.focus({ preventScroll: true });
		scrollActive(true);
	});
</script>

<div class="bsl">
	{#if choices.options.length > FILTER_FROM}
		<label class="bsl-filter">
			<Search size={14} strokeWidth={1.75} aria-hidden="true" />
			<input
				type="search"
				placeholder="Filter options"
				aria-label="Filter options"
				aria-controls={`${uid}-list`}
				autocomplete="off"
				spellcheck="false"
				bind:value={query}
				onkeydown={keydown}
			/>
		</label>
	{/if}
	<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
	<div
		bind:this={listEl}
		id={`${uid}-list`}
		class="bsl-list"
		role="listbox"
		tabindex="0"
		aria-label={label}
		aria-activedescendant={active >= 0 ? `${uid}-${active}` : undefined}
		onkeydown={keydown}
	>
		{#each rows as row (row.kind === 'group' ? row.key : row.index)}
			{#if row.kind === 'group'}
				<div class="bsl-group" class:off={row.off} role="presentation">{row.label}</div>
			{:else}
				{@const option = choices.options[row.index]}
				<div
					id={`${uid}-${row.index}`}
					class="bsl-option"
					class:active={row.index === active}
					class:selected={row.index === choices.selected}
					class:grouped={option.group !== undefined}
					role="option"
					aria-selected={row.index === choices.selected}
					aria-disabled={option.disabled}
					tabindex="-1"
					onpointermove={(e) => { if (e.pointerType === 'mouse' && !option.disabled) active = row.index; }}
					onclick={() => choose(row.index)}
					onkeydown={keydown}
				>
					<span class="bsl-check" aria-hidden="true">
						{#if row.index === choices.selected}<Check size={14} strokeWidth={2.25} />{/if}
					</span>
					<span class="bsl-label">{option.label || ' '}</span>
				</div>
			{/if}
		{:else}
			<p class="bsl-empty" role="status">
				{query.trim() ? `No options match “${query.trim()}”.` : 'This list has no options.'}
			</p>
		{/each}
	</div>
	{#if choices.truncated}
		<p class="bsl-note">Showing the first {choices.options.length.toLocaleString('en-US')} options.</p>
	{/if}
</div>

<style>
	.bsl {
		display: flex;
		flex-direction: column;
		min-height: 0;
		flex: 1;
	}

	.bsl-filter {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0 8px 6px;
		padding: 0 10px;
		height: 34px;
		flex-shrink: 0;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 70%, transparent);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 4px);
		transition: border-color 140ms ease, box-shadow 140ms ease;
	}
	.bsl-filter:focus-within {
		border-color: color-mix(in oklch, var(--ring) 70%, transparent);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 18%, transparent);
	}
	.bsl-filter input {
		flex: 1;
		min-width: 0;
		height: 100%;
		border: 0;
		outline: none;
		background: transparent;
		color: var(--foreground);
		font: inherit;
		font-size: 0.8125rem;
	}
	.bsl-filter input::placeholder {
		color: var(--subtle-foreground, var(--muted-foreground));
	}

	.bsl-list {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		overscroll-behavior: contain;
		touch-action: pan-y;
		padding: 2px 6px 6px;
		outline: none;
		scrollbar-width: thin;
		scrollbar-color: var(--scrollbar-thumb, var(--border)) transparent;
	}
	.bsl-list:focus-visible .bsl-option.active {
		box-shadow: inset 0 0 0 1.5px color-mix(in oklch, var(--ring) 85%, transparent);
	}

	.bsl-group {
		position: sticky;
		top: -2px;
		z-index: 1;
		padding: 10px 10px 4px;
		font-family: var(--font-mono);
		font-size: 0.625rem;
		font-weight: 600;
		letter-spacing: 0.09em;
		text-transform: uppercase;
		color: var(--muted-foreground);
		background: var(--popover);
	}

	.bsl-group.off {
		opacity: 0.5;
	}

	.bsl-option {
		display: flex;
		align-items: center;
		gap: 6px;
		min-height: 34px;
		padding: 0 10px 0 6px;
		border-radius: calc(var(--radius) - 4px);
		font-size: 0.8125rem;
		line-height: 1.25;
		color: var(--popover-foreground);
		cursor: pointer;
		user-select: none;
		-webkit-user-select: none;
		transition: background-color 90ms ease, color 90ms ease;
	}
	.bsl-option.grouped {
		padding-left: 16px;
	}
	.bsl-option.active {
		background: color-mix(in oklch, var(--primary) 14%, transparent);
	}
	.bsl-option.selected {
		color: var(--primary);
		font-weight: 600;
	}
	.bsl-option[aria-disabled='true'] {
		color: var(--subtle-foreground, var(--muted-foreground));
		opacity: 0.55;
		cursor: not-allowed;
		background: transparent;
	}
	.bsl-check {
		display: inline-flex;
		width: 16px;
		flex-shrink: 0;
		justify-content: center;
		color: var(--primary);
	}
	.bsl-label {
		min-width: 0;
		overflow-wrap: anywhere;
		padding: 7px 0;
	}

	.bsl-empty,
	.bsl-note {
		margin: 0;
		padding: 14px 12px;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		text-align: center;
	}
	.bsl-note {
		padding: 8px 12px 10px;
		border-top: 1px solid var(--border);
		flex-shrink: 0;
	}

	@media (pointer: coarse) {
		.bsl-option {
			min-height: 44px;
			font-size: 0.9375rem;
		}
		.bsl-filter {
			height: 40px;
		}
		.bsl-filter input {
			font-size: 16px; /* no iOS focus zoom */
		}
	}
</style>
