<script lang="ts">
	/**
	 * FilesTreeNode — one folder in the Files page tree sidebar.
	 *
	 * Lazy-loads its child directories on expand (the workspace API lists one
	 * directory at a time) and auto-expands along the active path so the tree
	 * always reveals where you are. Recurses by importing itself. Theme tokens.
	 */
	import { untrack } from 'svelte';
	import { withAuth } from '$lib/api/runtime';
	import { ChevronRight, Folder } from 'lucide-svelte';
	import { files, type FileItem } from '$lib/stores/files.svelte';
	import FilesTreeNode from './FilesTreeNode.svelte';

	interface Props {
		name: string;
		path: string;
		depth: number;
		currentPath: string;
		showHidden: boolean;
		onNavigate: (path: string) => void;
		/** Bumped by the parent after a rename/delete/create so cached children reload. */
		treeVersion?: number;
	}

	let { name, path, depth, currentPath, showHidden, onNavigate, treeVersion = 0 }: Props = $props();

	let expanded = $state(false);
	let children = $state<{ name: string; path: string }[] | null>(null);
	let loading = $state(false);

	const isActive = $derived(currentPath === path);
	const onPath = $derived(currentPath === path || currentPath.startsWith(path + '/'));

	async function loadChildren(force = false) {
		if ((children !== null && !force) || loading) return;
		loading = true;
		try {
			const params = new URLSearchParams({ path, show_hidden: String(showHidden) });
			const res = await fetch(`${files.base()}/files?${params}`, withAuth({ }));
			if (res.ok) {
				const data = await res.json();
				children = (data.files as FileItem[])
					.filter((f) => f.type === 'directory')
					.map((f) => ({ name: f.name, path: f.path }));
			} else {
				children = [];
			}
		} catch {
			children = [];
		} finally {
			loading = false;
		}
	}

	async function toggle(e: Event) {
		e.stopPropagation();
		expanded = !expanded;
		if (expanded) await loadChildren();
	}

	function activate() {
		onNavigate(path);
		if (!expanded) {
			expanded = true;
			loadChildren();
		}
	}

	// Auto-expand so the active path is always revealed in the tree.
	$effect(() => {
		if (onPath && !expanded) {
			expanded = true;
			loadChildren();
		}
	});

	// When the workspace changes (rename/delete/create), refetch so nodes never
	// show stale folders. An expanded node refetches in place — the keyed {#each}
	// then preserves child instances, so nested nodes stay expanded and refresh
	// through the same version prop. A collapsed node just drops its cache; it
	// refetches on next expand. Everything runs inside untrack: this effect must
	// depend on treeVersion only — reading `children` in loadChildren's guard
	// while writing it here would re-trigger the effect in a refetch loop.
	$effect(() => {
		if (treeVersion > 0) {
			untrack(() => {
				if (expanded) void loadChildren(true);
				else children = null;
			});
		}
	});
</script>

<div class="ftn">
	<div
		class="ftn-row"
		class:on={isActive}
		style="padding-left: {8 + depth * 15}px"
		role="button"
		tabindex="0"
		onclick={activate}
		onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); } }}
	>
		<button class="ftn-caret" class:open={expanded} onclick={toggle} aria-label={expanded ? 'Collapse' : 'Expand'} tabindex="-1">
			<ChevronRight size={13} strokeWidth={2} />
		</button>
		<Folder size={14} strokeWidth={1.8} />
		<span class="ftn-name">{name}</span>
	</div>

	{#if expanded && children}
		{#each children as child (child.path)}
			<FilesTreeNode name={child.name} path={child.path} depth={depth + 1} {currentPath} {showHidden} {onNavigate} {treeVersion} />
		{/each}
		{#if children.length === 0 && !loading}
			<div class="ftn-empty" style="padding-left: {8 + (depth + 1) * 15 + 18}px">empty</div>
		{/if}
	{/if}
</div>

<style>
	.ftn-row {
		display: flex;
		align-items: center;
		gap: 7px;
		padding: 6px 8px;
		border-radius: 7px;
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		cursor: pointer;
		transition: background-color 110ms ease, color 110ms ease;
		user-select: none;
	}
	.ftn-row:hover { background: var(--hover-overlay); }
	.ftn-row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.ftn-row.on { background: var(--primary); color: var(--primary-foreground); }
	.ftn-row.on :global(svg) { color: var(--primary-foreground); }
	.ftn-row :global(svg) { flex-shrink: 0; color: var(--muted-foreground); }

	.ftn-caret {
		display: grid;
		place-items: center;
		width: 16px;
		height: 16px;
		flex-shrink: 0;
		padding: 0;
		background: transparent;
		border: none;
		color: var(--muted-foreground);
		cursor: pointer;
		border-radius: 4px;
		transition: transform 140ms ease;
	}
	.ftn-caret.open { transform: rotate(90deg); }
	.ftn-row.on .ftn-caret { color: var(--primary-foreground); }

	.ftn-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.ftn-empty {
		font-size: 0.7rem;
		font-style: italic;
		color: var(--muted-foreground);
		padding-top: 2px;
		padding-bottom: 4px;
		opacity: 0.7;
	}
</style>
