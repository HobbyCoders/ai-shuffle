<script lang="ts">
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
	}

	let { onSelect, selectedPath = '' }: Props = $props();

	interface TreeNode {
		entry: DirectoryEntry;
		children: TreeNode[] | null; // null = not loaded, [] = empty
		expanded: boolean;
		loading: boolean;
	}

	let roots = $state<TreeNode[]>([]);
	let rootLoading = $state(true);
	let error = $state<string | null>(null);

	// Load root entries on mount
	$effect(() => {
		loadRoots();
	});

	async function loadRoots() {
		rootLoading = true;
		error = null;
		try {
			const response = await browseFilesystem();
			roots = response.entries
				.filter(e => e.is_dir)
				.map(entry => ({
					entry,
					children: null,
					expanded: false,
					loading: false
				}));
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
			node.loading = true;
			try {
				const response = await browseFilesystem(node.entry.path);
				node.children = response.entries
					.filter(e => e.is_dir)
					.map(entry => ({
						entry,
						children: null,
						expanded: false,
						loading: false
					}));
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
		<div class="error">{error}</div>
	{:else}
		<div class="tree">
			{#each roots as node (node.entry.path)}
				{@render treeItem(node, 0)}
			{/each}
		</div>
	{/if}
</div>

{#snippet treeItem(node: TreeNode, depth: number)}
	<div class="tree-item" style="--depth: {depth}">
		<button
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
		{#each node.children as child (child.entry.path)}
			{@render treeItem(child, depth + 1)}
		{/each}
	{/if}
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

	.loading, .error {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 16px;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
	}

	.error {
		color: var(--destructive);
	}

	.tree {
		padding: 4px 0;
	}

	.tree-item {
		padding-left: calc(var(--depth) * 16px);
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
