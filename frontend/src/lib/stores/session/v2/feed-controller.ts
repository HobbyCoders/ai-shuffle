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
import type { Task } from '../../types';

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
					activeTasks,
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
		feed = createSseFeed(sessionId, {
			onEvent: (e) => {
				// Only apply events for the session we're feeding.
				if (currentSessionId !== sessionId) return;
				seedOptimistic();
				reducerState = reduce(reducerState, e as WireEvent);
				scheduleFlush();
			},
			onError: (err) => {
				console.debug('[feedController] feed error', err);
			},
		});
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
		currentSessionId = null;
		reducerState = initialState();
	},

	isFeeding(sessionId?: string) {
		if (feed === null || currentSessionId === null) return false;
		return sessionId === undefined || sessionId === currentSessionId;
	},

	currentSessionId() {
		return currentSessionId;
	},
};
