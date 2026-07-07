/**
 * WebSocket Message Handlers — Notification-only dispatch for incoming WS messages.
 *
 * Architecture (polling-only):
 * - Handlers NEVER write session message state (_update with messages/isStreaming).
 * - Session state is written SOLELY by the session-poller via onPollApplied.
 * - WS handlers trigger polls (pollNow/startStreaming) when events arrive.
 * - WS handlers DO manage non-message state: permissions, questions, streaming
 *   sessions map, git state, and fire side-effect callbacks (notifications, etc).
 *
 * The 'history' handler is gone — session loading uses HTTP via sessionPoller.loadSession().
 */

import type { PermissionRequest as PermissionRequestType, UserQuestionRequest } from '$lib/api/client';
import type { ActiveSessionState, StreamingSessionInfo } from '../active-session';
import { notify, type NotificationAction, type NotificationSeverity, type NotificationSurface } from '../notifications';
import { rateLimit } from '../rate-limit';
import { reportClientError } from '$lib/services/error-reporter';
import { startFixChat } from '$lib/services/fix-with-ai';

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

	// Session poller & notifier
	sessionPoller: {
		pollNow: (sessionId: string) => void;
		pollFinal: (sessionId: string) => void;
		startStreaming: (sessionId: string) => void;
		stop: (sessionId: string) => void;
		isPolling: (sessionId: string) => boolean;
		loadSession: (sessionId: string) => Promise<unknown>;
	};
	sessionNotifier: {
		handleMessage: (sessionId: string, data: Record<string, unknown>) => void;
	};

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
		sessionPoller, sessionNotifier,
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
		// generator exits normally). If we process it, we'd clear interruptPending,
		// trigger pollFinal (which returns isStreaming=false for the ended old query),
		// and fire a premature completion notification — all of which clobber the
		// new query that's about to start. Skip entirely, like handleStoppedOrInterrupted.
		if (internal.interruptPending) { internal.pendingTools.clear(); return; }

		const metadata = data.metadata as Record<string, unknown>;
		const sessionId = data.session_id as string;
		// done event — poller will handle streaming→ended transition

		internal.interruptPending = false;

		const currentState = getState();
		const isActiveSession = !sessionId || sessionId === currentState.sessionId;
		const doneSessionId = sessionId || currentState.sessionId;

		// Trigger a final authoritative poll. Uses pollFinal (not pollNow)
		// because pollNow is debounced and its timer gets killed by stop()
		// if the interval timer's in-flight poll detects streamingJustEnded
		// first. pollFinal survives stop() so the post-end_streaming state
		// is always fetched, even if the interval poll got stale data.
		if (doneSessionId) sessionPoller.pollFinal(doneSessionId);

		if (isActiveSession) {
			clearStopTimeout();
			internal.pendingTools.clear();
			// Clear WS stream buffer — poller's final poll is authoritative
			const preState = getState();
			const preAssistantTexts = preState.messages.filter(
				(m) => m.role === 'assistant' && m.type === 'text'
			);
			console.warn(
				`[handleDone] clearing wsStreamContent`,
				`\n  wsStreamContent len: ${preState.wsStreamContent.length}`,
				`\n  isStreaming: ${preState.isStreaming}`,
				`\n  msgs: ${preState.messages.length}`,
				`\n  assistant texts: ${preAssistantTexts.length} [${preAssistantTexts.map(m => `id=${m.id} len=${(m.content||'').length} streaming=${m.streaming}`).join(', ')}]`
			);
			update(s => s.wsStreamContent ? { ...s, wsStreamContent: '' } : s);

			// Fire completion notification with metadata from the WS event.
			// State (isStreaming, messages) will be updated by the poller.
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
		// not clobber the ACTIVE session's pendingTools / wsStreamContent or fire
		// a spurious pollFinal on it. Pre-fix this handler ignored data.session_id
		// entirely and always mutated whatever session was in view.
		const sessionId = data.session_id as string | undefined;
		const currentState = getState();
		const isActiveSession = !sessionId || sessionId === currentState.sessionId;
		const stoppedSessionId = sessionId || currentState.sessionId;

		// Trigger a final authoritative poll for the session that actually
		// stopped (survives stop(), see handleDone).
		if (stoppedSessionId) sessionPoller.pollFinal(stoppedSessionId);

		if (isActiveSession) {
			clearStopTimeout();
			internal.pendingTools.clear();
			// Clear WS stream buffer
			update(s => s.wsStreamContent ? { ...s, wsStreamContent: '' } : s);
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

		// Forward to notification system (triggers poller via pollNow/startStreaming).
		//
		// CRITICAL: Use the event's actual session_id for routing, NOT the active
		// session's ID. Events from background/old sessions must not trigger polls
		// for the active session — that causes ghost polls that refresh context
		// values from JSONL, producing phantom token count jumps (16% → 99%).
		//
		// Only forward events that belong to the active session (or new sessions
		// we're awaiting). Events for other sessions are handled by handleDone/
		// handleStoppedOrInterrupted directly — the notifier doesn't need them.
		const state = getState();
		const eventSessionId = data.session_id as string | undefined;
		const isForActiveSession = !eventSessionId ||
			eventSessionId === state.sessionId ||
			(!state.sessionId && internal.awaitingNewSession);

		if (state.sessionId && isForActiveSession) {
			const isInterruptTransition = internal.interruptPending &&
				(msgType === 'interrupted' || msgType === 'stopped' || msgType === 'done');
			const shouldNotify = state.isStreaming ||
				internal.awaitingNewSession ||
				msgType === 'start' ||
				msgType === 'stream_start' ||
				msgType === 'done' ||
				msgType === 'stopped' ||
				msgType === 'interrupted' ||
				// PARKED-WATCHER RECOVERY (Bug 2): when a watched session parks on a
				// long-running background task and later UNPARKS, the follow-up turn
				// streams server-side and `sync_engine` broadcasts it as an
				// `event_type` ('stream_start'/'stream_end'/'stream_chunk') — NOT a
				// `type`. A parked watcher has `isStreaming=false` and no other
				// condition above matches an event_type, so without these the
				// notifier was never reached, the stopped poller never restarted, and
				// the UI showed stale state until a manual reload. We're already
				// inside the `isForActiveSession` guard, so this only ever fires for
				// the session in view — no cross-session ghost polls. The notifier
				// maps stream_start→startStreaming, stream_end→final poll, and has a
				// stream_chunk fallback that restarts a dropped poller mid-stream.
				eventType === 'stream_start' ||
				eventType === 'stream_end' ||
				eventType === 'stream_chunk';
			if (!isInterruptTransition && shouldNotify) {
				sessionNotifier.handleMessage(state.sessionId, data);
			}
		}

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

			// message_added from server-side queue processing (_process_next_queued).
			// The notifier gate above may skip this event when not streaming, so
			// handle it directly: trigger a poll so the user message appears in chat.
			if (eventType === 'message_added') {
				const addedSessionId = data.session_id as string | undefined;
				if (addedSessionId && addedSessionId === state.sessionId) {
					sessionPoller.pollNow(addedSessionId);
				}
			}

			return;
		}

		// ── User-level messages (not per-session) ─────────────────────

		if (msgType === 'tabs_sync') return;

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
			// ── Session lifecycle: notification-only ──────────────────
			// The poller is the sole authority for session state.
			// These handlers only need to accept the sessionId and ensure
			// polling is started.

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
					// freshly-adopted session before the first poll settles.
					update(s => ({ ...s, sessionId, activeTasks: [], ...(title ? { title } : {}) }));
					// Deliver a Stop the user clicked before the id existed (see
					// stopRequestedBeforeSession). Now that we have the id, the
					// backend can interrupt and discard the no-output orphan turn.
					if (internal.stopRequestedBeforeSession) {
						internal.stopRequestedBeforeSession = false;
						wsSend({ type: 'stop', session_id: sessionId });
					}
					onPersistSession();
				}
				appLoadSessions();
				break;
			}

			// Cloud sessions run on a second install: the backend acks a forwarded
			// prompt with 'cloud_forwarded' (no local stream). Treat it exactly
			// like 'start' so the streaming indicator shows and the poller runs —
			// the proxied /live endpoint then mirrors the remote stream.
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
						wsSend({ type: 'stop', session_id: sessionId });
					}
					// Set sessionId, streamStartTime, and inject a synthetic streaming
					// assistant message so the streaming indicator appears immediately
					// (don't wait for the poller to deliver the first assistant message).
					// Also explicitly set isStreaming: true — during interrupt_for_query,
					// a stale poll from the old streaming session can clobber isStreaming
					// to false between the interrupt and this start event.
					const messageId = data.message_id as string || `ws-stream-${Date.now()}`;
					update(s => {
						// Only inject if there isn't already a streaming assistant message
						const hasStreamingAssistant = s.messages.some(
							m => m.role === 'assistant' && m.streaming
						);
						if (hasStreamingAssistant) {
							return { ...s, sessionId, isStreaming: true, streamStartTime: Date.now(), wsStreamContent: '', ...(startAdoptsNewSession ? { activeTasks: [] } : {}) };
						}
						return {
							...s,
							sessionId,
							isStreaming: true,
							streamStartTime: Date.now(),
							wsStreamContent: '',
							...(startAdoptsNewSession ? { activeTasks: [] } : {}),
							messages: [...s.messages, {
								id: messageId,
								role: 'assistant' as const,
								type: 'text' as const,
								content: '',
								streaming: true,
								timestamp: new Date().toISOString(),
							}],
						};
					});
					// Ensure poller is running
					sessionPoller.startStreaming(sessionId);
					onPersistSession();
				}
				break;
			}

			// Notification-only events — poller handles state via sessionNotifier
			case 'history':
				// History is now loaded via HTTP (sessionPoller.loadSession).
				// If we receive a WS history message, just trigger a poll to sync.
				if (data.session_id && state.sessionId === data.session_id) {
					const historySessionId = data.session_id as string;
					sessionPoller.pollNow(historySessionId);
					const historyPendingPermissions = mapPermissionRequests(
						(data.pendingPermissions as PermissionRequestType[] | undefined) ?? [],
						historySessionId
					);
					const historyPendingQuestions = mapUserQuestions(
						(data.pendingQuestions as UserQuestionRequest[] | undefined) ?? [],
						historySessionId
					);
					// Populate server-side queue state from history response
					const historyQueued = data.queuedMessages as Array<{ id: string; content: string; created_at: string }> | undefined;
					setNeedsAttention(historySessionId, 0);
					update(s => ({
						...s,
						pendingPermissions: historyPendingPermissions,
						pendingQuestions: historyPendingQuestions,
						queuedMessages: historyQueued
							? historyQueued.map(m => ({ id: m.id, content: m.content, queuedAt: new Date(m.created_at).getTime() }))
							: [],
					}));
				}
				break;

			case 'stream_delta': {
				// Accumulate text content from WS into wsStreamContent buffer.
				// Do NOT modify messages here — the poller's onPollApplied callback
				// is the SOLE writer of message state. It merges wsStreamContent into
				// the streaming message when WS is ahead of the server. Modifying
				// messages here caused flickering: after the poller cleared
				// wsStreamContent, the next delta would set message content to just
				// the delta text (losing the base), then the poller would restore
				// full content — causing the UI to alternate between partial and
				// full content every poll cycle.
				//
				// Session guard: only accept deltas for the active session.
				// Without this, late-arriving deltas from a previous session
				// (in-flight when the user switched) contaminate the buffer
				// and bleed into the new session's messages.
				const deltaSessionId = data.session_id as string | undefined;
				const deltaState = getState();
				// Reject if session_id missing OR doesn't match active session.
				// Previously only rejected on mismatch, letting events without
				// session_id through — root cause of cross-session bleed.
				if (!deltaSessionId || deltaSessionId !== deltaState.sessionId) break;

				const deltaType = data.delta_type as string;
				if (deltaType === 'text') {
					const content = data.content as string;
					if (content) {
						update(s => ({ ...s, wsStreamContent: s.wsStreamContent + content }));
					}
				}
				// Ensure polling is started
				if (deltaState.isStreaming && deltaState.sessionId && !sessionPoller.isPolling(deltaState.sessionId)) {
					sessionPoller.startStreaming(deltaState.sessionId);
				}
				break;
			}
			case 'chunk': {
				// Legacy chunk (include_partial_messages=false) — accumulate into
				// wsStreamContent only. Same rationale as stream_delta: the poller's
				// onPollApplied is the sole message writer to prevent flickering.
				//
				// Session guard: same as stream_delta — reject chunks from
				// non-active sessions to prevent cross-session bleed.
				const chunkSessionId = data.session_id as string | undefined;
				const chunkState = getState();
				// Reject if session_id missing OR doesn't match (same fix as stream_delta)
				if (!chunkSessionId || chunkSessionId !== chunkState.sessionId) break;

				const chunkContent = data.content as string;
				if (chunkContent) {
					update(s => ({ ...s, wsStreamContent: s.wsStreamContent + chunkContent }));
				}
				if (chunkState.isStreaming && chunkState.sessionId && !sessionPoller.isPolling(chunkState.sessionId)) {
					sessionPoller.startStreaming(chunkState.sessionId);
				}
				break;
			}
			case 'stream_block_stop': {
				// End of ONE content block. Session guard: reject non-active sessions.
				const blockSessionId = data.session_id as string | undefined;
				const blockState = getState();
				if (!blockSessionId || blockSessionId !== blockState.sessionId) break;
				// Clear the WS text buffer at the block boundary — the principled fix
				// for the "mega-blob" incident. `wsStreamContent` accumulates every
				// stream_delta with no separator and is otherwise cleared ONLY at turn
				// boundaries (done / stopped / start). When a text block finalizes
				// mid-turn into a tool call, nothing clears the buffer at the source,
				// so cross-block deltas pile up and the poll-time staleness drop in
				// mergeWsStreamBuffer is the sole guard — which failed the moment an
				// upstream bug split one block into two finalized messages (the last
				// finalized short tail can never startsWith the long buffer). Clearing
				// here makes cross-block accumulation impossible: the next block refills
				// from empty (the documented intent, see session-init.ts merge notes).
				// Non-text blocks (tool_use/thinking) never wrote to the buffer, so this
				// clears an already-empty buffer — harmless. The `s.wsStreamContent ?`
				// guard keeps the state reference stable when nothing to clear.
				update(s => (s.wsStreamContent ? { ...s, wsStreamContent: '' } : s));
				// Ensure polling is started if we're streaming but poller isn't running.
				if (blockState.isStreaming && !sessionPoller.isPolling(blockState.sessionId)) {
					sessionPoller.startStreaming(blockState.sessionId);
				}
				break;
			}
			case 'stream_start':
			case 'stream_block_start':
			case 'system': {
				// Session guard: reject events not targeting the active session
				const sysSessionId = data.session_id as string | undefined;
				const sysState = getState();
				if (!sysSessionId || sysSessionId !== sysState.sessionId) break;
				// Ensure polling is started if we're streaming but poller isn't running.
				// This handles slash commands that don't trigger a 'start' event.
				if (sysState.isStreaming && !sessionPoller.isPolling(sysState.sessionId)) {
					sessionPoller.startStreaming(sysState.sessionId);
				}
				break;
			}
			case 'hook_event': {
				// SDK 0.1.81 hook pipeline event. Append as a collapsed system
				// message so users can see what hooks fire (with optional
				// allow/deny/defer decisions) without spamming the chat. The
				// poller never replays these — they're WS-only — so we write
				// directly to `messages` (mirrors `draft-manager.addSystemMessage`).
				const hookSessionId = data.session_id as string | undefined;
				const hookState = getState();
				if (!hookSessionId || hookSessionId !== hookState.sessionId) break;

				type HookEventPayload = {
					hook_event_name: string | null;
					matcher: string | null;
					decision: string | null;
					reason: string | null;
					system_message: string | null;
					tool_name: string | null;
					timestamp: string;
				};
				const hookData: HookEventPayload = {
					hook_event_name: (data.hook_event_name as string | null) ?? null,
					matcher: (data.matcher as string | null) ?? null,
					decision: (data.decision as string | null) ?? null,
					reason: (data.reason as string | null) ?? null,
					system_message: (data.system_message as string | null) ?? null,
					tool_name: (data.tool_name as string | null) ?? null,
					timestamp: (data.timestamp as string) ?? new Date().toISOString(),
				};

				const hookLabel =
					hookData.hook_event_name ||
					hookData.tool_name ||
					'hook_event';

				update(s => ({
					...s,
					messages: [
						...s.messages,
						{
							id: `hook-${hookData.timestamp}-${s.messages.length}`,
							role: 'system' as const,
							content: hookLabel,
							type: 'system' as const,
							systemSubtype: 'hook_event',
							systemData: hookData as unknown as Record<string, unknown>,
						},
					],
				}));
				break;
			}

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
				// repopulates the editor) and run a final poll so the poller —
				// the sole authority for the chat view — drops the now-removed
				// user message.
				const discardedSessionId = data.session_id as string | undefined;
				const prompt = typeof data.prompt === 'string' ? data.prompt : '';
				const discardState = getState();
				if (discardedSessionId && discardedSessionId === discardState.sessionId) {
					update(s => ({ ...s, pendingInterruptPrompt: prompt }));
					sessionPoller.pollFinal(discardedSessionId);
				}
				break;
			}

			case 'error': {
				// Route by the EVENT's session_id, not the active session — mirror
				// handleDone / handleStoppedOrInterrupted. A BACKGROUND session's
				// error must never stop the ACTIVE session's poller, reset its
				// isStreaming, blow away its wsStreamContent, or unwind its
				// interrupt-and-query promise. The backend now stamps session_id on
				// error events (interactive + headless paths); fall back to the
				// active session only when the id is absent (e.g. a synchronous
				// pre-stream command error like an empty prompt).
				const errSessionId = data.session_id as string | undefined;
				const errState = getState();
				const isActiveError = !errSessionId || errSessionId === errState.sessionId;
				const erroredSessionId = errSessionId || errState.sessionId;
				const errMessage = data.message as string;

				// Cross-session safe: stop the errored session's poller and drop its
				// streaming pill regardless of which session is in view.
				if (erroredSessionId) {
					sessionPoller.stop(erroredSessionId);
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
					update(s => ({ ...s, error: errMessage, isStreaming: false, streamStartTime: null, wsStreamContent: '' }));
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
				// Slice C: pipe to the rate-limit store. UsageLimitIndicator reads
				// reactively and renders a compact 5-hour usage pill + countdown.
				rateLimit.set({
					status: (data.status as string) ?? 'unknown',
					rate_limit_type: (data.rate_limit_type as string) ?? 'unknown',
					resets_at: data.resets_at as string | undefined,
					utilization: data.utilization as number | undefined,
					session_id: data.session_id as string | undefined,
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
