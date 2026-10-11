/**
 * Feed controller — owns the SSE-fed reducer lifecycle for the active v2 session
 * and is the SINGLE writer of `activeSession` for v2_stream sessions.
 *
 * V2 Phase 1:
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

import {
	reduce,
	parseWireEvent,
	initialState,
	releaseMessages,
	sealState,
	type V2SessionState,
	withLogEpoch,
} from './reducer';
import { createSseFeed, type SseFeed } from './sse-feed';
import type { ActiveSessionState } from '../../active-session';
import type { SdkContextUsage } from '$lib/api/sessions';
import type { QueuedMessage, Task } from '../../types';
import { browserPanel } from '../../browser-panel.svelte';
import { pendingQueue, reconcilePendingPeerMessages } from '../pending-queue';
import { swarms } from '../../swarms';

/** MCP browser-tool prefix — a live `tool.use` for one auto-opens the panel. */
const BROWSER_TOOL_PREFIX = 'mcp__browser__';

// ─── Injected writer deps ─────────────────────────────────────────────────────

export interface FeedControllerDeps {
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;
	/** Re-read the authoritative pending question rows after a durable request
	 * event. The feed must not create cards from replayed lifecycle events: an
	 * old requested event may already be answered or expired. */
	requestPendingQuestions: () => void;
	/** Same for permission rows. A permission raised on another worker (a chat
	 * that runs there, or a subagent's relayed prompt) reaches this device only
	 * as this durable event: the live `permission_request` frame stays on the
	 * worker that raised it (#1802). */
	requestPendingPermissions: () => void;
	/** #606 item 4: drop a pending/deferred permission or pending question by
	 *  `request_id` when a durable `interaction.answered` / `interaction.expired`
	 *  event says it was resolved (possibly on another device). Wired to
	 *  activeSession.resolveInteractionByRequestId; a cheap no-op when absent. */
	resolveInteractionByRequestId: (requestId: string) => void;
	/** #1908: the server answered this session's feed with 404/403 — it no
	 *  longer exists or this viewer may not read it. Called once, after the
	 *  feed has stopped for good; the store leaves the session. */
	onSessionUnavailable?: (sessionId: string, status: number) => void;
}

// ─── Module state ─────────────────────────────────────────────────────────────

let deps: FeedControllerDeps | null = null;
let feed: SseFeed | null = null;
let reducerState: V2SessionState = initialState();
let currentSessionId: string | null = null;
let rafHandle: number | null = null;
let timerHandle: ReturnType<typeof setTimeout> | null = null;
let pendingResyncHandle: ReturnType<typeof setTimeout> | null = null;
type InteractionKind = 'permission' | 'question';

/**
 * Delivered user messages are reported to `pendingQueue`, which owns the whole
 * question of which pending bubble survives (see that module's header).
 *
 * Two facts travel from here:
 *
 * - the `turn_id` (#588): a queued turn's `message.user` carries the same id the
 *   queue entry has (app/api/chat_ws_handler.py::_v2_queue_view: queue id ← turn
 *   id), which self-heals the drain path — it runs a queued turn WITHOUT
 *   broadcasting a clearing `queue_updated`.
 * - the content, but only for LIVE events: a message STEERED into a running turn
 *   is recorded on that turn's id, not the queued row's, so the id match cannot
 *   see it and text is the only remaining handle. Backfilled history is not an
 *   answer to anything pending now, so it is reported as non-live.
 */

// History/live boundary. While `false`, backfilled events are reduced WITHOUT
// scheduling a fold, so the whole persisted transcript paints in ONE batch on
// the `caught_up` sentinel instead of repainting per backfilled frame (which
// made the list grow one message at a time on open). Re-armed per SSE connect.
let caughtUp = false;
// Whether catch-up has EVER completed for the current feed (across reconnects).
// `onOpen` only re-arms the batch flag on the FIRST connection. Once catch-up
// has happened, reconnect backfills (cursor = lastSeq, near-empty) keep live
// cadence; the reconnect's duplicate sentinel is harmless.
let everCaughtUp = false;
// True when `caughtUp` was flipped by the hydration floor below rather than by a
// real `caught_up` sentinel — the server still owes us one. A frame arriving in
// this state means the backfill is flowing after all, so we RE-ENTER batch mode
// (see onEvent) instead of repainting the rest of history frame by frame.
let sentinelPending = false;
// A requested event during hydration is only a hint. Hold one dirty bit until
// the authoritative history boundary (or hydration fallback) so a long replay
// cannot issue one WS snapshot request per SSE chunk. Tracked per kind so a
// permission request never costs a question read, and the other way round.
const pendingResyncDirty = new Set<InteractionKind>();
// Events actually reduced on this feed since start(). The hydration floor keys
// off this: publishing "hydration complete" with ZERO reduced events renders an
// empty transcript, which the chat surface classifies as `cleared` — the
// "Conversation cleared" hero for a session that has messages (finding S1).
let eventsReduced = 0;
// Wall-clock start of the current hydration, for the absolute ceiling below.
let hydrationStartedAt = 0;
// Whether this hydration has already published a usable transcript (a cache hit
// publishes one synchronously in start()). When true nothing is waiting on the
// spinner, so the floor below has nothing to rescue.
let transcriptPublished = false;
// Whether the degraded "slow hydration" state has been surfaced (once per feed).
let hydrationSlow = false;
// Every start/stop advances this token. Fetch abort is asynchronous, so a
// stopped reader can still deliver an already-buffered frame; callbacks from
// that obsolete reader must never fold or complete the replacement hydration.
let feedGeneration = 0;
// The `from_seq` this feed's first connect asked for (0 = full replay, >0 = a
// cache resume). The server filter is exclusive (`seq > from_seq`), so an event
// at or below it means the log is not the one the cache was built from.
let resumeFromSeq = 0;
// Whether the feed has published `turn.usage`-derived numbers for this session.
// The `/live` REST snapshot consults this so a slow snapshot cannot overwrite
// fresher feed usage with older DB values (finding W7).
let usageApplied = false;
// Optimistic streaming latch (finding S6). The reducer is the SINGLE source of
// truth for `isStreaming`; this is the ONE sanctioned exception, covering the
// window between "user hit send / a WS `start` landed" and the feed's own
// `turn.started`. The fold ORs it in and CLEARS it the moment the reducer
// confirms a live turn, so a fold that lands between the two can no longer flip
// the indicator back to false (the turn-start stutter).
let optimisticStreaming = false;

// ─── No progressive paint ─────────────────────────────────────────────────────
//
// Hydration is atomic: nothing paints until `caught_up` (or the floor below).
// An earlier interim paint published the messages reduced so far during a long
// backfill; the reader then watched the chat rebuild itself turn by turn, with
// groups folding and unfolding as history streamed past. The loading state is
// the honest view of a load in progress, so a long log keeps it until the
// sentinel and is then revealed in one step.

// ─── Hydration floor ──────────────────────────────────────────────────────────
//
// Atomic hydration (batch until `caught_up`) needs a floor: a backend that never
// emits the sentinel, or a backfill that stalls part-way, would otherwise leave
// the session on its spinner forever with sends blocked behind
// `isRestoringSession`.
//
// The floor is IDLE-based, not a deadline from connect: a long backfill is a
// steady stream of frames, each re-arming the timer. Two budgets, because the
// two silences mean different things:
//
//   - BEFORE the first frame, silence is "slow", not "stalled". `onOpen` fires
//     as soon as the response headers land, but the backend then runs its
//     backfill queries (`ensure_backfilled`, compaction lookup, paged reads)
//     before byte one. On a long session or a loaded server that gap routinely
//     exceeds a second — and folding there published an EMPTY reducer, cleared
//     `isLoadingSession`, and showed the "Conversation cleared" hero for a
//     session full of messages (S1). So a pre-first-frame timeout NEVER
//     completes hydration; it only marks the session as slow (a degraded state
//     the UI can render as "still loading") and keeps waiting.
//   - AFTER frames have arrived, silence means the stream stalled mid-backfill.
//     Publishing what was reduced is strictly better than a blank view, so the
//     old one-second idle budget still applies there.
//
// Beyond both sits an absolute ceiling: a feed that has produced nothing at all
// for that long is dead rather than slow, and holding `isRestoringSession`
// forever would block sends. Hydration is released then, but `sentinelPending`
// stays set so a late backfill re-enters batch mode instead of repainting.
let hydrationTimer: ReturnType<typeof setTimeout> | null = null;
/** Idle budget once frames are flowing (a stalled mid-backfill stream). */
export const CATCHUP_FALLBACK_MS = 1_000;
/** Budget before the FIRST frame of a hydration — "slow" is not "stalled". */
export const FIRST_FRAME_BUDGET_MS = 8_000;
/** Absolute ceiling on a hydration that never produced a single event. */
export const HYDRATION_CEILING_MS = 30_000;

function clearCatchupFallback(): void {
	if (hydrationTimer !== null) {
		clearTimeout(hydrationTimer);
		hydrationTimer = null;
	}
}

function armCatchupFallback(generation: number): void {
	clearCatchupFallback();
	const budget = eventsReduced === 0 ? FIRST_FRAME_BUDGET_MS : CATCHUP_FALLBACK_MS;
	hydrationTimer = setTimeout(() => {
		hydrationTimer = null;
		// A stop() or a replacement start() since arming owns the view now.
		if (generation !== feedGeneration) return;
		if (caughtUp) return;

		if (eventsReduced === 0) {
			// Nothing has been reduced: there is no transcript to publish, only an
			// empty one to mis-render. Stay in the loading state and say "slow".
			markHydrationSlow();
			if (!transcriptPublished && Date.now() - hydrationStartedAt >= HYDRATION_CEILING_MS) {
				// Dead, not slow. Release so sends stop waiting; a frame arriving
				// later re-enters batch mode via `sentinelPending`.
				completeHydration(true);
				return;
			}
			armCatchupFallback(generation);
			return;
		}

		// Frames arrived and then the stream went quiet without a sentinel: fold
		// what we have so the view can never be stuck blank. `true` because this
		// stands in for the `caught_up` fold — the sentinel is the only other
		// thing that clears isLoadingSession / isRestoringSession, so a fallback
		// that folded messages but left those flags set would still hold the
		// spinner up and keep sendMessage waiting.
		completeHydration(true);
	}, budget);
}

/**
 * Surface the degraded "still hydrating, taking a while" state exactly once per
 * feed. Distinct from `isLoadingSession`, which stays TRUE throughout: this only
 * tells the UI that the wait is abnormal, never that it is over.
 */
function markHydrationSlow(): void {
	if (hydrationSlow || transcriptPublished || !deps) return;
	hydrationSlow = true;
	deps.update((s) =>
		s.sessionId === currentSessionId && !s.isHydrationSlow
			? { ...s, isHydrationSlow: true }
			: s
	);
}

/**
 * Declare hydration finished and publish. `premature` marks a floor-driven
 * completion (no `caught_up` seen): the sentinel is still owed, so any further
 * frame re-enters batch mode rather than repainting history one frame at a time.
 */
function completeHydration(premature: boolean): void {
	caughtUp = true;
	everCaughtUp = true;
	sentinelPending = premature;
	clearCatchupFallback();
	fold(true);
	// A floor-driven completion intentionally leaves sentinelPending=true, but
	// the durable rows are still authoritative now; force this one recovery read.
	schedulePendingResync(currentSessionId, feedGeneration, true);
	if (!premature) captureTranscript();
}

// ─── Transcript cache (finding S2) ────────────────────────────────────────────
//
// Switching A → B → A used to re-download and re-replay B's and A's ENTIRE event
// logs from seq 0 each way. This caches the reducer state — not the raw event
// log — per session, so a switch back folds the exact transcript synchronously
// and the feed resumes at `from_seq=lastSeq` instead of replaying from zero.
//
// WHY THIS IS EXACT, not a "stale snapshot" (the hazard that motivated removing
// cache-first rendering in session-switcher.ts):
//
//   1. `session_events` is APPEND-ONLY with a dense, monotonic per-session
//      `seq`. Even a transcript rewind is an appended `transcript.rewound`
//      event (app/core/turn_recorder.py), never a row deletion — so a resume
//      REPLAYS the rewind and the reducer truncates exactly as a full replay
//      would.
//   2. The server's backfill filter is `seq > from_seq` (app/api/events.py),
//      gap-free and duplicate-free. This is the same contract `revive()`
//      already depends on.
//   3. Therefore `reduce*(events where seq > lastSeq, cachedState)` is
//      identical to a full replay, and the cached state is exact AS OF
//      `lastSeq` — not an approximation of it.
//
// INVALIDATION RULES (all implemented below):
//
//   R1. Only a hydration completed by a REAL `caught_up` sentinel is cached. A
//       floor-driven (premature/partial) publish is never stored, so a degraded
//       load can never become the base of a future resume.
//   R2. `restart(sessionId)` drops the entry. That is the explicit "hydrate
//       from zero again" path used by rewind / reload-session.
//   R3. `invalidate(sessionId)` drops it on demand — for session delete, fork,
//       or any out-of-band signal that the log was rewritten. The
//       `user_session_cleared` / `user_session_deleted` WS broadcasts
//       (ws-message-handlers.ts) are the cross-device drivers: an in-place
//       clear or a delete elsewhere rewrites/removes the log WITHOUT appending
//       anything a resume cursor could see, so only this signal invalidates.
//   R4. Cursor sanity: because the server filter is exclusive, the first event
//       of a resumed connection MUST carry `seq > from_seq`. If one arrives at
//       or below the cursor, the log is not the one this entry was built from
//       (restored backup, seq reset, a fork that reused the id) — the entry is
//       dropped and the session re-hydrates from seq 0.
//   R5. Age bound: an entry older than TRANSCRIPT_CACHE_TTL_MS is ignored and
//       dropped. Belt-and-braces against anything the four rules above do not
//       model; the cost of being wrong is a full replay, i.e. today's behavior.
//   R6. Bounded memory: LRU-capped at TRANSCRIPT_CACHE_MAX sessions, storing
//       reducer state (which shares the already-rendered message objects), never
//       raw event logs.
//
// NOT invalidation cases, deliberately:
//   - COMPACTION. A fresh (`from_seq=0`) load may start after a compacted
//     prefix and synthesise a `system.compaction_boundary` marker; a resume
//     (`from_seq>0`) never elides. So a session compacted while the user was
//     away keeps the FULL transcript this tab already had, which is strictly
//     more history than a fresh load would show — not stale content.
//   - FORK. A fork mints a NEW session id and leaves the source log untouched,
//     so it lands on a different cache key.

interface CachedTranscript {
	/** Sealed reducer state, exact as of `lastSeq`. */
	state: V2SessionState;
	/** Highest event seq folded into `state` — the resume cursor. */
	lastSeq: number;
	/** Whether the feed had already published usage numbers (finding W7). */
	usageApplied: boolean;
	/** Server log epoch `lastSeq` belongs to, sent on resume so a rewritten log
	 *  answers with `reset`. Null when the server reports none (older servers):
	 *  such an entry cannot be verified on resume. */
	epoch: string | null;
	cachedAt: number;
}

export const TRANSCRIPT_CACHE_MAX = 8;
export const TRANSCRIPT_CACHE_TTL_MS = 30 * 60_000;

/** Insertion-ordered = LRU order (re-inserted on every read/write). */
const transcriptCache = new Map<string, CachedTranscript>();

function readCachedTranscript(sessionId: string): CachedTranscript | null {
	const entry = transcriptCache.get(sessionId);
	if (!entry) return null;
	if (Date.now() - entry.cachedAt > TRANSCRIPT_CACHE_TTL_MS) {
		transcriptCache.delete(sessionId); // R5
		return null;
	}
	// Touch for LRU.
	transcriptCache.delete(sessionId);
	transcriptCache.set(sessionId, entry);
	return entry;
}

/**
 * Store the CURRENT reducer state for the session being fed. Only ever called
 * once hydration completed on a real sentinel (R1), so a partial transcript can
 * never be cached. The state is sealed: the reducer copies on its next write
 * instead of mutating what the cache (and the rendered view) hold.
 */
function captureTranscript(): void {
	if (!currentSessionId) return;
	if (!everCaughtUp || sentinelPending) return; // R1
	if (reducerState.lastSeq <= 0) return; // nothing durable to resume from
	transcriptCache.delete(currentSessionId);
	transcriptCache.set(currentSessionId, {
		state: sealState(reducerState),
		lastSeq: reducerState.lastSeq,
		usageApplied,
		epoch: feed?.epoch ?? null,
		cachedAt: Date.now(),
	});
	while (transcriptCache.size > TRANSCRIPT_CACHE_MAX) {
		const oldest = transcriptCache.keys().next().value; // R6
		if (oldest === undefined) break;
		transcriptCache.delete(oldest);
	}
}

/** Test/diagnostic hook: how many sessions currently hold a cached transcript. */
export function cachedTranscriptCount(): number {
	return transcriptCache.size;
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
	// Runs before EVERY reduced SSE event, so it must not be O(user rows x
	// reducer rows): index the reducer's ids/command ids once per call (#1282).
	let knownIds: Set<string> | null = null;
	let knownCommandIds: Set<string> | null = null;
	for (let i = 0; i < app.messages.length; i++) {
		const m = app.messages[i];
		if (m.role !== 'user') continue;
		if (!knownIds || !knownCommandIds) {
			knownIds = new Set();
			knownCommandIds = new Set();
			for (const r of reducerState.messages) {
				knownIds.add(r.id);
				if (r.commandId) knownCommandIds.add(r.commandId);
			}
		}
		const known = knownIds.has(m.id) || (!!m.commandId && knownCommandIds.has(m.commandId));
		if (!known) {
			knownIds.add(m.id);
			if (m.commandId) knownCommandIds.add(m.commandId);
			reducerState = {
				...reducerState,
				messages: [...reducerState.messages, { ...m }],
			};
			// An un-reconciled optimistic user message means a send is in flight.
			// Expressed as the optimistic LATCH rather than a lie in the reducer
			// state, so the fold clears it the moment `turn.started` lands (S6).
			optimisticStreaming = true;
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
	if (cacheCreate != null) patch.totalCacheCreationTokens = cacheCreate;
	if (cacheRead != null) patch.totalCacheReadTokens = cacheRead;
	if (contextUsed != null) patch.contextUsed = contextUsed;
	if (contextMax != null) patch.contextMax = contextMax;
	return patch;
}

/**
 * Drop pending messages the transcript has since answered. Preserves the array
 * identity when nothing changed so the fold stays a no-op for the common case.
 */
function reconcileQueuedMessages(queued: QueuedMessage[]): QueuedMessage[] {
	if (queued.length === 0) return queued;
	return pendingQueue.reconcile(currentSessionId, queued);
}

/**
 * The patch a reducer state publishes into `activeSession`. Shared by the live
 * fold and by `cachedPatch()` (which primes a switch-in from the transcript
 * cache in the switcher's SINGLE update, so there is no empty flash between the
 * reset and the first fold).
 *
 * `isStreaming` is computed here and NOWHERE else: the reducer's value OR the
 * optimistic latch (S6). `optimisticStreaming` is cleared as soon as the reducer
 * confirms a live turn, which is what stops a fold landing between a WS `start`
 * and `turn.started` from flipping the indicator back to false.
 */
function transcriptPatch(
	rs: V2SessionState,
	opts: { hydrationComplete: boolean; appOptimistic: boolean }
): Partial<ActiveSessionState> {
	const optimistic = optimisticStreaming || opts.appOptimistic;
	const usagePatch = deriveUsageFields(rs.usage);
	// The Output row is a session total: sum completed turns, never the latest
	// per-call `turn.usage` output. Until a turn completes on this feed the
	// value from `/live` (the stored session total) stays in place.
	if ((rs.outputTokensTotal ?? 0) > 0) usagePatch.totalTokensOut = rs.outputTokensTotal;
	return {
		// Released: the array leaves the reducer here, so the next append copies
		// instead of mutating what the view now holds (reducer.ts ownership).
		messages: releaseMessages(rs),
		isStreaming: rs.isStreaming || optimistic,
		optimisticStreaming: rs.isStreaming ? false : optimistic,
		liveTurnId: rs.liveTurnId,
		turnParked: rs.parked,
		liveModel: rs.liveModel,
		liveRequestedModel: rs.liveRequestedModel,
		liveProvider: rs.liveProvider,
		liveCompaction: rs.compaction ?? null,
		activeTasks: deriveActiveTasks(rs.activeTaskSummaries),
		// Passed through with the same "extra fields may be present" contract the
		// /context-usage fetch uses; the mapper is what validates it.
		liveContextUsage: (rs.contextUsage as SdkContextUsage | null) ?? null,
		...(opts.hydrationComplete
			? { isLoadingSession: false, isRestoringSession: false, isHydrationSlow: false }
			: {}),
		...usagePatch,
	};
}

function fold(hydrationComplete = false): void {
	if (!deps) return;
	// Guard against a session switch that landed mid-batch.
	if (deps.getState().sessionId !== currentSessionId) return;
	hiddenStreamDirty = false;
	const patch = transcriptPatch(reducerState, {
		hydrationComplete,
		appOptimistic: deps.getState().optimisticStreaming === true,
	});
	// The reducer confirmed a live turn — the optimistic flip has served its
	// purpose and must not survive into the next turn boundary.
	if (reducerState.isStreaming) optimisticStreaming = false;
	// W7: from the first fold that carries `turn.usage`-derived numbers, the feed
	// owns the usage fields and a late `/live` snapshot must not overwrite them.
	// Set HERE, not in transcriptPatch, so a mere cache peek for another session
	// can never claim ownership on the fed session's behalf.
	if (reducerState.usage !== null) usageApplied = true;
	if (hydrationComplete) transcriptPublished = true;
	deps.update((s) =>
		s.sessionId === currentSessionId
			? {
					...s,
					...patch,
					queuedMessages: reconcileQueuedMessages(s.queuedMessages),
					pendingPeerMessages: reconcilePendingPeerMessages(s.pendingPeerMessages ?? [], patch.messages ?? s.messages),
				}
			: s
	);
}

// ─── Flush scheduling ─────────────────────────────────────────────────────────
//
// Two cadences, because two kinds of event have different latency needs:
//
//   - STREAMING deltas are throttled to one fold per STREAM_FOLD_MS. Nobody can
//     read text repainting at 60fps, and a per-frame fold re-renders the whole
//     subscriber graph; halving that is most of the win on a weak machine.
//   - EVERYTHING ELSE keeps the original rAF + 250ms cadence, so a permission
//     prompt, a tool result or a turn ending still appears on the next frame.
//     A non-streaming event arriving while a throttled fold is pending UPGRADES
//     the pending schedule instead of waiting out the throttle.
//
// The 250ms timer exists because browsers pause rAF in hidden or fully-occluded
// tabs: lifecycle/permission updates must still reach non-rendering subscribers.
// Streaming-only publication is deferred while hidden, without pausing SSE intake
// or reduction, and catches up once on visibility. No event buffer is retained.
export const STREAM_FOLD_MS = 50;
const THROTTLED_EVENT_TYPES = new Set([
	'assistant.text_delta',
	'assistant.thinking_delta',
	'subagent.progress',
]);

type FlushMode = 'none' | 'throttled' | 'immediate';
let scheduledMode: FlushMode = 'none';
let lastFoldAt = 0;
let hiddenStreamDirty = false;

function isHidden(): boolean {
	return typeof document !== 'undefined' && document.hidden;
}

function onVisibilityChange(): void {
	if (isHidden()) {
		if (scheduledMode === 'throttled') {
			clearScheduledFlush();
			hiddenStreamDirty = true;
		}
	} else if (hiddenStreamDirty) {
		scheduleFlush('throttled');
	}
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
	scheduledMode = 'none';
}

function scheduleFlush(mode: Exclude<FlushMode, 'none'>): void {
	if (mode === 'throttled' && isHidden()) {
		hiddenStreamDirty = true;
		return;
	}
	// An immediate schedule already covers anything a throttled one would do.
	if (scheduledMode === 'immediate') return;
	if (scheduledMode === mode) return;
	// throttled → immediate: cancel the throttle and reschedule at once.
	if (scheduledMode === 'throttled') clearScheduledFlush();

	const run = () => {
		// Whichever timer fired first wins the race: cancel the loser (rAF or
		// timeout) and clear the coalescing latch so the next batch can schedule
		// a fresh flush. This makes `run` execute exactly once per schedule even
		// when both handles are pending.
		clearScheduledFlush();
		// Visibility can change before its event is dispatched (or while a
		// callback is already queued), so check at execution as well.
		if (mode === 'throttled' && isHidden()) {
			hiddenStreamDirty = true;
			return;
		}
		lastFoldAt = Date.now();
		fold();
	};

	scheduledMode = mode;
	if (mode === 'throttled') {
		// Clamped both ways: a clock that jumped backwards (or a fold clock left
		// over from another feed) must never stretch the throttle window.
		const elapsed = Date.now() - lastFoldAt;
		const wait = elapsed < 0 || elapsed >= STREAM_FOLD_MS ? 0 : STREAM_FOLD_MS - elapsed;
		timerHandle = setTimeout(run, wait);
		return;
	}
	if (typeof requestAnimationFrame !== 'undefined') {
		rafHandle = requestAnimationFrame(run);
	}
	timerHandle = setTimeout(run, 250);
}

// ─── Public API ───────────────────────────────────────────────────────────────

/**
 * The ONLY sanctioned way to flip `isStreaming` on from outside the fold
 * (finding S6). Spread into an `activeSession` update when the user has just
 * sent — or a WS `start` has landed — and the event feed has not yet reported
 * `turn.started`.
 *
 * `isStreaming` here is not a competing opinion: it is exactly what the next
 * fold computes for this state (`reducer.isStreaming || optimisticStreaming`),
 * written locally so the indicator does not wait for a fold that may not be
 * imminent (a brand-new chat has no feed yet). The fold then CLEARS
 * `optimisticStreaming` the moment the reducer confirms the turn — which is
 * what stops the flip-flop that made the indicator stutter at turn start.
 */
export const OPTIMISTIC_STREAMING_ON = {
	isStreaming: true,
	optimisticStreaming: true,
} as const;

export interface FeedController {
	/** Inject the active-session writer. Call once at boot. */
	attach: (d: FeedControllerDeps) => void;
	/**
	 * Begin feeding a v2 session. Idempotent for the session already feeding.
	 *
	 * Resumes from the transcript cache when one is held for this session: the
	 * cached (exact) state is folded synchronously — no spinner, no empty flash —
	 * and the SSE feed opens at `from_seq=lastSeq` instead of replaying the whole
	 * log. Falls back to a full `from_seq=0` replay on a cache miss.
	 */
	start: (sessionId: string) => void;
	/** Explicitly discard reducer state AND the cached transcript, and hydrate
	 *  this session again from seq 0 (rewind / reload-session path). */
	restart: (sessionId: string) => void;
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
	/** Abort the feed and reset (caching the current transcript for resume). */
	stop: () => void;
	/** True when currently feeding (optionally: feeding this specific session). */
	isFeeding: (sessionId?: string) => boolean;

	// ── Transcript cache (S2) ──────────────────────────────────────
	/**
	 * The `activeSession` patch that a cached transcript would publish for this
	 * session, or null when nothing is cached. Lets the session-switcher prime
	 * its SINGLE switch-in update with the real transcript, so the reset never
	 * paints an empty list first. Peek only — `start()` adopts the same entry.
	 */
	cachedPatch: (sessionId: string) => Partial<ActiveSessionState> | null;
	/** Whether a resumable transcript is held for this session. */
	hasCachedTranscript: (sessionId: string) => boolean;
	/** Drop one session's cached transcript (delete / fork / out-of-band
	 *  rewrite), or the whole cache when called with no argument. */
	invalidate: (sessionId?: string) => void;
	/** Drop only cached transcripts without a server log epoch. Entries with one
	 *  are checked by the server when they resume (a rewritten log answers with
	 *  `reset`), so a missed out-of-band signal cannot leave them stale. */
	invalidateUnverified: () => void;
	/** Whether the feed for this session knows its server log epoch, so a
	 *  `revive()` resume is checked against a rewritten log. False on servers
	 *  that report none, and before the first `caught_up` of a fresh load. */
	hasLogEpoch: (sessionId: string) => boolean;

	// ── Field ownership (W7) ───────────────────────────────────────
	/**
	 * Whether the feed has already published `turn.usage`-derived numbers for
	 * this session. The `/live` REST snapshot must not overwrite those with
	 * older DB values when it lands late.
	 */
	hasAppliedUsage: (sessionId: string) => boolean;

	/** The reducer's own current `isStreaming` for a session (false when it is
	 *  not the session being fed). Lets a caller that must withdraw an optimistic
	 *  flip restore the authoritative value instead of guessing `false` and
	 *  killing the indicator for a turn that is genuinely live. */
	isReducerStreaming: (sessionId: string | null) => boolean;

	// ── Optimistic streaming (S6) ──────────────────────────────────
	/**
	 * Declare that a send is in flight for this session before the server has
	 * confirmed a turn. The fold ORs this into `isStreaming` and clears it as
	 * soon as the reducer reports a live turn — the sanctioned alternative to
	 * writing `isStreaming` directly from anywhere but the reducer.
	 */
	noteOptimisticSend: (sessionId: string | null) => void;
	/** Withdraw the optimistic flip (send failed, stop confirmed, turn settled). */
	clearOptimisticSend: (sessionId: string | null) => void;
}

/** Reset every per-feed flag. Shared by start() and teardown(). */
function resetFeedFlags(): void {
	caughtUp = false;
	everCaughtUp = false;
	sentinelPending = false;
	pendingResyncDirty.clear();
	eventsReduced = 0;
	transcriptPublished = false;
	hydrationSlow = false;
	usageApplied = false;
	resumeFromSeq = 0;
}

/**
 * Tear the feed down. `capture` stores the current transcript for a later
 * resume; the restart path passes false because it is explicitly discarding it.
 */
function teardown(capture: boolean): void {
	if (capture) captureTranscript();
	feedGeneration++;
	if (feed) {
		feed.stop();
		feed = null;
	}
	clearScheduledFlush();
	if (typeof document !== 'undefined') {
		document.removeEventListener('visibilitychange', onVisibilityChange);
	}
	hiddenStreamDirty = false;
	clearCatchupFallback();
	if (pendingResyncHandle !== null) {
		clearTimeout(pendingResyncHandle);
		pendingResyncHandle = null;
	}
	// The fold clock belongs to a feed, not to the module: a fresh feed must not
	// inherit the previous session's throttle window.
	lastFoldAt = 0;
	resetFeedFlags();
	optimisticStreaming = false;
	currentSessionId = null;
	reducerState = initialState();
	pendingQueue.reset(null);
}

/**
 * A durable requested event is a recovery hint, not a card payload. Coalesce
 * bursts (and replayed history) into one authoritative WS snapshot per kind so
 * a late event cannot resurrect a prompt that has already been answered/expired.
 */
function schedulePendingResync(sessionId: string | null, generation: number, force = false): void {
	if (!sessionId || pendingResyncDirty.size === 0) return;
	if (!force && (!caughtUp || sentinelPending)) return;
	if (pendingResyncHandle !== null) return;
	pendingResyncHandle = setTimeout(() => {
		pendingResyncHandle = null;
		if (generation !== feedGeneration || currentSessionId !== sessionId) return;
		const kinds = [...pendingResyncDirty];
		pendingResyncDirty.clear();
		if (kinds.includes('permission')) deps?.requestPendingPermissions();
		if (kinds.includes('question')) deps?.requestPendingQuestions();
	}, 0);
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
		// Lifecycle signals for one session can arrive through the switcher, WS
		// start/session_created, and reconnect watchdog. They all mean "ensure a
		// feed exists", not "throw away hydration and replay from zero again".
		if (feed !== null && currentSessionId === sessionId) return;
		teardown(true);
		const generation = ++feedGeneration;
		currentSessionId = sessionId;
		if (typeof document !== 'undefined') {
			document.addEventListener('visibilitychange', onVisibilityChange);
		}
		resetFeedFlags();
		hydrationStartedAt = Date.now();

		// S2: resume from the cached transcript when we hold one.
		const cached = readCachedTranscript(sessionId);
		reducerState = cached ? cached.state : initialState();
		resumeFromSeq = cached ? cached.lastSeq : 0;
		usageApplied = cached ? cached.usageApplied : false;

		pendingQueue.reset(sessionId);
		feed = createSseFeed(sessionId, {
			fromSeq: resumeFromSeq,
			epoch: cached ? cached.epoch : null,
			onOpen: () => {
				if (generation !== feedGeneration) return;
				swarms.refreshKnown(sessionId);
				// Initial hydration remains atomic until the backend's `caught_up`
				// sentinel, floored by the idle fallback. Reconnects after that
				// point resume at lastSeq and keep live cadence; they never reset
				// the reducer, and must not re-batch live events behind the timer.
				if (!everCaughtUp) {
					caughtUp = false;
					armCatchupFallback(generation);
				}
			},
			onEvent: (e) => {
				// Rendered assistant texts carry the log epoch their seq belongs to
				// (#2249). The feed learns it at `caught_up` (or, for an empty log,
				// from the first row); '0' is the empty-log placeholder, not an epoch.
				const adoptLogEpoch = () => {
					const epoch = feed?.epoch ?? null;
					if (epoch && epoch !== '0') reducerState = withLogEpoch(reducerState, epoch);
				};
				// Only the feed generation that owns this hydration may mutate it.
				if (generation !== feedGeneration || currentSessionId !== sessionId) return;
				const type = (e as { type?: string }).type;
				// History/live boundary sentinel: fold the whole backfill in one
				// batch, then switch to the per-frame live cadence below.
				if (type === 'caught_up') {
					adoptLogEpoch();
					caughtUp = true;
					everCaughtUp = true;
					sentinelPending = false;
					clearCatchupFallback();
					fold(true);
					schedulePendingResync(sessionId, generation);
					// Hydration finished on a REAL sentinel — the only state exact
					// enough to cache (invalidation rule R1).
					captureTranscript();
					return;
				}
				// The server rewrote this log since the epoch our resume cursor came
				// from (cleared, restored, reseeded), so neither the cursor nor the
				// state built on it means anything. Reload it from seq 0.
				if (type === 'reset') {
					console.info(
						`[feedController] event log for ${sessionId} changed on the server; reloading it`
					);
					transcriptCache.delete(sessionId);
					feedController.restart(sessionId);
					return;
				}
				// Optional server preamble ("I am alive, backfill is coming"). The
				// client tolerates its absence — it is purely a liveness signal that
				// re-arms the pre-first-frame budget — and must never be reduced or
				// counted as a transcript event.
				if (type === 'hydrating') {
					if (!caughtUp) armCatchupFallback(generation);
					return;
				}
				const ev = parseWireEvent(e);
				if (!ev) {
					console.warn('[feedController] Ignoring malformed session event envelope');
					return;
				}
				// R4 cursor sanity: the server filter is `seq > from_seq`, so a
				// resumed connection can never legitimately deliver an event at or
				// below the cursor. If it does, this log is not the one the cached
				// state was built from — drop the cache and replay from zero.
				if (
					resumeFromSeq > 0 &&
					!everCaughtUp &&
					typeof ev.seq === 'number' &&
					ev.seq <= resumeFromSeq
				) {
					console.warn(
						`[feedController] resumed feed for ${sessionId} delivered seq ${ev.seq} ` +
							`at cursor ${resumeFromSeq} — cached transcript is not this log; ` +
							'discarding it and replaying from seq 0'
					);
					transcriptCache.delete(sessionId);
					feedController.restart(sessionId);
					return;
				}
				// A user message on the transcript is the answer to a pending bubble —
				// by turn id for a drained queue row (#588), by text for one steered
				// into the running turn (whose event carries the LIVE turn's id).
				// Reported in both phases; only live events count as a delivery.
				if (ev.type === 'message.user') {
					pendingQueue.noteDelivered(
						currentSessionId,
						typeof ev.content === 'string' ? ev.content : '',
						typeof ev.turn_id === 'string' && ev.turn_id ? ev.turn_id : undefined,
						caughtUp
					);
				}
				seedOptimistic();
				// A frame arrived after the floor gave up waiting: the backfill is
				// flowing after all, so RE-ENTER batch mode (S1). Without this every
				// remaining history frame repaints individually — the transcript
				// visibly rebuilding message by message.
				if (caughtUp && sentinelPending) caughtUp = false;
				if (ev.type === 'swarm.updated') swarms.receive(ev.swarm, sessionId);
				reducerState = reduce(reducerState, ev);
				// An empty log learns its epoch from this first row (sse-feed).
				adoptLogEpoch();
				eventsReduced++;
				if (
					ev.type === 'interaction.requested' &&
					ev.session_id === sessionId &&
					(ev.kind === 'question' || ev.kind === 'permission') &&
					// The worker that raised a permission also sends it live; a card
					// already shown needs no re-read.
					!(ev.kind === 'permission' &&
						deps?.getState().pendingPermissions?.some((p) => p.request_id === ev.request_id))
				) {
					pendingResyncDirty.add(ev.kind);
					schedulePendingResync(sessionId, generation);
				}
				// A frame just arrived while still batching: the stream is alive,
				// so keep waiting for `caught_up`. Re-armed AFTER counting the
				// event so the budget switches from the pre-first-frame one to the
				// tighter mid-backfill idle budget.
				if (!caughtUp) armCatchupFallback(generation);
				// #606 item 4 — cross-device settle: the permission/question CARD was
				// added on THIS device via the legacy `get_pending_*` WS relay on
				// switch-in, but the user may ANSWER it on ANOTHER device, and no legacy
				// removal frame reaches here. These durable `interaction.*` events are
				// the only signal this device gets, so use them to clear the stale card.
				// Runs for BOTH backfill and live events — removal is idempotent and a
				// cheap no-op when the card isn't present (see the store method), so a
				// backfilled event for an already-gone card does nothing. Card CREATION
				// stays on the legacy relay by design (interaction.requested is not
				// used to reconstruct a card; it only triggers an authoritative resync,
				// above, through that relay).
				if (
					(ev.type === 'interaction.answered' || ev.type === 'interaction.expired') &&
					typeof ev.request_id === 'string' &&
					ev.request_id
				) {
					deps?.resolveInteractionByRequestId(ev.request_id);
				}
				// During backfill (!caughtUp) accumulate — the complete fold happens
				// on `caught_up`, however long the backfill runs. After
				// catch-up, fold on the live cadence: throttled for streaming deltas,
				// next-frame for everything else.
				if (!caughtUp) return;
				if (
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
				scheduleFlush(
					THROTTLED_EVENT_TYPES.has(String(ev.type)) ? 'throttled' : 'immediate'
				);
			},
			onUnavailable: (status) => {
				if (generation !== feedGeneration || currentSessionId !== sessionId) return;
				console.info(`[feedController] session ${sessionId} is unavailable (HTTP ${status}); leaving it`);
				transcriptCache.delete(sessionId);
				teardown(false);
				deps?.onSessionUnavailable?.(sessionId, status);
			},
			onError: (err) => {
				if (generation !== feedGeneration) return;
				console.debug('[feedController] feed error', err);
				// A real transport error, unlike silence, is evidence the feed is
				// not merely slow. Once past the first-frame budget with nothing
				// reduced and nothing published, release hydration so sends stop
				// waiting behind isRestoringSession — but keep the sentinel owed, so
				// a successful reconnect re-enters batch mode instead of repainting.
				if (
					!caughtUp &&
					eventsReduced === 0 &&
					!transcriptPublished &&
					Date.now() - hydrationStartedAt >= FIRST_FRAME_BUDGET_MS
				) {
					completeHydration(true);
				}
			},
		});
		armCatchupFallback(generation);
		// FIX B: seed optimistic messages BEFORE the immediate fold. A freshly
		// committed worktree session commits an optimistic user message +
		// isStreaming=true just before start(); folding an empty reducerState here
		// would clobber both to [] / false (surface reads it as 'cleared' → hero
		// flash → SSE replay rebuilds = the double-load). Seeding first carries the
		// optimistic message + streaming flag through this fold; the replayed
		// `message.user` later reconciles it in place by commandId (no duplicate).
		seedOptimistic();
		// On a cache hit the transcript published here is EXACT as of the resume
		// cursor, so hydration is complete for everything the user can see: fold
		// with `true` and the spinner never appears. On a miss this only clears
		// stale transcript state, leaving loading/restoring true until the
		// matching caught_up folds the final view.
		fold(cached !== null);
	},

	restart(sessionId: string) {
		if (!deps) return;
		// R2: an explicit re-hydration invalidates the cached transcript — the
		// caller is saying the state it was built from is no longer trustworthy.
		transcriptCache.delete(sessionId);
		deps.update((s) =>
			s.sessionId === sessionId
				? { ...s, messages: [], isLoadingSession: true, isHydrationSlow: false }
				: s
		);
		teardown(false);
		this.start(sessionId);
	},

	revive(sessionId?: string) {
		if (feed === null || currentSessionId === null) return;
		if (sessionId !== undefined && sessionId !== currentSessionId) return;
		feed.revive();
	},

	stop() {
		teardown(true);
	},

	isFeeding(sessionId?: string) {
		if (feed === null || currentSessionId === null) return false;
		return sessionId === undefined || sessionId === currentSessionId;
	},

	cachedPatch(sessionId: string) {
		const entry = readCachedTranscript(sessionId);
		if (!entry) return null;
		// The entry is sealed, so publishing its arrays is safe; `appOptimistic`
		// is false because a switch-in has no send of its own in flight.
		return transcriptPatch(entry.state, { hydrationComplete: true, appOptimistic: false });
	},

	hasCachedTranscript(sessionId: string) {
		return readCachedTranscript(sessionId) !== null;
	},

	invalidate(sessionId?: string) {
		if (sessionId === undefined) transcriptCache.clear();
		else transcriptCache.delete(sessionId);
	},

	invalidateUnverified() {
		for (const [id, entry] of transcriptCache) {
			if (!entry.epoch) transcriptCache.delete(id);
		}
	},

	hasLogEpoch(sessionId: string) {
		return feed !== null && currentSessionId === sessionId && !!feed.epoch;
	},

	hasAppliedUsage(sessionId: string) {
		if (currentSessionId === sessionId) return usageApplied;
		return readCachedTranscript(sessionId)?.usageApplied ?? false;
	},

	isReducerStreaming(sessionId: string | null) {
		if (sessionId === null || currentSessionId !== sessionId) return false;
		return reducerState.isStreaming;
	},

	noteOptimisticSend(sessionId: string | null) {
		// Only the session being fed (or a brand-new chat with no id yet, which
		// this feed will adopt on `start`) may raise the flag.
		if (sessionId !== null && currentSessionId !== null && sessionId !== currentSessionId) return;
		optimisticStreaming = true;
	},

	clearOptimisticSend(sessionId: string | null) {
		if (sessionId !== null && currentSessionId !== null && sessionId !== currentSessionId) return;
		optimisticStreaming = false;
		// Withdraw the flip on BOTH channels — the module latch above and the
		// state field the fold reads — or the next fold would re-raise it. The
		// published `isStreaming` falls back to the authoritative reducer value,
		// so withdrawing never kills a turn that is genuinely live.
		if (!deps) return;
		deps.update((s) => {
			if (sessionId !== null && s.sessionId !== sessionId) return s;
			if (!s.optimisticStreaming) return s;
			return {
				...s,
				optimisticStreaming: false,
				isStreaming:
					currentSessionId !== null && s.sessionId === currentSessionId
						? reducerState.isStreaming
						: s.isStreaming,
			};
		});
	},
};
