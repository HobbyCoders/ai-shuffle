<script lang="ts">
	/**
	 * DiffViewer - Diff viewer for a single file
	 *
	 * Lazily fetches and displays the diff for a file when mounted.
	 * Supports two rendering modes:
	 *   - 'unified' (default): traditional line-by-line with two-column gutter
	 *   - 'side-by-side': split view with old file on left, new on right
	 *
	 * When `embedded` is true, the header is hidden (used inside ChangedFilesPanel review mode).
	 */
	import { ArrowLeft, FileText, Loader2 } from 'lucide-svelte';
	import { git } from '$lib/stores/git';
	import * as gitApi from '$lib/api/git';
	import { parseDiff, toSideBySide, countChanges, shouldRefetchDiff, effectiveDiffMode } from '$lib/utils/diff-parser';
	import type { ParsedDiff, SideBySidePair } from '$lib/utils/diff-parser';

	interface Props {
		projectId: string;
		file: gitApi.FileChangeInfo;
		onClose: () => void;
		/** Rendering mode: unified (classic) or side-by-side (split view) */
		mode?: 'unified' | 'side-by-side';
		/** When true, hides the header bar (for embedding inside another panel) */
		embedded?: boolean;
	}

	let { projectId, file, onClose, mode = 'unified', embedded = false }: Props = $props();

	let diffResponse = $state<gitApi.FileDiffResponse | null>(null);
	let parsedDiff = $state<ParsedDiff | null>(null);
	let sideBySidePairs = $state<SideBySidePair[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Track viewport width for responsive mode switching
	let viewportWidth = $state(typeof window !== 'undefined' ? window.innerWidth : 1200);

	// Effective mode: auto-downgrade to unified on mobile
	const resolvedMode = $derived(effectiveDiffMode(mode, viewportWidth));

	// Track previous file to avoid re-fetching same file during streaming
	let prevFileKey = $state<string | null>(null);

	const statusLabel = $derived(getStatusLabel(file.status));
	const statusColor = $derived(getStatusColor(file.status));

	// Compact line list: flattens hunks into a single array and collapses
	// runs of >1 context lines into a single "collapse" indicator.
	// Keeps 1 context line before/after changes for orientation.
	type CompactLine = import('$lib/utils/diff-parser').DiffLine | { type: 'collapse'; content: string; oldLineNumber: null; newLineNumber: null };
	const compactLines = $derived.by((): CompactLine[] => {
		if (!parsedDiff) return [];
		const flat: import('$lib/utils/diff-parser').DiffLine[] = [];
		for (const hunk of parsedDiff.hunks) {
			flat.push(...hunk.lines);
		}

		// Identify runs of context lines and collapse them
		const result: CompactLine[] = [];
		let i = 0;
		while (i < flat.length) {
			const line = flat[i];
			if (line.type === 'context') {
				// Count consecutive context lines
				let runStart = i;
				while (i < flat.length && flat[i].type === 'context') i++;
				const runLen = i - runStart;

				if (runLen <= 2) {
					// Keep short context runs as-is
					for (let j = runStart; j < i; j++) result.push(flat[j]);
				} else {
					// Keep first and last context line, collapse the middle
					result.push(flat[runStart]);
					result.push({ type: 'collapse', content: '', oldLineNumber: null, newLineNumber: null });
					result.push(flat[i - 1]);
				}
			} else {
				result.push(line);
				i++;
			}
		}
		return result;
	});

	const totalChanges = $derived(() => {
		if (parsedDiff) {
			return countChanges(parsedDiff);
		}
		return { additions: file.additions, deletions: file.deletions };
	});

	function getStatusLabel(status: string): string {
		switch (status) {
			case 'A': return 'Added';
			case 'M': return 'Modified';
			case 'D': return 'Deleted';
			case 'R': return 'Renamed';
			case '?': return 'New File';
			case 'U': return 'Conflict';
			default: return status;
		}
	}

	function getStatusColor(status: string): string {
		switch (status) {
			case 'A': return 'bg-[var(--status-success-bg)] text-success ring-[var(--status-success-border)]';
			case 'M': return 'bg-[var(--status-warning-bg)] text-warning ring-[var(--status-warning-border)]';
			case 'D': return 'bg-[var(--status-error-bg)] text-destructive ring-[var(--status-error-border)]';
			case 'R': return 'bg-[var(--status-info-bg)] text-info ring-[var(--status-info-border)]';
			case '?': return 'bg-[var(--status-info-bg)] text-info ring-[var(--status-info-border)]';
			case 'U': return 'bg-[var(--status-warning-bg)] text-warning ring-[var(--status-warning-border)]';
			default: return 'bg-[var(--status-pending-bg)] text-muted-foreground ring-[var(--status-pending-border)]';
		}
	}

	$effect(() => {
		const f = file; // track
		const fileKey = `${f.file}::${f.category}`;

		// Skip re-fetch if same file+category (prevents flash during streaming)
		if (fileKey === prevFileKey) return;
		prevFileKey = fileKey;

		loading = true;
		error = null;
		diffResponse = null;
		parsedDiff = null;
		sideBySidePairs = [];

		const worktreeId = git.current.worktreeId ?? undefined;

		gitApi.getFileDiff(projectId, f.file, f.category === 'staged', worktreeId)
			.then(resp => {
				diffResponse = resp;
				if (!resp.is_binary && resp.diff) {
					const parsed = parseDiff(resp.diff);
					parsedDiff = parsed;
					sideBySidePairs = toSideBySide(parsed.hunks);
				}
				loading = false;
			})
			.catch(e => {
				error = e?.detail || e?.message || 'Failed to load diff';
				loading = false;
			});
	});
</script>

<svelte:window onresize={() => { viewportWidth = window.innerWidth; }} />

<div class="flex flex-col h-full bg-background text-foreground">
	<!-- Sticky Header (hidden when embedded) -->
	{#if !embedded}
		<header class="sticky top-0 z-10 flex items-center gap-3 px-4 py-3 bg-[var(--background)]/95 backdrop-blur-sm border-b border-border min-h-[52px]">
			<button
				onclick={onClose}
				class="flex items-center justify-center w-8 h-8 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent transition-colors shrink-0 focus:outline-none focus:ring-2 focus:ring-ring"
				aria-label="Back to file list"
			>
				<ArrowLeft size={18} />
			</button>

			<FileText size={16} class="text-muted-foreground shrink-0" />

			<span class="font-mono text-sm text-foreground truncate min-w-0" title={file.file}>
				{file.file}
			</span>

			<span class="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-sm ring-1 ring-inset shrink-0 {statusColor}">
				{statusLabel}
			</span>

			{#if totalChanges().additions > 0 || totalChanges().deletions > 0}
				<span class="text-xs font-mono shrink-0 ml-auto flex items-center gap-1.5">
					{#if totalChanges().additions > 0}
						<span class="text-success">+{totalChanges().additions}</span>
					{/if}
					{#if totalChanges().deletions > 0}
						<span class="text-destructive">-{totalChanges().deletions}</span>
					{/if}
				</span>
			{/if}
		</header>
	{/if}

	<!-- Content Area -->
	<div class="flex-1 overflow-auto diff-scroll-container">
		{#if loading}
			<div class="flex flex-col items-center justify-center py-20 gap-3">
				<Loader2 size={24} class="text-muted-foreground animate-spin" />
				<span class="text-sm text-muted-foreground">Loading diff...</span>
			</div>
		{:else if error}
			<div class="flex flex-col items-center justify-center py-20 gap-2 px-4">
				<div class="text-sm text-destructive bg-[var(--status-error-bg)] border border-[var(--status-error-border)] rounded-lg px-4 py-3 max-w-md text-center">
					{error}
				</div>
			</div>
		{:else if diffResponse?.is_binary}
			<div class="flex flex-col items-center justify-center py-20 gap-2">
				<span class="text-sm text-muted-foreground">Binary file — cannot display diff</span>
			</div>
		{:else if !parsedDiff || parsedDiff.hunks.length === 0}
			<div class="flex flex-col items-center justify-center py-20 gap-2">
				<span class="text-sm text-muted-foreground">No changes to display</span>
			</div>
		{:else if resolvedMode === 'side-by-side'}
			<!-- Side-by-Side Diff -->
			<div class="sbs-diff">
				<div class="sbs-table">
					{#each sideBySidePairs as pair}
						{#if pair.type === 'header'}
							<div class="sbs-row sbs-header">
								<div class="sbs-header-cell">{pair.left?.content ?? ''}</div>
							</div>
						{:else if pair.type === 'context'}
							<div class="sbs-row">
								<div class="sbs-gutter">{pair.left?.oldLineNumber ?? ''}</div>
								<div class="sbs-cell sbs-cell-ctx">{pair.left?.content ?? ''}</div>
								<div class="sbs-divider"></div>
								<div class="sbs-gutter">{pair.right?.newLineNumber ?? ''}</div>
								<div class="sbs-cell sbs-cell-ctx">{pair.right?.content ?? ''}</div>
							</div>
						{:else}
							<div class="sbs-row">
								<div class="sbs-gutter sbs-gutter-del">{pair.left?.oldLineNumber ?? ''}</div>
								<div class="sbs-cell" class:sbs-cell-del={pair.left !== null} class:sbs-cell-blank={pair.left === null}>
									{#if pair.left}<span class="sbs-prefix del-prefix">-</span><span class="sbs-del-text">{pair.left.content}</span>{/if}
								</div>
								<div class="sbs-divider"></div>
								<div class="sbs-gutter sbs-gutter-add">{pair.right?.newLineNumber ?? ''}</div>
								<div class="sbs-cell" class:sbs-cell-add={pair.right !== null} class:sbs-cell-blank={pair.right === null}>
									{#if pair.right}<span class="sbs-prefix add-prefix">+</span><span class="sbs-add-text">{pair.right.content}</span>{/if}
								</div>
							</div>
						{/if}
					{/each}
				</div>
			</div>

			{#if diffResponse?.truncated}
				<div class="sticky bottom-0 flex items-center justify-center gap-2 px-4 py-2.5 bg-[var(--status-warning-bg)] border-t border-[var(--status-warning-border)] text-warning text-xs font-medium">
					Diff output was truncated — only showing partial changes
				</div>
			{/if}
		{:else}
			<!-- Unified Diff -->
			<div class="diff-content">
				{#each compactLines as line}
					{#if line.type === 'collapse'}
						<div class="collapse-row">
							<div class="collapse-gutter"></div>
							<div class="collapse-line"></div>
						</div>
					{:else if line.type === 'header'}
						<div class="flex diff-line-header">
							<div class="gutter-col diff-header-gutter">···</div>
							<div class="gutter-col diff-header-gutter">···</div>
							<div class="flex-1 px-3 font-mono text-[11px] diff-header-text whitespace-pre select-none leading-[1.375rem]">
								{line.content}
							</div>
						</div>
					{:else if line.type === 'addition'}
						<div class="flex diff-line-add transition-colors duration-75">
							<div class="gutter-col diff-gutter-blank"></div>
							<div class="gutter-col diff-gutter-add">{line.newLineNumber}</div>
							<div class="flex-1 px-3 font-mono text-sm diff-text-add whitespace-pre">
								<span class="diff-prefix-add select-none mr-1">+</span>{line.content}
							</div>
						</div>
					{:else if line.type === 'deletion'}
						<div class="flex diff-line-del transition-colors duration-75">
							<div class="gutter-col diff-gutter-del">{line.oldLineNumber}</div>
							<div class="gutter-col diff-gutter-blank"></div>
							<div class="flex-1 px-3 font-mono text-sm diff-text-del whitespace-pre">
								<span class="diff-prefix-del select-none mr-1">-</span>{line.content}
							</div>
						</div>
					{:else}
						<div class="flex diff-line-ctx transition-colors duration-75">
							<div class="gutter-col diff-gutter-ctx">{line.oldLineNumber}</div>
							<div class="gutter-col diff-gutter-ctx">{line.newLineNumber}</div>
							<div class="flex-1 px-3 font-mono text-sm text-muted-foreground whitespace-pre">
								<span class="diff-gutter-ctx select-none mr-1">&nbsp;</span>{line.content}
							</div>
						</div>
					{/if}
				{/each}
			</div>

			{#if diffResponse?.truncated}
				<div class="sticky bottom-0 flex items-center justify-center gap-2 px-4 py-2.5 bg-[var(--status-warning-bg)] border-t border-[var(--status-warning-border)] text-warning text-xs font-medium">
					Diff output was truncated — only showing partial changes
				</div>
			{/if}
		{/if}
	</div>
</div>

<style>
	/* ─── Unified mode gutters ─────────────────────────────────────── */
	.gutter-col {
		width: 3.5rem;
		min-width: 3.5rem;
		padding: 0 0.5rem;
		text-align: right;
		font-family: var(--font-mono);
		font-size: 0.75rem;
		line-height: 1.5rem;
		user-select: none;
		color: inherit;
		border-right: 1px solid color-mix(in oklch, var(--border) 50%, transparent);
	}

	/* ─── Unified diff line backgrounds ──────────────────────────── */
	.diff-line-header {
		background: color-mix(in oklch, var(--info) 10%, transparent);
	}

	.diff-header-gutter {
		color: color-mix(in oklch, var(--info) 40%, transparent);
		font-size: 10px;
	}

	.diff-header-text {
		color: color-mix(in oklch, var(--info) 60%, transparent);
	}

	.diff-line-add {
		background: color-mix(in oklch, var(--success) 15%, transparent);
	}

	.diff-line-add:hover {
		background: color-mix(in oklch, var(--success) 25%, transparent);
	}

	.diff-gutter-blank {
		color: color-mix(in oklch, var(--muted-foreground) 30%, transparent);
	}

	.diff-gutter-add {
		color: color-mix(in oklch, var(--success) 60%, transparent);
	}

	.diff-text-add {
		color: color-mix(in oklch, var(--success) 85%, white);
	}

	.diff-prefix-add {
		color: color-mix(in oklch, var(--success) 60%, transparent);
	}

	.diff-line-del {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
	}

	.diff-line-del:hover {
		background: color-mix(in oklch, var(--destructive) 25%, transparent);
	}

	.diff-gutter-del {
		color: color-mix(in oklch, var(--destructive) 60%, transparent);
	}

	.diff-text-del {
		color: color-mix(in oklch, var(--destructive) 85%, white);
	}

	.diff-prefix-del {
		color: color-mix(in oklch, var(--destructive) 60%, transparent);
	}

	.diff-line-ctx:hover {
		background: color-mix(in oklch, var(--muted) 20%, transparent);
	}

	.diff-gutter-ctx {
		color: color-mix(in oklch, var(--muted-foreground) 30%, transparent);
	}

	/* ─── Collapsed context rows ──────────────────────────────────── */
	.collapse-row {
		display: flex;
		height: 6px;
	}

	.collapse-gutter {
		width: 7rem; /* 2 x 3.5rem gutters */
		min-width: 7rem;
		background: color-mix(in oklch, var(--muted) 15%, transparent);
	}

	.collapse-line {
		flex: 1;
		background: color-mix(in oklch, var(--muted) 8%, transparent);
		border-top: 1px dashed color-mix(in oklch, var(--border) 30%, transparent);
		border-bottom: 1px dashed color-mix(in oklch, var(--border) 30%, transparent);
	}

	/* Unified diff: grow container to widest line so scroll host
	   (.diff-scroll-container) produces a single horizontal scrollbar
	   instead of one per line. */
	.diff-content {
		display: flex;
		flex-direction: column;
		min-width: max-content;
	}

	.diff-scroll-container {
		scrollbar-width: thin;
		scrollbar-color: var(--scrollbar-thumb) transparent;
	}

	.diff-scroll-container::-webkit-scrollbar {
		width: 8px;
		height: 8px;
	}

	.diff-scroll-container::-webkit-scrollbar-track {
		background: transparent;
	}

	.diff-scroll-container::-webkit-scrollbar-thumb {
		background: var(--scrollbar-thumb);
		border-radius: calc(var(--radius) * 0.33);
	}

	/* ─── Side-by-Side mode ────────────────────────────────────────── */
	.sbs-diff {
		min-width: max-content;
	}

	.sbs-table {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		line-height: 1.375rem;
	}

	.sbs-row {
		display: grid;
		grid-template-columns: 3rem 1fr 1px 3rem 1fr;
	}

	.sbs-header {
		display: block;
	}

	.sbs-header-cell {
		padding: 3px 12px;
		background: color-mix(in oklch, var(--info) 15%, transparent);
		border-bottom: 1px solid color-mix(in oklch, var(--info) 10%, transparent);
		border-top: 1px solid color-mix(in oklch, var(--info) 5%, transparent);
		color: color-mix(in oklch, var(--info) 70%, transparent);
		font-size: 0.6875rem;
		user-select: none;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.sbs-gutter {
		padding: 0 6px;
		text-align: right;
		user-select: none;
		color: var(--border);
		font-size: 0.6875rem;
		line-height: 1.375rem;
	}

	.sbs-gutter-del { color: color-mix(in oklch, var(--destructive) 80%, transparent); }
	.sbs-gutter-add { color: color-mix(in oklch, var(--success) 80%, transparent); }

	.sbs-divider {
		background: color-mix(in oklch, var(--border) 25%, transparent);
	}

	.sbs-cell {
		padding: 0 8px;
		white-space: pre;
		overflow: hidden;
		min-height: 1.375rem;
	}

	.sbs-cell-ctx {
		color: var(--muted-foreground);
	}

	.sbs-cell-del {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
	}

	.sbs-cell-add {
		background: color-mix(in oklch, var(--success) 12%, transparent);
	}

	.sbs-cell-blank {
		background: color-mix(in oklch, var(--background) 50%, transparent);
	}

	.sbs-prefix {
		user-select: none;
		margin-right: 4px;
	}

	.del-prefix { color: color-mix(in oklch, var(--destructive) 50%, transparent); }
	.add-prefix { color: color-mix(in oklch, var(--success) 50%, transparent); }

	.sbs-del-text { color: color-mix(in oklch, var(--destructive) 85%, white); }
	.sbs-add-text { color: color-mix(in oklch, var(--success) 85%, white); }

	/* Hover highlight */
	.sbs-row:hover .sbs-cell-del { background: color-mix(in oklch, var(--destructive) 20%, transparent); }
	.sbs-row:hover .sbs-cell-add { background: color-mix(in oklch, var(--success) 20%, transparent); }
	.sbs-row:hover .sbs-cell-ctx { background: color-mix(in oklch, var(--muted) 20%, transparent); }

	/* Scrollbar for sbs */
	.sbs-diff {
		scrollbar-width: thin;
		scrollbar-color: var(--scrollbar-thumb) transparent;
	}
</style>
