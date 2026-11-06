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
		aria-current={isActive ? 'page' : undefined}
		title={path}
		style="padding-left: {8 + Math.min(depth, 7) * 15}px"
		role="button"
		tabindex="0"
		onclick={activate}
		onkeydown={(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); activate(); } }}
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
			<div class="ftn-empty" style="padding-left: {8 + Math.min(depth + 1, 7) * 15 + 18}px">No subfolders</div>
		{/if}
	{/if}
</div>

<style>
	.ftn-row { display: flex; align-items: center; gap: 7px; min-height: 34px; padding: 6px 8px; border-radius: var(--st-radius-md); font-size: 13px; color: var(--st-fg); cursor: pointer; user-select: none; }
	.ftn-row:hover { background: var(--st-hover); }
	.ftn-row:focus-visible { outline: 2px solid var(--st-primary); outline-offset: -2px; }
	.ftn-row.on { background: var(--st-accent); }
	.ftn-row :global(svg) { flex: none; color: var(--st-mfg); }
	.ftn-caret { display: grid; place-items: center; width: 20px; height: 20px; flex: none; padding: 0; border: 0; background: transparent; color: var(--st-mfg); cursor: pointer; border-radius: var(--st-radius-sm); transition: transform .2s ease; }
	.ftn-caret:hover { background: var(--st-hover); }
	.ftn-caret.open { transform: rotate(90deg); }
	.ftn-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.ftn-empty { padding: 4px 8px; font-size: 12px; color: var(--st-mfg); }
	@media (max-width: 600px), (pointer: coarse) { .ftn-row { min-height: 44px; } .ftn-caret { width: 28px; min-height: 32px; } }
	@media (prefers-reduced-motion: reduce) { .ftn-caret { transition: none; } }
</style>
