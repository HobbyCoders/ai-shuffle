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
import { toast } from '../toast';
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

	function setNeedsAttention(sessionId: string | undefined, count: number): void {
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

		appLoadSessions();
	}

	// ── Handler: stopped / interrupted ───────────────────────────────

	function handleStoppedOrInterrupted(_data: Record<string, unknown>): void {
		if (internal.interruptPending) { internal.pendingTools.clear(); return; }

		const currentState = getState();
		const stoppedSessionId = currentState.sessionId;

		// Trigger a final authoritative poll (survives stop(), see handleDone).
		if (stoppedSessionId) sessionPoller.pollFinal(stoppedSessionId);

		clearStopTimeout();
		internal.pendingTools.clear();
		// Clear WS stream buffer
		update(s => s.wsStreamContent ? { ...s, wsStreamContent: '' } : s);

		// Clean up streamingSessions
		if (stoppedSessionId) {
			streamingSessions.update(map => {
				if (!map.has(stoppedSessionId)) return map;
				const newMap = new Map(map);
				newMap.delete(stoppedSessionId);
				return newMap;
			});
		}
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
				msgType === 'interrupted';
			if (!isInterruptTransition && shouldNotify) {
				sessionNotifier.handleMessage(state.sessionId, data);
			}
		}

		// Handle sync events from other devices / server-originated broadcasts
		if (eventType) {
			// queue_updated is handled here directly since queue state
			// is not part of the poll response — it's server-side only.
			if (eventType === 'queue_updated') {
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
				const acceptCreated = currentState.sessionId === sessionId ||
					(!currentState.sessionId && internal.awaitingNewSession);
				if (acceptCreated) {
					internal.awaitingNewSession = false;
					// Set the sessionId and title so the header shows the correct
					// session title immediately (not stale 'New Chat').
					update(s => ({ ...s, sessionId, ...(title ? { title } : {}) }));
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
							return { ...s, sessionId, isStreaming: true, streamStartTime: Date.now(), wsStreamContent: '' };
						}
						return {
							...s,
							sessionId,
							isStreaming: true,
							streamStartTime: Date.now(),
							wsStreamContent: '',
							messages: [...s.messages, {
								id: messageId,
								role: 'assistant' as const,
								type: 'text' as const,
								content: '',
								streaming: true,
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
			case 'stream_start':
			case 'stream_block_start':
			case 'stream_block_stop':
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

			case 'error': {
				internal.interruptPending = false;
				// Reject any pending interrupt-and-query promise
				if (internal.interruptConfirmResolve) {
					internal.interruptConfirmResolve(false);
					internal.interruptConfirmResolve = null;
				}
				// Error is a terminal event — stop polling, reset streaming, show error.
				// Must reset isStreaming so the user can send another message.
				const errState = getState();
				if (errState.sessionId) sessionPoller.stop(errState.sessionId);
				const errMessage = data.message as string;
				update(s => ({ ...s, error: errMessage, isStreaming: false, streamStartTime: null, wsStreamContent: '' }));
				reportClientError({
					source: 'ws',
					message: errMessage || 'Stream error',
					detail: typeof data.detail === 'string' ? data.detail : undefined,
					session_id: errState.sessionId ?? undefined,
				});
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
				// Slice C: surface API-level errors (e.g. Anthropic 429/5xx) as a
				// transient toast. Backend-agnostic — we only key off `status`.
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
				// Rate-limit (429) is transient and self-resolves — no "Fix with AI".
				const showFix = status !== 429;
				toast.push(message, {
					tone: status === 429 ? 'warning' : 'error',
					duration_ms: showFix ? 10000 : 5000,
					action: showFix
						? {
								label: 'Fix with AI',
								onClick: () =>
									startFixChat({
										source: 'ws',
										message,
										status_code: status > 0 ? status : undefined,
										session_id: apiErrSessionId,
									}),
							}
						: undefined,
				});
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
				update(s => ({ ...s, pendingQuestions: [...s.pendingQuestions, request] }));
				break;
			}

			case 'user_question_response_ack':
			case 'user_question_timeout': {
				const responseSessionId = data.session_id as string | undefined;
				if (responseSessionId && responseSessionId !== getState().sessionId) break;
				const requestId = data.request_id as string;
				update(s => ({
					...s,
					pendingQuestions: s.pendingQuestions.filter(q => q.request_id !== requestId),
				}));
				break;
			}

			case 'pending_questions': {
				const pendingSessionId = data.session_id as string | undefined;
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

			default:
				break;
		}
	}

	return { handleMessage };
}
