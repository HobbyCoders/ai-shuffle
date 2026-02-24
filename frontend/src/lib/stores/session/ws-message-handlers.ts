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
import { messageCache } from '../message-cache';

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
	gitApplyServerUpdate: (projectId: string, worktreeId: string | null, changes: Record<string, unknown>) => void;

	// Callbacks for app-level side effects
	appLoadSessions: () => void;
	appLoadApiUserSessions: () => void;

	// Internal mutable state (shared reference)
	internal: InternalMutableState;

	// Callbacks for complex side effects that remain in active-session.ts
	onNewChat: () => void;
	onPersistSession: () => void;
	onProcessNextQueuedMessage: () => void;
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
		gitRefreshStatus, gitRefreshDiffStats, gitGetProjectId, gitGetLoading, gitApplyServerUpdate,
		appLoadSessions, appLoadApiUserSessions,
		internal,
		onNewChat, onPersistSession,
		onProcessNextQueuedMessage,
		onNotifySessionComplete, onNotifyWaitingForResponse,
		clearStopTimeout,
	} = deps;

	// ── Handler: done ────────────────────────────────────────────────

	function handleDone(data: Record<string, unknown>): void {
		const metadata = data.metadata as Record<string, unknown>;
		const sessionId = data.session_id as string;

		internal.interruptPending = false;

		const currentState = getState();
		const isActiveSession = !sessionId || sessionId === currentState.sessionId;
		const doneSessionId = sessionId || currentState.sessionId;

		// Trigger a final poll — the poller will detect streaming→ended
		// transition and update state authoritatively via onPollApplied.
		if (doneSessionId) sessionPoller.pollNow(doneSessionId);

		if (isActiveSession) {
			clearStopTimeout();
			internal.pendingTools.clear();

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
			setTimeout(() => onProcessNextQueuedMessage(), 500);
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

		appLoadSessions();
	}

	// ── Handler: stopped / interrupted ───────────────────────────────

	function handleStoppedOrInterrupted(_data: Record<string, unknown>): void {
		if (internal.interruptPending) { internal.pendingTools.clear(); return; }

		const currentState = getState();
		const stoppedSessionId = currentState.sessionId;

		// Trigger a poll — poller will detect streaming ended.
		if (stoppedSessionId) sessionPoller.pollNow(stoppedSessionId);

		clearStopTimeout();
		internal.pendingTools.clear();

		// Clean up streamingSessions
		if (stoppedSessionId) {
			streamingSessions.update(map => {
				if (!map.has(stoppedSessionId)) return map;
				const newMap = new Map(map);
				newMap.delete(stoppedSessionId);
				return newMap;
			});
		}

		setTimeout(() => onProcessNextQueuedMessage(), 500);
	}

	// ── Main dispatch ────────────────────────────────────────────────

	function handleMessage(data: Record<string, unknown>): void {
		const msgType = data.type as string;
		const eventType = data.event_type as string;

		// Forward to notification system (triggers poller via pollNow/startStreaming)
		const state = getState();
		if (state.sessionId) {
			const isInterruptTransition = internal.interruptPending &&
				(msgType === 'interrupted' || msgType === 'stopped');
			if (!isInterruptTransition) {
				sessionNotifier.handleMessage(state.sessionId, data);
			}
		}

		// Handle sync events from other devices — notifier already triggered pollNow
		if (eventType) {
			return;
		}

		// ── User-level messages (not per-session) ─────────────────────

		if (msgType === 'tabs_sync') return;

		if (msgType === 'user_session_created') {
			appLoadSessions();
			appLoadApiUserSessions();
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

		if (msgType === 'git_state_changed') {
			const projectId = data.project_id as string;
			const worktreeId = (data.worktree_id as string) || null;
			const changes = data.changes as Record<string, unknown>;
			if (projectId && changes) {
				gitApplyServerUpdate(projectId, worktreeId, changes);
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
				const currentState = getState();
				const acceptCreated = currentState.sessionId === sessionId ||
					(!currentState.sessionId && internal.awaitingNewSession);
				if (acceptCreated) {
					internal.awaitingNewSession = false;
					// Set the sessionId so the poller callback knows which session to write to.
					// This is the ONLY state write for session lifecycle — just the ID.
					update(s => ({ ...s, sessionId }));
					onPersistSession();
				}
				appLoadSessions();
				break;
			}

			case 'start': {
				if (!data.session_id) break;
				const sessionId = data.session_id as string;
				internal.interruptPending = false;
				const currentState = getState();
				const acceptStart = currentState.sessionId === sessionId ||
					(!currentState.sessionId && internal.awaitingNewSession);
				if (acceptStart) {
					internal.awaitingNewSession = false;
					// Set sessionId and streamStartTime so completion notification
					// can calculate duration. isStreaming and messages come from poller.
					update(s => ({ ...s, sessionId, streamStartTime: Date.now() }));
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
					sessionPoller.pollNow(data.session_id as string);
				}
				break;

			case 'chunk':
			case 'stream_start':
			case 'stream_block_start':
			case 'stream_delta':
			case 'stream_block_stop':
			case 'system': {
				// Ensure polling is started if we're streaming but poller isn't running.
				// This handles slash commands that don't trigger a 'start' event.
				const sysState = getState();
				if (sysState.isStreaming && sysState.sessionId && !sessionPoller.isPolling(sysState.sessionId)) {
					sessionPoller.startStreaming(sysState.sessionId);
				}
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
				const toolId = data.id as string;
				if (toolId) internal.pendingTools.add(toolId);
				break;
			}

			case 'tool_result': {
				const toolUseId = data.tool_use_id as string;
				if (toolUseId) internal.pendingTools.delete(toolUseId);
				break;
			}

			case 'subagent_tool_use': {
				const toolId = data.id as string;
				if (toolId) internal.pendingTools.add(toolId);
				break;
			}

			case 'subagent_tool_result': {
				const toolUseId = data.tool_use_id as string;
				if (toolUseId) internal.pendingTools.delete(toolUseId);
				break;
			}

			case 'done':
				handleDone(data);
				break;

			case 'stopped':
			case 'interrupted':
				handleStoppedOrInterrupted(data);
				break;

			case 'error': {
				internal.interruptPending = false;
				// Error is a terminal event — stop polling and show error.
				// The poller will also detect isStreaming:false on its next poll.
				const errState = getState();
				if (errState.sessionId) sessionPoller.stop(errState.sessionId);
				update(s => ({ ...s, error: data.message as string }));
				const errorSessionId = errState.sessionId;
				if (errorSessionId) {
					streamingSessions.update(map => {
						if (!map.has(errorSessionId)) return map;
						const newMap = new Map(map);
						newMap.delete(errorSessionId);
						return newMap;
					});
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
				const request = {
					request_id: data.request_id as string,
					tool_name: data.tool_name as string,
					tool_input: data.tool_input as Record<string, unknown>,
					queue_position: data.queue_position as number | undefined,
					queue_total: data.queue_total as number | undefined,
				} as PermissionRequestType;

				onNotifyWaitingForResponse('permission', { toolName: request.tool_name });
				update(s => ({ ...s, pendingPermissions: [...s.pendingPermissions, request] }));
				break;
			}

			case 'permission_queue_update': {
				const resolvedIds = data.resolved_ids as string[];
				update(s => ({
					...s,
					pendingPermissions: s.pendingPermissions.filter(p => !resolvedIds.includes(p.request_id)),
				}));
				break;
			}

			case 'permission_response_ack':
			case 'permission_timeout': {
				const requestId = data.request_id as string;
				update(s => ({
					...s,
					pendingPermissions: s.pendingPermissions.filter(p => p.request_id !== requestId),
				}));
				break;
			}

			case 'pending_permissions': {
				const requests = data.requests as PermissionRequestType[];
				update(s => ({ ...s, pendingPermissions: requests }));
				break;
			}

			case 'user_question': {
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
					request_id: data.request_id as string,
					tool_use_id: data.tool_use_id as string,
					questions: normalizedQuestions,
				} as UserQuestionRequest;

				onNotifyWaitingForResponse('question', { questionCount: request.questions.length });
				update(s => ({ ...s, pendingQuestions: [...s.pendingQuestions, request] }));
				break;
			}

			case 'user_question_response_ack':
			case 'user_question_timeout': {
				const requestId = data.request_id as string;
				update(s => ({
					...s,
					pendingQuestions: s.pendingQuestions.filter(q => q.request_id !== requestId),
				}));
				break;
			}

			case 'pending_questions': {
				const questions = data.questions as UserQuestionRequest[];
				update(s => {
					const existingIds = new Set(s.pendingQuestions.map(q => q.request_id));
					const newIds = new Set(questions.map(q => q.request_id));
					const same = existingIds.size === newIds.size && [...existingIds].every(id => newIds.has(id));
					if (same) return s;
					return { ...s, pendingQuestions: questions };
				});
				break;
			}

			default:
				if (msgType) console.debug(`[ActiveSession] Unhandled message type: ${msgType}`);
				break;
		}
	}

	return { handleMessage };
}
