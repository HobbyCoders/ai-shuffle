<script lang="ts">
	// Layout adapted from pingdotgg/t3code's MIT-licensed DiffPanel.
	// See t3code.LICENSE for the upstream notice.
	import { tick, untrack, type Snippet } from 'svelte';
	import { X, ChevronDown, ChevronRight, ChevronsDownUp, ChevronsUpDown, Rows3, Columns2, WrapText, FolderTree, Folder, FileText, Copy, RefreshCw, GitBranch, AlertTriangle, MessageSquareOff } from 'lucide-svelte';
	import * as gitApi from '$lib/api/git';
	import type { FileChangeInfo } from '$lib/api/git';
	import { git } from '$lib/stores/git.svelte';
	import { notify } from '$lib/stores/notifications';
	import { copyText } from '$lib/utils/clipboard';
	import type { ReviewScope } from '$lib/review/diff-feedback';
	import { shortSha } from '$lib/review/diff-feedback';
	import { baseScopeUnavailableReason, errorMessage, filterWorkingFiles, originLabel, statusLabel, type WorkingFilter } from '$lib/review/scope';
	import DiffViewer from './DiffViewer.svelte';

	interface Props {
		projectId: string;
		files: FileChangeInfo[];
		loading: boolean;
		selected: FileChangeInfo | null;
		onSelect: (file: FileChangeInfo | null) => void;
		onClose: () => void;
		embedded: boolean;
		panelActions?: Snippet;
		/** Tells the host which scope is showing (stage/discard only fit 'working'). */
		onScopeChange?: (scope: ReviewScope) => void;
		/** Sends a line comment to the current chat; enables diff line selection. */
		onAskAgent?: (prompt: string) => Promise<boolean>;
		/** Shown instead of line comments when there is no chat to send to. */
		askAgentUnavailable?: string;
	}
	let { projectId, files, loading, selected, onSelect, onClose, embedded, panelActions, onScopeChange, onAskAgent, askAgentUnavailable }: Props = $props();
	let mode = $state<'unified' | 'side-by-side'>('unified');
	let wrap = $state(false);
	let treeOpen = $state(false);
	let reviewScope = $state<ReviewScope>('working');
	let scope = $state<WorkingFilter>('all');
	let collapsed = $state<Set<string>>(new Set());
	let visible = $state<Set<string>>(new Set());
	let collapsedDirs = $state<Set<string>>(new Set());
	let revision = $state(0);
	let refreshing = $state(false);
	const elements = new Map<string, HTMLElement>();
	const key = (f: FileChangeInfo) => JSON.stringify([f.category, f.file]);

	// ── Changes vs base: everything the task changed since its merge base ──
	const sessionId = $derived(typeof git.current?.sessionId === 'string' ? git.current.sessionId : null);
	const baseUnavailable = $derived(baseScopeUnavailableReason(sessionId));
	let baseFiles = $state<FileChangeInfo[]>([]);
	let baseInfo = $state<{ baseRef: string | null; mergeBase: string | null }>({ baseRef: null, mergeBase: null });
	let baseLoading = $state(false);
	let baseLoaded = $state(false);
	let baseError = $state<string | null>(null);
	let baseRevision = $state(0);
	// Working-tree edits change what the base comparison shows too.
	const workingSignature = $derived(files.map(f => `${f.category}:${f.file}:${f.additions}:${f.deletions}`).join('|'));

	function setScope(next: ReviewScope) {
		if (next === reviewScope || (next === 'base' && baseUnavailable)) return;
		reviewScope = next;
		onScopeChange?.(next);
	}
	// Leaving the chat that owns the checkout drops back to the working tree.
	$effect(() => { if (baseUnavailable && untrack(() => reviewScope) === 'base') setScope('working'); });
	$effect(() => {
		if (reviewScope !== 'base' || !sessionId) return;
		const request = { project: projectId, session: sessionId };
		void workingSignature; void baseRevision;
		let disposed = false;
		baseLoading = true;
		baseError = null;
		gitApi.getBaseChangedFiles(request.project, request.session)
			.then(resp => {
				if (disposed) return;
				baseFiles = resp.files ?? [];
				baseInfo = { baseRef: resp.base_ref ?? null, mergeBase: resp.merge_base ?? null };
				baseLoaded = true;
			})
			.catch(e => {
				if (disposed) return;
				baseError = errorMessage(e, 'Could not compare with the base branch');
			})
			.finally(() => { if (!disposed) baseLoading = false; });
		return () => { disposed = true; };
	});
	// A different chat means a different checkout and base.
	$effect(() => { void sessionId; untrack(() => { baseFiles = []; baseLoaded = false; baseInfo = { baseRef: null, mergeBase: null }; }); });

	const listLoading = $derived(reviewScope === 'base' ? baseLoading && !baseLoaded : loading);
	const shown = $derived(reviewScope === 'base' ? (baseError ? [] : baseFiles) : filterWorkingFiles(files, scope));
	const emptyText = $derived(
		reviewScope === 'base'
			? `No changes since ${baseInfo.baseRef ?? 'the base branch'}.`
			: files.length ? 'No changes in this selection.' : 'No changes in your working tree.'
	);
	const totals = $derived(shown.reduce((s, f) => ({ additions: s.additions + f.additions, deletions: s.deletions + f.deletions }), { additions: 0, deletions: 0 }));
	const allCollapsed = $derived(shown.length > 0 && shown.every(f => collapsed.has(key(f))));
	const directories = $derived.by(() => {
		const groups = new Map<string, FileChangeInfo[]>();
		for (const f of shown) {
			const path = f.file.replaceAll('\\', '/');
			const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '.';
			groups.set(dir, [...(groups.get(dir) ?? []), f]);
		}
		return [...groups].sort(([a], [b]) => a.localeCompare(b));
	});
	const basename = (path: string) => path.replaceAll('\\', '/').split('/').pop();

	function toggle(set: Set<string>, value: string) {
		const next = new Set(set);
		if (next.has(value)) next.delete(value); else next.add(value);
		return next;
	}
	function observe(node: HTMLElement, fileKey: string) {
		elements.set(fileKey, node);
		const observer = new IntersectionObserver(entries => {
			if (entries.some(entry => entry.isIntersecting)) {
				visible = new Set([...visible, fileKey]);
				observer.disconnect();
			}
		}, { rootMargin: '300px' });
		observer.observe(node);
		return { destroy() { observer.disconnect(); elements.delete(fileKey); } };
	}
	async function reveal(file: FileChangeInfo) {
		const fileKey = key(file);
		collapsed = new Set([...collapsed].filter(k => k !== fileKey));
		visible = new Set([...visible, fileKey]);
		onSelect(file);
		await tick();
		elements.get(fileKey)?.scrollIntoView({ block: 'start', behavior: 'instant' });
	}
	// Keep actions bound to a current entry, including files staged and modified at once.
	$effect(() => {
		const current = selected ? shown.find(f => key(f) === key(selected!)) : null;
		const next = current ?? shown[0] ?? null;
		if (next !== selected) onSelect(next);
	});
	async function refresh() {
		refreshing = true;
		try { await git.refreshChangedFiles(); revision++; if (reviewScope === 'base') baseRevision++; }
		catch { notify('Failed to refresh changes', { severity: 'error' }); }
		finally { refreshing = false; }
	}
	async function copySha(sha: string) {
		try { await copyText(sha); notify('Merge-base SHA copied'); }
		catch { notify('Could not copy SHA', { severity: 'error' }); }
	}
	async function copyPath(path: string) {
		try { await copyText(path); notify('File path copied'); }
		catch { notify('Could not copy file path', { severity: 'error' }); }
	}
</script>

<div class="diff-review">
	<header class="toolbar">
		<div class="scope">
			<div class="scope-switch" role="group" aria-label="Review scope">
				<button aria-pressed={reviewScope === 'working'} title="Staged, unstaged and new files in the checkout" onclick={() => setScope('working')}>Uncommitted</button>
				<button aria-pressed={reviewScope === 'base'} disabled={!!baseUnavailable}
					title={baseUnavailable ?? 'Everything this task changed since it branched, committed or not'}
					onclick={() => setScope('base')}>Changes vs base</button>
			</div>
			{#if reviewScope === 'working'}
				<select aria-label="Diff scope" bind:value={scope}>
					<option value="all">All</option><option value="staged">Staged</option><option value="unstaged">Unstaged</option>
				</select>
			{/if}
		</div>
		<div class="tools">
			<span class="stats" aria-label={`${totals.additions} additions, ${totals.deletions} deletions`}><span class="added">+{totals.additions}</span><span class="deleted">−{totals.deletions}</span></span>
			<button title="Refresh diff" aria-label="Refresh diff" disabled={refreshing || listLoading} onclick={refresh}><RefreshCw size={14} class={refreshing ? 'animate-spin' : ''} /></button>
			<button title={allCollapsed ? 'Expand all files' : 'Collapse all files'} aria-label={allCollapsed ? 'Expand all files' : 'Collapse all files'} disabled={!shown.length} onclick={() => { collapsed = allCollapsed ? new Set() : new Set(shown.map(key)); }}>
				{#if allCollapsed}<ChevronsUpDown size={14} />{:else}<ChevronsDownUp size={14} />{/if}
			</button>
			<div class="segmented" role="group" aria-label="Diff layout">
				<button title="Stacked diff view" aria-label="Stacked diff view" aria-pressed={mode === 'unified'} onclick={() => { mode = 'unified'; }}><Rows3 size={14} /></button>
				<button title="Split diff view" aria-label="Split diff view" aria-pressed={mode === 'side-by-side'} onclick={() => { mode = 'side-by-side'; }}><Columns2 size={14} /></button>
			</div>
			<button title="Wrap diff lines" aria-label="Wrap diff lines" aria-pressed={wrap} onclick={() => { wrap = !wrap; }}><WrapText size={14} /></button>
			<button title="Toggle file tree" aria-label="Toggle file tree" aria-pressed={treeOpen} onclick={() => { treeOpen = !treeOpen; }}><FolderTree size={14} /></button>
			{#if !embedded}<button title="Close" aria-label="Close" onclick={onClose}><X size={16} /></button>{/if}
			{@render panelActions?.()}
		</div>
	</header>
	{#if reviewScope === 'base' && !baseError && baseInfo.mergeBase}
		<div class="base-strip" aria-label="Comparison">
			<GitBranch size={12} aria-hidden="true" />
			<span>Comparing with <strong>{baseInfo.baseRef ?? 'base'}</strong></span>
			<span class="sep" aria-hidden="true">·</span>
			<span>merge base</span>
			<button class="sha" title={`Copy ${baseInfo.mergeBase}`} aria-label={`Copy merge-base SHA ${baseInfo.mergeBase}`} onclick={() => copySha(baseInfo.mergeBase!)}>{shortSha(baseInfo.mergeBase)}</button>
			{#if baseLoading}<RefreshCw size={11} class="animate-spin muted" aria-label="Updating" />{/if}
		</div>
	{/if}
	{#if askAgentUnavailable && !onAskAgent && shown.length}
		<p class="ask-note" role="note"><MessageSquareOff size={12} aria-hidden="true" />{askAgentUnavailable}</p>
	{/if}
	<div class="body">
		<div class="diff-files" aria-label="File diffs" aria-busy={listLoading}>
			{#if reviewScope === 'base' && baseError}
				<div class="empty error" role="alert">
					<AlertTriangle size={18} aria-hidden="true" />
					<p class="error-title">Can't compare with the base branch</p>
					<p class="error-detail">{baseError}</p>
					<div class="error-actions">
						<button class="text-btn" onclick={() => { baseRevision++; }}>Try again</button>
						<button class="text-btn" onclick={() => setScope('working')}>Show uncommitted</button>
					</div>
				</div>
			{:else if !shown.length}
				<div class="empty" role="status">{listLoading ? 'Loading changes…' : emptyText}</div>
			{/if}
			{#each shown as file (key(file))}
				{@const fileKey = key(file)}
				<section class="diff-file" use:observe={fileKey} aria-label={`${file.file} (${file.category})`}>
					<div class="file-header" class:selected={selected && key(selected) === fileKey}>
						<button class="file-toggle" aria-expanded={!collapsed.has(fileKey)} aria-label={`${collapsed.has(fileKey) ? 'Expand' : 'Collapse'} ${file.file} (${file.category})`} onclick={() => { collapsed = toggle(collapsed, fileKey); onSelect(file); }}>
							{#if collapsed.has(fileKey)}<ChevronRight size={12} />{:else}<ChevronDown size={12} />{/if}
							<FileText size={14} /><span class="file-path" title={file.original_file ? `${file.original_file} → ${file.file}` : file.file}>{file.file}</span>
						</button>
						<button class="copy" title="Copy file path" aria-label={`Copy path ${file.file}`} onclick={() => copyPath(file.file)}><Copy size={12} /></button>
						<span class="category">{statusLabel(file)}</span>
						{#if file.change_origin}<span class="origin origin-{file.change_origin}">{originLabel(file.change_origin)}</span>{/if}
						<span class="stats"><span class="added">+{file.additions}</span><span class="deleted">−{file.deletions}</span></span>
					</div>
					{#if !collapsed.has(fileKey)}
						<div class="file-body">
							{#if visible.has(fileKey)}
								<DiffViewer {projectId} {file} {mode} {wrap} refreshKey={revision} scope={reviewScope} {onAskAgent} continuous embedded onClose={() => {}} />
							{:else}<div class="placeholder" role="status">Loading diff…</div>{/if}
						</div>
					{/if}
				</section>
			{/each}
		</div>
		{#if treeOpen}
			<nav class="file-tree" aria-label="Changed file tree">
				<div class="tree-heading">Files <span>{shown.length}</span></div>
				{#each directories as [dir, entries] (dir)}
					<button class="directory" aria-expanded={!collapsedDirs.has(dir)} onclick={() => { collapsedDirs = toggle(collapsedDirs, dir); }}>{#if collapsedDirs.has(dir)}<ChevronRight size={12} />{:else}<ChevronDown size={12} />{/if}<Folder size={13} /><span title={dir}>{dir}</span></button>
					{#if !collapsedDirs.has(dir)}
						{#each entries as file (key(file))}
							<button class="tree-file" aria-current={selected && key(selected) === key(file) ? 'true' : undefined} title={`${file.file} (${file.category})`} onclick={() => reveal(file)}><FileText size={13} /><span>{basename(file.file)}</span><small class:added={file.status === '?' || file.status === 'A'} class:deleted={file.status === 'D'}>{file.category === 'staged' ? 'S' : file.status}</small></button>
						{/each}
					{/if}
				{/each}
			</nav>
		{/if}
	</div>
</div>

<style>
	.diff-review { display: flex; flex: 1; flex-direction: column; min-height: 0; min-width: 0; background: var(--background); container-type: inline-size; }
	.toolbar { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 40px; padding: 4px 10px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
	.scope, .tools, .stats, .segmented { display: flex; align-items: center; gap: 6px; }
	.scope { min-width: 0; }
	/* Scope switch: two-state pill; the active half lifts onto the surface. */
	.scope-switch { display: inline-flex; flex-shrink: 0; gap: 2px; padding: 2px; border-radius: 7px; background: color-mix(in oklch, var(--muted) 70%, transparent); }
	.scope-switch button { height: 24px; min-width: 0; padding: 0 9px; font-size: 11.5px; font-weight: 500; white-space: nowrap; border-radius: 5px; transition: background-color .15s ease-out, color .15s ease-out, box-shadow .15s ease-out; }
	.scope-switch button[aria-pressed=true] { background: var(--background); color: var(--foreground); box-shadow: var(--shadow-s); }
	.scope-switch button:hover:not(:disabled):not([aria-pressed=true]) { background: transparent; color: var(--foreground); }
	.base-strip { display: flex; align-items: center; gap: 6px; min-height: 28px; padding: 3px 12px; flex-shrink: 0; overflow: hidden; white-space: nowrap; font-size: 11px; color: var(--muted-foreground); border-bottom: 1px solid color-mix(in srgb, var(--border) 60%, transparent); background: linear-gradient(90deg, color-mix(in oklch, var(--primary) 9%, transparent), transparent 70%); }
	.ask-note { display: flex; align-items: center; gap: 6px; margin: 0; padding: 4px 12px; flex-shrink: 0; font-size: 11px; color: var(--muted-foreground); border-bottom: 1px solid color-mix(in srgb, var(--border) 60%, transparent); }
	.ask-note :global(svg) { flex-shrink: 0; }
	.base-strip :global(svg) { flex-shrink: 0; color: var(--primary); }
	.base-strip strong { color: var(--foreground); font-weight: 500; font-family: var(--font-mono); }
	.base-strip .sep { opacity: .5; }
	.base-strip .sha { height: 20px; min-width: 0; padding: 0 5px; font-family: var(--font-mono); font-size: 11px; color: var(--foreground); background: color-mix(in oklch, var(--muted) 80%, transparent); }
	.base-strip :global(.muted) { color: var(--muted-foreground); }
	.origin { flex-shrink: 0; padding: 1px 6px; border-radius: 999px; font-size: 10px; line-height: 15px; white-space: nowrap; color: var(--muted-foreground); border: 1px solid var(--border); }
	.origin-uncommitted { color: var(--warning); border-color: color-mix(in oklch, var(--warning) 35%, transparent); }
	.origin-both { color: var(--info); border-color: color-mix(in oklch, var(--info) 35%, transparent); }
	.empty.error { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; }
	.empty.error :global(svg) { color: var(--warning); }
	.error-title { margin: 0; color: var(--foreground); font-weight: 500; }
	.error-detail { margin: 0; max-width: 360px; overflow-wrap: anywhere; }
	.error-actions { display: flex; gap: 6px; margin-top: 4px; }
	.text-btn { height: 28px; padding: 0 10px; border: 1px solid var(--border); font-size: 12px; color: var(--foreground); }
	select { color: var(--muted-foreground); background: var(--background); border: 0; font-size: 11px; max-width: 125px; padding: 4px; }
	.tools { gap: 2px; flex-shrink: 0; }
	button { display: inline-flex; align-items: center; justify-content: center; height: 28px; min-width: 28px; border: 0; border-radius: 4px; background: transparent; color: var(--muted-foreground); cursor: pointer; }
	button:hover { background: var(--muted); color: var(--foreground); }
	button:focus-visible, select:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	button:disabled { opacity: .4; cursor: default; }
	button[aria-pressed=true] { background: var(--muted); color: var(--foreground); }
	.segmented { gap: 1px; padding: 2px; border: 1px solid var(--border); border-radius: 5px; }
	.segmented button { height: 22px; min-width: 25px; }
	.stats { font-family: var(--font-mono); font-size: 11px; white-space: nowrap; font-variant-numeric: tabular-nums; gap: 7px; padding: 0 4px; }
	.added { color: var(--success); } .deleted { color: var(--destructive); }
	.body { display: flex; flex: 1; min-height: 0; overflow: hidden; }
	.diff-files { flex: 1; min-width: 0; overflow: auto; overscroll-behavior: contain; scrollbar-width: thin; container-type: inline-size; }
	.diff-file { scroll-margin-top: 0; }
	.file-header { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 3px; min-height: 34px; padding: 0 8px; background: var(--background); border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent); }
	.file-header.selected { box-shadow: inset 2px 0 var(--muted-foreground); }
	.file-toggle { justify-content: flex-start; gap: 7px; min-width: 0; flex: 1; text-align: left; font-size: 12px; color: var(--foreground); }
	.file-toggle :global(svg) { flex-shrink: 0; color: var(--muted-foreground); }
	.file-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.category { font-size: 10px; color: var(--muted-foreground); }
	.copy { opacity: 0; min-width: 24px; }
	.file-header:hover .copy, .file-header:focus-within .copy { opacity: 1; }
	.file-body { padding-bottom: 12px; }
	.empty { display: grid; place-items: center; height: 100%; min-height: 120px; padding: 24px; color: var(--muted-foreground); text-align: center; font-size: 12px; }
	.placeholder { height: 160px; padding: 20px; font-size: 12px; color: var(--muted-foreground); }
	.file-tree { width: 220px; max-width: 35%; flex-shrink: 0; overflow: auto; border-left: 1px solid var(--border); padding: 8px 5px; font-size: 11px; }
	.tree-heading { display: flex; justify-content: space-between; padding: 3px 8px 10px; color: var(--muted-foreground); }
	.directory, .tree-file { justify-content: flex-start; gap: 5px; width: 100%; text-align: left; font-size: 11px; }
	.directory span, .tree-file span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.directory :global(svg), .tree-file :global(svg) { flex-shrink: 0; }
	.tree-file { padding-left: 22px; } .tree-file small { margin-left: auto; }
	.tree-file[aria-current=true] { background: var(--muted); color: var(--foreground); }
	@container (max-width: 520px) { .toolbar { flex-wrap: wrap; gap: 2px; padding: 4px 6px; } .scope { flex: 1 0 100%; } .tools { justify-content: flex-end; width: 100%; } .tools > .stats { margin-right: auto; } .body { position: relative; } .file-tree { position: absolute; right: 0; top: 0; bottom: 0; width: 210px; max-width: 80%; background: var(--background); z-index: 3; box-shadow: -6px 0 16px #0002; } .copy { opacity: 1; } }
	@media (pointer: coarse) { button { min-height: 36px; min-width: 36px; } .segmented button { min-height: 32px; } .copy { opacity: 1; } .scope-switch button { min-height: 34px; padding: 0 12px; } .base-strip .sha { min-height: 28px; } }
</style>
