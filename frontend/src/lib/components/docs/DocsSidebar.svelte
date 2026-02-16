<script lang="ts">
	/**
	 * DocsSidebar - Left navigation tree for documentation
	 *
	 * Shows all docs grouped by category with active highlighting.
	 */
	import type { DocEntry, DocCategory } from '$lib/docs/types';
	import { ChevronDown, ChevronRight } from 'lucide-svelte';

	interface Props {
		categories: DocCategory[];
		docsByCategory: Record<string, DocEntry[]>;
		activeSlug: string;
		activeCategory: string;
		onSelectDoc: (category: string, slug: string) => void;
	}

	let { categories, docsByCategory, activeSlug, activeCategory, onSelectDoc }: Props = $props();

	// Track collapsed categories
	let collapsed = $state<Set<string>>(new Set());

	function toggleCategory(id: string) {
		const next = new Set(collapsed);
		if (next.has(id)) {
			next.delete(id);
		} else {
			next.add(id);
		}
		collapsed = next;
	}

	function isExpanded(id: string): boolean {
		return !collapsed.has(id);
	}

	function formatSlug(slug: string): string {
		if (slug === '_index') return 'Overview';
		return slug
			.replace(/-/g, ' ')
			.replace(/\b\w/g, (c) => c.toUpperCase());
	}
</script>

<nav class="docs-sidebar">
	{#each categories as cat}
		{@const catDocs = docsByCategory[cat.id] || []}
		{#if catDocs.length > 0}
			<div class="sidebar-section">
				<button
					class="sidebar-category"
					class:active={activeCategory === cat.id}
					onclick={() => toggleCategory(cat.id)}
				>
					<span class="category-chevron">
						{#if isExpanded(cat.id)}
							<ChevronDown size={14} />
						{:else}
							<ChevronRight size={14} />
						{/if}
					</span>
					<span class="category-label">{cat.label}</span>
					<span class="category-count">{catDocs.length}</span>
				</button>

				{#if isExpanded(cat.id)}
					<ul class="sidebar-docs">
						{#each catDocs as doc}
							<li>
								<button
									class="sidebar-doc"
									class:active={activeCategory === cat.id && activeSlug === doc.slug}
									onclick={() => onSelectDoc(cat.id, doc.slug)}
								>
									{doc.title || formatSlug(doc.slug)}
								</button>
							</li>
						{/each}
					</ul>
				{/if}
			</div>
		{/if}
	{/each}
</nav>

<style>
	.docs-sidebar {
		width: 240px;
		min-width: 240px;
		padding: var(--space-3) 0;
		border-right: 1px solid var(--border-subtle);
		overflow-y: auto;
		overflow-x: hidden;
		height: 100%;
	}

	.sidebar-section {
		margin-bottom: var(--space-1);
	}

	.sidebar-category {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		width: 100%;
		padding: 8px var(--space-3);
		background: none;
		border: none;
		cursor: pointer;
		color: var(--text-secondary);
		font-size: 13px;
		font-weight: 600;
		text-align: left;
		transition: color 0.15s;
	}

	.sidebar-category:hover {
		color: var(--text-primary);
	}

	.sidebar-category.active {
		color: var(--text-primary);
	}

	.category-chevron {
		display: flex;
		align-items: center;
		color: var(--text-tertiary);
		flex-shrink: 0;
	}

	.category-label {
		flex: 1;
	}

	.category-count {
		font-size: 11px;
		font-weight: 500;
		color: var(--text-tertiary);
		background: var(--surface-active);
		padding: 1px 6px;
		border-radius: 10px;
	}

	.sidebar-docs {
		list-style: none;
		padding: 0;
		margin: 0 0 var(--space-2) 0;
	}

	.sidebar-doc {
		display: block;
		width: 100%;
		padding: 6px var(--space-3) 6px calc(var(--space-3) + 22px);
		background: none;
		border: none;
		border-left: 2px solid transparent;
		cursor: pointer;
		color: var(--text-tertiary);
		font-size: 13px;
		text-align: left;
		transition: all 0.15s;
		line-height: 1.4;
	}

	.sidebar-doc:hover {
		color: var(--text-primary);
		background: var(--surface-0);
	}

	.sidebar-doc.active {
		color: var(--primary);
		border-left-color: var(--primary);
		background: color-mix(in oklch, var(--primary), transparent 92%);
	}

	/* Scrollbar */
	.docs-sidebar::-webkit-scrollbar {
		width: 4px;
	}

	.docs-sidebar::-webkit-scrollbar-thumb {
		background: var(--border-default);
		border-radius: 2px;
	}

	@media (max-width: 768px) {
		.docs-sidebar {
			display: none;
		}
	}
</style>
