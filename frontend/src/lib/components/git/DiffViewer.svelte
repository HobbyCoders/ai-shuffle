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
			case 'A': return 'bg-emerald-500/20 text-emerald-400 ring-emerald-500/30';
			case 'M': return 'bg-amber-500/20 text-amber-400 ring-amber-500/30';
			case 'D': return 'bg-red-500/20 text-red-400 ring-red-500/30';
			case 'R': return 'bg-violet-500/20 text-violet-400 ring-violet-500/30';
			case '?': return 'bg-sky-500/20 text-sky-400 ring-sky-500/30';
			case 'U': return 'bg-orange-500/20 text-orange-400 ring-orange-500/30';
			default: return 'bg-zinc-500/20 text-zinc-400 ring-zinc-500/30';
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

<div class="flex flex-col h-full bg-zinc-900 text-zinc-100">
	<!-- Sticky Header (hidden when embedded) -->
	{#if !embedded}
		<header class="sticky top-0 z-10 flex items-center gap-3 px-4 py-3 bg-zinc-900/95 backdrop-blur-sm border-b border-zinc-800 min-h-[52px]">
			<button
				onclick={onClose}
				class="flex items-center justify-center w-8 h-8 rounded-md text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 transition-colors shrink-0 focus:outline-none focus:ring-2 focus:ring-zinc-500"
				aria-label="Back to file list"
			>
				<ArrowLeft size={18} />
			</button>

			<FileText size={16} class="text-zinc-500 shrink-0" />

			<span class="font-mono text-sm text-zinc-200 truncate min-w-0" title={file.file}>
				{file.file}
			</span>

			<span class="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded ring-1 ring-inset shrink-0 {statusColor}">
				{statusLabel}
			</span>

			{#if totalChanges().additions > 0 || totalChanges().deletions > 0}
				<span class="text-xs font-mono shrink-0 ml-auto flex items-center gap-1.5">
					{#if totalChanges().additions > 0}
						<span class="text-green-400">+{totalChanges().additions}</span>
					{/if}
					{#if totalChanges().deletions > 0}
						<span class="text-red-400">-{totalChanges().deletions}</span>
					{/if}
				</span>
			{/if}
		</header>
	{/if}

	<!-- Content Area -->
	<div class="flex-1 overflow-y-auto overflow-x-hidden">
		{#if loading}
			<div class="flex flex-col items-center justify-center py-20 gap-3">
				<Loader2 size={24} class="text-zinc-500 animate-spin" />
				<span class="text-sm text-zinc-500">Loading diff...</span>
			</div>
		{:else if error}
			<div class="flex flex-col items-center justify-center py-20 gap-2 px-4">
				<div class="text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg px-4 py-3 max-w-md text-center">
					{error}
				</div>
			</div>
		{:else if diffResponse?.is_binary}
			<div class="flex flex-col items-center justify-center py-20 gap-2">
				<span class="text-sm text-zinc-500">Binary file — cannot display diff</span>
			</div>
		{:else if !parsedDiff || parsedDiff.hunks.length === 0}
			<div class="flex flex-col items-center justify-center py-20 gap-2">
				<span class="text-sm text-zinc-500">No changes to display</span>
			</div>
		{:else if resolvedMode === 'side-by-side'}
			<!-- ═══ Side-by-Side Diff ═══ -->
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
				<div class="sticky bottom-0 flex items-center justify-center gap-2 px-4 py-2.5 bg-amber-500/10 border-t border-amber-500/20 text-amber-400 text-xs font-medium">
					Diff output was truncated — only showing partial changes
				</div>
			{/if}
		{:else}
			<!-- ═══ Unified Diff ═══ -->
			<div class="diff-content">
				{#each compactLines as line}
					{#if line.type === 'collapse'}
						<div class="collapse-row">
							<div class="collapse-gutter"></div>
							<div class="collapse-line"></div>
						</div>
					{:else if line.type === 'header'}
						<div class="flex bg-blue-900/10">
							<div class="gutter-col text-blue-900/40 text-[10px]">···</div>
							<div class="gutter-col text-blue-900/40 text-[10px]">···</div>
							<div class="flex-1 px-3 font-mono text-[11px] text-blue-400/60 whitespace-pre select-none overflow-x-auto leading-[1.375rem]">
								{line.content}
							</div>
						</div>
					{:else if line.type === 'addition'}
						<div class="flex bg-green-900/15 hover:bg-green-900/25 transition-colors duration-75">
							<div class="gutter-col text-zinc-700"></div>
							<div class="gutter-col text-green-700">{line.newLineNumber}</div>
							<div class="flex-1 px-3 font-mono text-sm text-green-300 whitespace-pre overflow-x-auto">
								<span class="text-green-600 select-none mr-1">+</span>{line.content}
							</div>
						</div>
					{:else if line.type === 'deletion'}
						<div class="flex bg-red-900/15 hover:bg-red-900/25 transition-colors duration-75">
							<div class="gutter-col text-red-700">{line.oldLineNumber}</div>
							<div class="gutter-col text-zinc-700"></div>
							<div class="flex-1 px-3 font-mono text-sm text-red-300 whitespace-pre overflow-x-auto">
								<span class="text-red-600 select-none mr-1">-</span>{line.content}
							</div>
						</div>
					{:else}
						<div class="flex hover:bg-zinc-800/30 transition-colors duration-75">
							<div class="gutter-col text-zinc-700">{line.oldLineNumber}</div>
							<div class="gutter-col text-zinc-700">{line.newLineNumber}</div>
							<div class="flex-1 px-3 font-mono text-sm text-zinc-400 whitespace-pre overflow-x-auto">
								<span class="text-zinc-700 select-none mr-1">&nbsp;</span>{line.content}
							</div>
						</div>
					{/if}
				{/each}
			</div>

			{#if diffResponse?.truncated}
				<div class="sticky bottom-0 flex items-center justify-center gap-2 px-4 py-2.5 bg-amber-500/10 border-t border-amber-500/20 text-amber-400 text-xs font-medium">
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
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace;
		font-size: 0.75rem;
		line-height: 1.5rem;
		user-select: none;
		color: inherit;
		border-right: 1px solid rgb(39, 39, 42, 0.5);
	}

	/* ─── Collapsed context rows ──────────────────────────────────── */
	.collapse-row {
		display: flex;
		height: 6px;
	}

	.collapse-gutter {
		width: 7rem; /* 2 × 3.5rem gutters */
		min-width: 7rem;
		background: rgb(39 39 42 / 0.15);
	}

	.collapse-line {
		flex: 1;
		background: rgb(39 39 42 / 0.08);
		border-top: 1px dashed rgb(63 63 70 / 0.3);
		border-bottom: 1px dashed rgb(63 63 70 / 0.3);
	}

	.diff-content :global(.overflow-x-auto) {
		scrollbar-width: thin;
		scrollbar-color: rgb(63, 63, 70) transparent;
	}

	.diff-content :global(.overflow-x-auto::-webkit-scrollbar) {
		height: 4px;
	}

	.diff-content :global(.overflow-x-auto::-webkit-scrollbar-track) {
		background: transparent;
	}

	.diff-content :global(.overflow-x-auto::-webkit-scrollbar-thumb) {
		background: rgb(63, 63, 70);
		border-radius: 2px;
	}

	/* ─── Side-by-Side mode ────────────────────────────────────────── */
	.sbs-diff {
		min-width: max-content;
	}

	.sbs-table {
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace;
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
		background: rgb(30 58 138 / 0.15);
		border-bottom: 1px solid rgb(59 130 246 / 0.1);
		border-top: 1px solid rgb(59 130 246 / 0.05);
		color: rgb(147 197 253 / 0.7);
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
		color: rgb(63 63 70);
		font-size: 0.6875rem;
		line-height: 1.375rem;
	}

	.sbs-gutter-del { color: rgb(127 29 29 / 0.8); }
	.sbs-gutter-add { color: rgb(20 83 45 / 0.8); }

	.sbs-divider {
		background: rgb(63 63 70 / 0.25);
	}

	.sbs-cell {
		padding: 0 8px;
		white-space: pre;
		overflow: hidden;
		min-height: 1.375rem;
	}

	.sbs-cell-ctx {
		color: rgb(161 161 170);
	}

	.sbs-cell-del {
		background: rgb(127 29 29 / 0.12);
	}

	.sbs-cell-add {
		background: rgb(20 83 45 / 0.12);
	}

	.sbs-cell-blank {
		background: rgb(24 24 27 / 0.5);
	}

	.sbs-prefix {
		user-select: none;
		margin-right: 4px;
	}

	.del-prefix { color: rgb(248 113 113 / 0.5); }
	.add-prefix { color: rgb(74 222 128 / 0.5); }

	.sbs-del-text { color: rgb(252 165 165); }
	.sbs-add-text { color: rgb(134 239 172); }

	/* Hover highlight */
	.sbs-row:hover .sbs-cell-del { background: rgb(127 29 29 / 0.2); }
	.sbs-row:hover .sbs-cell-add { background: rgb(20 83 45 / 0.2); }
	.sbs-row:hover .sbs-cell-ctx { background: rgb(39 39 42 / 0.2); }

	/* Scrollbar for sbs */
	.sbs-diff {
		scrollbar-width: thin;
		scrollbar-color: rgb(63 63 70) transparent;
	}
</style>
