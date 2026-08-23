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
import type { SdkContextUsage } from '$lib/api/sessions';
import type { QueuedMessage, Task } from '../../types';
import { browserPanel } from '../../browser-panel.svelte';

/** MCP browser-tool prefix — a live `tool.use` for one auto-opens the panel. */
const BROWSER_TOOL_PREFIX = 'mcp__browser__';

// ─── Injected writer deps ─────────────────────────────────────────────────────

export interface FeedControllerDeps {
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;
	/** #606 item 4: drop a pending/deferred permission or pending question by
	 *  `request_id` when a durable `interaction.answered` / `interaction.expired`
	 *  event says it was resolved (possibly on another device). Wired to
	 *  activeSession.resolveInteractionByRequestId; a cheap no-op when absent. */
	resolveInteractionByRequestId: (requestId: string) => void;
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
// Whether catch-up has EVER completed for the current feed (across reconnects).
// #611: `onOpen` only re-arms the batch flag on the FIRST connection. Once
// catch-up has happened, reconnect backfills (cursor = lastSeq, near-empty) keep
// `caughtUp === true` so live events fold immediately instead of waiting on the
// throttleable `CATCHUP_FALLBACK_MS` timer in a hidden tab. Reset in start()/stop().
let everCaughtUp = false;
// Safety net for an old backend that never emits `caught_up`: if NO frame has
// arrived for this long, fold whatever has accumulated so the view can never be
// stuck blank. IDLE-based, not a deadline from connect: a long backfill is a
// steady stream of frames, each of which re-arms the timer — flipping to live
// cadence mid-backfill made every remaining history frame repaint individually,
// which surfaced as the transcript visibly replaying (and auto-scroll chasing)
// on large sessions. Cleared on `caught_up`.
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
			// No sentinel AND a full second of silence: give up batching and fold
			// what we have; subsequent events flush per the normal live cadence.
			caughtUp = true;
			everCaughtUp = true;
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
	// Passed through with the same "extra fields may be present" contract the
	// /context-usage fetch uses; the mapper is what validates it.
	const liveContextUsage = (reducerState.contextUsage as SdkContextUsage | null) ?? null;
	deps.update((s) =>
		s.sessionId === currentSessionId
			? {
					...s,
					messages: reducerState.messages,
					isStreaming: reducerState.isStreaming,
					liveTurnId: reducerState.liveTurnId,
					liveModel: reducerState.liveModel,
					liveRequestedModel: reducerState.liveRequestedModel,
					liveProvider: reducerState.liveProvider,
					activeTasks,
					liveContextUsage,
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
	/**
	 * Nudge a possibly-half-open feed: abort the reader and reconnect at the
	 * cursor (from_seq=lastSeq). Unlike start(), this keeps the reducer state —
	 * no teardown, no full re-backfill, no transcript repaint (#637) — and the
	 * resume is gap/duplicate-free (server filter is `seq > from_seq`). Call it
	 * when an out-of-band signal says the network just changed (e.g. the chat WS
	 * reconnected) while isFeeding() is true. No-op unless feeding (optionally:
	 * this specific session).
	 */
	revive: (sessionId?: string) => void;
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
		everCaughtUp = false;
		feed = createSseFeed(sessionId, {
			onOpen: () => {
				// #611: only re-arm the batch flag on the FIRST connection. The
				// initial connect replays the full history backfill terminated by
				// `caught_up`, which must batch into one fold (#636). But a reconnect
				// AFTER catch-up (revive / dropped stream) resumes at cursor=lastSeq —
				// a near-empty backfill that is just the live tail. Re-arming
				// `caughtUp=false` there would re-batch live events behind the
				// throttleable `CATCHUP_FALLBACK_MS` timer, which a hidden tab freezes
				// → transcript stall. So once catch-up has happened, keep
				// `caughtUp === true` so those events fold immediately; a duplicate
				// `caught_up` sentinel from the reconnect backfill just calls fold()
				// again (harmless).
				if (!everCaughtUp) {
					caughtUp = false;
					armCatchupFallback();
				}
			},
			onEvent: (e) => {
				// Only apply events for the session we're feeding.
				if (currentSessionId !== sessionId) return;
				// History/live boundary sentinel: fold the whole backfill in one
				// batch, then switch to the per-frame live cadence below.
				if ((e as { type?: string }).type === 'caught_up') {
					caughtUp = true;
					everCaughtUp = true;
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
				// A frame just arrived while still batching: the stream is alive,
				// so keep waiting for `caught_up` (idle-based fallback above).
				if (!caughtUp) armCatchupFallback();
				reducerState = reduce(reducerState, ev);
				// #606 item 4 — cross-device settle: the permission/question CARD was
				// added on THIS device via the legacy `get_pending_*` WS relay on
				// switch-in, but the user may ANSWER it on ANOTHER device, and no legacy
				// removal frame reaches here. These durable `interaction.*` events are
				// the only signal this device gets, so use them to clear the stale card.
				// Runs for BOTH backfill and live events — removal is idempotent and a
				// cheap no-op when the card isn't present (see the store method), so a
				// backfilled event for an already-gone card does nothing. Card CREATION
				// stays on the legacy relay by design (interaction.requested is not
				// consumed here).
				if (
					(ev.type === 'interaction.answered' || ev.type === 'interaction.expired') &&
					typeof ev.request_id === 'string' &&
					ev.request_id
				) {
					deps?.resolveInteractionByRequestId(ev.request_id);
				}
				// During backfill (!caughtUp) accumulate silently — the single fold
				// happens on `caught_up`. After catch-up, keep the existing
				// incremental per-frame paint so live streaming is unchanged.
					if (
						caughtUp &&
						ev.type === 'tool.use' &&
						typeof ev.name === 'string' &&
						ev.name.startsWith(BROWSER_TOOL_PREFIX)
					) {
						// Auto-open the live browser panel the first time the agent drives
						// a browser tool. LIVE events only (gated on `caughtUp`), so
						// replaying a session's history never pops the panel open;
						// `autoOpenFor` no-ops if the user closed it for this session.
						browserPanel.autoOpenFor(currentSessionId);
					}
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

	revive(sessionId?: string) {
		if (feed === null || currentSessionId === null) return;
		if (sessionId !== undefined && sessionId !== currentSessionId) return;
		feed.revive();
	},

	stop() {
		if (feed) {
			feed.stop();
			feed = null;
		}
		clearScheduledFlush();
		clearCatchupFallback();
		caughtUp = false;
		everCaughtUp = false;
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
