<script lang="ts">
	/**
	 * DocsSearch - Search input with dropdown results
	 *
	 * Provides full-text search across all documentation
	 * with keyboard navigation and result highlighting.
	 */
	import { Search, X } from 'lucide-svelte';
	import type { SearchResult } from '$lib/docs/types';
	import type { DocsSearchEngine } from '$lib/docs/search';

	interface Props {
		searchEngine: DocsSearchEngine;
		onSelectResult: (slug: string) => void;
	}

	let { searchEngine, onSelectResult }: Props = $props();

	let query = $state('');
	let results = $state<SearchResult[]>([]);
	let showResults = $state(false);
	let selectedIndex = $state(-1);
	let inputEl = $state<HTMLInputElement | null>(null);
	let debounceTimer: ReturnType<typeof setTimeout> | null = null;

	function handleInput() {
		if (debounceTimer) clearTimeout(debounceTimer);
		debounceTimer = setTimeout(() => {
			if (query.trim().length >= 2) {
				results = searchEngine.search(query);
				showResults = results.length > 0;
				selectedIndex = -1;
			} else {
				results = [];
				showResults = false;
			}
		}, 200);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (!showResults) return;

		if (e.key === 'ArrowDown') {
			e.preventDefault();
			selectedIndex = Math.min(selectedIndex + 1, results.length - 1);
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			selectedIndex = Math.max(selectedIndex - 1, -1);
		} else if (e.key === 'Enter' && selectedIndex >= 0) {
			e.preventDefault();
			selectResult(results[selectedIndex]);
		} else if (e.key === 'Escape') {
			showResults = false;
			inputEl?.blur();
		}
	}

	function selectResult(result: SearchResult) {
		onSelectResult(result.slug);
		query = '';
		results = [];
		showResults = false;
	}

	function clear() {
		query = '';
		results = [];
		showResults = false;
		inputEl?.focus();
	}

	function getCategoryLabel(cat: string): string {
		const map: Record<string, string> = {
			'getting-started': 'Getting Started',
			connectors: 'Connectors',
			features: 'Features',
			'api-reference': 'API'
		};
		return map[cat] || cat;
	}
</script>

<div class="docs-search">
	<div class="search-input-wrapper">
		<Search size={14} class="search-icon" />
		<input
			bind:this={inputEl}
			bind:value={query}
			oninput={handleInput}
			onkeydown={handleKeydown}
			onfocus={() => {
				if (results.length > 0) showResults = true;
			}}
			onblur={() => {
				// Delay to allow click on results
				setTimeout(() => (showResults = false), 200);
			}}
			type="text"
			placeholder="Search docs..."
			class="search-input"
		/>
		{#if query}
			<button class="search-clear" onclick={clear} aria-label="Clear search">
				<X size={14} />
			</button>
		{/if}
	</div>

	{#if showResults}
		<div class="search-results">
			{#each results as result, i}
				<button
					class="search-result"
					class:selected={i === selectedIndex}
					onmousedown={() => selectResult(result)}
					onmouseenter={() => (selectedIndex = i)}
				>
					<div class="result-header">
						<span class="result-title">{result.title}</span>
						<span class="result-badge">{getCategoryLabel(result.category)}</span>
					</div>
					{#if result.snippets.length > 0}
						<div class="result-snippet">{result.snippets[0]}</div>
					{/if}
				</button>
			{/each}
		</div>
	{/if}
</div>

<style>
	.docs-search {
		position: relative;
		width: 240px;
	}

	.search-input-wrapper {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 0 10px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 8px;
		transition: border-color 0.15s;
	}

	.search-input-wrapper:focus-within {
		border-color: var(--primary);
	}

	.search-input-wrapper :global(.search-icon) {
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.search-input {
		flex: 1;
		padding: 7px 0;
		background: none;
		border: none;
		outline: none;
		font-size: 13px;
		color: var(--foreground);
	}

	.search-input::placeholder {
		color: var(--muted-foreground);
	}

	.search-clear {
		display: flex;
		align-items: center;
		padding: 2px;
		background: none;
		border: none;
		color: var(--muted-foreground);
		cursor: pointer;
	}

	.search-clear:hover {
		color: var(--foreground);
	}

	.search-results {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		right: 0;
		max-height: 400px;
		overflow-y: auto;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 10px;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.28);
		z-index: 100;
	}

	.search-result {
		display: block;
		width: 100%;
		padding: 10px 12px;
		background: none;
		border: none;
		border-bottom: 1px solid var(--border);
		cursor: pointer;
		text-align: left;
		transition: background 0.1s;
	}

	.search-result:last-child {
		border-bottom: none;
	}

	.search-result:hover,
	.search-result.selected {
		background: var(--muted);
	}

	.result-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-bottom: 2px;
	}

	.result-title {
		font-size: 13px;
		font-weight: 500;
		color: var(--foreground);
	}

	.result-badge {
		font-size: 11px;
		color: var(--muted-foreground);
		background: var(--muted);
		padding: 1px 6px;
		border-radius: 4px;
		flex-shrink: 0;
	}

	.result-snippet {
		font-size: 12px;
		color: var(--muted-foreground);
		line-height: 1.4;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
