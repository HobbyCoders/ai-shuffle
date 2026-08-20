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
	import { Loader2, AlertCircle, Check, Monitor, Cloud, GitBranch } from 'lucide-svelte';
	import type { Session } from '$lib/api/client';
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
</script>

<button
	class="scard"
	class:active
	aria-current={active ? 'true' : undefined}
	onclick={() => onOpen(session.id)}
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

	<span class="l2">{title}</span>

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
</button>

<style>
	.scard {
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
	.scard:hover { background: var(--sidebar-accent); }
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
	}
	.timer.live { color: var(--primary); }

	.l2 {
		display: block;
		margin-top: 3px;
		font-size: 13.5px;
		font-weight: 500;
		letter-spacing: -0.008em;
		color: var(--sidebar-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
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

	@media (prefers-reduced-motion: reduce) {
		.status :global(svg),
		.status :global(.spin) { animation: none; }
	}
</style>
