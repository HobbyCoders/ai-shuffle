<script lang="ts">
	/**
	 * SessionCard - three-line session row for the Studio Noir sidebar.
	 *
	 * L1: project · status (+ live timer)
	 * L2: session title
	 * L3: device · branch
	 *
	 * The mockup's third line also carries a +add/−del diffstat and a provider
	 * glyph; neither is derivable from the sessions store without new API work,
	 * so those two segments are omitted rather than faked.
	 */
	import { onDestroy } from 'svelte';
	import {
		Loader2,
		AlertCircle,
		Check,
		Monitor,
		Cloud,
		GitBranch,
		Undo2,
		Pencil,
		GitFork,
		Trash2,
		X
	} from 'lucide-svelte';
	import type { Session } from '$lib/api/client';
	import { forkSession } from '$lib/api/client';
	import { appData } from '$lib/stores/app-data.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { toast } from '$lib/stores/toast';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';
	import {
		STATUS_LABELS,
		isLiveStatus,
		formatElapsed,
		formatAge,
		deviceLabel,
		branchLabel,
		sessionTimestamp,
		type SessionCardStatus
	} from './session-card-data';

	interface Props {
		session: Session;
		status: SessionCardStatus;
		/** Display name of the session's project. */
		projectLabel: string;
		/** True when this session is the one open in the main panel. */
		active?: boolean;
		/** Current clock (ms) supplied by the sidebar's shared ticker. */
		now: number;
		onOpen: (sessionId: string) => void;
	}

	let { session, status, projectLabel, active = false, now, onOpen }: Props = $props();

	const live = $derived(isLiveStatus(status));
	const elapsed = $derived(Math.max(0, now - sessionTimestamp(session)));
	const timer = $derived(live ? formatElapsed(elapsed) : formatAge(elapsed));
	const title = $derived(session.title?.trim() || 'Untitled');
	const device = $derived(deviceLabel(session));
	const branch = $derived(branchLabel(session));
	const statusLabel = $derived(STATUS_LABELS[status]);

	let cardRef = $state<HTMLElement | null>(null);
	let actionsOpen = $state(false);
	let deleteArmed = $state(false);
	let deleteLoading = $state(false);
	let deleteConfirmTimer: ReturnType<typeof setTimeout> | null = null;
	let renaming = $state(false);
	let renameValue = $state('');

	function disarmDelete() {
		if (deleteConfirmTimer !== null) {
			clearTimeout(deleteConfirmTimer);
			deleteConfirmTimer = null;
		}
		deleteArmed = false;
	}

	function closeActions() {
		actionsOpen = false;
		disarmDelete();
	}

	function handleWindowClick(event: MouseEvent) {
		if (actionsOpen && cardRef && !cardRef.contains(event.target as Node)) closeActions();
	}

	function handleWindowKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			if (renaming) {
				renaming = false;
				renameValue = '';
			}
			closeActions();
		}
	}

	function toggleActions() {
		if (actionsOpen) closeActions();
		else actionsOpen = true;
	}

	async function toggleSettled(event: MouseEvent | KeyboardEvent) {
		event.stopPropagation();
		actionsOpen = false;
		try {
			await appData.setSessionActive(session.id, !session.is_active);
		} catch {
			toast.push('Failed to update session', { tone: 'error' });
		}
	}

	async function handleFork(event: MouseEvent | KeyboardEvent) {
		event.stopPropagation();
		actionsOpen = false;
		try {
			const result = await forkSession(session.id);
			await appData.loadSessions();
			toast.push('Session forked', { tone: 'success' });
			onOpen(result.session_id);
		} catch {
			toast.push('Failed to fork session', { tone: 'error' });
		}
	}

	function armDelete() {
		disarmDelete();
		deleteArmed = true;
		actionsOpen = true;
		deleteConfirmTimer = setTimeout(() => {
			deleteArmed = false;
			deleteConfirmTimer = null;
		}, 8000);
	}

	async function handleDelete(event: MouseEvent | KeyboardEvent) {
		event.stopPropagation();
		if (!deleteArmed) {
			armDelete();
			return;
		}

		disarmDelete();
		actionsOpen = false;
		deleteLoading = true;
		try {
			await appData.deleteSession(session.id);
			if (activeSession.current.sessionId === session.id) activeSession.newChat();
		} catch {
			toast.push('Failed to delete session', { tone: 'error' });
		} finally {
			deleteLoading = false;
		}
	}

	function cancelDelete(event: MouseEvent | KeyboardEvent) {
		event.stopPropagation();
		closeActions();
	}

	function focusSelect(node: HTMLInputElement) {
		node.focus();
		node.select();
	}

	function startRename(event: MouseEvent | KeyboardEvent) {
		event.stopPropagation();
		actionsOpen = false;
		disarmDelete();
		renaming = true;
		renameValue = session.title || '';
	}

	function cancelRename() {
		renaming = false;
		renameValue = '';
	}

	async function commitRename() {
		if (!renaming) return;
		const next = renameValue.trim();
		const current = (session.title || '').trim();
		cancelRename();
		if (!next || next === current) return;
		try {
			await appData.renameSession(session.id, next);
			activeSession.setTitle(session.id, next);
		} catch {
			toast.push('Failed to rename session', { tone: 'error' });
		}
	}

	function handleRenameKeydown(event: KeyboardEvent) {
		event.stopPropagation();
		if (event.key === 'Enter') {
			event.preventDefault();
			commitRename();
		} else if (event.key === 'Escape') {
			event.preventDefault();
			cancelRename();
		}
	}

	function openCard() {
		if (deleteArmed || deleteLoading || renaming) return;
		onOpen(session.id);
	}

	function handleCardKeydown(event: KeyboardEvent) {
		if (event.target !== event.currentTarget) return;
		if (event.key === 'Enter' || event.key === ' ') {
			event.preventDefault();
			openCard();
		}
	}

	onDestroy(disarmDelete);
</script>

<svelte:window onclick={handleWindowClick} onkeydown={handleWindowKeydown} />

<div
	bind:this={cardRef}
	class="scard"
	class:active
	class:actions-open={actionsOpen || deleteArmed}
	role="button"
	tabindex="0"
	aria-current={active ? 'true' : undefined}
	onclick={openCard}
	onkeydown={handleCardKeydown}
	title={title}
>
	<span class="l1">
		<span class="proj">{projectLabel}</span>
		<span class="dot" aria-hidden="true"></span>
		<span class="status {status}">
			{#if status === 'working'}
				<Loader2 size={11} strokeWidth={2.6} class="spin" />
			{:else if status === 'await'}
				<AlertCircle size={11} strokeWidth={2.2} />
			{:else if status === 'done'}
				<Check size={11} strokeWidth={2.6} />
			{/if}
			{statusLabel}
		</span>
		<span class="timer" class:live>{timer}</span>
	</span>

	{#if renaming}
		<input
			class="rename-input"
			type="text"
			bind:value={renameValue}
			use:focusSelect
			onclick={(event) => event.stopPropagation()}
			onkeydown={handleRenameKeydown}
			onblur={commitRename}
			aria-label="Rename session"
		/>
	{:else}
		<span class="l2">{title}</span>
	{/if}

	<span class="l3">
		<span class="seg">
			{#if session.is_remote}
				<Cloud size={12} strokeWidth={1.9} />
			{:else}
				<Monitor size={12} strokeWidth={1.9} />
			{/if}
			<span class="dev">{device}</span>
		</span>
		{#if branch}
			<span class="seg">
				<GitBranch size={12} strokeWidth={1.9} />
				<span class="branch">{branch}</span>
			</span>
		{/if}
	</span>

	<span class="card-actions">
		{#snippet settleToggle()}
			{#if session.is_active}
				<button
					class="row-action state-toggle"
					title="Settle session"
					aria-label="Settle session"
					disabled={deleteArmed || deleteLoading}
					onclick={toggleSettled}
				>
					<Check size={13} strokeWidth={1.8} />
				</button>
			{:else}
				<button
					class="row-action state-toggle"
					title="Move to Active"
					aria-label="Move session to Active"
					disabled={deleteArmed || deleteLoading}
					onclick={toggleSettled}
				>
					<Undo2 size={13} strokeWidth={1.8} />
				</button>
			{/if}
		{/snippet}

		<RowActionMenu
			open={actionsOpen}
			onToggle={toggleActions}
			label="Session actions"
			persistent={settleToggle}
		>
			{#if deleteArmed}
				<button class="row-action cancel" title="Cancel" aria-label="Cancel delete" onclick={cancelDelete}>
					<X size={13} strokeWidth={2} />
				</button>
			{/if}
			<button
				class="row-action"
				title="Rename session"
				aria-label="Rename session"
				disabled={deleteArmed || deleteLoading}
				onclick={startRename}
			>
				<Pencil size={13} strokeWidth={1.7} />
			</button>
			<button
				class="row-action"
				title="Fork session"
				aria-label="Fork session"
				disabled={deleteArmed || deleteLoading}
				onclick={handleFork}
			>
				<GitFork size={13} strokeWidth={1.7} />
			</button>
			<button
				class="row-action"
				class:danger={deleteArmed}
				title={deleteArmed ? 'Confirm delete' : 'Delete session'}
				aria-label={deleteArmed ? 'Confirm delete' : 'Delete session'}
				disabled={deleteLoading}
				onclick={handleDelete}
			>
				<Trash2 size={13} strokeWidth={deleteArmed ? 2 : 1.7} />
			</button>
		</RowActionMenu>
	</span>
</div>

<style>
	.scard {
		--ram-reveal: 0;
		display: block;
		width: 100%;
		text-align: left;
		padding: 8px 7px 9px;
		border: none;
		border-radius: 9px;
		background: transparent;
		color: inherit;
		cursor: pointer;
		transition: background-color 0.13s ease;
		position: relative;
	}
	.scard:hover,
	.scard:focus-within,
	.scard.actions-open {
		--ram-reveal: 1;
		background: var(--sidebar-accent);
	}
	.scard.active { background: var(--sidebar-accent-active); }
	.scard.active::before {
		content: '';
		position: absolute;
		left: 0;
		top: 9px;
		bottom: 9px;
		width: 2px;
		border-radius: 2px;
		background: var(--primary);
		box-shadow: 0 0 8px -1px var(--glow);
	}
	.scard:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	.l1 {
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
		padding-right: 58px;
	}
	.proj {
		font-size: 12px;
		color: var(--sidebar-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 96px;
	}
	.dot {
		width: 2px;
		height: 2px;
		border-radius: 50%;
		background: var(--subtle-foreground);
		flex: none;
	}
	.status {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 12px;
		white-space: nowrap;
	}
	.status.working { color: var(--primary); }
	.status.await { color: var(--amber); }
	.status.done { color: var(--muted-foreground); }
	.status.idle { color: var(--subtle-foreground); }
	.status :global(svg) { flex: none; }
	.status.working :global(svg) {
		filter: drop-shadow(0 0 4px var(--glow));
		animation: card-pulse 2.4s ease-in-out infinite;
	}
	.status.await :global(svg) { animation: card-pulse 2.4s ease-in-out infinite; }
	.status :global(.spin) { animation: card-spin 1.4s linear infinite; }

	@keyframes card-spin {
		to { transform: rotate(360deg); }
	}
	@keyframes card-pulse {
		0%, 100% { opacity: 0.75; }
		50% { opacity: 1; }
	}

	.timer {
		margin-left: auto;
		font-family: var(--font-mono);
		font-variant-numeric: tabular-nums;
		font-size: 11.5px;
		color: var(--sidebar-muted);
		white-space: nowrap;
		transition: opacity 0.12s ease;
	}
	.timer.live { color: var(--primary); }
	.scard:hover .timer,
	.scard:focus-within .timer,
	.scard.actions-open .timer { opacity: 0; }

	.l2 {
		display: block;
		margin-top: 3px;
		padding-right: 58px;
		font-size: 13.5px;
		font-weight: 500;
		letter-spacing: -0.008em;
		color: var(--sidebar-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.rename-input {
		display: block;
		width: calc(100% - 58px);
		height: 24px;
		margin-top: 1px;
		padding: 2px 6px;
		border: 1px solid var(--ring);
		border-radius: 6px;
		background: var(--background);
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
		outline: none;
	}

	.l3 {
		margin-top: 4px;
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: 11px;
		color: var(--sidebar-muted);
		min-width: 0;
		flex-wrap: nowrap;
		overflow: hidden;
		padding-right: 58px;
	}
	.l3 :global(svg) { color: var(--subtle-foreground); flex: none; }
	.seg {
		display: inline-flex;
		align-items: center;
		gap: 3.5px;
		min-width: 0;
		flex: none;
		white-space: nowrap;
	}
	.dev {
		display: block;
		min-width: 0;
		max-width: 96px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.branch {
		max-width: 78px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: var(--font-mono);
		font-size: 10px;
	}

	.card-actions {
		position: absolute;
		top: 4px;
		right: 4px;
		bottom: 4px;
		display: flex;
		align-items: center;
		z-index: 5;
	}
	.card-actions :global(.ram) { height: 100%; }
	.row-action {
		width: 25px;
		height: 25px;
		padding: 0;
		border: none;
		border-radius: 6px;
		background: transparent;
		color: var(--sidebar-muted);
		display: grid;
		place-items: center;
		cursor: pointer;
		transition: color 0.12s ease, background-color 0.12s ease, opacity 0.12s ease;
	}
	.row-action:hover {
		color: var(--foreground);
		background: color-mix(in oklch, var(--sidebar-accent-active) 78%, transparent);
	}
	.row-action.state-toggle { color: var(--muted-foreground); }
	.row-action.danger {
		--ram-reveal: 1;
		color: var(--destructive);
		background: color-mix(in oklch, var(--destructive) 14%, transparent);
	}
	.row-action.cancel { --ram-reveal: 1; }
	.row-action:disabled { opacity: 0.45; cursor: not-allowed; }
	.row-action:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	@media (hover: none), (max-width: 1080px) {
		.scard .timer { opacity: 1; }
	}

	@media (prefers-reduced-motion: reduce) {
		.status :global(svg),
		.status :global(.spin) { animation: none; }
	}
</style>
