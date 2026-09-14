<script lang="ts">
	/**
	 * ChangedFilesPanel — Slide-out panel showing all changed files in a git repo,
	 * grouped by category (conflicts, staged, modified, untracked), with actions
	 * to stage/unstage/discard files and view diffs.
	 *
	 * Review mode uses a continuous diff list with an optional file tree and
	 * per-file staging/discard actions.
	 */
	import {
		X, ChevronDown, ChevronRight,
		Plus, Minus, Eye, Undo2, Trash2,
		AlertTriangle, Check, Circle,
		FileText, Loader2
	} from 'lucide-svelte';
	import { git, changedFiles, changedFilesLoading } from '$lib/stores/git.svelte';
	import type { FileChangeInfo } from '$lib/api/git';
	import { platform } from '$lib/stores/platform.svelte';
	import ReviewDiffPanel from './ReviewDiffPanel.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import { notify } from '$lib/stores/notifications';

	interface Props {
		projectId: string;
		open: boolean;
		onClose: () => void;
		onViewDiff: (file: FileChangeInfo) => void;
		/** When true, shows the full review layout with inline side-by-side diff */
		reviewMode?: boolean;
		embedded?: boolean;
	}

	let { projectId, open, onClose, onViewDiff, reviewMode = false, embedded = false }: Props = $props();

	// ── Grouped file lists ──────────────────────────────────────────────
	const conflicts = $derived(($changedFiles ?? []).filter(f => f.category === 'conflict'));
	const staged    = $derived(($changedFiles ?? []).filter(f => f.category === 'staged'));
	const modified  = $derived(($changedFiles ?? []).filter(f => f.category === 'modified'));
	const untracked = $derived(($changedFiles ?? []).filter(f => f.category === 'untracked'));

	const totalCount = $derived(
		conflicts.length + staged.length + modified.length + untracked.length
	);

	const isEmpty = $derived(totalCount === 0 && !$changedFilesLoading);

	// ── Collapsible section state ───────────────────────────────────────
	let collapsedSections = $state<Record<string, boolean>>({});

	function toggleSection(key: string) {
		collapsedSections = { ...collapsedSections, [key]: !collapsedSections[key] };
	}

	function isSectionCollapsed(key: string): boolean {
		return !!collapsedSections[key];
	}

	// ── In-flight operations (disable buttons while running) ────────────
	let inFlightFiles = $state(new Set<string>());

	function markInFlight(file: string) {
		const next = new Set(inFlightFiles);
		next.add(file);
		inFlightFiles = next;
	}

	function clearInFlight(file: string) {
		const next = new Set(inFlightFiles);
		next.delete(file);
		inFlightFiles = next;
	}

	// ── Discard / delete confirmation (shared modal) ────────────────────
	// All destructive file ops (per-file discard/delete, group discard-all/
	// delete-all, review-mode decline) route through one ConfirmDialog driven
	// by this object. Lightweight inline confirms were replaced per the
	// "modals for file ops" decision.
	let pendingConfirm = $state<{
		title: string;
		message: string;
		confirmLabel: string;
		action: () => void;
	} | null>(null);

	function confirmDiscardFile(file: FileChangeInfo, group: string) {
		const name = basename(file.file);
		pendingConfirm = group === 'untracked'
			? {
				title: 'Delete file',
				message: `Permanently delete ${name}? This cannot be undone.`,
				confirmLabel: 'Delete',
				action: () => discardFile(file),
			}
			: {
				title: 'Discard changes',
				message: `Discard all changes to ${name}? This cannot be undone.`,
				confirmLabel: 'Discard',
				action: () => discardFile(file),
			};
	}

	function confirmDiscardGroup(files: FileChangeInfo[], type: 'modified' | 'untracked') {
		pendingConfirm = type === 'untracked'
			? {
				title: 'Delete all untracked files',
				message: `Permanently delete all ${files.length} untracked files? This cannot be undone.`,
				confirmLabel: 'Delete All',
				action: () => discardAll(files),
			}
			: {
				title: 'Discard all changes',
				message: `Discard all changes to ${files.length} files? This cannot be undone.`,
				confirmLabel: 'Discard All',
				action: () => discardAll(files),
			};
	}

	function confirmReviewDecline() {
		if (!reviewSelectedFile) return;
		const name = basename(reviewSelectedFile.file);
		pendingConfirm = {
			title: 'Discard changes',
			message: `Discard all changes to ${name}? This cannot be undone.`,
			confirmLabel: 'Discard',
			action: () => reviewDecline(),
		};
	}

	function runPendingConfirm() {
		const action = pendingConfirm?.action;
		pendingConfirm = null;
		action?.();
	}

	// ── Review mode: selected file + footer confirmation ────────────────
	let reviewSelectedFile = $state<FileChangeInfo | null>(null);
	let reviewActionInFlight = $state(false);

	// Keep review ordering consistent with the classic status groups.
	const flatFiles = $derived([...conflicts, ...staged, ...modified, ...untracked]);


	async function reviewAccept() {
		if (!reviewSelectedFile || reviewActionInFlight) return;
		reviewActionInFlight = true;
		try {
			if (reviewSelectedFile.category === 'staged') {
				await git.unstageFiles([reviewSelectedFile.file]);
			} else {
				await git.stageFiles([reviewSelectedFile.file]);
			}
			await git.refreshChangedFiles();
			advanceReviewFile();
		} catch {
			notify('Failed to update file', { severity: 'error' });
		} finally {
			reviewActionInFlight = false;
		}
	}

	async function reviewDecline() {
		if (!reviewSelectedFile || reviewActionInFlight) return;
		reviewActionInFlight = true;
		try {
			await git.discardChanges(
				[reviewSelectedFile.file],
				{ [reviewSelectedFile.file]: reviewSelectedFile.category }
			);
			await git.refreshChangedFiles();
			advanceReviewFile();
		} catch {
			notify('Failed to discard changes', { severity: 'error' });
		} finally {
			reviewActionInFlight = false;
		}
	}

	function advanceReviewFile() {
		const files = $changedFiles ?? [];
		if (files.length === 0) {
			reviewSelectedFile = null;
			return;
		}
		const curIdx = reviewSelectedFile
			? files.findIndex(f => f.file === reviewSelectedFile!.file)
			: -1;
		const nextIdx = Math.min(curIdx >= 0 ? curIdx : 0, files.length - 1);
		reviewSelectedFile = files[nextIdx];
	}

	// ── Actions ─────────────────────────────────────────────────────────

	async function stageFile(file: FileChangeInfo) {
		markInFlight(file.file);
		try {
			await git.stageFiles([file.file]);
		} catch {
			notify('Failed to stage file', { severity: 'error' });
		} finally {
			clearInFlight(file.file);
		}
	}

	async function unstageFile(file: FileChangeInfo) {
		markInFlight(file.file);
		try {
			await git.unstageFiles([file.file]);
		} catch {
			notify('Failed to unstage file', { severity: 'error' });
		} finally {
			clearInFlight(file.file);
		}
	}

	async function discardFile(file: FileChangeInfo) {
		markInFlight(file.file);
		try {
			await git.discardChanges([file.file], { [file.file]: file.category });
		} catch {
			notify('Failed to discard changes', { severity: 'error' });
		} finally {
			clearInFlight(file.file);
		}
	}

	async function stageAll(files: FileChangeInfo[]) {
		const paths = files.map(f => f.file);
		for (const p of paths) markInFlight(p);
		try {
			await git.stageFiles(paths);
		} catch {
			notify('Failed to stage files', { severity: 'error' });
		} finally {
			for (const p of paths) clearInFlight(p);
		}
	}

	async function unstageAll(files: FileChangeInfo[]) {
		const paths = files.map(f => f.file);
		for (const p of paths) markInFlight(p);
		try {
			await git.unstageFiles(paths);
		} catch {
			notify('Failed to unstage files', { severity: 'error' });
		} finally {
			for (const p of paths) clearInFlight(p);
		}
	}

	async function discardAll(files: FileChangeInfo[]) {
		const paths = files.map(f => f.file);
		for (const p of paths) markInFlight(p);
		try {
			await git.discardChanges(
				paths,
				Object.fromEntries(files.map(f => [f.file, f.category]))
			);
		} catch {
			notify('Failed to discard changes', { severity: 'error' });
		} finally {
			for (const p of paths) clearInFlight(p);
		}
	}

	// ── Helpers ─────────────────────────────────────────────────────────

	function basename(filepath: string): string {
		const parts = filepath.replace(/\\/g, '/').split('/');
		return parts[parts.length - 1];
	}

	function dirname(filepath: string): string {
		const normalized = filepath.replace(/\\/g, '/');
		const idx = normalized.lastIndexOf('/');
		return idx > 0 ? normalized.slice(0, idx) + '/' : '';
	}

	function statusColor(status: string): string {
		switch (status) {
			case 'A': return 'text-success';
			case 'M': return 'text-warning';
			case 'D': return 'text-destructive';
			case 'R': return 'text-info';
			case 'U': return 'text-destructive';
			default:  return 'text-muted-foreground';
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (!open || e.defaultPrevented) return;
		const target = e.target instanceof HTMLElement ? e.target : null;
		// This is non-modal when docked: never consume chat typing or arrow keys.
		if (embedded && e.key !== 'Escape' && (!target || !panelElement?.contains(target)
			|| target.closest('input, textarea, select, [contenteditable="true"]'))) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			// Modal layering: Escape dismisses an open confirm dialog first,
			// leaving the panel open. A second Escape then closes the panel.
			if (pendingConfirm) {
				pendingConfirm = null;
				return;
			}
			onClose();
			return;
		}

	}

	// ── Shell ───────────────────────────────────────────────────────────
	// Fullscreen on the mobile shell (the Android WebView). The desktop shell
	// keeps the resizable side drawer at every window width; CSS clamps it to
	// the viewport so a narrow browser window never overflows.
	const isMobile = $derived(platform.isMobile);

	// ── Resize logic (desktop shell only) ───────────────────────────────
	let panelWidth = $state(
		Math.min((typeof window !== 'undefined' ? window.innerWidth : 0) * 0.6 || 800, 1100)
	);
	let isResizing = $state(false);
	let panelElement: HTMLElement | undefined = $state(undefined);

	function startResize(e: PointerEvent) {
		if (platform.isMobile) return; // no resize on mobile (panel is fullscreen)
		e.preventDefault();
		isResizing = true;
		const startX = e.clientX;
		const startWidth = panelWidth;

		function onMove(ev: PointerEvent) {
			const delta = startX - ev.clientX;
			const newWidth = Math.max(400, Math.min(window.innerWidth - 60, startWidth + delta));
			panelWidth = newWidth;
		}

		function onUp() {
			isResizing = false;
			window.removeEventListener('pointermove', onMove);
			window.removeEventListener('pointerup', onUp);
		}

		window.addEventListener('pointermove', onMove);
		window.addEventListener('pointerup', onUp);
	}

	// Reset state when panel closes
	$effect(() => {
		if (!open) {
			pendingConfirm = null;
			reviewSelectedFile = null;
			reviewActionInFlight = false;
		}
	});
</script>

<svelte:window onkeydown={handleKeydown} />

{#snippet statusIcon(status: string)}
	<span class={`shrink-0 ${statusColor(status)}`}>
		{#if status === 'A'}
			<Plus size={14} strokeWidth={2} />
		{:else if status === 'M'}
			<Circle size={14} strokeWidth={2} />
		{:else if status === 'D'}
			<Minus size={14} strokeWidth={2} />
		{:else if status === 'R'}
			<FileText size={14} strokeWidth={2} />
		{:else if status === 'U'}
			<AlertTriangle size={14} strokeWidth={2} />
		{:else}
			<Circle size={12} strokeWidth={1.5} />
		{/if}
	</span>
{/snippet}

{#snippet fileRow(file: FileChangeInfo, group: 'staged' | 'modified' | 'untracked' | 'conflict')}
	{@const isInFlight = inFlightFiles.has(file.file)}

	<div class="file-row group/row" class:opacity-50={isInFlight} class:pointer-events-none={isInFlight}>
		<!-- Status icon -->
		{@render statusIcon(file.status)}

		<!-- File name -->
		<div class="file-name">
			<span class="file-basename">{basename(file.file)}</span>
			{#if dirname(file.file)}
				<span class="file-dirname">{dirname(file.file)}</span>
			{/if}
		</div>

		<!-- Diff stats -->
		{#if group !== 'untracked' && !file.is_binary}
			<span class="diff-stats">
				{#if file.additions > 0}
					<span class="text-success">+{file.additions}</span>
				{/if}
				{#if file.deletions > 0}
					<span class="text-destructive">-{file.deletions}</span>
				{/if}
			</span>
		{/if}

		<!-- Action buttons -->
		<div class="file-actions">
			{#if group === 'staged'}
				<button
					class="action-btn"
					title="Unstage"
					onclick={() => unstageFile(file)}
					disabled={isInFlight}
				>
					<Minus size={14} strokeWidth={1.5} />
				</button>
				<button
					class="action-btn"
					title="View diff"
					onclick={() => onViewDiff(file)}
				>
					<Eye size={14} strokeWidth={1.5} />
				</button>
			{:else if group === 'modified'}
				<button
					class="action-btn"
					title="Stage"
					onclick={() => stageFile(file)}
					disabled={isInFlight}
				>
					<Plus size={14} strokeWidth={1.5} />
				</button>
				<button
					class="action-btn action-btn-danger"
					title="Discard changes"
					onclick={() => confirmDiscardFile(file, group)}
					disabled={isInFlight}
				>
					<Undo2 size={14} strokeWidth={1.5} />
				</button>
				<button
					class="action-btn"
					title="View diff"
					onclick={() => onViewDiff(file)}
				>
					<Eye size={14} strokeWidth={1.5} />
				</button>
			{:else if group === 'untracked'}
				<button
					class="action-btn"
					title="Stage"
					onclick={() => stageFile(file)}
					disabled={isInFlight}
				>
					<Plus size={14} strokeWidth={1.5} />
				</button>
				<button
					class="action-btn action-btn-danger"
					title="Delete file"
					onclick={() => confirmDiscardFile(file, group)}
					disabled={isInFlight}
				>
					<Trash2 size={14} strokeWidth={1.5} />
				</button>
			{:else if group === 'conflict'}
				<button
					class="action-btn"
					title="View diff"
					onclick={() => onViewDiff(file)}
				>
					<Eye size={14} strokeWidth={1.5} />
				</button>
			{/if}
		</div>
	</div>
{/snippet}

{#snippet sectionIcon(Icon: typeof AlertTriangle, accentClass: string)}
	<span class={`section-icon ${accentClass}`}>
		<Icon size={14} strokeWidth={2} />
	</span>
{/snippet}

{#snippet sectionHeader(
	key: string,
	label: string,
	count: number,
	accentClass: string,
	icon: typeof AlertTriangle,
	groupActions?: { files: FileChangeInfo[]; type: 'staged' | 'modified' | 'untracked' }
)}
	<div class="section-header" role="group">
		<button
			class="section-toggle"
			onclick={() => toggleSection(key)}
			aria-expanded={!isSectionCollapsed(key)}
		>
			<span class="section-chevron">
				{#if isSectionCollapsed(key)}
					<ChevronRight size={14} strokeWidth={1.5} />
				{:else}
					<ChevronDown size={14} strokeWidth={1.5} />
				{/if}
			</span>
			{@render sectionIcon(icon, accentClass)}
			<span class="section-label">{label}</span>
			<span class={`section-count ${accentClass}`}>{count}</span>
		</button>

		{#if groupActions && !reviewMode}
			<div class="section-actions">
				{#if groupActions.type === 'staged'}
					<button
						class="group-action-btn"
						onclick={() => unstageAll(groupActions.files)}
					>
						Unstage All
					</button>
				{:else if groupActions.type === 'modified'}
					<button
						class="group-action-btn"
						onclick={() => stageAll(groupActions.files)}
					>
						Stage All
					</button>
					<button
						class="group-action-btn group-action-danger"
						onclick={() => confirmDiscardGroup(groupActions.files, 'modified')}
					>
						Discard All
					</button>
				{:else if groupActions.type === 'untracked'}
					<button
						class="group-action-btn"
						onclick={() => stageAll(groupActions.files)}
					>
						Stage All
					</button>
					<button
						class="group-action-btn group-action-danger"
						onclick={() => confirmDiscardGroup(groupActions.files, 'untracked')}
					>
						Delete All
					</button>
				{/if}
			</div>
		{/if}
	</div>
{/snippet}

<!-- ═══════════════════════════════════════════════════════════════════
     REVIEW MODE: file sidebar + inline diff + accept/decline footer
     ═══════════════════════════════════════════════════════════════════ -->


{#snippet fileListContent()}
	{#if $changedFilesLoading && totalCount === 0}
		<div class="loading-skeleton">
			{#each Array(5) as _}
				<div class="skeleton-row">
					<div class="skeleton-icon"></div>
					<div class="skeleton-text"></div>
				</div>
			{/each}
		</div>
	{:else if isEmpty}
		<div class="empty-state">
			<div class="empty-icon">
				<Check size={28} strokeWidth={1.5} />
			</div>
			<span class="empty-text">Working tree clean</span>
		</div>
	{:else}
		{#if conflicts.length > 0}
			{@render sectionHeader('conflicts', 'Merge Conflicts', conflicts.length, 'text-destructive', AlertTriangle)}
			{#if !isSectionCollapsed('conflicts')}
				<div class="section-files">
					{#each conflicts as file (file.file)}
						{@render fileRow(file, 'conflict')}
					{/each}
				</div>
			{/if}
		{/if}

		{#if staged.length > 0}
			{@render sectionHeader('staged', 'Staged Changes', staged.length, 'text-success', Check, { files: staged, type: 'staged' })}
			{#if !isSectionCollapsed('staged')}
				<div class="section-files">
					{#each staged as file (file.file)}
						{@render fileRow(file, 'staged')}
					{/each}
				</div>
			{/if}
		{/if}

		{#if modified.length > 0}
			{@render sectionHeader('modified', 'Changes', modified.length, 'text-warning', Circle, { files: modified, type: 'modified' })}
			{#if !isSectionCollapsed('modified')}
				<div class="section-files">
					{#each modified as file (file.file)}
						{@render fileRow(file, 'modified')}
					{/each}
				</div>
			{/if}
		{/if}

		{#if untracked.length > 0}
			{@render sectionHeader('untracked', 'Untracked Files', untracked.length, 'text-muted-foreground', Plus, { files: untracked, type: 'untracked' })}
			{#if !isSectionCollapsed('untracked')}
				<div class="section-files">
					{#each untracked as file (file.file)}
						{@render fileRow(file, 'untracked')}
					{/each}
				</div>
			{/if}
		{/if}
	{/if}
{/snippet}

{#if open}
	<!-- Backdrop (no click-to-close — panel is persistent) -->
	{#if !embedded}<div class="panel-backdrop" class:review-backdrop={reviewMode} transition:fade={{ duration: 150 }}></div>{/if}

	<!-- Panel -->
	<div
		bind:this={panelElement}
		class="panel"
		class:embedded
		class:panel-review={reviewMode}
		class:panel-mobile={isMobile}
		class:panel-resizing={isResizing}
		role="dialog"
		aria-modal={embedded ? undefined : true}
		aria-label={reviewMode ? 'Review changes' : 'Changed files'}
		style={!embedded && reviewMode && !isMobile ? `width: ${panelWidth}px` : ''}
		transition:slideIn={{ duration: 200 }}
	>
		<!-- Resize handle (desktop review mode only) -->
		{#if !embedded && reviewMode && !isMobile}
			<div
				class="resize-handle"
				onpointerdown={startResize}
				role="separator"
				aria-orientation="vertical"
				aria-label="Resize panel"
			>
				<div class="resize-handle-bar"></div>
			</div>
		{/if}
		{#if !reviewMode}
		<!-- Header -->
		<div class="panel-header">
			<div class="panel-header-left">
				<h2 class="panel-title">Changed Files</h2>
				{#if totalCount > 0}
					<span class="total-badge">{totalCount}</span>
				{/if}
			</div>
			{#if !embedded}<button
				class="close-btn"
				onclick={onClose}
				aria-label="Close"
			>
				<X size={16} strokeWidth={1.5} />
			</button>{/if}
		</div>

		{/if}

		{#if reviewMode}
			<ReviewDiffPanel
				{projectId} files={flatFiles} loading={$changedFilesLoading}
				selected={reviewSelectedFile} onSelect={(file) => { reviewSelectedFile = file; }}
				{onClose} {embedded}
			/>

			<!-- Accept / Decline footer -->
			{#if reviewSelectedFile}
				<div class="review-footer">
					<div class="review-footer-info">
						<span class="review-footer-filename" title={reviewSelectedFile.file}>
							{basename(reviewSelectedFile.file)}
						</span>
						<span class="review-footer-category review-cat-{reviewSelectedFile.category}">
							{reviewSelectedFile.category}
						</span>
					</div>
					<div class="review-footer-actions">
						{#if reviewSelectedFile.category === 'staged'}
							<button class="review-btn review-btn-unstage" onclick={reviewAccept} disabled={reviewActionInFlight}>
								{#if reviewActionInFlight}<Loader2 size={14} strokeWidth={2} class="animate-spin" />{:else}<Undo2 size={14} strokeWidth={2} />{/if}
								Unstage
							</button>
						{:else}
							<button class="review-btn review-btn-accept" onclick={reviewAccept} disabled={reviewActionInFlight}>
								{#if reviewActionInFlight}<Loader2 size={14} strokeWidth={2} class="animate-spin" />{:else}<Check size={14} strokeWidth={2} />{/if}
								Accept
							</button>
						{/if}
						<button class="review-btn review-btn-decline" onclick={confirmReviewDecline} disabled={reviewActionInFlight}>
							<X size={14} strokeWidth={2} />
							Decline
						</button>
					</div>
				</div>
			{/if}
		{:else}
			<!-- ═══ Classic file list layout ═══ -->
			<div class="panel-content">
				{@render fileListContent()}
			</div>
		{/if}
	</div>

	{#if pendingConfirm}
		<ConfirmDialog
			open={true}
			title={pendingConfirm.title}
			message={pendingConfirm.message}
			confirmLabel={pendingConfirm.confirmLabel}
			danger={true}
			onConfirm={runPendingConfirm}
			onCancel={() => { pendingConfirm = null; }}
		/>
	{/if}
{/if}

<script module lang="ts">
	import { fade } from 'svelte/transition';

	/**
	 * Custom slide-in transition from the right edge.
	 */
	function slideIn(node: HTMLElement, params: { duration?: number } = {}) {
		const duration = params.duration ?? 200;
		return {
			duration,
			css: (t: number) => {
				const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
				return `transform: translateX(${(1 - eased) * 100}%)`;
			}
		};
	}
</script>

<style>
	/* ── Backdrop ────────────────────────────────────────────────────── */
	.panel-backdrop {
		position: fixed;
		inset: 0;
		z-index: 90;
		background: var(--modal-overlay);
	}

	.review-backdrop {
		background: oklch(0 0 0 / 0.55);
	}

	/* ── Panel ───────────────────────────────────────────────────────── */
	.panel {
		position: fixed;
		top: 0;
		right: 0;
		bottom: 0;
		width: 360px;
		/* Desktop shell stays a side drawer at every width — clamp, never overflow. */
		max-width: 100vw;
		z-index: 95;
		display: flex;
		flex-direction: column;
		background: color-mix(in oklch, var(--popover) 95%, transparent);
		backdrop-filter: blur(8px);
		border-left: 1px solid color-mix(in oklch, var(--border) 50%, transparent);
		box-shadow:
			-8px 0 24px rgb(0 0 0 / 0.3),
			-2px 0 8px rgb(0 0 0 / 0.15);
		overflow: hidden;
	}

	.panel.embedded { container-type: inline-size; position: relative; inset: auto; width: 100%; height: 100%; min-height: 0; border: 0; box-shadow: none; backdrop-filter: none; }

	/* Review mode: width controlled by JS via style attribute */
	.panel-review {
		width: min(80vw, 1100px);
	}

	/* Mobile: always fullscreen */
	.panel-mobile {
		width: 100% !important;
		border-left: none;
	}

	/* Disable pointer events on content while dragging resize */
	.panel-resizing {
		user-select: none;
	}

	.panel-resizing :global(*) {
		pointer-events: none;
	}

	/* ── Resize handle ──────────────────────────────────────────────── */
	.resize-handle {
		position: absolute;
		top: 0;
		left: -4px;
		bottom: 0;
		width: 8px;
		z-index: 100;
		cursor: col-resize;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.resize-handle-bar {
		width: 3px;
		height: 48px;
		border-radius: 2px;
		background: color-mix(in oklch, var(--border) 40%, transparent);
		transition: background 150ms ease, height 150ms ease;
	}

	.resize-handle:hover .resize-handle-bar {
		background: color-mix(in oklch, var(--ring) 60%, transparent);
		height: 64px;
	}

	/* ── Header ──────────────────────────────────────────────────────── */
	.panel-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 16px 16px 12px;
		border-bottom: 1px solid color-mix(in oklch, var(--border) 40%, transparent);
		flex-shrink: 0;
	}

	.panel-header-left {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.panel-title {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
		letter-spacing: -0.01em;
	}

	.total-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 22px;
		height: 20px;
		padding: 0 6px;
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--border) 50%, transparent);
		border-radius: var(--radius);
	}

	.close-btn {
		width: 30px;
		height: 30px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius) - 2px);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
		flex-shrink: 0;
	}

	.close-btn:hover {
		background: color-mix(in oklch, var(--border) 50%, transparent);
		color: var(--foreground);
	}

	.close-btn:focus-visible {
		outline: 2px solid color-mix(in oklch, var(--ring) 50%, transparent);
		outline-offset: -2px;
	}

	/* ── Content (classic mode) ──────────────────────────────────────── */
	.panel-content {
		flex: 1;
		overflow-y: auto;
		overflow-x: hidden;
		overscroll-behavior: contain;
	}

	/* Custom scrollbar */
	.panel-content::-webkit-scrollbar { width: 6px; }
	.panel-content::-webkit-scrollbar-track { background: transparent; }
	.panel-content::-webkit-scrollbar-thumb { background: var(--scrollbar-thumb); border-radius: calc(var(--radius) - 6px); }
	.panel-content::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-thumb-hover); }

	/* ═══════════════════════════════════════════════════════════════════
	   REVIEW MODE LAYOUT
	   ═══════════════════════════════════════════════════════════════════ */

	/* ─── Review footer (Accept / Decline) ─────────────────────────── */
	@container (max-width: 420px) { .review-footer { flex-wrap: wrap; gap: 6px; padding: 8px; } .review-footer-info { flex-basis: 100%; } }

	.review-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 8px 16px;
		border-top: 1px solid color-mix(in oklch, var(--border) 50%, transparent);
		background: color-mix(in oklch, var(--muted) 95%, transparent);
		backdrop-filter: blur(4px);
		flex-shrink: 0;
	}

	.review-footer-info {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		flex: 1;
	}

	.review-footer-filename {
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		font-family: var(--font-mono);
	}

	.review-footer-category {
		font-size: 0.5625rem;
		font-weight: 700;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		flex-shrink: 0;
	}

	.review-cat-staged { color: var(--success); }
	.review-cat-modified { color: var(--warning); }
	.review-cat-untracked { color: var(--info); }
	.review-cat-conflict { color: var(--destructive); }

	.review-footer-actions {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-shrink: 0;
	}

	.review-btn {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 5px 12px;
		font-size: 0.75rem;
		font-weight: 600;
		border: 1px solid transparent;
		border-radius: calc(var(--radius) - 2px);
		cursor: pointer;
		transition: all 120ms ease;
		white-space: nowrap;
	}

	.review-btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
		pointer-events: none;
	}

	.review-btn:focus-visible {
		outline: 2px solid color-mix(in oklch, var(--ring) 50%, transparent);
		outline-offset: 1px;
	}

	.review-btn-accept {
		background: color-mix(in oklch, var(--success) 20%, transparent);
		border-color: color-mix(in oklch, var(--success) 40%, transparent);
		color: var(--success);
	}
	.review-btn-accept:hover:not(:disabled) {
		background: color-mix(in oklch, var(--success) 35%, transparent);
		border-color: color-mix(in oklch, var(--success) 60%, transparent);
	}

	.review-btn-unstage {
		background: color-mix(in oklch, var(--warning) 15%, transparent);
		border-color: color-mix(in oklch, var(--warning) 30%, transparent);
		color: var(--warning);
	}
	.review-btn-unstage:hover:not(:disabled) {
		background: color-mix(in oklch, var(--warning) 25%, transparent);
		border-color: color-mix(in oklch, var(--warning) 50%, transparent);
	}

	.review-btn-decline {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		border-color: color-mix(in oklch, var(--destructive) 30%, transparent);
		color: var(--destructive);
	}
	.review-btn-decline:hover:not(:disabled) {
		background: color-mix(in oklch, var(--destructive) 22%, transparent);
		border-color: color-mix(in oklch, var(--destructive) 50%, transparent);
	}

	@media (max-width: 639px) {
		.review-footer {
			flex-direction: column;
			gap: 8px;
			padding: 10px 16px;
		}
		.review-footer-info {
			width: 100%;
		}
		.review-footer-actions {
			width: 100%;
			justify-content: flex-end;
		}
		.review-btn { padding: 8px 16px; min-height: 44px; }
	}

	/* ═══════════════════════════════════════════════════════════════════
	   CLASSIC MODE STYLES (unchanged from original)
	   ═══════════════════════════════════════════════════════════════════ */

	/* ── Section header ──────────────────────────────────────────────── */
	.section-header {
		display: flex;
		align-items: center;
		gap: 0;
		width: 100%;
		background: color-mix(in oklch, var(--muted) 50%, transparent);
		border-bottom: 1px solid color-mix(in oklch, var(--border) 25%, transparent);
		transition: background 150ms ease;
	}

	.section-header:hover {
		background: color-mix(in oklch, var(--muted) 80%, transparent);
	}

	.section-toggle {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
		min-width: 0;
		padding: 10px 8px 10px 16px;
		background: none;
		border: none;
		cursor: pointer;
		text-align: left;
	}

	.section-toggle:focus-visible {
		outline: 2px solid color-mix(in oklch, var(--ring) 50%, transparent);
		outline-offset: -2px;
	}

	.section-chevron {
		color: color-mix(in oklch, var(--muted-foreground) 70%, transparent);
		flex-shrink: 0;
		display: flex;
	}

	.section-icon {
		flex-shrink: 0;
		display: flex;
	}

	.section-label {
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--muted-foreground);
		letter-spacing: 0.02em;
		text-transform: uppercase;
		flex: 1;
	}

	.section-count {
		font-size: 0.6875rem;
		font-weight: 600;
		min-width: 18px;
		text-align: center;
	}

	.section-actions {
		display: none;
		align-items: center;
		gap: 4px;
		padding-right: 12px;
		flex-shrink: 0;
	}

	.section-header:hover .section-actions,
	.section-header:focus-within .section-actions {
		display: flex;
	}

	@media (max-width: 639px) {
		.section-actions { display: flex; }
	}

	@media (pointer: coarse) {
		.section-actions { display: flex; }
	}

	.group-action-btn {
		padding: 2px 8px;
		font-size: 0.625rem;
		font-weight: 500;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--border) 50%, transparent);
		border: 1px solid color-mix(in oklch, var(--border) 50%, transparent);
		border-radius: calc(var(--radius) - 4px);
		cursor: pointer;
		transition: all 120ms ease;
		white-space: nowrap;
	}

	.group-action-btn:hover {
		background: color-mix(in oklch, var(--border) 80%, transparent);
		color: var(--foreground);
	}

	.group-action-danger {
		color: var(--destructive);
		border-color: color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	.group-action-danger:hover {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
	}

	/* ── Section files ───────────────────────────────────────────────── */
	.section-files {
		border-bottom: 1px solid color-mix(in oklch, var(--border) 20%, transparent);
	}

	/* ── File row ────────────────────────────────────────────────────── */
	.file-row {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 16px 6px 40px;
		min-height: 36px;
		transition: background 100ms ease;
	}

	.file-row:hover {
		background: color-mix(in oklch, var(--muted) 50%, transparent);
	}

	@media (max-width: 639px) {
		.file-row {
			min-height: 44px;
			padding: 8px 16px 8px 36px;
		}
	}

	@media (pointer: coarse) {
		.file-row { min-height: 44px; }
	}

	/* ── File name ───────────────────────────────────────────────────── */
	.file-name, .review-file-name {
		display: flex;
		flex-direction: column;
		gap: 1px;
		flex: 1;
		min-width: 0;
		overflow: hidden;
	}

	.file-basename {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.file-dirname {
		font-size: 0.6875rem;
		color: color-mix(in oklch, var(--muted-foreground) 70%, transparent);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* ── Diff stats ──────────────────────────────────────────────────── */
	.diff-stats {
		display: flex;
		gap: 6px;
		font-size: 0.6875rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		flex-shrink: 0;
	}

	/* ── File action buttons ─────────────────────────────────────────── */
	.file-actions {
		display: none;
		align-items: center;
		gap: 2px;
		flex-shrink: 0;
	}

	.file-row:hover .file-actions { display: flex; }

	/* Touch / mobile shell: actions can't be hover-revealed — keep them visible.
	   (hover: none) covers real touch devices, including touch laptops. */
	@media (hover: none) {
		.file-actions { display: flex; }
	}

	.action-btn {
		width: 28px;
		height: 28px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius) - 4px);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 120ms ease;
		flex-shrink: 0;
	}

	.action-btn:hover {
		background: color-mix(in oklch, var(--border) 60%, transparent);
		color: var(--foreground);
	}

	.action-btn:focus-visible {
		outline: 2px solid color-mix(in oklch, var(--ring) 50%, transparent);
		outline-offset: -2px;
	}

	.action-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	.action-btn-danger:hover {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
	}

	/* ── Empty state ─────────────────────────────────────────────────── */
	.empty-state {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		padding: 64px 24px;
	}

	.empty-icon {
		width: 48px;
		height: 48px;
		border-radius: 50%;
		background: color-mix(in oklch, var(--success) 10%, transparent);
		color: var(--success);
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.empty-text {
		font-size: 0.8125rem;
		color: color-mix(in oklch, var(--muted-foreground) 70%, transparent);
		font-weight: 500;
	}

	/* ── Loading skeleton ────────────────────────────────────────────── */
	.loading-skeleton {
		padding: 12px 16px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.skeleton-row {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 6px 0;
	}

	.skeleton-icon {
		width: 14px;
		height: 14px;
		border-radius: calc(var(--radius) - 6px);
		background: color-mix(in oklch, var(--border) 40%, transparent);
		animation: pulse 1.5s ease-in-out infinite;
	}

	.skeleton-text {
		height: 12px;
		border-radius: calc(var(--radius) - 6px);
		background: color-mix(in oklch, var(--border) 30%, transparent);
		animation: pulse 1.5s ease-in-out infinite;
		animation-delay: 75ms;
	}

	.skeleton-row:nth-child(1) .skeleton-text { width: 60%; }
	.skeleton-row:nth-child(2) .skeleton-text { width: 75%; }
	.skeleton-row:nth-child(3) .skeleton-text { width: 50%; }
	.skeleton-row:nth-child(4) .skeleton-text { width: 65%; }
	.skeleton-row:nth-child(5) .skeleton-text { width: 45%; }

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.4; }
	}
</style>
