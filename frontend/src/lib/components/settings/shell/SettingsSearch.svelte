<script lang="ts">
	/**
	 * SettingsSearch: the box at the top of the index. While it has text its
	 * results replace the category nav (combobox + listbox): arrows move,
	 * Enter jumps to the result (and flashes the row), Esc clears.
	 * Ctrl+K and "/" focus it (SettingsShell).
	 */
	import { tick } from 'svelte';
	import { Search } from 'lucide-svelte';
	import { query as searchQuery } from '$lib/settings/search';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import type { SearchItem } from '$lib/settings/types';

	interface Props {
		query?: string;
	}

	let { query = $bindable('') }: Props = $props();

	const uid = $props.id();
	const listId = `${uid}-results`;
	let input: HTMLInputElement | undefined = $state();
	let active = $state(0);
	const open = $derived(query.trim().length > 0);
	const results: SearchItem[] = $derived(open ? searchQuery(query) : []);
	const shortcut =
		typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘K' : 'Ctrl K';

	$effect(() => {
		void query;
		active = 0;
	});

	let lastFocusRequest = settingsNav.searchFocusRequest;
	$effect(() => {
		const n = settingsNav.searchFocusRequest;
		if (n === lastFocusRequest) return;
		lastFocusRequest = n;
		void focusWhenVisible();
	});

	async function focusWhenVisible(tries = 20) {
		await tick();
		const visible = input && input.getClientRects().length > 0;
		if (input && (visible || tries <= 0 || typeof requestAnimationFrame === 'undefined')) {
			input.focus();
			input.select();
			return;
		}
		requestAnimationFrame(() => void focusWhenVisible(tries - 1));
	}

	function pick(index: number) {
		const hit = results[index];
		if (!hit) return;
		query = '';
		settingsNav.go(hit.target);
	}

	function onkeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			if (query) {
				event.preventDefault();
				event.stopPropagation();
				query = '';
			}
			return;
		}
		if (!results.length) return;
		if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
			event.preventDefault();
			const step = event.key === 'ArrowDown' ? 1 : -1;
			active = (active + step + results.length) % results.length;
			document.getElementById(`${uid}-opt-${active}`)?.scrollIntoView?.({ block: 'nearest' });
		} else if (event.key === 'Enter') {
			event.preventDefault();
			pick(active);
		}
	}
</script>

<div class="st-search">
	<span class="st-search-icon"><Search /></span>
	<label class="st-vh" for="{uid}-q">Search settings</label>
	<input
		bind:this={input}
		id="{uid}-q"
		type="search"
		placeholder="Search settings"
		autocomplete="off"
		spellcheck="false"
		role="combobox"
		aria-expanded={open}
		aria-controls={listId}
		aria-autocomplete="list"
		aria-activedescendant={open && results.length ? `${uid}-opt-${active}` : undefined}
		bind:value={query}
		{onkeydown}
	/>
	<kbd aria-hidden="true">{shortcut}</kbd>
</div>
<div class="st-results" id={listId} role="listbox" aria-label="Search results" hidden={!open}>
	{#if open}
		{#each results as hit, i (`${hit.title}|${hit.where}`)}
			<div
				id="{uid}-opt-{i}"
				class="st-res"
				class:sel={i === active}
				role="option"
				tabindex="-1"
				aria-selected={i === active}
				onmousedown={(e) => e.preventDefault()}
				onclick={() => pick(i)}
				onkeydown={(e) => {
					if (e.key === 'Enter') pick(i);
				}}
				onmouseenter={() => (active = i)}
			>
				<b>{hit.title}</b>
				<span>{hit.where}</span>
			</div>
		{:else}
			<div class="st-res-empty" role="status">No setting matches “{query.trim()}”</div>
		{/each}
	{/if}
</div>

<style>
	.st-search {
		position: relative;
		margin-bottom: 6px;
	}
	.st-search-icon {
		position: absolute;
		left: 10px;
		top: 9px;
		display: flex;
		color: var(--st-mfg);
		pointer-events: none;
	}
	.st-search-icon :global(svg.lucide) {
		width: 15px;
		height: 15px;
	}
	input {
		width: 100%;
		height: 33px;
		border-radius: var(--st-radius-md);
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		color: var(--st-fg);
		padding: 0 52px 0 32px;
		font-size: 13px;
	}
	input::-webkit-search-cancel-button {
		-webkit-appearance: none;
	}
	input::placeholder {
		color: var(--st-subtle);
	}
	input:focus {
		outline: none;
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	kbd {
		position: absolute;
		right: 8px;
		top: 7px;
		font-family: var(--st-mono);
		font-size: 10.5px;
		color: var(--st-subtle);
		border: 1px solid var(--st-border-strong);
		border-radius: 4px;
		padding: 0 5px;
		line-height: 17px;
	}
	.st-results {
		display: grid;
		gap: 1px;
		min-width: 0;
		grid-template-columns: minmax(0, 1fr);
		margin-top: 6px;
	}
	.st-results[hidden] {
		display: none;
	}
	.st-res {
		display: block;
		width: 100%;
		min-width: 0;
		overflow: hidden;
		text-align: left;
		border-radius: var(--st-radius-md);
		padding: 7px 10px;
		cursor: pointer;
	}
	.st-res.sel {
		background: var(--st-accent);
	}
	.st-res b {
		display: block;
		font-weight: 500;
		font-size: 13px;
		color: var(--st-fg);
	}
	.st-res span {
		display: block;
		font-size: 12px;
		color: var(--st-mfg);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.st-res-empty {
		font-size: 13px;
		color: var(--st-mfg);
		padding: 8px 10px;
	}
</style>
