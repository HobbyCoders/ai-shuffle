/**
 * WebSocket Message Handlers — Notification + control dispatch for incoming WS messages.
 *
 * Architecture (SSE-fed):
 * - The SSE-fed reducer (feedController) owns the session transcript and its
 *   streaming state — it is the sole writer of `messages` and isStreaming for
 *   the active session. These handlers NEVER render stream content.
 * - WS handlers DO manage non-transcript state: permissions, questions, the
 *   cross-session streaming map, git state, session adoption (session_created/
 *   start → start the feed), and fire side-effect callbacks (completion
 *   notifications, error banners, etc).
 * - Session history loads via the feed's from_seq=0 replay, not WS.
 */

import type { PermissionRequest as PermissionRequestType, UserQuestionRequest } from '$lib/api/client';
import type { ActiveSessionState, StreamingSessionInfo } from '../active-session';
import { notify, type NotificationAction, type NotificationSeverity, type NotificationSurface } from '../notifications';
import { rateLimit } from '../rate-limit';
import { reportClientError } from '$lib/services/error-reporter';
import { startFixChat } from '$lib/services/fix-with-ai';
// The SSE-fed reducer store — the transcript authority for a session.
import { feedController } from './v2/feed-controller';

// ─── Interfaces ──────────────────────────────────────────────────────────────

/**
 * Mutable internal state shared between active-session.ts and the handlers.
 * Simplified: removed guards that were only needed for the dual-authority pattern.
 */
export interface InternalMutableState {
	pendingTools: Set<string>;
	interruptPending: boolean;
	/** True when sendMessage() sent a query for a NEW session (no sessionId yet).
	 *  Used by session_created/start to know whether to accept the sessionId. */
	awaitingNewSession: boolean;
	/** Client-generated id attached to the outgoing new-session `query` frame while
	 *  awaitingNewSession. handle_query echoes it in the `session_created` and first
	 *  `start` frames, and the awaiting-branch adoption predicates accept ONLY when
	 *  the incoming frame's request_id matches. This authenticates adoption so a
	 *  server-initiated turn for ANOTHER session (unpark follow-up / queue drain) —
	 *  which carries NO request_id — can never be adopted during the awaiting
	 *  window on the shared device socket. null when not awaiting a new session. */
	pendingRequestId: string | null;
	/** True when the user clicked Stop on a brand-new session BEFORE the backend
	 *  minted its sessionId (stopGeneration had no id to send, so the stop would
	 *  otherwise be dropped — "Stop fired before the session started"). Consumed
	 *  by the session_created/start handlers: once the id is known they send the
	 *  deferred stop so the backend can discard the orphan (no-output) turn. */
	stopRequestedBeforeSession: boolean;
	/** The `stop_background` choice captured when Stop was clicked before the
	 *  sessionId existed (see stopRequestedBeforeSession). true also tears down
	 *  in-flight background sub-agents; false stops only the foreground turn.
	 *  Defaults false so a deferred stop never kills agents unless the user
	 *  explicitly opted in. Consumed alongside the deferred stop send. */
	stopBackgroundRequested: boolean;
	/** Monotonic counter incremented on every session switch / new-chat.
	 *  autoCreateWorktree captures the value at start and bails if it changes
	 *  during an await, preventing the async callback from clobbering the
	 *  active session after the user navigated away. */
	worktreeCreationGeneration: number;
	/** AbortController for the currently in-flight autoCreateWorktree request,
	 *  if any. session-switcher aborts this on every session switch / new-chat
	 *  so we don't leak orphan worktrees on the backend when the user navigates
	 *  away mid-creation (e.g. phone lock). */
	worktreeCreateAbort: AbortController | null;
	/** Resolve callback for the interrupt_confirmed promise.
	 *  Set by sendMessage's interrupt path, called by the interrupt_confirmed
	 *  WS handler so the frontend waits for backend confirmation before
	 *  sending the new query. */
	interruptConfirmResolve: ((confirmed: boolean) => void) | null;
}

/**
 * Dependencies injected from the active-session store factory.
 */
export interface MessageHandlerDeps {
	// Store access
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;

	// WebSocket
	wsSend: (data: Record<string, unknown>) => boolean;

	// External store access
	streamingSessions: {
		update: (fn: (m: Map<string, StreamingSessionInfo>) => Map<string, StreamingSessionInfo>) => void;
		set: (m: Map<string, StreamingSessionInfo>) => void;
	};
	backgroundPermissions: {
		update: (fn: (m: Map<string, number>) => Map<string, number>) => void;
	};

	// Callbacks for git
	gitRefreshStatus: () => void;
	gitRefreshDiffStats: () => void;
	gitGetProjectId: () => string | null;
	gitGetLoading: () => boolean;
	// Callbacks for app-level side effects
	appLoadSessions: () => void;
	appLoadApiUserSessions: () => void;

	// Internal mutable state (shared reference)
	internal: InternalMutableState;

	// Callbacks for complex side effects that remain in active-session.ts
	onNewChat: () => void;
	onPersistSession: () => void;
	onNotifySessionComplete: (info: {
		sessionId: string;
		sessionTitle: string | null;
		durationSeconds?: number;
		totalCostUsd?: number;
		tokensIn?: number;
		tokensOut?: number;
		model?: string;
	}) => void;
	onNotifyWaitingForResponse: (type: 'permission' | 'question', details?: { toolName?: string; questionCount?: number }) => void;

	// WebSocket connection helpers
	wsIsConnected: () => boolean;

	// Stop timeout management (owned by active-session.ts)
	clearStopTimeout: () => void;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createMessageHandlers(deps: MessageHandlerDeps): {
	handleMessage: (data: Record<string, unknown>) => void;
} {
	const {
		update, getState, wsSend,
		streamingSessions, backgroundPermissions,
		gitRefreshStatus, gitRefreshDiffStats, gitGetProjectId, gitGetLoading,
		appLoadSessions, appLoadApiUserSessions,
		internal,
		onNewChat, onPersistSession,
		onNotifySessionComplete, onNotifyWaitingForResponse,
		clearStopTimeout,
	} = deps;

	function mapPermissionRequests(
		requests: PermissionRequestType[],
		sessionId?: string
	): PermissionRequestType[] {
		return requests.map(req => ({
			...req,
			session_id: req.session_id ?? sessionId,
		}));
	}

	function mapUserQuestions(
		questions: UserQuestionRequest[],
		sessionId?: string
	): UserQuestionRequest[] {
		return questions.map(question => ({
			...question,
			session_id: question.session_id ?? sessionId,
		}));
	}

	function setNeedsAttention(sessionId: string | null | undefined, count: number): void {
		if (!sessionId) return;
		backgroundPermissions.update(map => {
			const next = new Map(map);
			if (count > 0) next.set(sessionId, count);
			else next.delete(sessionId);
			return next;
		});
	}

	function bumpNeedsAttention(sessionId: string | undefined): void {
		if (!sessionId) return;
		backgroundPermissions.update(map => {
			const next = new Map(map);
			next.set(sessionId, (next.get(sessionId) ?? 0) + 1);
			return next;
		});
	}

	// ── Handler: done ────────────────────────────────────────────────

	function handleDone(data: Record<string, unknown>): void {
		// During interrupt_for_query, the OLD query drains naturally and yields
		// a 'done' event (SDK interrupt doesn't raise CancelledError, so the
		// generator exits normally). If we process it, we'd clear interruptPending
		// and fire a premature completion notification — both of which clobber the
		// new query that's about to start. Skip entirely, like handleStoppedOrInterrupted.
		if (internal.interruptPending) { internal.pendingTools.clear(); return; }

		const metadata = data.metadata as Record<string, unknown>;
		const sessionId = data.session_id as string;
		// done event — the SSE feed folds the streaming→ended transition into
		// the transcript; this handler only fires the completion notification
		// and cleans cross-session bookkeeping.

		internal.interruptPending = false;

		const currentState = getState();
		const isActiveSession = !sessionId || sessionId === currentState.sessionId;
		const doneSessionId = sessionId || currentState.sessionId;

		if (isActiveSession) {
			clearStopTimeout();
			internal.pendingTools.clear();

			// Fire completion notification with metadata from the WS event.
			// State (isStreaming, messages) is owned by the SSE feed.
			const durationSeconds = currentState.streamStartTime
				? (Date.now() - currentState.streamStartTime) / 1000
				: undefined;

			onNotifySessionComplete({
				sessionId: sessionId || '',
				sessionTitle: currentState.title ?? null,
				durationSeconds,
				totalCostUsd: metadata?.total_cost_usd as number | undefined,
				tokensIn: metadata?.tokens_in as number | undefined,
				tokensOut: metadata?.tokens_out as number | undefined,
				model: metadata?.model as string | undefined,
			});

			onPersistSession();
		}

		// Always clean up streamingSessions (cross-session/cross-device)
		if (doneSessionId) {
			streamingSessions.update(map => {
				if (!map.has(doneSessionId)) return map;
				const newMap = new Map(map);
				newMap.delete(doneSessionId);
				return newMap;
			});
		}

		// A finished session can't be awaiting input. Clear any attention dot here
		// too, in case the backend's count=0 broadcast was missed (dead socket).
		// Without this the dot is sticky until the user opens the session, and the
		// attention dot now takes precedence over the streaming dot in the board.
		setNeedsAttention(doneSessionId, 0);

		appLoadSessions();
	}

	// ── Handler: stopped / interrupted ───────────────────────────────

	function handleStoppedOrInterrupted(data: Record<string, unknown>): void {
		if (internal.interruptPending) { internal.pendingTools.clear(); return; }

		// Route by the EVENT's session_id, not the active session — mirror
		// handleDone. A stopped/interrupted event for a BACKGROUND session must
		// not clobber the ACTIVE session's pendingTools. Pre-fix this handler
		// ignored data.session_id entirely and always mutated whatever session
		// was in view.
		const sessionId = data.session_id as string | undefined;
		const currentState = getState();
		const isActiveSession = !sessionId || sessionId === currentState.sessionId;
		const stoppedSessionId = sessionId || currentState.sessionId;

		if (isActiveSession) {
			clearStopTimeout();
			internal.pendingTools.clear();
		}

		// Clean up streamingSessions (cross-session/cross-device safe — keyed by
		// the stopped session's id).
		if (stoppedSessionId) {
			streamingSessions.update(map => {
				if (!map.has(stoppedSessionId)) return map;
				const newMap = new Map(map);
				newMap.delete(stoppedSessionId);
				return newMap;
			});
		}

		// A stopped/interrupted session can't be awaiting input — clear any stale
		// attention dot (mirrors handleDone; guards against a missed count=0).
		setNeedsAttention(stoppedSessionId, 0);
	}

	// ── Main dispatch ────────────────────────────────────────────────

	function handleMessage(data: Record<string, unknown>): void {
		const msgType = data.type as string;
		const eventType = data.event_type as string;

		// Lifecycle events logged only on error path (see 'error' case below)

		const state = getState();
		const eventSessionId = data.session_id as string | undefined;

		// Handle sync events from other devices / server-originated broadcasts
		if (eventType) {
			// queue_updated is handled here directly since queue state
			// is not part of the poll response — it's server-side only.
			// Scope to the active session: a late/cross-session event (e.g. a
			// stale queue_updated for the session we just switched away from, or
			// the watch_session restore racing the switch) must not clobber the
			// current session's queue.
			if (eventType === 'queue_updated' &&
				(!eventSessionId || eventSessionId === state.sessionId)) {
				const syncData = data.data as Record<string, unknown> | undefined;
				const messages = (syncData?.queued_messages ?? []) as Array<{ id: string; content: string; created_at: string }>;
				update(s => ({ ...s, queuedMessages: messages.map(m => ({ id: m.id, content: m.content, queuedAt: new Date(m.created_at).getTime() })) }));
			}

			// session_updated carries metadata changes (e.g. a backfilled title
			// for sessions created without one, like via auto-worktree). Apply
			// the title to the active session immediately and refresh the
			// sidebar list so the new title is visible everywhere.
			if (eventType === 'session_updated') {
				const syncData = data.data as Record<string, unknown> | undefined;
				const updates = (syncData?.updates ?? {}) as Record<string, unknown>;
				const updatedTitle = typeof updates.title === 'string' ? updates.title : undefined;
				if (updatedTitle) {
					const updatedSessionId = data.session_id as string | undefined;
					const currentState = getState();
					if (updatedSessionId && updatedSessionId === currentState.sessionId) {
						update(s => ({ ...s, title: updatedTitle }));
						onPersistSession();
					}
					appLoadSessions();
				}
			}

			// message_added from server-side queue processing (_process_next_queued)
			// needs no handling here: the SSE feed delivers the queued user message
			// to the transcript.

			return;
		}

		// ── User-level messages (not per-session) ─────────────────────

		if (msgType === 'tabs_sync') return;

		// Per-install Claude usage snapshot from the backend poller (not tied to
		// the active session). Each install runs a different account, so the
		// payload carries a `snapshots` array (one bucket per install) plus
		// `self_instance_id`; the legacy top-level fields describe the LOCAL
		// install and are folded into a self bucket when `snapshots` is absent.
		// Feeds the usage-limits store that drives the header pill + popover.
		if (msgType === 'usage_snapshot') {
			rateLimit.applySnapshot({
				windows: (data.windows as { type: string; utilization: number; resets_at?: string | null }[]) ?? [],
				fetched_at: (data.fetched_at as number | null) ?? null,
				available: (data.available as boolean) ?? false,
				generation: (data.generation as number | null) ?? undefined,
				snapshots: (data.snapshots as import('$lib/stores/rate-limit').SnapshotInstance[] | undefined) ?? undefined,
				self_instance_id: (data.self_instance_id as string | null | undefined) ?? undefined,
			});
			return;
		}

		if (msgType === 'user_session_created') {
			appLoadSessions();
			appLoadApiUserSessions();
			return;
		}

		// Unified notification funnel: server emitted a user-facing message via
		// app/services/notification_service.emit_notification. Already persisted
		// server-side — feed it to the store for live render + the bell. Actions
		// can't cross the wire as closures, so we rebuild interactive ones
		// (e.g. "Fix with AI") here, keyed on `category`.
		if (msgType === 'notification') {
			const n = data.notification as
				| {
						id: string;
						severity: NotificationSeverity;
						surface?: NotificationSurface;
						category?: string;
						title?: string;
						message: string;
						session_id?: string;
						read?: boolean;
						created_at?: number;
						action?: { kind?: string; payload?: Record<string, unknown> } | null;
				  }
				| undefined;
			if (!n) return;

			let action: NotificationAction | undefined;
			let duration_ms: number | undefined;
			if (n.category === 'api_error') {
				// Mirror the legacy api_error UX: 429 is transient (5s, no fix),
				// other errors get 10s + a one-click "Fix with AI".
				duration_ms = n.severity === 'warning' ? 5000 : 10000;
				if (n.severity !== 'warning') {
					const payload = n.action?.payload ?? {};
					action = {
						label: 'Fix with AI',
						onClick: () =>
							startFixChat({
								source: 'ws',
								message: n.message,
								status_code:
									typeof payload.status_code === 'number' ? payload.status_code : undefined,
								session_id: n.session_id ?? (payload.session_id as string | undefined),
							}),
					};
				}
			}

			// Drop the server-side `{kind, payload}` action descriptor — it can't be
			// rendered directly; the rebuilt closure (if any) is passed separately.
			const { action: _serverAction, ...payload } = n;
			notify.ingest({ ...payload, duration_ms }, action);
			return;
		}

		if (msgType === 'user_streaming_changed') {
			if (!data.session_id) return;
			const ssId = data.session_id as string;
			const isStreamingFlag = data.is_streaming as boolean;
			const projectId = (data.project_id as string | null) ?? null;
			const worktreeId = (data.worktree_id as string | null) ?? null;
			streamingSessions.update(map => {
				const newMap = new Map(map);
				if (isStreamingFlag) {
					newMap.set(ssId, { projectId, worktreeId });
				} else {
					newMap.delete(ssId);
				}
				return newMap;
			});
			return;
		}

		if (msgType === 'user_streaming_sessions') {
			const sessions = data.sessions as Array<{ session_id: string; project_id: string | null; worktree_id?: string | null }> | undefined;
			const sessionIds = data.session_ids as string[] | undefined;
			const newMap = new Map<string, StreamingSessionInfo>();
			if (sessions) {
				for (const s of sessions) {
					newMap.set(s.session_id, { projectId: s.project_id ?? null, worktreeId: s.worktree_id ?? null });
				}
			} else if (sessionIds) {
				for (const sid of sessionIds) {
					newMap.set(sid, { projectId: null, worktreeId: null });
				}
			}
			streamingSessions.set(newMap);
			return;
		}

		if (msgType === 'user_branch_changed') {
			const projectId = data.project_id as string;
			const gitProjectId = gitGetProjectId();
			if (gitProjectId === projectId && !gitGetLoading()) {
				gitRefreshStatus();
				gitRefreshDiffStats();
			}
			return;
		}

		// ── Per-session message switch ────────────────────────────────

		switch (msgType) {
			// ── Session lifecycle ─────────────────────────────────────
			// These handlers accept the sessionId and start the SSE feed,
			// which owns the transcript and streaming state from there on.

			case 'session_created': {
				if (!data.session_id) break;
				const sessionId = data.session_id as string;
				const title = (data.title as string) || undefined;
				const currentState = getState();
				// Authenticate new-session adoption: while awaiting, accept ONLY
				// when the frame echoes our client-generated request_id. This blocks
				// the session-adoption hijack — a server-initiated turn for another
				// session (unpark follow-up / queue drain) unparking on the shared
				// device socket during the awaiting window carries no request_id, so
				// it can never be adopted here. See `pendingRequestId`.
				const createdRequestId = data.request_id as string | undefined;
				const createdAwaitingMatch = !currentState.sessionId && internal.awaitingNewSession &&
					!!internal.pendingRequestId && createdRequestId === internal.pendingRequestId;
				const acceptCreated = currentState.sessionId === sessionId || createdAwaitingMatch;
				if (acceptCreated) {
					internal.awaitingNewSession = false;
					internal.pendingRequestId = null;
					// Set the sessionId and title so the header shows the correct
					// session title immediately (not stale 'New Chat'). Reset
					// activeTasks to [] on session commit (mirrors PR #448) so a
					// prior/foreign session's running-task strip can't flash on the
					// freshly-adopted session before the feed settles.
					update(s => ({ ...s, sessionId, activeTasks: [], ...(title ? { title } : {}) }));
					// Deliver a Stop the user clicked before the id existed (see
					// stopRequestedBeforeSession). Now that we have the id, the
					// backend can interrupt and discard the no-output orphan turn.
					if (internal.stopRequestedBeforeSession) {
						internal.stopRequestedBeforeSession = false;
						wsSend({ type: 'stop', session_id: sessionId, stop_background: internal.stopBackgroundRequested });
						internal.stopBackgroundRequested = false;
					}
					// Adopt a freshly-minted session by starting the SSE feed
					// (idempotent — start() no-ops if already feeding this id).
					// The feed's replay renders the turn.
					if (!feedController.isFeeding(sessionId)) {
						feedController.start(sessionId);
					}
					onPersistSession();
				}
				appLoadSessions();
				break;
			}

			// Cloud sessions run on a second install: the backend acks a forwarded
			// prompt with 'cloud_forwarded' (no local stream). Treat it exactly
			// like 'start' — the SSE feed mirrors the remote stream.
			case 'cloud_forwarded':
			case 'start': {
				if (!data.session_id) break;
				const sessionId = data.session_id as string;
				internal.interruptPending = false;
				const currentState = getState();
				// Authenticate new-session adoption (see session_created): while
				// awaiting, accept the `start` frame ONLY when it echoes our
				// client-generated request_id. A server-initiated turn for a foreign
				// session unparking on the shared device socket carries no request_id
				// and so can never hijack the awaiting new chat. See `pendingRequestId`.
				const startRequestId = data.request_id as string | undefined;
				const startAwaitingMatch = !currentState.sessionId && internal.awaitingNewSession &&
					!!internal.pendingRequestId && startRequestId === internal.pendingRequestId;
				const acceptStart = currentState.sessionId === sessionId || startAwaitingMatch;
				// activeTasks is reset only when this frame actually adopts a NEW
				// sessionId (mirrors PR #448) — a `start` for the already-open session
				// (interrupt_for_query follow-up) must not wipe its live task strip.
				const startAdoptsNewSession = currentState.sessionId !== sessionId;
				if (acceptStart) {
					internal.awaitingNewSession = false;
					internal.pendingRequestId = null;
					// Deliver a Stop deferred before the id existed (see
					// stopRequestedBeforeSession) in case 'start' beat
					// 'session_created' here. The flag self-clears so we never
					// double-send.
					if (internal.stopRequestedBeforeSession) {
						internal.stopRequestedBeforeSession = false;
						wsSend({ type: 'stop', session_id: sessionId, stop_background: internal.stopBackgroundRequested });
						internal.stopBackgroundRequested = false;
					}
					// The SSE feed owns the transcript and streaming state. Adopt the
					// sessionId and start the feed; the feed's replay renders the turn.
					update(s => ({
						...s,
						sessionId,
						isStreaming: true,
						streamStartTime: Date.now(),
						...(startAdoptsNewSession ? { activeTasks: [] } : {}),
					}));
					if (!feedController.isFeeding(sessionId)) {
						feedController.start(sessionId);
					}
					onPersistSession();
				}
				break;
			}

			// hook_event: the ungated WS append was removed — the SSE feed replays
			// hook events from the session's event log, so appending here would
			// double-render. Folded into the no-op group below.
			case 'hook_event':
			case 'subagent_start':
			case 'subagent_chunk':
			case 'subagent_done':
			case 'plugin_message':
				break;

			case 'stream_message_delta':
				break;

			case 'tool_use': {
				const tuSessionId = data.session_id as string | undefined;
				if (!tuSessionId || tuSessionId !== getState().sessionId) break;
				const toolId = data.id as string;
				if (toolId) internal.pendingTools.add(toolId);
				break;
			}

			case 'tool_result': {
				const trSessionId = data.session_id as string | undefined;
				if (!trSessionId || trSessionId !== getState().sessionId) break;
				const toolUseId = data.tool_use_id as string;
				if (toolUseId) internal.pendingTools.delete(toolUseId);
				break;
			}

			case 'subagent_tool_use': {
				const satuSessionId = data.session_id as string | undefined;
				if (!satuSessionId || satuSessionId !== getState().sessionId) break;
				const toolId = data.id as string;
				if (toolId) internal.pendingTools.add(toolId);
				break;
			}

			case 'subagent_tool_result': {
				const satrSessionId = data.session_id as string | undefined;
				if (!satrSessionId || satrSessionId !== getState().sessionId) break;
				const toolUseId = data.tool_use_id as string;
				if (toolUseId) internal.pendingTools.delete(toolUseId);
				break;
			}

			case 'done':
				handleDone(data);
				break;

			case 'interrupt_confirmed': {
				// Backend confirmed the interrupt + drain completed.
				// Resolve the promise so sendMessage can proceed with the query.
				const confirmedSessionId = data.session_id as string | undefined;
				const currentSt = getState();
				if (confirmedSessionId && confirmedSessionId === currentSt.sessionId && internal.interruptConfirmResolve) {
					internal.interruptConfirmResolve(true);
					internal.interruptConfirmResolve = null;
				}
				break;
			}

			case 'interrupt_failed': {
				// Backend failed to interrupt — resolve with false so sendMessage
				// can restore the message to the input.
				const failedSessionId = data.session_id as string | undefined;
				const failedSt = getState();
				if (failedSessionId && failedSessionId === failedSt.sessionId && internal.interruptConfirmResolve) {
					internal.interruptConfirmResolve(false);
					internal.interruptConfirmResolve = null;
				}
				break;
			}

			case 'stopped':
			case 'interrupted':
				handleStoppedOrInterrupted(data);
				break;

			case 'user_message_discarded': {
				// The user pressed Stop before any assistant output. The backend
				// removed the orphan user message from the live store (so the
				// turn isn't shown as a saved-but-empty exchange) and sent its
				// text back so we can restore it to the input box for editing or
				// resending. Set pendingInterruptPrompt (ChatInput watches it and
				// repopulates the editor); the SSE feed drops the now-removed user
				// message from the transcript.
				const discardedSessionId = data.session_id as string | undefined;
				const prompt = typeof data.prompt === 'string' ? data.prompt : '';
				const discardState = getState();
				if (discardedSessionId && discardedSessionId === discardState.sessionId) {
					update(s => ({ ...s, pendingInterruptPrompt: prompt }));
				}
				break;
			}

			case 'error': {
				// Route by the EVENT's session_id, not the active session — mirror
				// handleDone / handleStoppedOrInterrupted. A BACKGROUND session's
				// error must never reset the ACTIVE session's isStreaming or unwind
				// its interrupt-and-query promise. The backend now stamps session_id
				// on error events (interactive + headless paths); fall back to the
				// active session only when the id is absent (e.g. a synchronous
				// pre-stream command error like an empty prompt).
				const errSessionId = data.session_id as string | undefined;
				const errState = getState();
				const isActiveError = !errSessionId || errSessionId === errState.sessionId;
				const erroredSessionId = errSessionId || errState.sessionId;
				const errMessage = data.message as string;

				// Cross-session safe: drop the errored session's streaming pill
				// regardless of which session is in view.
				if (erroredSessionId) {
					streamingSessions.update(map => {
						if (!map.has(erroredSessionId)) return map;
						const newMap = new Map(map);
						newMap.delete(erroredSessionId);
						return newMap;
					});
				}

				// Always log for observability (keyed to the session that errored).
				reportClientError({
					source: 'ws',
					message: errMessage || 'Stream error',
					detail: typeof data.detail === 'string' ? data.detail : undefined,
					session_id: erroredSessionId ?? undefined,
				});

				// Only the in-view session surfaces the banner, resets its streaming
				// flag (so the user can send again), and rejects a pending
				// interrupt-and-query promise.
				if (isActiveError) {
					internal.interruptPending = false;
					if (internal.interruptConfirmResolve) {
						internal.interruptConfirmResolve(false);
						internal.interruptConfirmResolve = null;
					}
					update(s => ({ ...s, error: errMessage, isStreaming: false, streamStartTime: null }));
				}
				break;
			}

			case 'session_not_found': {
				if (!data.session_id) break;
				const missingId = data.session_id as string;
				console.warn(`[ActiveSession] Session not found: ${missingId}`);
				appLoadSessions();
				const currentId = getState().sessionId;
				if (currentId === missingId) {
					update(s => ({ ...s, error: 'This session no longer exists.' }));
					onNewChat();
				}
				break;
			}

			case 'query_queued': {
				// Send-now replacement routed to the queue (Finding F5). The user
				// clicked "Send now" mid-stream: sendMessage sent interrupt_for_query,
				// latched internal.interruptPending, and set optimistic isStreaming=true.
				// The backend confirmed the interrupt, we sent the query — but the pooled
				// client was still winding down the force-cancelled turn, so the backend
				// queued the query instead of starting a turn and acked with this frame
				// (the queue chip is driven by the separate queue_updated broadcast).
				//
				// Without this handler interruptPending stays latched and isStreaming
				// stays optimistically true → the spinner never clears. Release the
				// latch, drop the optimistic streaming state, and clean the
				// streaming-session map (mirrors the done/stopped terminal frames).
				// The SSE feed truth-ups the transcript.
				const queuedSessionId = data.session_id as string | undefined;
				const queuedState = getState();
				if (!queuedSessionId || queuedSessionId !== queuedState.sessionId) break;

				internal.interruptPending = false;
				internal.pendingTools.clear();
				clearStopTimeout();
				update(s => ({ ...s, isStreaming: false, streamStartTime: null }));
				streamingSessions.update(map => {
					if (!map.has(queuedSessionId)) return map;
					const newMap = new Map(map);
					newMap.delete(queuedSessionId);
					return newMap;
				});
				break;
			}

			case 'message_queued':
			case 'queued_message_processing':
				// Legacy — replaced by queue_updated
				break;

			case 'session_locked': {
				const lockedBy = data.locked_by as string;
				console.warn(`[ActiveSession] Session locked by device ${lockedBy}`);
				update(s => ({
					...s,
					error: 'This session is already open on another device. Click "Take Over" to use it here.',
					sessionLocked: true,
					sessionLockedBy: lockedBy,
				}));
				break;
			}

			case 'session_takeover': {
				const newDevice = data.new_device as string;
				console.warn(`[ActiveSession] Session taken over by device ${newDevice}`);
				update(s => ({
					...s,
					error: 'This session was taken over by another device.',
					sessionLocked: false,
					sessionLockedBy: undefined,
				}));
				break;
			}

			case 'ping': {
				wsSend({ type: 'pong' });
				break;
			}

			case 'api_error': {
				// API-level errors (e.g. Anthropic 429/5xx) are now surfaced through
				// the unified notification funnel: the backend emits a `notification`
				// event (persisted + the bell + every device) which the handler above
				// renders — including the "Fix with AI" action. We no longer toast
				// here (that would double up). We DO still log to the durable
				// error_log for observability (GetAppErrors), keyed off `status`.
				const status = Number(data.status ?? 0);
				const customMessage = typeof data.message === 'string' ? data.message : '';
				let message: string;
				if (status === 429) {
					message = customMessage || 'Rate limited — slow down or wait for the next window.';
				} else if (status >= 500 && status < 600) {
					message = customMessage || `Service error: HTTP ${status}`;
				} else if (status > 0) {
					message = customMessage || `API error: HTTP ${status}`;
				} else {
					message = customMessage || 'An API error occurred.';
				}
				const apiErrSessionId = getState().sessionId ?? undefined;
				reportClientError({
					source: 'ws',
					message,
					status_code: status > 0 ? status : undefined,
					session_id: apiErrSessionId,
				});
				break;
			}

			case 'rate_limit': {
				// Live SDK rate-limit event — refines a single usage window of the
				// EMITTING install's bucket. `instance_id` tags which install it came
				// from (relayed remote-session events carry the worker's id); the
				// store routes by it so an offloaded session's usage lands in the
				// right bucket. The UsageLimitIndicator reads the store reactively.
				rateLimit.applyLiveEvent({
					status: (data.status as string) ?? 'unknown',
					rate_limit_type: (data.rate_limit_type as string) ?? 'unknown',
					resets_at: data.resets_at as string | undefined,
					utilization: data.utilization as number | undefined,
					session_id: data.session_id as string | undefined,
					instance_id: (data.instance_id as string | null | undefined) ?? undefined,
				});
				break;
			}

			case 'heartbeat': {
				const pendingPermCount = data.pending_permissions as number | undefined;
				const pendingQCount = data.pending_questions as number | undefined;
				if (pendingPermCount && pendingPermCount > 0) {
					const cs = getState();
					if (!cs.pendingPermissions || cs.pendingPermissions.length === 0) {
						wsSend({ type: 'get_pending_permissions' });
					}
				}
				if (pendingQCount && pendingQCount > 0) {
					const cs = getState();
					if (!cs.pendingQuestions || cs.pendingQuestions.length === 0) {
						wsSend({ type: 'get_pending_questions' });
					}
				}
				break;
			}

			case 'permission_request': {
				const requestSessionId = data.session_id as string | undefined;
				const currentSessionId = getState().sessionId;
				if (requestSessionId && requestSessionId !== currentSessionId) {
					bumpNeedsAttention(requestSessionId);
					break;
				}
				const request = {
					session_id: requestSessionId ?? currentSessionId ?? undefined,
					request_id: data.request_id as string,
					tool_name: data.tool_name as string,
					tool_input: data.tool_input as Record<string, unknown>,
					queue_position: data.queue_position as number | undefined,
					queue_total: data.queue_total as number | undefined,
					// Slice D enrichment (SDK 0.1.81+) — forwarded only when present.
					title: data.title as string | undefined,
					display_name: data.display_name as string | undefined,
					description: data.description as string | undefined,
					decision_reason: data.decision_reason as string | undefined,
					blocked_path: data.blocked_path as string | undefined,
				} as PermissionRequestType;

				setNeedsAttention(request.session_id, 0);
				onNotifyWaitingForResponse('permission', { toolName: request.tool_name });
				update(s => ({
					...s,
					pendingPermissions: [...s.pendingPermissions, request],
					// Slice B: if this request was previously deferred and is now
					// being re-broadcast (after `requeue_deferred_permissions`),
					// drop it from the deferred queue to avoid duplicates.
					deferredPermissions: s.deferredPermissions.filter(
						p => p.request_id !== request.request_id
					),
				}));
				break;
			}

			case 'permission_queue_update': {
				const queueSessionId = data.session_id as string | undefined;
				if (queueSessionId && queueSessionId !== getState().sessionId) {
					const remainingCount = data.remaining_count as number | undefined;
					if (remainingCount !== undefined) setNeedsAttention(queueSessionId, remainingCount);
					break;
				}
				const resolvedIds = data.resolved_ids as string[];
				update(s => ({
					...s,
					pendingPermissions: s.pendingPermissions.filter(p => !resolvedIds.includes(p.request_id)),
				}));
				break;
			}

			case 'permission_response_ack':
			case 'permission_timeout': {
				const responseSessionId = data.session_id as string | undefined;
				if (responseSessionId && responseSessionId !== getState().sessionId) break;
				const requestId = data.request_id as string;
				update(s => ({
					...s,
					pendingPermissions: s.pendingPermissions.filter(p => p.request_id !== requestId),
				}));
				break;
			}

			case 'pending_permissions': {
				const pendingSessionId = data.session_id as string | undefined;
				// A cross-install relay-read failure replies with `stale: true` and a
				// non-authoritative (local, empty) list. Replacing pendingPermissions /
				// deferredPermissions with it would wipe an open permission modal on
				// every reconnect/switch resync. Leave state untouched; a later
				// authoritative reply (no marker) reconciles. A successful relay, even
				// with zero items, is authoritative and omits the marker so it flows
				// through below.
				if (data.stale === true) {
					console.debug('[ws] ignoring stale pending_permissions (relay to owner failed)');
					break;
				}
				// Some backends spell the active-queue field `requests`, others
				// (per SDK 0.1.81 contract) ship it under `pending`. Accept both.
				const activeRaw =
					(data.requests as PermissionRequestType[] | undefined) ??
					(data.pending as PermissionRequestType[] | undefined) ??
					[];
				const requests = mapPermissionRequests(activeRaw, pendingSessionId);
				// Slice B: a fresh-server (SDK 0.1.81+) also reports deferred items.
				const deferredRaw = (data.deferred as PermissionRequestType[] | undefined) ?? [];
				const deferred = mapPermissionRequests(deferredRaw, pendingSessionId);

				if (pendingSessionId && pendingSessionId !== getState().sessionId) {
					setNeedsAttention(pendingSessionId, requests.length);
					break;
				}
				setNeedsAttention(pendingSessionId, 0);
				update(s => ({
					...s,
					pendingPermissions: requests,
					deferredPermissions: deferred,
				}));
				break;
			}

			case 'user_question': {
				const questionSessionId = data.session_id as string | undefined;
				const currentSessionId = getState().sessionId;
				if (questionSessionId && questionSessionId !== currentSessionId) {
					bumpNeedsAttention(questionSessionId);
					break;
				}
				const rawQuestions = data.questions as Array<Record<string, unknown>>;
				const normalizedQuestions = rawQuestions.map((q, idx) => ({
					question: q.question as string || '',
					header: q.header as string || `Question ${idx + 1}`,
					options: Array.isArray(q.options) ? q.options.map((opt: unknown) => ({
						label: typeof opt === 'object' && opt !== null ? (opt as Record<string, unknown>).label as string || '' : String(opt),
						description: typeof opt === 'object' && opt !== null ? (opt as Record<string, unknown>).description as string || '' : '',
					})) : [],
					multiSelect: Boolean(q.multiSelect),
				}));

				const request = {
					session_id: questionSessionId ?? currentSessionId ?? undefined,
					request_id: data.request_id as string,
					tool_use_id: data.tool_use_id as string,
					questions: normalizedQuestions,
				} as UserQuestionRequest;

				setNeedsAttention(request.session_id, 0);
				onNotifyWaitingForResponse('question', { questionCount: request.questions.length });
				// Dedup by request_id: a reconnect resync (get_pending_questions ->
				// pending_questions) can race with this live event, and without this
				// guard the same question would render twice.
				update(s => s.pendingQuestions.some(q => q.request_id === request.request_id)
					? s
					: { ...s, pendingQuestions: [...s.pendingQuestions, request] });
				break;
			}

			case 'user_question_response_ack':
			case 'user_question_timeout': {
				const responseSessionId = data.session_id as string | undefined;
				if (responseSessionId && responseSessionId !== getState().sessionId) break;
				const requestId = data.request_id as string;
				// A failed ack carries WHY it failed. Only a retryable delivery
				// failure (`reason: 'relay_offline'` — owner install unreachable)
				// keeps the modal up so the user's in-progress answer isn't lost.
				// Every other failure (`not_pending`, or a legacy ack without a
				// reason) means the owner authoritatively has no such pending
				// question — the modal is stale and must be dismissed, matching
				// pre-relay behavior. The shared `user_question_timeout` type
				// carries NO success field, so it also removes unconditionally.
				if (
					data.type === 'user_question_response_ack' &&
					data.success === false &&
					data.reason === 'relay_offline'
				) {
					notify.error(
						"Couldn't deliver your answer — the owning device is unreachable. Try again.",
						{ category: 'user-question-relay-error', session_id: responseSessionId }
					);
					break;
				}
				update(s => ({
					...s,
					pendingQuestions: s.pendingQuestions.filter(q => q.request_id !== requestId),
				}));
				break;
			}

			case 'pending_questions': {
				const pendingSessionId = data.session_id as string | undefined;
				// A cross-install relay-read failure replies with `stale: true` and a
				// non-authoritative (local, empty) list. Replacing pendingQuestions with
				// it would wipe a locally-open question modal (losing in-progress
				// answers) on every reconnect/switch resync. Leave state untouched; a
				// later authoritative reply (no marker) reconciles. A successful relay,
				// even with zero questions, is authoritative and still clears stale
				// modals — that omits the marker so it flows through below.
				if (data.stale === true) {
					console.debug('[ws] ignoring stale pending_questions (relay to owner failed)');
					break;
				}
				const questions = mapUserQuestions(
					(data.questions as UserQuestionRequest[] | undefined) ?? [],
					pendingSessionId
				);
				if (pendingSessionId && pendingSessionId !== getState().sessionId) {
					setNeedsAttention(pendingSessionId, questions.length);
					break;
				}
				setNeedsAttention(pendingSessionId, 0);
				update(s => {
					const existingIds = new Set(s.pendingQuestions.map(q => q.request_id));
					const newIds = new Set(questions.map(q => q.request_id));
					const same = existingIds.size === newIds.size && [...existingIds].every(id => newIds.has(id));
					if (same) return s;
					return { ...s, pendingQuestions: questions };
				});
				break;
			}

			case 'session_attention': {
				// User-level broadcast: a (possibly background) session is awaiting
				// the user's input. Reaches every one of the user's devices, so the
				// sessions board lights up regardless of which tab owns the query.
				const attnSessionId = data.session_id as string | undefined;
				const count = (data.count as number | undefined) ?? 0;
				// The active session surfaces the prompt through its own modal, not a
				// background dot. But session_attention is the ONE signal that always
				// reaches us: the per-socket user_question/permission_request event is
				// lost whenever its origin socket died (reconnect mid-turn rebinds
				// broadcast_func nowhere), the turn is headless, or it originated on
				// another device/install. So rather than blindly ignore it, use it as a
				// resync trigger — ask the server to replay the pending queues for the
				// active session. The pending_permissions/pending_questions replies
				// REPLACE by request_id, so re-requesting never double-shows a modal.
				if (count > 0 && attnSessionId === getState().sessionId) {
					const cs = getState();
					if (!cs.pendingPermissions || cs.pendingPermissions.length === 0) {
						wsSend({ type: 'get_pending_permissions' });
					}
					if (!cs.pendingQuestions || cs.pendingQuestions.length === 0) {
						wsSend({ type: 'get_pending_questions' });
					}
					break;
				}
				setNeedsAttention(attnSessionId, count);
				break;
			}

			default:
				break;
		}
	}

	return { handleMessage };
}
