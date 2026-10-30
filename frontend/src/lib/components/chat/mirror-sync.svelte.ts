/**
 * The sync check of a mirrored (remote) chat: polls the owner's sync status
 * for the open session, pins an opening checkpoint, and reports when the
 * mirror has caught up with it.
 *
 * Headless, so ChatView can run it for whichever chat is open, whatever its
 * layout: a remote chat opened from the centred hero keeps that hero while it
 * loads, and its sync check must start then, not only once a message column
 * exists to host MirrorSyncGate. MirrorSyncGate renders this state (the
 * "Checking sync…" pane, the floating pill) and creates its own controller
 * only when it is not handed one.
 *
 * Must be created during component initialisation (it owns an $effect).
 */
import { untrack } from 'svelte';
import { apiBase, withAuth } from '$lib/api/runtime';
import { createConnectionBannerGrace } from '$lib/stores/session/connection-banner-grace';

/** How long a mirrored chat must stay behind its owner, after it was
 *  current, before the "Syncing updates" pill shows again. Owner-wide
 *  replication gaps of a poll or two are normal and must not flash it. */
export const MIRROR_SYNC_NOTICE_GRACE_MS = 3000;
/** Poll interval of the sync status. */
export const MIRROR_SYNC_POLL_MS = 2000;
/** Failed sync checks in a row before a waiting chat stops waiting and says
 *  it cannot reach its owner (#814). */
export const MIRROR_SYNC_FAILURE_LIMIT = 3;
/** A sync check that hasn't answered by then counts as a failed one. */
export const MIRROR_SYNC_REQUEST_TIMEOUT_MS = 10_000;
/** Longest gap between background retries once the owner is unreachable. */
export const MIRROR_SYNC_MAX_BACKOFF_MS = 30_000;

/** Delay before the next poll after `failures` failed checks in a row:
 *  the normal interval, then doubling up to the cap once unreachable. */
export function mirrorSyncRetryDelay(failures: number): number {
	if (failures < MIRROR_SYNC_FAILURE_LIMIT) return MIRROR_SYNC_POLL_MS;
	return Math.min(MIRROR_SYNC_POLL_MS * 2 ** (failures - MIRROR_SYNC_FAILURE_LIMIT + 1), MIRROR_SYNC_MAX_BACKOFF_MS);
}

export type MirrorSyncState = 'checking' | 'syncing' | 'current' | 'offline';

export interface MirrorSyncInput {
	sessionId: string | null;
	/** Whether the chat is a mirror of another worker's (only then is it polled). */
	remote: boolean;
	/** A conversation composed in this view has no pre-existing history to
	 *  open. Keep its local echo visible and follow the live feed, not a reset. */
	createdFromComposer?: boolean;
	/** Fires ONCE per opened session: its opening checkpoint is materialized.
	 *  Freshness is not invalidation: neither this first catch-up nor later
	 *  backlog drains should discard the reducer or cursor. The live feed
	 *  delivers every replicated event; a rewrite has its own `reset`. */
	onCaughtUp: () => void;
	graceMs?: number;
}

export interface MirrorSync {
	/** The chat polled now: a remote chat is open. */
	readonly active: boolean;
	readonly syncState: MirrorSyncState;
	/** Updates still to replicate for this chat (0 when only owner-wide counts are known). */
	readonly pending: number;
	/** The open remote chat has not been revealed yet: its first catch-up (or
	 *  an offline/current answer) is still owed. */
	readonly waiting: boolean;
	/** Behind the owner right now (checking or syncing). */
	readonly behind: boolean;
	/** The "Syncing updates" pill is due (behind for the grace period). */
	readonly noticeVisible: boolean;
	/** The sync check failed MIRROR_SYNC_FAILURE_LIMIT times in a row: the
	 *  chat shows what it has (syncState 'offline') and retries with backoff. */
	readonly unreachable: boolean;
	/** A retry asked for with `retry()` is in flight. */
	readonly retrying: boolean;
	/** Check again now instead of waiting for the next backoff step. */
	retry(): void;
}

export function createMirrorSync(input: () => MirrorSyncInput): MirrorSync {
	let syncState = $state<MirrorSyncState>('checking');
	let pending = $state(0);
	let revealedSession = $state<string | null>(null);
	let noticeVisible = $state(false);
	let unreachable = $state(false);
	let retrying = $state(false);
	let retryNow: (() => void) | null = null;
	// The input may read whole stores (ChatView's $activeSession/$sessions).
	// Equal primitive values must not restart polling when those stores update.
	const sessionId = $derived(input().sessionId);
	const remote = $derived(input().remote);
	const createdFromComposer = $derived(input().createdFromComposer === true);
	// Owner-wide watermarks advance for unrelated chats too. Gate the initial
	// catch-up, then keep the conversation mounted during incremental sync.
	const waiting = $derived(!!sessionId && remote && !createdFromComposer && revealedSession !== sessionId);
	const behind = $derived(syncState === 'syncing' || syncState === 'checking');

	$effect(() => {
		const id = sessionId;
		if (!id || !remote) return;
		const controller = new AbortController();
		let timer: ReturnType<typeof setTimeout>;
		let failures = 0;
		let inFlight = false;
		// A fresh compose already shows the outgoing message; there is no
		// earlier transcript to reveal or reload at the opening checkpoint.
		let caughtUpOnce = createdFromComposer;
		let initialTarget: { seq: number; epoch: string; tail_id: string } | null = null;
		const notice = createConnectionBannerGrace({
			onShow: () => { noticeVisible = true; },
			onHide: () => { noticeVisible = false; },
			graceMs: untrack(() => input().graceMs ?? MIRROR_SYNC_NOTICE_GRACE_MS),
		});
		revealedSession = null;
		noticeVisible = false;
		syncState = 'checking';
		pending = 0;
		unreachable = false;
		retrying = false;
		async function poll() {
			inFlight = true;
			// A stalled request (half-open socket, a proxy that never answers)
			// must count as a failure too, or the chat waits forever (#814).
			const request = new AbortController();
			const abortRequest = () => request.abort();
			controller.signal.addEventListener('abort', abortRequest, { once: true });
			const deadline = setTimeout(abortRequest, MIRROR_SYNC_REQUEST_TIMEOUT_MS);
			try {
				const query = new URLSearchParams();
				if (initialTarget && !caughtUpOnce) {
					query.set('initial_seq', String(initialTarget.seq));
					query.set('initial_epoch', initialTarget.epoch);
					query.set('initial_tail_id', initialTarget.tail_id);
				}
				const suffix = query.size ? `?${query}` : '';
				const response = await fetch(`${apiBase()}/sessions/${encodeURIComponent(id!)}/sync-status${suffix}`, withAuth({ signal: request.signal }));
				if (!response.ok) throw new Error('Sync status unavailable');
				const status = await response.json();
				if (controller.signal.aborted) return;
				if (!['checking', 'syncing', 'current', 'offline'].includes(status.state)) throw new Error('Invalid sync status');
				// Pin an opening checkpoint instead of chasing a continuously writing
				// owner. The server replaces it on an epoch change or truncation.
				if (!caughtUpOnce && status.initial_target) initialTarget = status.initial_target;
				const firstCatchUp = !caughtUpOnce && (status.state === 'current' || status.initial_caught_up === true);
				if (firstCatchUp) caughtUpOnce = true;
				syncState = status.state;
				pending = status.scope === 'owner' ? 0 : (!caughtUpOnce
					? status.initial_pending_updates ?? status.pending_updates ?? 0
					: status.pending_updates ?? 0);
				if (firstCatchUp || status.state === 'current' || status.state === 'offline') revealedSession = id;
				notice.setConnected(status.state !== 'syncing' && status.state !== 'checking');
				failures = 0;
				unreachable = false;
				if (firstCatchUp) untrack(() => input().onCaughtUp());
			} catch {
				// Keep a known backlog visible on a failed poll; never call it current.
				if (controller.signal.aborted) return;
				failures += 1;
				if (failures >= MIRROR_SYNC_FAILURE_LIMIT) {
					// Stop waiting: show what this worker has, like an offline
					// owner, and keep retrying in the background with backoff.
					unreachable = true;
					syncState = 'offline';
					revealedSession = id;
					notice.setConnected(true);
				}
			} finally {
				clearTimeout(deadline);
				controller.signal.removeEventListener('abort', abortRequest);
				inFlight = false;
				retrying = false;
				if (!controller.signal.aborted) timer = setTimeout(poll, mirrorSyncRetryDelay(failures));
			}
		}
		retryNow = () => {
			if (inFlight || controller.signal.aborted) return;
			clearTimeout(timer);
			retrying = true;
			void poll();
		};
		void poll();
		return () => { controller.abort(); clearTimeout(timer); notice.destroy(); retryNow = null; };
	});

	return {
		get active() { return !!sessionId && remote; },
		get syncState() { return syncState; },
		get pending() { return pending; },
		get waiting() { return waiting; },
		get behind() { return behind; },
		get noticeVisible() { return noticeVisible; },
		get unreachable() { return unreachable; },
		get retrying() { return retrying; },
		retry() { retryNow?.(); },
	};
}

/** The waiting line for a mirrored chat: "Checking sync with Unraid…". */
export function mirrorSyncHeadline(state: MirrorSyncState, deviceName: string | null | undefined): string {
	return `${state === 'syncing' ? 'Syncing from' : 'Checking sync with'} ${deviceName || 'the owning worker'}…`;
}
