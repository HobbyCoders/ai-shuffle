<script lang="ts">
	/**
	 * ChangedFilesPanel — Slide-out panel showing all changed files in a git repo,
	 * grouped by category (conflicts, staged, modified, untracked), with actions
	 * to stage/unstage/discard files and view diffs.
	 */
	import {
		X, ChevronDown, ChevronRight,
		Plus, Minus, Eye, Undo2, Trash2,
		AlertTriangle, Check, Circle,
		FileText
	} from 'lucide-svelte';
	import { git, changedFiles, changedFilesLoading } from '$lib/stores/git.svelte';
	import type { FileChangeInfo } from '$lib/api/git';

	interface Props {
		projectId: string;
		open: boolean;
		onClose: () => void;
		onViewDiff: (file: FileChangeInfo) => void;
	}

	let { projectId, open, onClose, onViewDiff }: Props = $props();

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

	// ── Discard / delete confirmation state ─────────────────────────────
	let confirmingDiscard = $state(new Set<string>());
	// Track "discard all" / "delete all" group-level confirmations
	let confirmingGroupAction = $state<string | null>(null);

	function requestConfirm(file: string) {
		const next = new Set(confirmingDiscard);
		next.add(file);
		confirmingDiscard = next;
	}

	function cancelConfirm(file: string) {
		const next = new Set(confirmingDiscard);
		next.delete(file);
		confirmingDiscard = next;
	}

	// ── Actions ─────────────────────────────────────────────────────────

	async function stageFile(file: FileChangeInfo) {
		markInFlight(file.file);
		try {
			await git.stageFiles([file.file]);
		} finally {
			clearInFlight(file.file);
		}
	}

	async function unstageFile(file: FileChangeInfo) {
		markInFlight(file.file);
		try {
			await git.unstageFiles([file.file]);
		} finally {
			clearInFlight(file.file);
		}
	}

	async function discardFile(file: FileChangeInfo) {
		markInFlight(file.file);
		cancelConfirm(file.file);
		try {
			await git.discardChanges([file.file], { [file.file]: file.category });
		} finally {
			clearInFlight(file.file);
		}
	}

	async function stageAll(files: FileChangeInfo[]) {
		const paths = files.map(f => f.file);
		for (const p of paths) markInFlight(p);
		try {
			await git.stageFiles(paths);
		} finally {
			for (const p of paths) clearInFlight(p);
		}
	}

	async function unstageAll(files: FileChangeInfo[]) {
		const paths = files.map(f => f.file);
		for (const p of paths) markInFlight(p);
		try {
			await git.unstageFiles(paths);
		} finally {
			for (const p of paths) clearInFlight(p);
		}
	}

	async function discardAll(files: FileChangeInfo[]) {
		confirmingGroupAction = null;
		const paths = files.map(f => f.file);
		for (const p of paths) markInFlight(p);
		try {
			await git.discardChanges(
				paths,
				Object.fromEntries(files.map(f => [f.file, f.category]))
			);
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
			case 'A': return 'text-green-400';
			case 'M': return 'text-amber-400';
			case 'D': return 'text-red-400';
			case 'R': return 'text-blue-400';
			case 'U': return 'text-red-400';
			default:  return 'text-zinc-500';
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (!open) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			onClose();
		}
	}

	// Reset confirmation state when panel closes
	$effect(() => {
		if (!open) {
			confirmingDiscard = new Set();
			confirmingGroupAction = null;
		}
	});
</script>

<svelte:window onkeydown={handleKeydown} />

{#snippet statusIcon(status: string)}
	<span class={`flex-shrink-0 ${statusColor(status)}`}>
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
	{@const isConfirming = confirmingDiscard.has(file.file)}
	{@const isInFlight = inFlightFiles.has(file.file)}

	<div class="file-row group/row" class:opacity-50={isInFlight} class:pointer-events-none={isInFlight}>
		{#if isConfirming}
			<!-- Inline discard/delete confirmation -->
			<div class="confirm-bar">
				<span class="confirm-text">
					{group === 'untracked' ? 'Delete file?' : 'Discard changes?'}
				</span>
				<button
					class="confirm-yes"
					onclick={() => discardFile(file)}
				>
					Yes
				</button>
				<button
					class="confirm-no"
					onclick={() => cancelConfirm(file.file)}
				>
					No
				</button>
			</div>
		{:else}
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
						<span class="text-green-400">+{file.additions}</span>
					{/if}
					{#if file.deletions > 0}
						<span class="text-red-400">-{file.deletions}</span>
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
						onclick={() => requestConfirm(file.file)}
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
						onclick={() => requestConfirm(file.file)}
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
		{/if}
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

		{#if groupActions}
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
					{#if confirmingGroupAction === 'discard-modified'}
						<button
							class="group-action-btn group-action-danger"
							onclick={() => discardAll(groupActions.files)}
						>
							Confirm
						</button>
						<button
							class="group-action-btn"
							onclick={() => { confirmingGroupAction = null; }}
						>
							Cancel
						</button>
					{:else}
						<button
							class="group-action-btn group-action-danger"
							onclick={() => { confirmingGroupAction = 'discard-modified'; }}
						>
							Discard All
						</button>
					{/if}
				{:else if groupActions.type === 'untracked'}
					<button
						class="group-action-btn"
						onclick={() => stageAll(groupActions.files)}
					>
						Stage All
					</button>
					{#if confirmingGroupAction === 'delete-untracked'}
						<button
							class="group-action-btn group-action-danger"
							onclick={() => discardAll(groupActions.files)}
						>
							Confirm
						</button>
						<button
							class="group-action-btn"
							onclick={() => { confirmingGroupAction = null; }}
						>
							Cancel
						</button>
					{:else}
						<button
							class="group-action-btn group-action-danger"
							onclick={() => { confirmingGroupAction = 'delete-untracked'; }}
						>
							Delete All
						</button>
					{/if}
				{/if}
			</div>
		{/if}
	</div>
{/snippet}

{#if open}
	<!-- Backdrop -->
	<div class="panel-backdrop" transition:fade={{ duration: 150 }}>
		<button
			class="backdrop-click-target"
			onclick={onClose}
			tabindex="-1"
			aria-label="Close changed files panel"
		></button>
	</div>

	<!-- Panel -->
	<div
		class="panel"
		role="dialog"
		aria-modal="true"
		aria-label="Changed files"
		transition:slideIn={{ duration: 200 }}
	>
		<!-- Header -->
		<div class="panel-header">
			<div class="panel-header-left">
				<h2 class="panel-title">Changed Files</h2>
				{#if totalCount > 0}
					<span class="total-badge">{totalCount}</span>
				{/if}
			</div>
			<button
				class="close-btn"
				onclick={onClose}
				aria-label="Close"
			>
				<X size={16} strokeWidth={1.5} />
			</button>
		</div>

		<!-- Content -->
		<div class="panel-content">
			{#if $changedFilesLoading && totalCount === 0}
				<!-- Loading skeleton -->
				<div class="loading-skeleton">
					{#each Array(5) as _}
						<div class="skeleton-row">
							<div class="skeleton-icon"></div>
							<div class="skeleton-text"></div>
						</div>
					{/each}
				</div>
			{:else if isEmpty}
				<!-- Empty state -->
				<div class="empty-state">
					<div class="empty-icon">
						<Check size={28} strokeWidth={1.5} />
					</div>
					<span class="empty-text">Working tree clean</span>
				</div>
			{:else}
				<!-- Merge Conflicts -->
				{#if conflicts.length > 0}
					{@render sectionHeader('conflicts', 'Merge Conflicts', conflicts.length, 'text-red-400', AlertTriangle)}
					{#if !isSectionCollapsed('conflicts')}
						<div class="section-files">
							{#each conflicts as file (file.file)}
								{@render fileRow(file, 'conflict')}
							{/each}
						</div>
					{/if}
				{/if}

				<!-- Staged Changes -->
				{#if staged.length > 0}
					{@render sectionHeader('staged', 'Staged Changes', staged.length, 'text-green-400', Check, { files: staged, type: 'staged' })}
					{#if !isSectionCollapsed('staged')}
						<div class="section-files">
							{#each staged as file (file.file)}
								{@render fileRow(file, 'staged')}
							{/each}
						</div>
					{/if}
				{/if}

				<!-- Changes (unstaged) -->
				{#if modified.length > 0}
					{@render sectionHeader('modified', 'Changes', modified.length, 'text-amber-400', Circle, { files: modified, type: 'modified' })}
					{#if !isSectionCollapsed('modified')}
						<div class="section-files">
							{#each modified as file (file.file)}
								{@render fileRow(file, 'modified')}
							{/each}
						</div>
					{/if}
				{/if}

				<!-- Untracked -->
				{#if untracked.length > 0}
					{@render sectionHeader('untracked', 'Untracked Files', untracked.length, 'text-zinc-500', Plus, { files: untracked, type: 'untracked' })}
					{#if !isSectionCollapsed('untracked')}
						<div class="section-files">
							{#each untracked as file (file.file)}
								{@render fileRow(file, 'untracked')}
							{/each}
						</div>
					{/if}
				{/if}
			{/if}
		</div>
	</div>
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
		background: rgba(0, 0, 0, 0.4);
	}

	.backdrop-click-target {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		background: transparent;
		border: none;
		cursor: default;
		padding: 0;
	}

	/* ── Panel ───────────────────────────────────────────────────────── */
	.panel {
		position: fixed;
		top: 0;
		right: 0;
		bottom: 0;
		width: 360px;
		z-index: 95;
		display: flex;
		flex-direction: column;
		background: rgb(24 24 27 / 0.95);
		backdrop-filter: blur(8px);
		border-left: 1px solid rgb(63 63 70 / 0.5);
		box-shadow:
			-8px 0 24px rgb(0 0 0 / 0.3),
			-2px 0 8px rgb(0 0 0 / 0.15);
		overflow: hidden;
	}

	@media (max-width: 639px) {
		.panel {
			width: 100%;
		}
	}

	/* ── Header ──────────────────────────────────────────────────────── */
	.panel-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 16px 16px 12px;
		border-bottom: 1px solid rgb(63 63 70 / 0.4);
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
		color: rgb(244 244 245);
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
		color: rgb(161 161 170);
		background: rgb(63 63 70 / 0.5);
		border-radius: 10px;
	}

	.close-btn {
		width: 30px;
		height: 30px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: rgb(161 161 170);
		cursor: pointer;
		transition: all 150ms ease;
		flex-shrink: 0;
	}

	.close-btn:hover {
		background: rgb(63 63 70 / 0.5);
		color: rgb(244 244 245);
	}

	.close-btn:focus-visible {
		outline: 2px solid rgb(161 161 170 / 0.5);
		outline-offset: -2px;
	}

	/* ── Content ─────────────────────────────────────────────────────── */
	.panel-content {
		flex: 1;
		overflow-y: auto;
		overflow-x: hidden;
		overscroll-behavior: contain;
	}

	/* Custom scrollbar */
	.panel-content::-webkit-scrollbar {
		width: 6px;
	}

	.panel-content::-webkit-scrollbar-track {
		background: transparent;
	}

	.panel-content::-webkit-scrollbar-thumb {
		background: rgb(63 63 70 / 0.6);
		border-radius: 3px;
	}

	.panel-content::-webkit-scrollbar-thumb:hover {
		background: rgb(82 82 91 / 0.8);
	}

	/* ── Section header ──────────────────────────────────────────────── */
	.section-header {
		display: flex;
		align-items: center;
		gap: 0;
		width: 100%;
		background: rgb(39 39 42 / 0.5);
		border-bottom: 1px solid rgb(63 63 70 / 0.25);
		transition: background 150ms ease;
	}

	.section-header:hover {
		background: rgb(39 39 42 / 0.8);
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
		outline: 2px solid rgb(161 161 170 / 0.5);
		outline-offset: -2px;
	}

	.section-chevron {
		color: rgb(113 113 122);
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
		color: rgb(161 161 170);
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

	/* Show group actions on hover (desktop) and always on mobile */
	.section-header:hover .section-actions,
	.section-header:focus-within .section-actions {
		display: flex;
	}

	@media (max-width: 639px) {
		.section-actions {
			display: flex;
		}
	}

	@media (pointer: coarse) {
		.section-actions {
			display: flex;
		}
	}

	.group-action-btn {
		padding: 2px 8px;
		font-size: 0.625rem;
		font-weight: 500;
		color: rgb(161 161 170);
		background: rgb(63 63 70 / 0.5);
		border: 1px solid rgb(63 63 70 / 0.5);
		border-radius: 4px;
		cursor: pointer;
		transition: all 120ms ease;
		white-space: nowrap;
	}

	.group-action-btn:hover {
		background: rgb(63 63 70 / 0.8);
		color: rgb(228 228 231);
	}

	.group-action-danger {
		color: rgb(248 113 113);
		border-color: rgb(248 113 113 / 0.3);
	}

	.group-action-danger:hover {
		background: rgb(248 113 113 / 0.15);
		color: rgb(252 165 165);
	}

	/* ── Section files ───────────────────────────────────────────────── */
	.section-files {
		border-bottom: 1px solid rgb(63 63 70 / 0.2);
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
		background: rgb(39 39 42 / 0.5);
	}

	@media (max-width: 639px) {
		.file-row {
			min-height: 44px;
			padding: 8px 16px 8px 36px;
		}
	}

	@media (pointer: coarse) {
		.file-row {
			min-height: 44px;
		}
	}

	/* ── File name ───────────────────────────────────────────────────── */
	.file-name {
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
		color: rgb(228 228 231);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.file-dirname {
		font-size: 0.6875rem;
		color: rgb(113 113 122);
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

	.file-row:hover .file-actions {
		display: flex;
	}

	@media (max-width: 639px) {
		.file-actions {
			display: flex;
		}
	}

	@media (pointer: coarse) {
		.file-actions {
			display: flex;
		}
	}

	.action-btn {
		width: 28px;
		height: 28px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: 5px;
		color: rgb(161 161 170);
		cursor: pointer;
		transition: all 120ms ease;
		flex-shrink: 0;
	}

	.action-btn:hover {
		background: rgb(63 63 70 / 0.6);
		color: rgb(228 228 231);
	}

	.action-btn:focus-visible {
		outline: 2px solid rgb(161 161 170 / 0.5);
		outline-offset: -2px;
	}

	.action-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	.action-btn-danger:hover {
		background: rgb(248 113 113 / 0.15);
		color: rgb(248 113 113);
	}

	/* ── Inline confirm bar ──────────────────────────────────────────── */
	.confirm-bar {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
	}

	.confirm-text {
		font-size: 0.75rem;
		color: rgb(250 204 21);
		font-weight: 500;
		flex: 1;
	}

	.confirm-yes {
		padding: 3px 12px;
		font-size: 0.6875rem;
		font-weight: 600;
		color: rgb(254 226 226);
		background: rgb(220 38 38 / 0.5);
		border: 1px solid rgb(220 38 38 / 0.6);
		border-radius: 5px;
		cursor: pointer;
		transition: all 120ms ease;
	}

	.confirm-yes:hover {
		background: rgb(220 38 38 / 0.7);
	}

	.confirm-no {
		padding: 3px 12px;
		font-size: 0.6875rem;
		font-weight: 600;
		color: rgb(161 161 170);
		background: rgb(63 63 70 / 0.4);
		border: 1px solid rgb(63 63 70 / 0.5);
		border-radius: 5px;
		cursor: pointer;
		transition: all 120ms ease;
	}

	.confirm-no:hover {
		background: rgb(63 63 70 / 0.7);
		color: rgb(228 228 231);
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
		background: rgb(34 197 94 / 0.1);
		color: rgb(74 222 128);
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.empty-text {
		font-size: 0.8125rem;
		color: rgb(113 113 122);
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
		border-radius: 3px;
		background: rgb(63 63 70 / 0.4);
		animation: pulse 1.5s ease-in-out infinite;
	}

	.skeleton-text {
		height: 12px;
		border-radius: 3px;
		background: rgb(63 63 70 / 0.3);
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
