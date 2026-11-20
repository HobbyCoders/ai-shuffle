<script lang="ts">
	import { onMount } from 'svelte';
	import Banner from '$lib/components/Banner.svelte';
	/**
	 * DirectoryBrowser - Tree browser for picking directories
	 *
	 * Used in the unified "Add Project" modal for selecting project locations.
	 * In local mode: browses the full filesystem.
	 * In Docker mode: browses within the workspace directory.
	 * Fetches directory listings from the backend filesystem browse API.
	 */
	import { Folder, FolderOpen, ChevronRight, Loader2 } from 'lucide-svelte';
	import { browseFilesystem, type DirectoryEntry } from '$lib/api/filesystem';

	interface Props {
		onSelect: (path: string) => void;
		selectedPath?: string;
		browse?: typeof browseFilesystem;
	}

	let { onSelect, selectedPath = '', browse = browseFilesystem }: Props = $props();

	const PAGE_SIZE = 200;

	interface TreeNode {
		entry: DirectoryEntry;
		pageStart: number;
		children: TreeNode[] | null; // null = not loaded, [] = empty
		expanded: boolean;
		loading: boolean;
	}

	let roots = $state<TreeNode[]>([]);
	let rootPageStart = $state(0);
	let rootLoading = $state(true);
	let error = $state<string | null>(null);

	// The picker lives for one browse source. A parent can recreate its callback
	// during a row refresh without replacing the already expanded tree.
	onMount(() => { void loadRoots(); });

	function directoryNodes(entries: DirectoryEntry[]): TreeNode[] {
		return entries.filter(entry => entry.is_dir).map(entry => ({
			entry, children: null, expanded: false, loading: false, pageStart: 0
		}));
	}

	async function loadRoots() {
		rootLoading = true;
		error = null;
		try {
			const response = await browse();
			roots = directoryNodes(response.entries);
		} catch (e: unknown) {
			const err = e as { detail?: string; message?: string };
			error = err.detail || err.message || 'Failed to browse filesystem';
		} finally {
			rootLoading = false;
		}
	}

	async function toggleNode(node: TreeNode) {
		if (node.expanded) {
			node.expanded = false;
			return;
		}

		// Load children if not loaded
		if (node.children === null) {
			if (node.loading) return;
			node.loading = true;
			try {
				const response = await browse(node.entry.path);
				node.children = directoryNodes(response.entries);
			} catch {
				node.children = [];
			} finally {
				node.loading = false;
			}
		}

		node.expanded = true;
	}

	function selectNode(node: TreeNode) {
		onSelect(node.entry.path);
	}
</script>

<div class="directory-browser">
	{#if rootLoading}
		<div class="loading">
			<Loader2 size={16} strokeWidth={1.5} class="spin" />
			<span>Loading...</span>
		</div>
	{:else if error}
		<Banner message={error} />
	{:else}
		<div class="tree">
			{@render pageOf(roots, 0, rootPageStart, (start) => (rootPageStart = start))}
		</div>
	{/if}
</div>

{#snippet treeItem(node: TreeNode, depth: number)}
	<div class="tree-item" style="--depth: {depth}">
		<button
			type="button"
			class="tree-row"
			class:selected={selectedPath === node.entry.path}
			onclick={() => selectNode(node)}
			ondblclick={() => toggleNode(node)}
		>
			<span
				role="button"
				class="expand-btn"
				class:hidden={!node.entry.has_children}
				class:expanded={node.expanded}
				onclick={(e) => { e.stopPropagation(); toggleNode(node); }}
				onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); toggleNode(node); } }}
				tabindex={-1}
			>
				{#if node.loading}
					<Loader2 size={12} strokeWidth={2} class="spin" />
				{:else}
					<ChevronRight size={12} strokeWidth={2} />
				{/if}
			</span>
			{#if node.expanded}
				<FolderOpen size={14} strokeWidth={1.5} />
			{:else}
				<Folder size={14} strokeWidth={1.5} />
			{/if}
			<span class="tree-name">{node.entry.name}</span>
		</button>
	</div>
	{#if node.expanded && node.children}
		{@render pageOf(node.children, depth + 1, node.pageStart, (start) => (node.pageStart = start))}
	{/if}
{/snippet}

{#snippet pageOf(nodes: TreeNode[], depth: number, start: number, setStart: (start: number) => void)}
	{#if nodes.length > PAGE_SIZE}
		<div class="tree-page" style="--depth: {depth}">
			<button type="button" class="st-btn" aria-label="Show previous folders" disabled={start === 0}
				onclick={() => setStart(start - PAGE_SIZE)}>Previous</button>
			<span aria-live="polite">{start + 1}–{Math.min(start + PAGE_SIZE, nodes.length)} of {nodes.length}</span>
			<button type="button" class="st-btn" aria-label="Show next folders" disabled={start + PAGE_SIZE >= nodes.length}
				onclick={() => setStart(start + PAGE_SIZE)}>Next</button>
		</div>
	{/if}
	{#each nodes.slice(start, start + PAGE_SIZE) as node (node.entry.path)}
		{@render treeItem(node, depth)}
	{/each}
{/snippet}

<style>
	.directory-browser {
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--background);
		max-height: 280px;
		overflow-y: auto;
		overflow-x: hidden;
	}

	.loading {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 16px;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
	}


	.tree {
		padding: 4px 0;
	}

	.tree-item {
		padding-left: calc(var(--depth) * 16px);
	}

	.tree-page {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 4px;
		padding: 4px 8px 4px min(calc(8px + var(--depth) * 16px), 20%);
		font-size: 0.8125rem;
		color: var(--muted-foreground);
	}

	.tree-row {
		display: flex;
		align-items: center;
		gap: 4px;
		width: 100%;
		padding: 4px 8px;
		background: transparent;
		border: none;
		border-radius: 4px;
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		text-align: left;
		transition: background-color 100ms ease;
	}

	.tree-row:hover {
		background: var(--hover-overlay);
	}

	.tree-row.selected {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
	}

	.tree-row :global(svg) {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}

	.tree-row.selected :global(svg) {
		color: var(--primary);
	}

	.expand-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 16px;
		height: 16px;
		padding: 0;
		background: transparent;
		border: none;
		cursor: pointer;
		color: var(--muted-foreground);
		transition: transform 150ms ease;
	}

	.expand-btn.expanded {
		transform: rotate(90deg);
	}

	.expand-btn.hidden {
		visibility: hidden;
	}

	.tree-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		flex: 1;
	}

	:global(.spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
