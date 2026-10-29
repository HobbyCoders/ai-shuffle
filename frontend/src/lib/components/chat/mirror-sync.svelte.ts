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

export type MirrorSyncState = 'checking' | 'syncing' | 'current' | 'offline';

export interface MirrorSyncInput {
	sessionId: string | null;
	/** Whether the chat is a mirror of another worker's (only then is it polled). */
	remote: boolean;
	/** Fires ONCE per opened session: its opening checkpoint is materialized.
	 *  Later drains of a live backlog must not reload the transcript —
	 *  the live SSE feed already delivers every replicated event, and an
	 *  explicit log rewrite reaches the feed as its own `reset` (#2227). */
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
}

export function createMirrorSync(input: () => MirrorSyncInput): MirrorSync {
	let syncState = $state<MirrorSyncState>('checking');
	let pending = $state(0);
	let revealedSession = $state<string | null>(null);
	let noticeVisible = $state(false);
	// The input may read whole stores (ChatView's $activeSession/$sessions).
	// Equal primitive values must not restart polling when those stores update.
	const sessionId = $derived(input().sessionId);
	const remote = $derived(input().remote);
	// The open chat while it ran on this worker. Moved away while open, it
	// keeps what it shows: this worker's own transcript, current at the move,
	// which the live feed carries on from. Gating it as a first open blanked it
	// for a frame and reloaded it. Opening another chat forgets it, so the
	// moved chat is checked like any mirror when it is opened again.
	let ranHere = $state<string | null>(null);
	$effect.pre(() => {
		const id = sessionId;
		if (id && !remote) ranHere = id;
		else if (id !== untrack(() => ranHere)) ranHere = null;
	});
	// Owner-wide watermarks advance for unrelated chats too. Gate the initial
	// catch-up, then keep the conversation mounted during incremental sync.
	const waiting = $derived(!!sessionId && remote && revealedSession !== sessionId && ranHere !== sessionId);
	const behind = $derived(syncState === 'syncing' || syncState === 'checking');

	$effect(() => {
		const id = sessionId;
		if (!id || !remote) return;
		const controller = new AbortController();
		let timer: ReturnType<typeof setTimeout>;
		const movedAway = untrack(() => ranHere) === id;
		// A chat moved away while open has no opening checkpoint to reach.
		let caughtUpOnce = movedAway;
		let initialTarget: { seq: number; epoch: string; tail_id: string } | null = null;
		const notice = createConnectionBannerGrace({
			onShow: () => { noticeVisible = true; },
			onHide: () => { noticeVisible = false; },
			graceMs: untrack(() => input().graceMs ?? MIRROR_SYNC_NOTICE_GRACE_MS),
		});
		revealedSession = movedAway ? id : null;
		noticeVisible = false;
		syncState = 'checking';
		pending = 0;
		async function poll() {
			try {
				const query = new URLSearchParams();
				if (initialTarget && !caughtUpOnce) {
					query.set('initial_seq', String(initialTarget.seq));
					query.set('initial_epoch', initialTarget.epoch);
					query.set('initial_tail_id', initialTarget.tail_id);
				}
				const suffix = query.size ? `?${query}` : '';
				const response = await fetch(`${apiBase()}/sessions/${encodeURIComponent(id!)}/sync-status${suffix}`, withAuth({ signal: controller.signal }));
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
				if (firstCatchUp) untrack(() => input().onCaughtUp());
			} catch {
				// Keep a known backlog visible on a failed poll; never call it current.
			} finally {
				if (!controller.signal.aborted) timer = setTimeout(poll, MIRROR_SYNC_POLL_MS);
			}
		}
		void poll();
		return () => { controller.abort(); clearTimeout(timer); notice.destroy(); };
	});

	return {
		get active() { return !!sessionId && remote; },
		get syncState() { return syncState; },
		get pending() { return pending; },
		get waiting() { return waiting; },
		get behind() { return behind; },
		get noticeVisible() { return noticeVisible; },
	};
}

/** The waiting line for a mirrored chat: "Checking sync with Unraid…". */
export function mirrorSyncHeadline(state: MirrorSyncState, deviceName: string | null | undefined): string {
	return `${state === 'syncing' ? 'Syncing from' : 'Checking sync with'} ${deviceName || 'the owning worker'}…`;
}
