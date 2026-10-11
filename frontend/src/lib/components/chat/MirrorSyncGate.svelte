<script lang="ts" module>
	/** How long a mirrored chat must stay behind its owner, after it was
	 *  current, before the "Syncing updates" pill shows again. Owner-wide
	 *  replication gaps of a poll or two are normal and must not flash it. */
	export const MIRROR_SYNC_NOTICE_GRACE_MS = 3000;
</script>

<script lang="ts">
	import { untrack, type Snippet } from 'svelte';
	import { apiBase, withAuth } from '$lib/api/runtime';
	import { createConnectionBannerGrace } from '$lib/stores/session/connection-banner-grace';
	import { LoaderCircle, CloudOff } from 'lucide-svelte';

	let { sessionId, remote = true, deviceName, onCaughtUp, graceMs = MIRROR_SYNC_NOTICE_GRACE_MS, children }: {
		sessionId: string | null;
		remote?: boolean;
		deviceName?: string | null;
		/** Fires ONCE per opened session: its first transition to `current`.
		 *  Later drains of an owner-wide backlog must not reload the transcript —
		 *  the live SSE feed already delivers every replicated event, and an
		 *  explicit log rewrite reaches the feed as its own `reset` (#2227). */
		onCaughtUp: () => void;
		graceMs?: number;
		children: Snippet;
	} = $props();
	let syncState = $state<'checking' | 'syncing' | 'current' | 'offline'>('checking');
	let pending = $state(0);
	let revealedSession = $state<string | null>(null);
	// Shown only after the chat has stayed behind for the grace period.
	let syncNoticeVisible = $state(false);
	// Props may be getters over whole stores (ChatView's $activeSession/$sessions).
	// Equal primitive values must not restart polling when those stores reload.
	const pollingSessionId = $derived(sessionId);
	const pollingRemote = $derived(remote);
	// Owner-wide watermarks advance for unrelated chats too. Gate the initial
	// catch-up, then keep the conversation mounted during incremental sync.
	const waiting = $derived(!!sessionId && remote && revealedSession !== sessionId);
	const behind = $derived(syncState === 'syncing' || syncState === 'checking');
	const deviceLabel = $derived(deviceName || 'the owning worker');

	$effect(() => {
		const id = pollingSessionId;
		if (!id || !pollingRemote) return;
		const controller = new AbortController();
		let timer: ReturnType<typeof setTimeout>;
		let caughtUpOnce = false;
		const notice = createConnectionBannerGrace({
			onShow: () => { syncNoticeVisible = true; },
			onHide: () => { syncNoticeVisible = false; },
			graceMs: untrack(() => graceMs),
		});
		revealedSession = null;
		syncNoticeVisible = false;
		syncState = 'checking';
		pending = 0;
		async function poll() {
			try {
				const response = await fetch(`${apiBase()}/sessions/${encodeURIComponent(id!)}/sync-status`, withAuth({ signal: controller.signal }));
				if (!response.ok) throw new Error('Sync status unavailable');
				const status = await response.json();
				if (controller.signal.aborted) return;
				if (!['checking', 'syncing', 'current', 'offline'].includes(status.state)) throw new Error('Invalid sync status');
				// Only the first catch-up may rebuild the transcript.
				const firstCatchUp = status.state === 'current' && !caughtUpOnce;
				if (firstCatchUp) caughtUpOnce = true;
				syncState = status.state;
				pending = status.pending_updates ?? 0;
				if (status.state === 'current' || status.state === 'offline') revealedSession = id;
				notice.setConnected(status.state !== 'syncing' && status.state !== 'checking');
				if (firstCatchUp) untrack(onCaughtUp);
			} catch {
				// Keep a known backlog visible on a failed poll; never call it current.
			} finally {
				if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
			}
		}
		void poll();
		return () => { controller.abort(); clearTimeout(timer); notice.destroy(); };
	});
</script>

{#if waiting}
	<div class="mirror-sync" role="status" aria-live="polite">
		<LoaderCircle size={20} aria-hidden="true" />
		<strong>{syncState === 'syncing' ? 'Syncing from' : 'Checking sync with'} {deviceLabel}…</strong>
		<p>Catching up before showing the current conversation, tasks and agents.</p>
		{#if syncState === 'syncing' && pending > 0}<p>{pending} updates remaining</p>{/if}
	</div>
{:else}
	{#if sessionId && remote}
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
	{@render children()}
{/if}

<style>
	.mirror-sync { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 2rem 1.25rem; text-align: center; color: var(--muted-foreground); }
	.mirror-sync strong { color: var(--foreground); font-size: 1rem; }
	.mirror-sync p { margin: 0; max-width: 42ch; font-size: 0.875rem; }
	/* Positioned against ChatView's .chat-column; zero-size when empty. */
	.mirror-status-slot {
		position: absolute;
		top: 0.5rem;
		left: 0.75rem;
		right: 0.75rem;
		z-index: 5;
		display: flex;
		justify-content: center;
		pointer-events: none;
	}	.mirror-pill {
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
	}
</style>
