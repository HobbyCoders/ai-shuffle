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
			<button class="search-clear" onclick={clear}>
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
		gap: var(--space-2);
		padding: 0 var(--space-2);
		background: var(--surface-0);
		border: 1px solid var(--border-subtle);
		border-radius: 6px;
		transition: border-color 0.15s;
	}

	.search-input-wrapper:focus-within {
		border-color: var(--primary);
	}

	.search-input-wrapper :global(.search-icon) {
		color: var(--text-tertiary);
		flex-shrink: 0;
	}

	.search-input {
		flex: 1;
		padding: 6px 0;
		background: none;
		border: none;
		outline: none;
		font-size: 13px;
		color: var(--text-primary);
	}

	.search-input::placeholder {
		color: var(--text-tertiary);
	}

	.search-clear {
		display: flex;
		align-items: center;
		padding: 2px;
		background: none;
		border: none;
		color: var(--text-tertiary);
		cursor: pointer;
	}

	.search-clear:hover {
		color: var(--text-primary);
	}

	.search-results {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		right: 0;
		max-height: 400px;
		overflow-y: auto;
		background: var(--surface-card);
		border: 1px solid var(--border-default);
		border-radius: 8px;
		box-shadow: var(--shadow-md);
		z-index: 100;
	}

	.search-result {
		display: block;
		width: 100%;
		padding: 10px 12px;
		background: none;
		border: none;
		border-bottom: 1px solid var(--border-subtle);
		cursor: pointer;
		text-align: left;
		transition: background 0.1s;
	}

	.search-result:last-child {
		border-bottom: none;
	}

	.search-result:hover,
	.search-result.selected {
		background: var(--surface-active);
	}

	.result-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2);
		margin-bottom: 2px;
	}

	.result-title {
		font-size: 13px;
		font-weight: 500;
		color: var(--text-primary);
	}

	.result-badge {
		font-size: 11px;
		color: var(--text-tertiary);
		background: var(--surface-0);
		padding: 1px 6px;
		border-radius: 4px;
		flex-shrink: 0;
	}

	.result-snippet {
		font-size: 12px;
		color: var(--text-tertiary);
		line-height: 1.4;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
</style>
