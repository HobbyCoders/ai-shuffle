<script module lang="ts">
	let helpSeq = 0;
</script>

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
	 *
	 * With `onAskAgent`, the owner can select a line range (drag or shift-click
	 * line numbers, tap lines on touch, arrow keys when focused) and send one
	 * comment about it to the current chat. Nothing is written to disk or GitHub.
	 */
	import { ArrowLeft, FileText, Loader2, MessageSquareText, Send, X } from 'lucide-svelte';
	import { onMount, tick } from 'svelte';
	import { highlightToInnerHtml, onHighlighterReady, resolveShikiLang } from '$lib/utils/shiki';
	import { sanitizeHtml } from '$lib/utils/sanitize';
	import { git } from '$lib/stores/git.svelte';
	import * as gitApi from '$lib/api/git';
	import { parseDiff, toSideBySide, countChanges, effectiveDiffMode } from '$lib/utils/diff-parser';
	import type { DiffLine, ParsedDiff, SideBySidePair } from '$lib/utils/diff-parser';
	import { notify } from '$lib/stores/notifications';
	import {
		EXCERPT_LIMITS, composeDiffFeedbackPrompt, describeRanges, isSelectableLine, orderedRange,
		rangeChip, selectedRanges, type ReviewScope,
	} from '$lib/review/diff-feedback';

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
		/** 'base' diffs the session's checkout against its merge base. */
		scope?: ReviewScope;
		/** Enables line feedback; resolves true once the message was sent. */
		onAskAgent?: (prompt: string) => Promise<boolean>;
	}

	let { projectId, file, onClose, mode = 'unified', embedded = false, continuous = false, wrap = false, refreshKey = 0, scope = 'working', onAskAgent }: Props = $props();

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
	const requestKey = $derived(JSON.stringify([projectId, file.file, file.category, git.current.worktreeId, git.current.sessionId ?? git.current.workerTarget, refreshKey, scope]));

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
		const [project, path, category, worktreeId, sessionId, , requestScope] = JSON.parse(requestKey);
		let disposed = false;
		loading = true;
		error = null;
		diffResponse = null;
		parsedDiff = null;
		sideBySidePairs = [];
		selection = null;
		const request = requestScope === 'base'
			? typeof sessionId === 'string'
				? gitApi.getBaseFileDiff(project, path, sessionId)
				: Promise.reject(new Error('Changes vs base needs a chat with its own checkout'))
			: gitApi.getFileDiff(project, path, category === 'staged', worktreeId ?? undefined, sessionId ?? undefined);
		request
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

	// ── Line feedback ───────────────────────────────────────────────────
	type Selection = { anchor: number; focus: number; mode: 'unified' | 'side-by-side' };
	let selection = $state<Selection | null>(null);
	let comment = $state('');
	/** Full message being edited by hand; null while it follows the comment. */
	let draft = $state<string | null>(null);
	let sending = $state(false);
	let sendError = $state<string | null>(null);
	let linesEl = $state<HTMLElement | undefined>();
	let commentEl = $state<HTMLTextAreaElement | undefined>();
	/** Mouse drag in progress: the card waits so it cannot push rows under the pointer. */
	let dragging = $state(false);
	let lastPointer = $state('mouse');
	const helpId = `diff-feedback-help-${++helpSeq}`;

	const feedbackEnabled = $derived(!!onAskAgent && !!parsedDiff && !diffResponse?.is_binary && !diffResponse?.truncated);
	/** Diff lines each rendered row stands for, in render order. */
	const rowLines = $derived.by((): DiffLine[][] => {
		if (resolvedMode === 'side-by-side') {
			return sideBySidePairs.map(pair => {
				if (pair.type === 'header') return [];
				const out: DiffLine[] = [];
				for (const line of [pair.left, pair.right]) {
					if (line && isSelectableLine(line) && !out.includes(line)) out.push(line);
				}
				return out;
			});
		}
		return compactLines.map(line => (isSelectableLine(line) ? [line] : []));
	});
	const lineOrder = $derived.by(() => {
		const order = new Map<DiffLine, number>();
		parsedDiff?.hunks.forEach(hunk => hunk.lines.forEach(line => order.set(line, order.size)));
		return order;
	});
	const range = $derived(feedbackEnabled && selection && selection.mode === resolvedMode
		? orderedRange(selection.anchor, selection.focus) : null);
	const selectedLines = $derived.by((): DiffLine[] => {
		if (!range) return [];
		const lines = [...new Set(rowLines.slice(range.start, range.end + 1).flat())];
		return lines.sort((a, b) => (lineOrder.get(a) ?? 0) - (lineOrder.get(b) ?? 0));
	});
	const ranges = $derived(selectedRanges(selectedLines));
	const composed = $derived(range ? composeDiffFeedbackPrompt({
		file: file.file,
		originalFile: file.original_file,
		lines: selectedLines,
		scope: { scope, category: file.category, baseRef: diffResponse?.base_ref, mergeBase: diffResponse?.merge_base },
		comment,
	}) : '');
	const canSend = $derived(!sending && (draft !== null ? draft.trim().length > 0 : comment.trim().length > 0));
	const isSingle = $derived(!!selection && selection.anchor === selection.focus);

	// A layout switch re-indexes rows, so a selection made in the other layout is dropped.
	let selectionMode: string | null = null;
	$effect(() => {
		if (selectionMode !== null && resolvedMode !== selectionMode) { selection = null; draft = null; }
		selectionMode = resolvedMode;
	});

	function startSelection(row: number) {
		selection = { anchor: row, focus: row, mode: resolvedMode };
		draft = null;
		sendError = null;
	}
	function clearSelection() {
		selection = null;
		draft = null;
		sendError = null;
	}
	function rowFromEvent(e: Event): number | null {
		const el = (e.target as Element | null)?.closest?.('[data-row]');
		if (!el || !linesEl?.contains(el)) return null;
		const row = Number(el.getAttribute('data-row'));
		return rowLines[row]?.length ? row : null;
	}
	function onLinesPointerDown(e: PointerEvent) {
		lastPointer = e.pointerType || 'mouse';
		if (!feedbackEnabled || lastPointer === 'touch' || (e.button ?? 0) !== 0) return;
		if (!(e.target as Element | null)?.closest?.('.gutter-col, .sbs-gutter')) return;
		const row = rowFromEvent(e);
		if (row === null) return;
		e.preventDefault();
		linesEl?.focus({ preventScroll: true });
		if (e.shiftKey && selection && selection.mode === resolvedMode) selection = { ...selection, focus: row };
		else startSelection(row);
		dragging = true;
	}
	function onLinesPointerMove(e: PointerEvent) {
		if (!dragging || !selection) return;
		const row = rowFromEvent(e);
		if (row !== null && row !== selection.focus) selection = { ...selection, focus: row };
	}
	async function endDrag() {
		if (!dragging) return;
		dragging = false;
		await tick();
		commentEl?.focus({ preventScroll: true });
	}
	/** Touch: the first tap picks a line, a tap on another line makes it a range. */
	function onLinesClick(e: MouseEvent) {
		if (lastPointer !== 'touch' || !feedbackEnabled) return;
		const row = rowFromEvent(e);
		if (row === null) return;
		if (selection && selection.mode === resolvedMode && isSingle) {
			if (row === selection.anchor) clearSelection();
			else selection = { ...selection, focus: row };
		} else startSelection(row);
	}
	function nextSelectableRow(from: number, dir: 1 | -1): number | null {
		for (let i = from + dir; i >= 0 && i < rowLines.length; i += dir) if (rowLines[i].length) return i;
		return null;
	}
	function onLinesKeydown(e: KeyboardEvent) {
		if (!feedbackEnabled || e.target !== linesEl) return;
		if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
			const dir = e.key === 'ArrowDown' ? 1 : -1;
			const live = selection && selection.mode === resolvedMode ? selection : null;
			const next = nextSelectableRow(live ? live.focus : dir > 0 ? -1 : rowLines.length, dir);
			if (next === null) return;
			e.preventDefault();
			lastPointer = 'keyboard';
			if (e.shiftKey && live) selection = { ...live, focus: next };
			else startSelection(next);
			linesEl?.querySelector(`[data-row="${next}"]`)?.scrollIntoView?.({ block: 'nearest' });
		} else if (e.key === 'Enter' && range) {
			e.preventDefault();
			commentEl?.focus();
		} else if (e.key === 'Escape' && selection) {
			e.preventDefault();
			clearSelection();
		}
	}
	function onAskKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			e.preventDefault();
			clearSelection();
			linesEl?.focus({ preventScroll: true });
			return;
		}
		const submit = draft === null
			? e.key === 'Enter' && !e.shiftKey && !e.isComposing
			: e.key === 'Enter' && (e.metaKey || e.ctrlKey);
		if (submit) {
			e.preventDefault();
			void sendFeedback();
		}
	}
	function toggleDraft() {
		draft = draft === null ? composed : null;
		void tick().then(() => commentEl?.focus());
	}
	async function sendFeedback() {
		if (!canSend || !onAskAgent) return;
		const text = draft ?? composed;
		sending = true;
		sendError = null;
		try {
			if (await onAskAgent(text)) {
				comment = '';
				clearSelection();
				notify('Sent to the chat');
			} else sendError = 'Could not send to the chat. Try again.';
		} catch {
			sendError = 'Could not send to the chat. Try again.';
		} finally {
			sending = false;
		}
	}
	const hint = $derived(
		lastPointer === 'touch' ? (isSingle ? 'Tap another line to select a range' : 'Tap a line to start over')
			: draft !== null ? 'Ctrl+Enter to send'
				: lastPointer === 'keyboard' ? 'Shift+arrows extend · Enter to write'
					: 'Shift-click or drag line numbers to extend'
	);
</script>

<svelte:window onresize={() => { viewportWidth = window.innerWidth; }} onpointerup={endDrag} onpointercancel={() => { dragging = false; }} />

{#snippet askCard()}
	<div class="ask-card" role="group" aria-label="Ask the agent about the selected lines">
		<div class="ask-head">
			<MessageSquareText size={13} aria-hidden="true" />
			<span class="ask-title">Ask agent</span>
			<span class="ask-chip">{rangeChip(ranges)}</span>
			<span class="ask-meta" title={describeRanges(ranges, '–')}>
				{selectedLines.length} line{selectedLines.length === 1 ? '' : 's'}{selectedLines.length > EXCERPT_LIMITS.maxLines ? ` · first ${EXCERPT_LIMITS.maxLines} quoted` : ''}
			</span>
			<button class="ask-icon" type="button" aria-label="Clear line selection" title="Clear selection (Esc)" onclick={clearSelection}><X size={13} /></button>
		</div>
		{#if draft === null}
			<textarea bind:this={commentEl} bind:value={comment} rows="2" class="ask-input"
				placeholder="What should the agent change or explain here?" aria-label="Comment for the agent"
				onkeydown={onAskKeydown}></textarea>
		{:else}
			<textarea bind:this={commentEl} bind:value={draft} rows="9" class="ask-input ask-draft"
				aria-label="Message to send" spellcheck="false" onkeydown={onAskKeydown}></textarea>
		{/if}
		<div class="ask-foot">
			{#if sendError}
				<span class="ask-error" role="alert">{sendError}</span>
			{:else}
				<span class="ask-hint">{hint}</span>
			{/if}
			<button class="ask-ghost" type="button" aria-pressed={draft !== null} onclick={toggleDraft}>
				{draft === null ? 'Edit message' : 'Back to comment'}
			</button>
			<button class="ask-send" type="button" disabled={!canSend} onclick={sendFeedback}>
				{#if sending}<Loader2 size={13} class="animate-spin" />{:else}<Send size={13} />{/if}
				Send
			</button>
		</div>
	</div>
{/snippet}

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
				<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
				<div class="sbs-table" class:feedback={feedbackEnabled} bind:this={linesEl}
					tabindex={feedbackEnabled ? 0 : undefined} role={feedbackEnabled ? 'group' : undefined}
					aria-label={feedbackEnabled ? `Diff lines of ${file.file}` : undefined}
					aria-describedby={feedbackEnabled ? helpId : undefined}
					onpointerdown={onLinesPointerDown} onpointermove={onLinesPointerMove}
					onclick={onLinesClick} onkeydown={onLinesKeydown}>
					{#each sideBySidePairs as pair, i}
						{@const picked = !!range && i >= range.start && i <= range.end && rowLines[i]?.length > 0}
						{#if pair.type === 'header'}
							<div class="sbs-row sbs-header">
								<div class="sbs-header-cell">{@render sourceLine(pair.left)}</div>
							</div>
						{:else if pair.type === 'context'}
							<div class="sbs-row" data-row={i} class:row-selected={picked}>
								<div class="sbs-gutter">{pair.left?.oldLineNumber ?? ''}</div>
								<div class="sbs-cell sbs-cell-ctx">{@render sourceLine(pair.left)}</div>
								<div class="sbs-divider"></div>
								<div class="sbs-gutter">{pair.right?.newLineNumber ?? ''}</div>
								<div class="sbs-cell sbs-cell-ctx">{@render sourceLine(pair.right)}</div>
							</div>
						{:else}
							<div class="sbs-row" data-row={i} class:row-selected={picked}>
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
						{#if range && i === range.end && !dragging}{@render askCard()}{/if}
					{/each}
				</div>
			</div>

			{#if diffResponse?.truncated}{@render truncatedNote()}{/if}
		{:else}
			<!-- Unified Diff -->
			<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
			<div class="diff-content" class:feedback={feedbackEnabled} bind:this={linesEl}
				tabindex={feedbackEnabled ? 0 : undefined} role={feedbackEnabled ? 'group' : undefined}
				aria-label={feedbackEnabled ? `Diff lines of ${file.file}` : undefined}
				aria-describedby={feedbackEnabled ? helpId : undefined}
				onpointerdown={onLinesPointerDown} onpointermove={onLinesPointerMove}
				onclick={onLinesClick} onkeydown={onLinesKeydown}>
				{#each compactLines as line, i}
					{@const picked = !!range && i >= range.start && i <= range.end && rowLines[i]?.length > 0}
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
						<div class="flex diff-line-add transition-colors duration-75" data-row={i} class:row-selected={picked}>
							<div class="gutter-col diff-gutter-blank"></div>
							<div class="gutter-col diff-gutter-add">{line.newLineNumber}</div>
							<div class="flex-1 px-3 font-mono text-sm diff-text-add whitespace-pre">
								<span class="diff-prefix-add select-none mr-1">+</span>{@render sourceLine(line)}
							</div>
						</div>
					{:else if line.type === 'deletion'}
						<div class="flex diff-line-del transition-colors duration-75" data-row={i} class:row-selected={picked}>
							<div class="gutter-col diff-gutter-del">{line.oldLineNumber}</div>
							<div class="gutter-col diff-gutter-blank"></div>
							<div class="flex-1 px-3 font-mono text-sm diff-text-del whitespace-pre">
								<span class="diff-prefix-del select-none mr-1">-</span>{@render sourceLine(line)}
							</div>
						</div>
					{:else}
						<div class="flex diff-line-ctx transition-colors duration-75" data-row={i} class:row-selected={picked}>
							<div class="gutter-col diff-gutter-ctx">{line.oldLineNumber}</div>
							<div class="gutter-col diff-gutter-ctx">{line.newLineNumber}</div>
							<div class="flex-1 px-3 font-mono text-sm text-muted-foreground whitespace-pre">
								<span class="diff-gutter-ctx select-none mr-1">&nbsp;</span>{@render sourceLine(line)}
							</div>
						</div>
					{/if}
					{#if range && i === range.end && !dragging}{@render askCard()}{/if}
				{/each}
			</div>

			{#if diffResponse?.truncated}{@render truncatedNote()}{/if}
		{/if}
	</div>
	{#if feedbackEnabled}
		<span id={helpId} class="sr-only">Drag or shift-click line numbers to select lines, or use the arrow keys with Shift. Press Enter to write a comment for the agent.</span>
		<span class="sr-only" role="status" aria-live="polite">{range ? `Selected ${describeRanges(ranges, ' to ')}` : ''}</span>
	{/if}
</div>

{#snippet truncatedNote()}
	<div class="sticky bottom-0 flex items-center justify-center gap-2 px-4 py-2.5 bg-(--status-warning-bg) border-t border-(--status-warning-border) text-warning text-xs font-medium">
		<span>Diff output was truncated — only showing partial changes</span>
		{#if onAskAgent}<span class="opacity-80">· line comments are off</span>{/if}
	</div>
{/snippet}

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

	/* ─── Line feedback: selection rail + inline "Ask agent" card ───── */
	.feedback { --pick: color-mix(in oklch, var(--primary) 17%, transparent); outline: none; }
	.feedback:focus-visible { box-shadow: inset 0 0 0 2px color-mix(in oklch, var(--ring) 70%, transparent); }
	.feedback .gutter-col, .feedback .sbs-gutter { cursor: pointer; transition: color .12s ease-out, background-color .12s ease-out; }
	.feedback .gutter-col:hover, .feedback .sbs-gutter:hover {
		color: var(--primary);
		background-color: color-mix(in oklch, var(--primary) 10%, transparent);
	}
	/* Tint layers over the add/delete colour so the change type stays readable. */
	.diff-viewer .row-selected > div { background-image: linear-gradient(var(--pick), var(--pick)); }
	.diff-viewer .row-selected > div:first-child { box-shadow: inset 3px 0 0 var(--primary); color: var(--primary); }
	.diff-viewer .row-selected .gutter-col, .diff-viewer .row-selected .sbs-gutter { color: var(--primary); }

	.ask-card {
		position: sticky;
		left: 10px;
		z-index: 1;
		box-sizing: border-box;
		/* Sized to the visible panel (the review panel is a size container), not the scrolled diff. */
		width: min(640px, calc(100cqi - 28px));
		min-width: 220px;
		margin: 6px 0 10px 10px;
		padding: 8px 8px 7px;
		border: 1px solid color-mix(in srgb, var(--primary) 45%, var(--border));
		border-radius: 8px;
		background: var(--popover);
		color: var(--popover-foreground, var(--foreground));
		box-shadow: var(--shadow-m);
		font-family: var(--font-sans, inherit);
		white-space: normal;
		animation: ask-in .16s cubic-bezier(.2, .8, .2, 1);
	}
	/* Notch pointing up at the last selected line. */
	.ask-card::before {
		content: '';
		position: absolute;
		top: -5px;
		left: 26px;
		width: 8px;
		height: 8px;
		background: var(--popover);
		border-left: 1px solid color-mix(in srgb, var(--primary) 45%, var(--border));
		border-top: 1px solid color-mix(in srgb, var(--primary) 45%, var(--border));
		transform: rotate(45deg);
	}
	.ask-head { display: flex; align-items: center; gap: 6px; min-width: 0; margin-bottom: 6px; font-size: 12px; }
	.ask-head :global(svg) { flex-shrink: 0; color: var(--primary); }
	.ask-title { font-weight: 600; letter-spacing: .01em; }
	.ask-chip {
		padding: 1px 6px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--primary) 16%, transparent);
		color: var(--primary);
		font-family: var(--font-mono);
		font-size: 11px;
		font-variant-numeric: tabular-nums;
	}
	.ask-meta { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted-foreground); font-size: 11px; }
	.ask-icon {
		display: inline-flex; align-items: center; justify-content: center;
		width: 24px; height: 24px; margin-left: auto; flex-shrink: 0;
		border: 0; border-radius: 5px; background: transparent; color: var(--muted-foreground); cursor: pointer;
	}
	.ask-icon:hover { background: var(--muted); color: var(--foreground); }
	.ask-input {
		display: block;
		box-sizing: border-box;
		width: 100%;
		min-height: 52px;
		max-height: 280px;
		padding: 7px 9px;
		resize: vertical;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--background);
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
		line-height: 1.45;
	}
	.ask-input::placeholder { color: var(--muted-foreground); }
	.ask-input:focus { outline: none; border-color: color-mix(in srgb, var(--ring) 70%, var(--border)); box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 18%, transparent); }
	.ask-draft { font-family: var(--font-mono); font-size: 12px; line-height: 1.5; white-space: pre; overflow: auto; }
	.ask-foot { display: flex; align-items: center; gap: 6px; margin-top: 7px; min-width: 0; }
	.ask-hint, .ask-error { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
	.ask-hint { color: var(--muted-foreground); }
	.ask-error { color: var(--destructive); white-space: normal; }
	.ask-ghost, .ask-send {
		display: inline-flex; align-items: center; gap: 5px; flex-shrink: 0;
		height: 28px; padding: 0 10px;
		border-radius: 6px; font-size: 12px; font-weight: 500; cursor: pointer;
		transition: background-color .12s ease-out, opacity .12s ease-out, transform .08s ease-out;
	}
	.ask-ghost { border: 1px solid var(--border); background: transparent; color: var(--muted-foreground); }
	.ask-ghost:hover { background: var(--muted); color: var(--foreground); }
	.ask-ghost[aria-pressed=true] { color: var(--foreground); border-color: color-mix(in srgb, var(--primary) 45%, var(--border)); }
	.ask-send { border: 0; background: var(--primary); color: var(--primary-foreground); }
	.ask-send:hover:not(:disabled) { background: color-mix(in oklch, var(--primary) 88%, white); }
	.ask-send:active:not(:disabled), .ask-ghost:active { transform: translateY(1px); }
	.ask-send:disabled { opacity: .45; cursor: default; }
	.ask-icon:focus-visible, .ask-ghost:focus-visible, .ask-send:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	@keyframes ask-in { from { opacity: 0; transform: translateY(-3px); } to { opacity: 1; transform: none; } }
	@media (prefers-reduced-motion: reduce) { .ask-card { animation: none; } }
	@media (pointer: coarse) {
		.ask-ghost, .ask-send { height: 36px; padding: 0 12px; }
		.ask-icon { width: 36px; height: 36px; }
		.ask-input { font-size: 16px; } /* no iOS zoom-on-focus */
	}
</style>
