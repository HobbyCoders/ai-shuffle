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
	import { onMount } from 'svelte';
	import { highlightToInnerHtml, onHighlighterReady, resolveShikiLang } from '$lib/utils/shiki';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import { git } from '$lib/stores/git.svelte';
	import * as gitApi from '$lib/api/git';
	import { parseDiff, toSideBySide, countChanges, effectiveDiffMode } from '$lib/utils/diff-parser';
	import type { DiffLine, ParsedDiff, SideBySidePair } from '$lib/utils/diff-parser';

	interface Props {
		projectId: string;
		file: gitApi.FileChangeInfo;
		onClose: () => void;
		/** Rendering mode: unified (classic) or side-by-side (split view) */
		mode?: 'unified' | 'side-by-side';
		/** When true, hides the header bar (for embedding inside another panel) */
		embedded?: boolean;
		continuous?: boolean;
		wrap?: boolean;
		refreshKey?: number;
	}

	let { projectId, file, onClose, mode = 'unified', embedded = false, continuous = false, wrap = false, refreshKey = 0 }: Props = $props();

	let diffResponse = $state<gitApi.FileDiffResponse | null>(null);
	let parsedDiff = $state<ParsedDiff | null>(null);
	let sideBySidePairs = $state<SideBySidePair[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Track viewport width for responsive mode switching
	let viewportWidth = $state(typeof window !== 'undefined' ? window.innerWidth : 1200);

	// Effective mode: auto-downgrade to unified on mobile
	const resolvedMode = $derived(effectiveDiffMode(mode, viewportWidth));

	// Stable primitive identity avoids refetches on status polling.
	const requestKey = $derived(JSON.stringify([projectId, file.file, file.category, git.current.worktreeId, git.current.sessionId ?? git.current.workerTarget, refreshKey]));

	const lineKey = (line: DiffLine) => `${line.type}:${line.oldLineNumber}:${line.newLineNumber}`;
	let highlightVersion = $state(0);
	onMount(() => onHighlighterReady(() => { highlightVersion++; }));
	const highlighted = $derived.by(() => {
		highlightVersion;
		const result = new Map<string, string>();
		const lang = resolveShikiLang(file.file.split('.').pop());
		if (!continuous || !parsedDiff || !lang) return result;
		for (const hunk of parsedDiff.hunks) {
			// Tokenize each side separately so removed code cannot change the new side's syntax.
			for (const excluded of ['addition', 'deletion']) {
				const lines = hunk.lines.filter(line => line.type !== 'header' && line.type !== excluded);
				const html = highlightToInnerHtml(lines.map(line => line.content).join('\n'), lang);
				if (!html) continue;
				const rendered = html.split('\n');
				lines.forEach((line, i) => { if (rendered[i]) result.set(lineKey(line), sanitizeHtml(rendered[i])); });
			}
		}
		return result;
	});

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

		if (continuous) return flat;

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
			case 'A': return 'bg-(--status-success-bg) text-success ring-(--status-success-border)';
			case 'M': return 'bg-(--status-warning-bg) text-warning ring-(--status-warning-border)';
			case 'D': return 'bg-(--status-error-bg) text-destructive ring-(--status-error-border)';
			case 'R': return 'bg-(--status-info-bg) text-info ring-(--status-info-border)';
			case '?': return 'bg-(--status-info-bg) text-info ring-(--status-info-border)';
			case 'U': return 'bg-(--status-warning-bg) text-warning ring-(--status-warning-border)';
			default: return 'bg-(--status-pending-bg) text-muted-foreground ring-(--status-pending-border)';
		}
	}

	$effect(() => {
		const [project, path, category, worktreeId, sessionId] = JSON.parse(requestKey);
		let disposed = false;
		loading = true;
		error = null;
		diffResponse = null;
		parsedDiff = null;
		sideBySidePairs = [];
		gitApi.getFileDiff(project, path, category === 'staged', worktreeId ?? undefined, sessionId ?? undefined)
			.then(resp => {
				if (disposed) return;
				diffResponse = resp;
				if (!resp.is_binary && resp.diff) {
					const parsed = parseDiff(resp.diff);
					parsedDiff = parsed;
					sideBySidePairs = toSideBySide(parsed.hunks);
				}
				loading = false;
			})
			.catch(e => {
				if (disposed) return;
				error = e?.detail || e?.message || 'Failed to load diff';
				loading = false;
			});
		return () => { disposed = true; };
	});
</script>

<svelte:window onresize={() => { viewportWidth = window.innerWidth; }} />

{#snippet sourceLine(line: DiffLine | null)}
	{#if line}{#if highlighted.has(lineKey(line))}<span class="syntax">{@html highlighted.get(lineKey(line))!}</span>{:else}{line.content}{/if}{/if}
{/snippet}

<div class="diff-viewer flex flex-col h-full bg-background text-foreground" class:continuous class:wrap>
	<!-- Sticky Header (hidden when embedded) -->
	{#if !embedded}
		<header class="sticky top-0 z-10 flex items-center gap-3 px-4 py-3 bg-(--background)/95 backdrop-blur-xs border-b border-border min-h-[52px]">
			<button
				onclick={onClose}
				class="flex items-center justify-center w-8 h-8 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent transition-colors shrink-0 focus:outline-hidden focus:ring-2 focus:ring-ring"
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
				<div class="text-sm text-destructive bg-(--status-error-bg) border border-(--status-error-border) rounded-lg px-4 py-3 max-w-md text-center">
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
								<div class="sbs-header-cell">{@render sourceLine(pair.left)}</div>
							</div>
						{:else if pair.type === 'context'}
							<div class="sbs-row">
								<div class="sbs-gutter">{pair.left?.oldLineNumber ?? ''}</div>
								<div class="sbs-cell sbs-cell-ctx">{@render sourceLine(pair.left)}</div>
								<div class="sbs-divider"></div>
								<div class="sbs-gutter">{pair.right?.newLineNumber ?? ''}</div>
								<div class="sbs-cell sbs-cell-ctx">{@render sourceLine(pair.right)}</div>
							</div>
						{:else}
							<div class="sbs-row">
								<div class="sbs-gutter sbs-gutter-del">{pair.left?.oldLineNumber ?? ''}</div>
								<div class="sbs-cell" class:sbs-cell-del={pair.left !== null} class:sbs-cell-blank={pair.left === null}>
									{#if pair.left}<span class="sbs-prefix del-prefix">-</span><span class="sbs-del-text">{@render sourceLine(pair.left)}</span>{/if}
								</div>
								<div class="sbs-divider"></div>
								<div class="sbs-gutter sbs-gutter-add">{pair.right?.newLineNumber ?? ''}</div>
								<div class="sbs-cell" class:sbs-cell-add={pair.right !== null} class:sbs-cell-blank={pair.right === null}>
									{#if pair.right}<span class="sbs-prefix add-prefix">+</span><span class="sbs-add-text">{@render sourceLine(pair.right)}</span>{/if}
								</div>
							</div>
						{/if}
					{/each}
				</div>
			</div>

			{#if diffResponse?.truncated}
				<div class="sticky bottom-0 flex items-center justify-center gap-2 px-4 py-2.5 bg-(--status-warning-bg) border-t border-(--status-warning-border) text-warning text-xs font-medium">
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
							<div class="flex-1 px-3 font-mono text-[11px] diff-header-text whitespace-pre select-none leading-5.5">
								{line.content}
							</div>
						</div>
					{:else if line.type === 'addition'}
						<div class="flex diff-line-add transition-colors duration-75">
							<div class="gutter-col diff-gutter-blank"></div>
							<div class="gutter-col diff-gutter-add">{line.newLineNumber}</div>
							<div class="flex-1 px-3 font-mono text-sm diff-text-add whitespace-pre">
								<span class="diff-prefix-add select-none mr-1">+</span>{@render sourceLine(line)}
							</div>
						</div>
					{:else if line.type === 'deletion'}
						<div class="flex diff-line-del transition-colors duration-75">
							<div class="gutter-col diff-gutter-del">{line.oldLineNumber}</div>
							<div class="gutter-col diff-gutter-blank"></div>
							<div class="flex-1 px-3 font-mono text-sm diff-text-del whitespace-pre">
								<span class="diff-prefix-del select-none mr-1">-</span>{@render sourceLine(line)}
							</div>
						</div>
					{:else}
						<div class="flex diff-line-ctx transition-colors duration-75">
							<div class="gutter-col diff-gutter-ctx">{line.oldLineNumber}</div>
							<div class="gutter-col diff-gutter-ctx">{line.newLineNumber}</div>
							<div class="flex-1 px-3 font-mono text-sm text-muted-foreground whitespace-pre">
								<span class="diff-gutter-ctx select-none mr-1">&nbsp;</span>{@render sourceLine(line)}
							</div>
						</div>
					{/if}
				{/each}
			</div>

			{#if diffResponse?.truncated}
				<div class="sticky bottom-0 flex items-center justify-center gap-2 px-4 py-2.5 bg-(--status-warning-bg) border-t border-(--status-warning-border) text-warning text-xs font-medium">
					Diff output was truncated — only showing partial changes
				</div>
			{/if}
		{/if}
	</div>
</div>

<style>

	.syntax :global(span) { color: var(--shiki-light, inherit); }
	:global(.dark) .syntax :global(span) { color: var(--shiki-dark, inherit); }
	/* Continuous review surface: compact neutral code, tinted change rows. */
	.continuous { height: auto; min-width: 0; }
	.continuous .diff-scroll-container { flex: none; }
	.continuous .gutter-col { width: 3rem; min-width: 3rem; font-size: 11px; line-height: 22px; }
	.continuous .diff-content :global(.font-mono) { font-size: 12px; line-height: 22px; }
	.continuous .diff-text-add, .continuous .diff-text-del,
	.continuous .sbs-del-text, .continuous .sbs-add-text { color: var(--foreground); }
	.continuous .diff-line-add { background: color-mix(in srgb, var(--success) 10%, transparent); }
	.continuous .diff-line-del { background: color-mix(in srgb, var(--destructive) 10%, transparent); }
	.continuous .diff-line-header, .continuous .sbs-header-cell { background: var(--muted); color: var(--muted-foreground); }
	.continuous .diff-header-text { color: var(--muted-foreground); }
	.wrap .diff-content, .wrap .sbs-diff { min-width: 0; width: 100%; }
	.wrap .diff-content :global(.whitespace-pre) { white-space: pre-wrap; overflow-wrap: anywhere; min-width: 0; }
	.wrap .sbs-row { grid-template-columns: 3rem minmax(0, 1fr) 1px 3rem minmax(0, 1fr); }
	.wrap .sbs-cell { white-space: pre-wrap; overflow-wrap: anywhere; min-width: 0; }
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
