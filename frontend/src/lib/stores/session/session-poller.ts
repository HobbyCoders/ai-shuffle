/**
 * Session Poller — SOLE authority for session state from the server.
 *
 * Architecture (polling-only, no dual-authority):
 * - This is the ONLY module that writes to session-state and activeSession.
 * - WebSocket is notification-only: it triggers pollNow() or startStreaming().
 * - Session loading (on switch, reconnect) uses HTTP fetch, not WS messages.
 * - All state transitions (streaming start/end) are detected from poll responses.
 *
 * Polling rates:
 * - 150ms during streaming (real-time feel)
 * - 1500ms "park poll" while a turn is parked waiting on background tasks
 *   (isStreaming=false but activeTasks.length>0) — keeps tasks fresh AND
 *   detects the wake when streaming resumes
 * - Stops entirely only when streaming ends AND there are no active tasks
 *
 * Key behaviors:
 * - Sends ?since_update_id=N → gets 304 if nothing changed (cheap)
 * - On update: replaces session state entirely (no merging)
 * - Upshifts to streaming rate when isStreaming flips true; downshifts to the
 *   park rate when parked with active tasks; stops when fully idle.
 */

import type { ChatMessage, SubagentChildMessage, MessageType, Task } from '../types';
import { sessionState, type SessionUsage } from './session-state';
import { STREAM_DEBUG } from '$lib/utils/debug-log';

// ─── Configuration ───────────────────────────────────────────────────────────

/** Fast poll rate while a turn is actively streaming. */
const STREAMING_POLL_INTERVAL_MS = 150;

/**
 * Slow "park poll" rate. Used when a turn has parked (isStreaming=false) but
 * still has background tasks in flight (activeTasks.length>0). Keeps the active
 * tasks fresh and detects the wake (next poll where isStreaming flips back to
 * true) so we can upshift to the streaming rate. Without this, a parked session
 * waiting on a long task goes stale and never auto-refreshes (Bug 2).
 */
const PARK_POLL_INTERVAL_MS = 1500;

/**
 * Hard timeout for a single `/live` poll fetch.
 *
 * Every poll self-serializes through the `_inFlight` guard (no overlapping
 * requests). That guard is cleared in `_poll`'s `finally` — which only runs once
 * the fetch settles. A request that never settles (the backend holding the
 * response open mid-teardown after the SDK client was killed, a half-open
 * socket, etc.) therefore pins `_inFlight` forever: every subsequent interval
 * tick early-returns at the guard and the poll loop is wedged even though the
 * timer keeps firing. The UI freezes on its last-applied state (stuck
 * "streaming", stale task strip) until a full page reload — whose loadSession()
 * path has its OWN AbortController and so always recovers. Time-boxing the poll
 * fetch removes that asymmetry: a stall aborts → catch → finally clears
 * `_inFlight` → the next tick re-polls and reconciles. Matches loadSession's
 * window.
 */
const POLL_TIMEOUT_MS = 15000;

// ─── Types ───────────────────────────────────────────────────────────────────

interface PollTimer {
	timer: ReturnType<typeof setInterval>;
	sessionId: string;
	interval: number;
}

type StreamEndCallback = (sessionId: string, usage: SessionUsage) => void;
type PollErrorCallback = (sessionId: string, consecutiveErrors: number, error: unknown) => void;

/** Worktree metadata from the server, included in /live responses. */
export interface SessionMeta {
	title?: string;
	worktree_id?: string | null;
	worktree_branch?: string | null;
	worktree_deleted?: boolean;
	execution_mode?: string;
	/** No-worktree cloud sessions only: the branch the worker checked out, persisted
	 *  at intake so the UI can show it after reloads. cloud+worktree uses
	 *  `worktree_branch` instead (the two are mutually exclusive). */
	cloud_branch?: string | null;
	/** Cross-install (cloud) sessions only: whether the owning device is
	 *  currently reachable. When false, the /live response is served from
	 *  shared-DB history and the session is view-only (see `controllable`). */
	device_online?: boolean;
	/** Whether live control (send / interrupt) can reach the owning device.
	 *  false for a remote session whose owner is offline/view-only — the UI
	 *  shows a "device offline" badge and disables sending. */
	controllable?: boolean;
}

/** Data shape returned by the onPollApplied callback */
export interface PollAppliedData {
	messages: ChatMessage[];
	isStreaming: boolean;
	usage: SessionUsage;
	sessionMeta?: SessionMeta;
	/**
	 * Currently-running background tasks (sub-agents + shell subprocesses) for
	 * this session. Always present; `[]` when none. Fresh on every poll,
	 * independent of message deltas. Non-empty exactly while the session has
	 * background work in flight (including while a turn is PARKED).
	 */
	activeTasks: Task[];
}

/** Number of consecutive failures before notifying the error callback */
const CONSECUTIVE_ERROR_THRESHOLD = 3;

// ─── Poller Class ────────────────────────────────────────────────────────────

class SessionPoller {
	/** Active polling timers keyed by sessionId */
	private _timers: Map<string, PollTimer> = new Map();

	/** In-flight poll requests (prevent overlapping) */
	private _inFlight: Set<string> = new Set();

	/** Pending debounced pollNow timers keyed by sessionId */
	private _pendingPollNow: Map<string, ReturnType<typeof setTimeout>> = new Map();

	/** Pending final-poll timers (survive stop(), cleared only by stopAll/fire) */
	private _pendingFinalPoll: Map<string, ReturnType<typeof setTimeout>> = new Map();

	/** Consecutive error counts per session */
	private _errorCounts: Map<string, number> = new Map();

	/**
	 * Streaming generation counter per session. Incremented each time
	 * startStreaming() is called. Captured before each _poll() so that
	 * _applyPollResult can detect stale responses — if the generation
	 * changed between poll start and response, a new streaming session
	 * was started and the old poll's streamingJustEnded is invalid.
	 */
	private _streamingGeneration: Map<string, number> = new Map();

	/** Callback when streaming ends (cleanup: cache, remove session, refresh list) */
	private _onStreamEnd: StreamEndCallback | null = null;

	/** Callback after each successful poll applies state (direct path to activeSession) */
	private _onPollApplied: ((sessionId: string, data: PollAppliedData) => void) | null = null;

	/** Callback when consecutive poll errors exceed threshold */
	private _onPollError: PollErrorCallback | null = null;

	/**
	 * Predicate: is this session mid-`interrupt_for_query` (old query interrupted,
	 * the replacement query not yet streaming)?
	 *
	 * While true, a poll that reports `is_streaming=false` is the DRAINED OLD
	 * query, not a genuine end-of-turn — so the lifecycle below must keep the
	 * streaming-rate timer alive (and skip stream-end side effects) instead of
	 * stopping. Without this guard, the first `is_streaming=false` poll during the
	 * drain stops the timer; if the new query's `start` then arrives while a stale
	 * poll is in flight, `startStreaming` no-ops (timer still present → generation
	 * not bumped) and the late poll re-stops the timer — leaving the poller dead so
	 * NO new messages render until the user refreshes the page.
	 *
	 * Injected so the single source of truth stays activeSession's
	 * `interruptPending` flag rather than duplicating transition state here.
	 */
	private _isInterrupting: ((sessionId: string) => boolean) | null = null;

	/**
	 * Register a callback for when streaming ends.
	 * Used to trigger notifications, session list refresh, etc.
	 */
	onStreamEnd(callback: StreamEndCallback): void {
		this._onStreamEnd = callback;
	}

	/**
	 * Register the interrupt-transition guard (see `_isInterrupting`). The
	 * predicate returns true while `sessionId` is between an `interrupt_for_query`
	 * and the replacement query's first streaming poll.
	 */
	setInterruptGuard(callback: (sessionId: string) => boolean): void {
		this._isInterrupting = callback;
	}

	/**
	 * Register a callback invoked after each successful poll writes to sessionState.
	 * This is the SOLE path from server → activeSession state.
	 */
	onPollApplied(callback: (sessionId: string, data: PollAppliedData) => void): void {
		this._onPollApplied = callback;
	}

	/**
	 * Register a callback for poll errors.
	 * Called when consecutive errors reach the threshold (3 by default).
	 * Used to show error indicators in the UI.
	 */
	onPollError(callback: PollErrorCallback): void {
		this._onPollError = callback;
	}

	/**
	 * Start polling for a session at the streaming rate.
	 * If already polling this session at streaming rate, this is a no-op.
	 *
	 * Preserves existing session state (messages, usage) when restarting
	 * polling for an existing session. Only creates fresh state for sessions
	 * not yet tracked. This prevents the context usage bar from flashing
	 * to 0% on visibility changes, WS reconnects, or fallback recovery.
	 */
	startStreaming(sessionId: string): void {
		if (this._timers.has(sessionId)) {
			const existing = this._timers.get(sessionId)!;
			if (existing.interval !== STREAMING_POLL_INTERVAL_MS) {
				this.stop(sessionId);
				// Fall through to create new timer
			} else {
				// Already polling at streaming rate — no-op.
				return;
			}
		}

		// Bump generation ONLY when actually starting new polling.
		// Any in-flight polls from the old generation will be discarded
		// in _applyPollResult via stale-response detection.
		const nextGen = (this._streamingGeneration.get(sessionId) ?? 0) + 1;
		this._streamingGeneration.set(sessionId, nextGen);

		if (nextGen === 1) console.debug(`[Poller] startStreaming(${sessionId}): gen=${nextGen}`);

		// Clear pending pollNow timers and in-flight flag so our fresh
		// since_update_id=0 poll can proceed immediately. Without this,
		// a pollNow() from a message_added event can block startStreaming's
		// initial poll — the _inFlight guard in _poll() would skip it.
		const pendingTimer = this._pendingPollNow.get(sessionId);
		if (pendingTimer) {
			clearTimeout(pendingTimer);
			this._pendingPollNow.delete(sessionId);
		}
		this._inFlight.delete(sessionId);

		// Prepare session state for polling.
		// If the session already has state (e.g., resuming after visibility
		// change or WS reconnect), preserve it and only reset the lastUpdateId
		// so the next poll fetches fresh data. This avoids the destructive
		// removeSession→initSession pattern that wiped contextUsed to 0,
		// causing the context usage bar to flash on every tab switch.
		const existingSession = sessionState.getSession(sessionId);
		if (existingSession) {
			sessionState.resetForPolling(sessionId);
		} else {
			sessionState.initSession(sessionId);
		}

		// Poll immediately, then on interval
		this._poll(sessionId);
		this._ensureTimer(sessionId, STREAMING_POLL_INTERVAL_MS);
	}

	/**
	 * Ensure a polling timer is running for `sessionId` at exactly `interval`.
	 *
	 * Idempotent: if a timer at the requested interval already exists, this is a
	 * no-op. If a timer at a DIFFERENT interval exists, it is swapped for one at
	 * the new interval (this is the upshift/downshift between the streaming rate
	 * and the park rate). Does NOT poll immediately — callers that need a fresh
	 * poll (e.g. startStreaming) trigger one themselves; the lifecycle transitions
	 * in _applyPollResult are already running inside a poll.
	 */
	private _ensureTimer(sessionId: string, interval: number): void {
		const existing = this._timers.get(sessionId);
		if (existing) {
			if (existing.interval === interval) return; // already at the right rate
			clearInterval(existing.timer);
		}
		const timer = setInterval(() => this._poll(sessionId), interval);
		this._timers.set(sessionId, { timer, sessionId, interval });
	}

	/**
	 * Trigger an immediate poll for a session (called by WebSocket notifier).
	 * Does not start a timer — call startStreaming() for ongoing polling.
	 */
	pollNow(sessionId: string): void {
		// Debounce: coalesce rapid pollNow calls within 100ms
		if (this._pendingPollNow.has(sessionId)) return;

		const timer = setTimeout(() => {
			this._pendingPollNow.delete(sessionId);
			this._poll(sessionId);
		}, 100);
		this._pendingPollNow.set(sessionId, timer);
	}

	/**
	 * Schedule a final authoritative poll that survives stop().
	 *
	 * Used by handleDone/handleStoppedOrInterrupted to ensure the
	 * post-streaming state is fetched even if the interval timer's
	 * in-flight poll triggers streamingJustEnded → stop() first.
	 * stop() kills pending pollNow timers, but _pendingFinalPoll
	 * timers are deliberately NOT cleared by stop().
	 */
	pollFinal(sessionId: string): void {
		// If a final poll is already pending, don't double-schedule
		if (this._pendingFinalPoll.has(sessionId)) return;

		const timer = setTimeout(() => {
			this._pendingFinalPoll.delete(sessionId);
			this._poll(sessionId);
		}, 150);
		this._pendingFinalPoll.set(sessionId, timer);
	}

	/**
	 * Stop polling for a session.
	 */
	stop(sessionId: string): void {
		const entry = this._timers.get(sessionId);
		if (entry) {
			clearInterval(entry.timer);
			this._timers.delete(sessionId);
		}
		this._inFlight.delete(sessionId);
		this._errorCounts.delete(sessionId);
		// NOTE: Do NOT delete _streamingGeneration here. The generation counter
		// must persist across stop→startStreaming cycles so that in-flight polls
		// from the old generation are correctly detected as stale. Deleting it
		// resets the counter to 0, allowing stale polls with higher generation
		// numbers to bypass the staleness check and clobber the new streaming
		// session (the root cause of interrupt_for_query getting stuck).

		const pending = this._pendingPollNow.get(sessionId);
		if (pending) {
			clearTimeout(pending);
			this._pendingPollNow.delete(sessionId);
		}
		// NOTE: _pendingFinalPoll timers are NOT cleared here.
		// They are fire-and-forget safety nets from handleDone/handleStopped.
	}

	/**
	 * Stop all polling (e.g., on logout or page unload).
	 */
	stopAll(): void {
		for (const [, entry] of this._timers) {
			clearInterval(entry.timer);
		}
		this._timers.clear();
		this._inFlight.clear();
		this._errorCounts.clear();
		this._streamingGeneration.clear();

		for (const timer of this._pendingPollNow.values()) {
			clearTimeout(timer);
		}
		this._pendingPollNow.clear();

		for (const timer of this._pendingFinalPoll.values()) {
			clearTimeout(timer);
		}
		this._pendingFinalPoll.clear();
	}

	/**
	 * Check if currently polling a session.
	 */
	isPolling(sessionId: string): boolean {
		return this._timers.has(sessionId);
	}

	/**
	 * Load a session's state via HTTP. This replaces the old WS-based
	 * load_session → history flow. Returns the poll data on success, or
	 * null on failure.
	 *
	 * This does NOT start polling — it's a one-shot fetch that writes to
	 * sessionState and fires onPollApplied. If the session turns out to
	 * be streaming, the caller should call startStreaming() afterwards.
	 */
	async loadSession(sessionId: string): Promise<PollAppliedData | null> {
		// loadSession is a one-shot fetch — no need for per-call logging

		// Ensure session state exists AND force lastUpdateId → 0 so the HTTP
		// response is ALWAYS applied. Use initSession when missing, otherwise
		// resetForPolling (which preserves existing messages/usage so there's
		// no visible flash to 0%, but zeroes lastUpdateId).
		//
		// Why this matters: loadSession fetches with ?since_update_id=0, so the
		// server returns the FULL current state. But _applyPollResult →
		// applyServerState has a fast-path that SKIPS the apply when an existing
		// sessionState entry's lastUpdateId already equals the server's
		// last_update_id. A stale entry can linger in sessionStateManager for a
		// session that streamed/finished while it was NOT the active session
		// (e.g. a background `done` runs pollFinal, which writes the final
		// lastUpdateId without ever removing the entry). Switching back to that
		// session would then hit the fast-path skip, onPollApplied would never
		// fire, and the store would keep the stale IndexedDB-cached messages —
		// rendering the last user message / dropping the streamed assistant
		// tail. Forcing lastUpdateId → 0 here guarantees a full, authoritative
		// apply on every load, which is exactly loadSession's contract.
		if (!sessionState.getSession(sessionId)) {
			sessionState.initSession(sessionId);
		} else {
			sessionState.resetForPolling(sessionId);
		}

		try {
			// Timeout prevents UI freeze when backend is slow or hung
			// (e.g., parsing large/corrupt JSONL, resource exhaustion)
			const controller = new AbortController();
			const timeoutId = setTimeout(() => controller.abort(), 15000);

			const response = await fetch(
				`/api/v1/sessions/${sessionId}/live?since_update_id=0`,
				{ credentials: 'include', signal: controller.signal }
			);
			clearTimeout(timeoutId);

			if (!response.ok) {
				console.error(`[Poller] loadSession(${sessionId}): HTTP ${response.status}`);
				return null;
			}

			const data = await response.json();
			// Use generation 0 — loadSession is not part of a streaming cycle,
			// so stale detection isn't needed. The generation check in
			// _applyPollResult will pass because we're at generation 0 or matching.
			const currentGen = this._streamingGeneration.get(sessionId) ?? 0;
			this._applyPollResult(sessionId, data, currentGen);

			const session = sessionState.getSession(sessionId);
			if (session) {
				const sessionMeta = data.session_meta as SessionMeta | undefined;
				return {
					messages: session.messages,
					isStreaming: session.isStreaming,
					usage: session.usage,
					sessionMeta,
					activeTasks: parseActiveTasks(data.activeTasks ?? data.active_tasks),
				};
			}
			return null;
		} catch (e) {
			if (e instanceof DOMException && e.name === 'AbortError') {
				console.error(`[Poller] loadSession(${sessionId}): timed out after 15s`);
			} else {
				console.error('[Poller] loadSession error:', e);
			}
			return null;
		}
	}

	// ─── Private ─────────────────────────────────────────────────────────────

	private async _poll(sessionId: string): Promise<void> {
		if (this._inFlight.has(sessionId)) return;
		this._inFlight.add(sessionId);

		const pollGeneration = this._streamingGeneration.get(sessionId) ?? 0;

		// Time-box the fetch so a never-settling `/live` request can't pin
		// `_inFlight` and wedge the loop forever (see POLL_TIMEOUT_MS).
		const controller = new AbortController();
		const timeoutId = setTimeout(() => controller.abort(), POLL_TIMEOUT_MS);

		try {
			const lastUpdateId = sessionState.getLastUpdateId(sessionId);

			const response = await fetch(
				`/api/v1/sessions/${sessionId}/live?since_update_id=${lastUpdateId}`,
				{ credentials: 'include', signal: controller.signal }
			);

			if (response.status === 304) {
				this._errorCounts.delete(sessionId);
				return;
			}

			if (!response.ok) {
				console.warn(`[Poller] _poll(${sessionId}): HTTP ${response.status}`);
				this._recordError(sessionId, new Error(`HTTP ${response.status}`));
				return;
			}

			const data = await response.json();
			this._applyPollResult(sessionId, data, pollGeneration);
			this._errorCounts.delete(sessionId);
		} catch (e) {
			if (e instanceof DOMException && e.name === 'AbortError') {
				// Stalled poll aborted by the watchdog — the `finally` below clears
				// `_inFlight` so the next interval tick issues a fresh request.
				console.warn(`[Poller] _poll(${sessionId}): timed out after ${POLL_TIMEOUT_MS}ms — unwedging`);
			} else {
				console.warn(`[Poller] _poll(${sessionId}): fetch error`, e);
			}
			this._recordError(sessionId, e);
		} finally {
			clearTimeout(timeoutId);
			this._inFlight.delete(sessionId);
		}
	}

	private _recordError(sessionId: string, error: unknown): void {
		const count = (this._errorCounts.get(sessionId) || 0) + 1;
		this._errorCounts.set(sessionId, count);

		console.warn(`[Poller] Poll error for ${sessionId} (${count} consecutive):`, error);

		if (count >= CONSECUTIVE_ERROR_THRESHOLD && this._onPollError) {
			this._onPollError(sessionId, count, error);
		}
	}

	/**
	 * Convert server response into session state and apply it.
	 *
	 * Handles two response formats:
	 * - Full: All messages (initial_message_count = 0 or absent) — first poll
	 * - Delta: Only streaming messages (initial_message_count > 0) — subsequent polls
	 */
	private _applyPollResult(sessionId: string, data: Record<string, unknown>, pollGeneration: number): void {
		const newUpdateId = data.last_update_id as number;
		const isStreaming = data.is_streaming as boolean;
		const serverMessages = (data.messages as Array<Record<string, unknown>>) || [];
		const initialMessageCount = (data.initial_message_count as number) || 0;

		// Active background tasks (sub-agents + shell subprocesses). Always present
		// on a fresh backend; tolerate a missing field gracefully (treat as []).
		// Only ever contains currently-running tasks.
		const activeTasks = parseActiveTasks(data.activeTasks ?? data.active_tasks);

		const rawMessages = serverMessages.map((m, i) => convertServerMessage(m, i));

		// Filter noise messages
		const streamingMessages = rawMessages.filter(m => {
			if (m.type === 'text' && m.role === 'assistant' && !m.content?.trim()) {
				return false;
			}
			if (m.role === 'user' && m.content && typeof m.content === 'string' && m.content.includes('<task-notification>')) {
				return false;
			}
			return true;
		}).map(m => {
			// Bug 1: when the turn is no longer streaming, no message may carry a
			// streaming flag — otherwise the bouncing "..." dots persist forever
			// after a turn parks/ends. The server payload should already reflect
			// this, but force-clear here as the authoritative belt-and-suspenders
			// (the synthetic injected streaming message in active-session state is
			// separately reconciled away by the poll-apply merge once we deliver
			// non-streaming server messages).
			if (!isStreaming && m.streaming) return { ...m, streaming: false };
			return m;
		});

		// Get existing state for merging and transition detection
		const existing = sessionState.getSession(sessionId);
		const existingMessages = existing?.messages;
		const wasStreaming = existing?.isStreaming ?? false;
		const streamingJustEnded = wasStreaming && !isStreaming;

		// Delta merge
		let messages: ChatMessage[];
		if (initialMessageCount > 0 && existingMessages && existingMessages.length >= initialMessageCount) {
			// The server splits at a positional boundary (initialMessageCount).
			// If that boundary shifts by one (e.g. end_streaming filtered a
			// history message, or a compaction mutated the list), a real
			// streaming message can fall inside the "history" slice and then be
			// re-added from streamingMessages → a duplicate card. Dedup the
			// history slice against the streaming ids so the now-stable message
			// ids (see jsonl_parser tool id alignment) self-correct the split.
			const streamingIds = new Set(streamingMessages.map(m => m.id));
			const historyMessages = existingMessages
				.slice(0, initialMessageCount)
				.filter(m => !streamingIds.has(m.id));
			messages = [...historyMessages, ...streamingMessages];
		} else if (initialMessageCount > 0 && existingMessages && existingMessages.length > 0) {
			// existingMessages may already contain streaming messages from a
			// previous poll. Concatenating blindly would duplicate them.
			// Deduplicate by collecting IDs from streaming messages and only
			// keeping existing messages that don't overlap.
			const streamingIds = new Set(streamingMessages.map(m => m.id));
			const safeExisting = existingMessages.filter(m => !streamingIds.has(m.id));
			messages = [...safeExisting, ...streamingMessages];
		} else {
			messages = streamingMessages;
		}

		// Stale response detection
		const currentGeneration = this._streamingGeneration.get(sessionId) ?? 0;
		const isStaleResponse = pollGeneration < currentGeneration;

		if (isStaleResponse) {
			return;
		}

		const usageRaw = (data.usage as Record<string, number>) || {};
		const usage: SessionUsage = {
			inputTokens: usageRaw.input_tokens || 0,
			outputTokens: usageRaw.output_tokens || 0,
			cacheCreationTokens: usageRaw.cache_creation_tokens || 0,
			cacheReadTokens: usageRaw.cache_read_tokens || 0,
			contextUsed:
				(usageRaw.input_tokens || 0) +
				(usageRaw.cache_creation_tokens || 0) +
				(usageRaw.cache_read_tokens || 0),
		};

		// ── Diagnostic: log streaming→ended transition data ──────
		if (STREAM_DEBUG && streamingJustEnded) {
			const assistantTexts = messages.filter(m => m.role === 'assistant' && m.type === 'text');
			console.warn(
				`[Poller:StreamEnd] streamingJustEnded`,
				`\n  rawServerMsgs: ${serverMessages.length}, afterFilter: ${streamingMessages.length}, afterMerge: ${messages.length}`,
				`\n  initialMessageCount: ${initialMessageCount}, isStreaming: ${isStreaming}`,
				`\n  assistantTexts: ${assistantTexts.length} [${assistantTexts.map(m => `id=${m.id} len=${(m.content||'').length} streaming=${m.streaming}`).join(', ')}]`,
				`\n  existingMsgs: ${existingMessages?.length ?? 'none'}, updateId: ${existing?.lastUpdateId ?? 'none'} → ${newUpdateId}`
			);
		}

		// Apply to store — ONLY place state gets written
		sessionState.applyServerState(
			sessionId,
			messages,
			isStreaming,
			newUpdateId,
			usage,
			existingMessages
		);

		// Extract session metadata (worktree state) if present in server response
		const sessionMeta = data.session_meta as SessionMeta | undefined;

		// Direct callback to activeSession — sole path from server → UI
		if (this._onPollApplied) {
			const session = sessionState.getSession(sessionId);
			if (session) {
				this._onPollApplied(sessionId, {
					messages: session.messages,
					isStreaming: session.isStreaming,
					usage: session.usage,
					sessionMeta,
					activeTasks,
				});
			} else {
				console.warn(`[Poller] onPollApplied(${sessionId}): session not found in sessionState after apply`);
			}
		}

		// ── Poll lifecycle: upshift / downshift / stop ──────────────────────
		//
		// Three modes, selected by (isStreaming, activeTasks):
		//   1. isStreaming                     → STREAMING rate (upshift / keep fast)
		//   2. !isStreaming && tasks in flight → PARK rate (downshift, keep polling)
		//   3. !isStreaming && no tasks        → STOP (fully idle)
		//
		// Bug 2: previously mode 2 fell into the stop path, so a turn parked on a
		// long-running background task went stale and never auto-refreshed (the
		// user had to send a message to unfreeze). The park poll keeps activeTasks
		// fresh AND detects the wake — the next poll where isStreaming flips back
		// to true upshifts to the streaming rate.
		//
		// `unparkPending` extends mode 2 across the HAND-OFF GAP: when a parked
		// session's background sub-agent completes, the backend drops it from
		// activeTasks (it's done) the instant it records the completion, but the
		// "unpark" follow-up turn it triggers only flips isStreaming back to true a
		// beat later (after a DB write + history seeding). In that gap a poll
		// reports (isStreaming=false, activeTasks=[]) — which without this flag is
		// indistinguishable from "fully idle" and STOPS the poller. The follow-up
		// then streams into a dead poller and the UI is stranded stale with no task
		// bar (the origin socket is gone, so nothing restarts polling). The backend
		// latches unpark_pending for exactly that gap, so we keep park-polling and
		// catch the wake. Mirrors the `interrupting` guard above.
		const hasActiveTasks = activeTasks.length > 0;
		const unparkPending = (data.unpark_pending as boolean) ?? false;
		const hasTimer = this._timers.has(sessionId);
		const interrupting = this._isInterrupting?.(sessionId) ?? false;

		if (isStreaming) {
			// Upshift (or keep): ensure the fast streaming-rate timer is running.
			// Only manage the timer if one already exists for this session, so a
			// one-shot loadSession (no timer) doesn't silently start a background
			// poll loop — its caller (onConnected) owns the startStreaming call.
			if (hasTimer) this._ensureTimer(sessionId, STREAMING_POLL_INTERVAL_MS);
		} else if (interrupting) {
			// interrupt_for_query in flight: this `is_streaming=false` poll is the
			// DRAINED OLD query, not a real end-of-turn. Keep the streaming-rate
			// timer alive so we catch the NEW query's stream the instant it begins;
			// do NOT stop, downshift, or fire stream-end. This deterministically
			// removes the teardown window — otherwise a drain-window poll (or a
			// stale in-flight one landing after the new query's `start`) stops the
			// timer and nothing restarts it, so new messages never render until a
			// page refresh. See `_isInterrupting`.
			if (hasTimer) this._ensureTimer(sessionId, STREAMING_POLL_INTERVAL_MS);
		} else if (hasActiveTasks || unparkPending) {
			// Park: downshift to the slow park-poll rate (or start it). We start a
			// timer even from loadSession here, because a freshly-loaded parked
			// session must keep itself fresh to detect the wake. _ensureTimer is
			// idempotent and swaps the interval if we were at the streaming rate.
			// `unparkPending` (no active tasks) keeps us here across the unpark
			// hand-off gap so we don't STOP and miss the follow-up's stream.
			const wasStreamingTimer = hasTimer &&
				this._timers.get(sessionId)!.interval === STREAMING_POLL_INTERVAL_MS;
			this._ensureTimer(sessionId, PARK_POLL_INTERVAL_MS);
			// On the streaming→parked transition, fire the stream-end side effects
			// (session list refresh, message cache write) once — the turn's
			// foreground output is done even though background work continues.
			if (streamingJustEnded || wasStreamingTimer) {
				if (this._onStreamEnd) this._onStreamEnd(sessionId, usage);
			}
		} else {
			// Idle: fully stop. Covers both the streaming→ended transition with no
			// tasks, and a park poll whose last task just finished.
			if (hasTimer) this.stop(sessionId);
			if (streamingJustEnded || hasTimer) {
				if (this._onStreamEnd) this._onStreamEnd(sessionId, usage);
			}
		}
	}
}

// ─── Active-task parsing ──────────────────────────────────────────────────────

const VALID_TASK_STATUSES = new Set(['running', 'completed', 'error', 'killed']);
const VALID_TASK_KINDS = new Set(['agent', 'shell']);

/**
 * Parse the `activeTasks` field from a /live response into well-typed Task[].
 *
 * Tolerant by contract: a missing/non-array field yields `[]`. Each entry is
 * lightly validated (id present, known status/kind) and normalized — malformed
 * rows are dropped rather than rendered. The backend contract guarantees
 * camelCase + only currently-running tasks, but we accept a snake_case
 * `started_at`/`updated_at` fallback so the UI degrades gracefully.
 */
function parseActiveTasks(raw: unknown): Task[] {
	if (!Array.isArray(raw)) return [];
	const tasks: Task[] = [];
	for (const entry of raw) {
		if (!entry || typeof entry !== 'object') continue;
		const t = entry as Record<string, unknown>;
		const id = typeof t.id === 'string' ? t.id : '';
		if (!id) continue;
		const status = typeof t.status === 'string' && VALID_TASK_STATUSES.has(t.status)
			? (t.status as Task['status'])
			: 'running';
		const kind = typeof t.kind === 'string' && VALID_TASK_KINDS.has(t.kind)
			? (t.kind as Task['kind'])
			: 'shell';
		tasks.push({
			id,
			subject: typeof t.subject === 'string' ? t.subject : '',
			status,
			kind,
			summary: typeof t.summary === 'string' ? t.summary : null,
			startedAt: (typeof t.startedAt === 'string' ? t.startedAt
				: typeof t.started_at === 'string' ? t.started_at : ''),
			updatedAt: (typeof t.updatedAt === 'string' ? t.updatedAt
				: typeof t.updated_at === 'string' ? t.updated_at : ''),
		});
	}
	return tasks;
}

// ─── Message conversion helpers ──────────────────────────────────────────────

function convertServerMessage(m: Record<string, unknown>, index: number): ChatMessage {
	const serverType = (m.type as MessageType) || 'text';
	const serverRole = (m.role as 'user' | 'assistant' | 'system') || 'assistant';

	const msg: ChatMessage = {
		id: (m.id as string) || `live-${index}`,
		role: serverRole,
		content: (m.content as string) || '',
		type: serverType,
		streaming: (m.streaming as boolean) || false,
	};

	// Raw JSONL entry uuid (when the server knows it). Kept separate from `id`
	// — `id` is normalized for Svelte keying (e.g. `text-<uuid>`, `msg-<row>`)
	// and is NOT a valid rewind target. jsonlUuid lets MessageActions rewind by
	// uuid directly instead of falling back to fragile content matching.
	if (m.uuid) msg.jsonlUuid = m.uuid as string;

	if (m.toolName) msg.toolName = m.toolName as string;
	if (m.toolId) msg.toolId = m.toolId as string;
	if (m.toolInput) msg.toolInput = m.toolInput as Record<string, unknown>;
	if (m.toolResult !== undefined) msg.toolResult = m.toolResult as string;
	if (m.toolStatus) msg.toolStatus = m.toolStatus as 'running' | 'complete' | 'error';
	if (m.toolScreenshot) msg.toolScreenshot = m.toolScreenshot as string;

	if (serverType === 'tool_use' && msg.streaming && msg.content) {
		msg.partialToolInput = msg.content;
	}

	if (m.agentId) msg.agentId = m.agentId as string;
	if (m.agentType) msg.agentType = m.agentType as string;
	if (m.agentDescription) msg.agentDescription = m.agentDescription as string;
	if (m.agentPrompt) msg.agentPrompt = m.agentPrompt as string;
	if (m.agentStatus) msg.agentStatus = m.agentStatus as 'pending' | 'running' | 'completed' | 'error';
	if (m.agentChildren) {
		msg.agentChildren = (m.agentChildren as SubagentChildMessage[]).map(child => {
			// For streaming tool_use children, set partialToolInput from content
			// (mirrors the top-level tool_use handling above)
			if (child.type === 'tool_use' && child.streaming && child.content && !child.toolInput) {
				return { ...child, partialToolInput: child.content };
			}
			return child;
		});
	}
	if (m.agentUsage) msg.agentUsage = m.agentUsage as ChatMessage['agentUsage'];

	if (m.systemSubtype) msg.systemSubtype = m.systemSubtype as string;
	if (m.systemData) msg.systemData = m.systemData as Record<string, unknown>;

	if (m.thinkingTokens !== undefined) msg.thinkingTokens = m.thinkingTokens as number;

	if (m.metadata) msg.metadata = m.metadata as Record<string, unknown>;

	return msg;
}

// ─── Export singleton ────────────────────────────────────────────────────────

export const sessionPoller = new SessionPoller();
