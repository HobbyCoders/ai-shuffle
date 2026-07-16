/**
 * Feed controller — owns the SSE-fed reducer lifecycle for the active v2 session
 * and is the SINGLE writer of `activeSession` for v2_stream sessions.
 *
 * V2 Phase 1 (docs/V2_PHASE1_DESIGN.md §6):
 *   - `start(sessionId)` opens the SSE feed at from_seq=0 (full replay = history
 *     load) and folds reducer state into `activeSession` via ONE `update()` per
 *     SSE batch, coalesced on requestAnimationFrame.
 *   - `stop()` aborts the feed and resets.
 *   - It sets messages / isStreaming / activeTasks / usage on `activeSession`.
 *     `parked` is surfaced through the existing UI convention (isStreaming=false
 *     with a non-empty activeTasks strip), so no new activeSession field is added.
 *
 * Wiring: this is a module singleton. `attach()` injects
 * the private `update`/`getState` from active-session.ts once at boot; the gating
 * modules (session-switcher, ws-message-handlers) import the singleton directly.
 */

import { reduce, initialState, type V2SessionState, type WireEvent } from './reducer';
import { createSseFeed, type SseFeed } from './sse-feed';
import type { ActiveSessionState } from '../../active-session';
import type { QueuedMessage, Task } from '../../types';

// ─── Injected writer deps ─────────────────────────────────────────────────────

export interface FeedControllerDeps {
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;
}

// ─── Module state ─────────────────────────────────────────────────────────────

let deps: FeedControllerDeps | null = null;
let feed: SseFeed | null = null;
let reducerState: V2SessionState = initialState();
let currentSessionId: string | null = null;
let flushScheduled = false;
let rafHandle: number | null = null;
let timerHandle: ReturnType<typeof setTimeout> | null = null;

/**
 * Turn ids of queued messages the server has since DELIVERED, harvested from the
 * SSE transcript (#588). A queued turn's `message.user` event carries the same
 * `turn_id` as the queue entry's id (app/api/chat_ws_handler.py::_v2_queue_view:
 * queue id ← turn id; app/api/events.py::_wire stamps turn_id on every frame), so
 * this set lets `fold()` drop a stale "Queued (N)" chip once its message actually
 * ran.
 *
 * Why this is needed: the turn-scheduler drain path
 * (app/core/turn_scheduler.py::_on_turn_settled_impl → pop_ready → kick) runs a
 * queued turn WITHOUT broadcasting a clearing `queue_updated`, and watch_session
 * suppresses the empty-queue frame, so the queue chip is otherwise never told the
 * queue shrank. Reconciling against the delivered transcript self-heals it.
 *
 * Session-scoped: cleared on start()/stop(). Non-queued turns' ids also land here
 * but never match a chip id (chips exist only for queued turns), so filtering is
 * exact — a chip is removed only when ITS turn produced a user message.
 */
const deliveredTurnIds = new Set<string>();

// History/live boundary. While `false`, backfilled events are reduced WITHOUT
// scheduling a fold, so the whole persisted transcript paints in ONE batch on
// the `caught_up` sentinel instead of repainting per backfilled frame (which
// made the list grow one message at a time on open). Re-armed per SSE connect.
let caughtUp = false;
// Safety net for an old backend that never emits `caught_up`: if the sentinel
// hasn't arrived within this window, fold whatever has accumulated so the view
// can never be stuck blank. Re-armed per connect; cleared on `caught_up`.
let catchupFallbackTimer: ReturnType<typeof setTimeout> | null = null;
const CATCHUP_FALLBACK_MS = 1_000;

function clearCatchupFallback(): void {
	if (catchupFallbackTimer !== null) {
		clearTimeout(catchupFallbackTimer);
		catchupFallbackTimer = null;
	}
}

function armCatchupFallback(): void {
	clearCatchupFallback();
	catchupFallbackTimer = setTimeout(() => {
		catchupFallbackTimer = null;
		if (!caughtUp) {
			// No sentinel arrived (old backend). Give up batching and fold what we
			// have; subsequent events flush per the normal live cadence.
			caughtUp = true;
			fold();
		}
	}, CATCHUP_FALLBACK_MS);
}

// ─── Optimistic-message seeding ───────────────────────────────────────────────

/**
 * Pull any locally-appended optimistic user messages (stamped with a `commandId`
 * by message-sender, or a plain trailing user send) into the reducer BEFORE the
 * next event is reduced, so `message.user`'s reconcile can replace them in place
 * (keeping the id → no Svelte remount) instead of appending a duplicate.
 *
 * Idempotent: a message already present in the reducer (by id, or already
 * reconciled by command_id) is not re-seeded. During a fresh switch-in
 * (activeSession.messages == []) this is a no-op.
 */
function seedOptimistic(): void {
	if (!deps) return;
	const app = deps.getState();
	if (app.sessionId !== currentSessionId) return;
	for (const m of app.messages) {
		if (m.role !== 'user') continue;
		const known = reducerState.messages.some(
			(r) => r.id === m.id || (!!m.commandId && r.commandId === m.commandId)
		);
		if (!known) {
			reducerState = {
				...reducerState,
				messages: [...reducerState.messages, { ...m }],
				isStreaming: true,
			};
		}
	}
}

// ─── Fold reducer state → activeSession (rAF-coalesced) ───────────────────────

function deriveActiveTasks(summaries: Task[]): Task[] {
	// Only currently-running tasks drive the strip / parked indicator
	// (activeTasks contract: running work only).
	return summaries.filter((t) => t.status === 'running');
}

function deriveUsageFields(usage: Record<string, unknown> | null): Partial<ActiveSessionState> {
	if (!usage) return {};
	const num = (k: string): number | undefined =>
		typeof usage[k] === 'number' ? (usage[k] as number) : undefined;
	const inTok = num('input_tokens');
	const outTok = num('output_tokens');
	const cacheCreate = num('cache_creation_input_tokens');
	const cacheRead = num('cache_read_input_tokens');
	// Best-effort context gauge: prefer an explicit field, else sum the input +
	// cache tokens. (The backend `turn.usage` payload shape is not finalized in
	// Phase 1 — see the design §3 note; this maps the conventional Anthropic
	// usage keys and degrades gracefully when absent.)
	const contextUsed =
		num('context_used') ??
		num('context_tokens') ??
		(inTok != null ? inTok + (cacheCreate ?? 0) + (cacheRead ?? 0) : undefined);
	const contextMax = num('context_window') ?? num('context_max');

	const patch: Partial<ActiveSessionState> = {};
	if (inTok != null) patch.totalTokensIn = inTok;
	if (outTok != null) patch.totalTokensOut = outTok;
	if (cacheCreate != null) patch.totalCacheCreationTokens = cacheCreate;
	if (cacheRead != null) patch.totalCacheReadTokens = cacheRead;
	if (contextUsed != null) patch.contextUsed = contextUsed;
	if (contextMax != null) patch.contextMax = contextMax;
	return patch;
}

/**
 * Drop queue chips whose turn has been delivered (#588). Preserves the array
 * identity when nothing changed so the fold stays a no-op for the common case.
 */
function reconcileQueuedMessages(queued: QueuedMessage[]): QueuedMessage[] {
	if (queued.length === 0 || deliveredTurnIds.size === 0) return queued;
	const next = queued.filter((q) => !deliveredTurnIds.has(q.id));
	return next.length === queued.length ? queued : next;
}

function fold(): void {
	if (!deps) return;
	// Guard against a session switch that landed mid-batch.
	if (deps.getState().sessionId !== currentSessionId) return;
	const activeTasks = deriveActiveTasks(reducerState.activeTaskSummaries);
	const usagePatch = deriveUsageFields(reducerState.usage);
	deps.update((s) =>
		s.sessionId === currentSessionId
			? {
					...s,
					messages: reducerState.messages,
					isStreaming: reducerState.isStreaming,
					liveTurnId: reducerState.liveTurnId,
					activeTasks,
					queuedMessages: reconcileQueuedMessages(s.queuedMessages),
					...usagePatch,
				}
			: s
	);
}

function clearScheduledFlush(): void {
	if (rafHandle !== null && typeof cancelAnimationFrame !== 'undefined') {
		cancelAnimationFrame(rafHandle);
	}
	if (timerHandle !== null) {
		clearTimeout(timerHandle);
	}
	rafHandle = null;
	timerHandle = null;
	flushScheduled = false;
}

function scheduleFlush(): void {
	if (flushScheduled) return;
	flushScheduled = true;
	const run = () => {
		// Whichever timer fired first wins the race: cancel the loser (rAF or
		// timeout) and clear the coalescing latch so the next batch can schedule
		// a fresh flush. This makes `run` execute exactly once per schedule even
		// when both handles are pending.
		clearScheduledFlush();
		fold();
	};
	// Race the rAF against a setTimeout fallback. Browsers pause rAF in hidden
	// or fully-occluded tabs, so a pure-rAF schedule would strand
	// `flushScheduled=true` forever — the reducer keeps advancing but the UI
	// never folds, freezing the live view until the tab is refocused/reloaded.
	// The 250ms timer guarantees the fold still runs while backgrounded; when
	// visible, rAF fires first (~16ms) and cancels the timer, preserving the
	// single-flush-per-frame coalescing.
	if (typeof requestAnimationFrame !== 'undefined') {
		rafHandle = requestAnimationFrame(run);
	}
	timerHandle = setTimeout(run, 250);
}

// ─── Public API ───────────────────────────────────────────────────────────────

export interface FeedController {
	/** Inject the active-session writer. Call once at boot. */
	attach: (d: FeedControllerDeps) => void;
	/** Begin feeding a v2 session: full replay from_seq=0, then live tail. */
	start: (sessionId: string) => void;
	/** Abort the feed and reset. */
	stop: () => void;
	/** True when currently feeding (optionally: feeding this specific session). */
	isFeeding: (sessionId?: string) => boolean;
	/** The session currently being fed, or null. */
	currentSessionId: () => string | null;
}

export const feedController: FeedController = {
	attach(d: FeedControllerDeps) {
		deps = d;
	},

	start(sessionId: string) {
		if (!deps) {
			console.warn('[feedController] start() before attach() — ignoring');
			return;
		}
		this.stop();
		currentSessionId = sessionId;
		reducerState = initialState();
		deliveredTurnIds.clear();
		caughtUp = false;
		feed = createSseFeed(sessionId, {
			onOpen: () => {
				// Re-arm the batch flag for THIS connection: the backend replays a
				// fresh backfill (near-empty on a reconnect) terminated by another
				// `caught_up`, so batch it and fold once when the sentinel lands.
				caughtUp = false;
				armCatchupFallback();
			},
			onEvent: (e) => {
				// Only apply events for the session we're feeding.
				if (currentSessionId !== sessionId) return;
				// History/live boundary sentinel: fold the whole backfill in one
				// batch, then switch to the per-frame live cadence below.
				if ((e as { type?: string }).type === 'caught_up') {
					caughtUp = true;
					clearCatchupFallback();
					fold();
					return;
				}
				const ev = e as WireEvent;
				// #588: a delivered queued turn arrives as its own user message; note
				// its turn_id so fold() can clear the matching stale queue chip. Runs
				// for real wire events in BOTH the backfill and live phases.
				if (ev.type === 'message.user' && typeof ev.turn_id === 'string' && ev.turn_id) {
					deliveredTurnIds.add(ev.turn_id);
				}
				seedOptimistic();
				reducerState = reduce(reducerState, ev);
				// During backfill (!caughtUp) accumulate silently — the single fold
				// happens on `caught_up`. After catch-up, keep the existing
				// incremental per-frame paint so live streaming is unchanged.
				if (caughtUp) scheduleFlush();
			},
			onError: (err) => {
				console.debug('[feedController] feed error', err);
			},
		});
		// Arm the no-sentinel safety net even before the first connect resolves.
		armCatchupFallback();
		// FIX B: seed optimistic messages BEFORE the immediate fold. A freshly
		// committed worktree session commits an optimistic user message +
		// isStreaming=true just before start(); folding an empty reducerState here
		// would clobber both to [] / false (surface reads it as 'cleared' → hero
		// flash → SSE replay rebuilds = the double-load). Seeding first carries the
		// optimistic message + streaming flag through this fold; the replayed
		// `message.user` later reconciles it in place by commandId (no duplicate).
		seedOptimistic();
		// Immediate fold so the switch-in clears any transient loading state even
		// before the first frame lands.
		fold();
	},

	stop() {
		if (feed) {
			feed.stop();
			feed = null;
		}
		clearScheduledFlush();
		clearCatchupFallback();
		caughtUp = false;
		currentSessionId = null;
		reducerState = initialState();
		deliveredTurnIds.clear();
	},

	isFeeding(sessionId?: string) {
		if (feed === null || currentSessionId === null) return false;
		return sessionId === undefined || sessionId === currentSessionId;
	},

	currentSessionId() {
		return currentSessionId;
	},
};
