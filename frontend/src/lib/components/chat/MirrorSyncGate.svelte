<script lang="ts">
	import { untrack, type Snippet } from 'svelte';
	import { LoaderCircle, CloudOff } from 'lucide-svelte';
	import { createMirrorSync, mirrorSyncHeadline, MIRROR_SYNC_NOTICE_GRACE_MS, type MirrorSync } from './mirror-sync.svelte';

	let { sessionId = null, remote = true, deviceName, onCaughtUp, graceMs = MIRROR_SYNC_NOTICE_GRACE_MS, sync: external, children }: {
		sessionId?: string | null;
		remote?: boolean;
		deviceName?: string | null;
		/** Fires ONCE per opened session: its opening checkpoint is materialized.
		 *  Freshness reveals the live transcript, without discarding its reducer
		 *  or cursor. The live SSE feed delivers replicated events; a real log
		 *  rewrite reaches it independently as `reset`.
		 *  Unused when `sync` is given (its owner wired it). */
		onCaughtUp?: () => void;
		graceMs?: number;
		/** A sync check the parent already runs for this chat (ChatView runs one
		 *  whatever its layout, so a chat opened from the hero is checked while
		 *  it loads). Without it the gate runs its own. */
		sync?: MirrorSync;
		children: Snippet;
	} = $props();
	// Decided once: a gate either renders its parent's check or owns one.
	const sync: MirrorSync = untrack(() => external) ?? createMirrorSync(() => ({
		sessionId,
		remote,
		graceMs,
		onCaughtUp: () => onCaughtUp?.(),
	}));
	const waiting = $derived(sync.waiting);
	const keepMounted = import.meta.env.VITE_PERF_KEEP_MIRROR_CACHE !== '0';
	const syncState = $derived(sync.syncState);
	const pending = $derived(sync.pending);
	const behind = $derived(sync.behind);
	const syncNoticeVisible = $derived(sync.noticeVisible);
	const deviceLabel = $derived(deviceName || 'the owning worker');
</script>

{#if waiting}
	<div class="mirror-sync" role="status" aria-live="polite">
		<LoaderCircle size={20} aria-hidden="true" />
		<strong>{mirrorSyncHeadline(syncState, deviceName)}</strong>
		<p>Catching up before showing the current conversation, tasks and agents.</p>
		{#if syncState === 'syncing' && pending > 0}<p>{pending} updates remaining</p>{/if}
	</div>
{:else}
	{#if sync.active}
		<!-- Floats over the transcript instead of pushing it down, so a later
		     drain never moves the conversation. The live region stays mounted so
		     screen readers announce its text changes. -->
		<div class="mirror-status-slot" role="status" aria-live="polite">
			{#if syncState === 'offline'}
				<span class="mirror-pill offline"><CloudOff size={14} aria-hidden="true" />Owning worker offline. Showing the last saved conversation.</span>
			{:else if behind && syncNoticeVisible}
				<span class="mirror-pill"><LoaderCircle size={14} aria-hidden="true" class="mirror-spin" />Syncing updates from {deviceLabel}…{#if pending > 0}{' '}{pending} updates remaining{/if}</span>
			{/if}
		</div>
	{/if}
{/if}
<!-- Freshness withholds stale state, not the component tree or its geometry. -->
{#if keepMounted || !waiting}
	<div class="mirror-content" inert={waiting} aria-hidden={waiting || undefined} style:visibility={waiting ? 'hidden' : undefined}>
		{@render children()}
	</div>
{/if}

<style>
	.mirror-content { display: contents; }
	.mirror-sync { position: absolute; inset: 0; z-index: 1; overflow: auto; display: flex; flex-direction: column; align-items: center; gap: 12px; padding: calc(2rem + var(--chat-status-offset, 0px)) 1.25rem 2rem; text-align: center; color: var(--muted-foreground); }
	.mirror-sync strong { color: var(--foreground); font-size: 1rem; }
	.mirror-sync p { margin: 0; max-width: 42ch; font-size: 0.875rem; }
	/* Positioned against ChatView's .chat-column; spans the column top; empty when current.
	   ChatView sets --chat-status-offset while its reconnect pill holds this spot. */
	.mirror-status-slot {
		position: absolute;
		top: calc(0.5rem + var(--chat-status-offset, 0px));
		transition: top 160ms ease-out;
		left: 0.75rem;
		right: 0.75rem;
		z-index: 5;
		display: flex;
		justify-content: center;
		pointer-events: none;
	}
	.mirror-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		max-width: 100%;
		padding: 0.375rem 0.75rem;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--popover);
		color: var(--muted-foreground);
		box-shadow: var(--shadow-m);
		font-size: 0.8125rem;
		line-height: 1.3;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		animation: mirror-pill-in 160ms ease-out;
	}
	.mirror-pill :global(svg) { flex-shrink: 0; }
	.mirror-pill.offline { white-space: normal; border-radius: var(--radius); }
	.mirror-pill :global(.mirror-spin) { animation: mirror-spin 1s linear infinite; }
	@keyframes mirror-spin { to { transform: rotate(360deg); } }
	@keyframes mirror-pill-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
	@media (max-width: 480px) {
		.mirror-pill { white-space: normal; border-radius: var(--radius); }
	}
	@media (prefers-reduced-motion: reduce) {
		.mirror-pill, .mirror-pill :global(.mirror-spin) { animation: none; }
		.mirror-status-slot { transition: none; }
	}
</style>
