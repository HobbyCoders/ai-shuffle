/**
 * WebSocket Message Handlers — Dispatch table for incoming WS messages.
 *
 * Extracted from active-session.ts to reduce file size and isolate
 * the message-handling logic from session lifecycle management.
 *
 * Design principle:
 * - Handlers that just update store state live here directly.
 * - Handlers that trigger complex side effects (done, stopped, etc.)
 *   invoke callbacks back to active-session.ts.
 * - The `history` handler is self-contained here since it's purely
 *   about translating server data into store state.
 */

import type { PermissionRequest as PermissionRequestType, UserQuestionRequest } from '$lib/api/client';
import type {
	ChatMessage, MessageType, TodoItem, SubagentChildMessage,
} from '../types';
import type { ActiveSessionState, StreamingSessionInfo } from '../active-session';
import { messageCache } from '../message-cache';

// ─── Interfaces ──────────────────────────────────────────────────────────────

/**
 * Mutable internal state shared between active-session.ts and the handlers.
 * These are module-level scalars in the factory closure; we pass them by
 * reference via a single object so handlers can read AND mutate them.
 */
export interface InternalMutableState {
	waitingForHistory: Array<Record<string, unknown>> | null;
	expectedSessionId: string | null;
	pendingTools: Set<string>;
	streamEndCleanedUp: string | null;
	streamingOnDisconnect: string | null;
	interruptPending: boolean;
}

/**
 * Dependencies injected from the active-session store factory.
 * Keeps handlers decoupled from the concrete store implementation.
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
	};
	sessionNotifier: {
		handleMessage: (sessionId: string, data: Record<string, unknown>) => void;
	};

	// Session state (for streaming-on-disconnect grace period)
	sessionState: {
		getSession: (sessionId: string) => { isStreaming: boolean } | undefined;
		removeSession: (sessionId: string) => void;
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
	onStartPolling: (sessionId: string) => void;
	onStopPolling: () => void;
	onProcessNextQueuedMessage: () => void;
	onProcessWaitingEvents: () => void;
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
		sessionPoller, sessionNotifier, sessionState: sessionStateDep,
		streamingSessions, backgroundPermissions,
		gitRefreshStatus, gitRefreshDiffStats, gitGetProjectId, gitGetLoading, gitApplyServerUpdate,
		appLoadSessions, appLoadApiUserSessions,
		internal,
		onNewChat, onPersistSession,
		onStartPolling, onStopPolling,
		onProcessNextQueuedMessage, onProcessWaitingEvents,
		onNotifySessionComplete, onNotifyWaitingForResponse,
		wsIsConnected,
		clearStopTimeout,
	} = deps;

	// ── Handler: history ─────────────────────────────────────────────────

	function handleHistory(data: Record<string, unknown>): void {
		if (!data.session_id) return;
		const receivedSessionId = data.session_id as string;
		if (internal.expectedSessionId && receivedSessionId !== internal.expectedSessionId) {
			internal.waitingForHistory = null;
			return;
		}
		internal.expectedSessionId = null;

		const rawMessages = (data.messages as Array<Record<string, unknown>>) || [];
		const toolResultMap: Map<string, { content: string; toolStatus: 'complete' | 'error' }> = new Map();
		let latestTodos: TodoItem[] = [];

		// Pre-scan for tool_results and todos
		for (const m of rawMessages) {
			const rawMsgType = m.type as MessageType | undefined;
			const role = m.role as string;
			const toolName = (m.toolName || m.tool_name) as string | undefined;
			const toolInput = (m.toolInput || m.tool_input) as Record<string, unknown> | undefined;

			if ((rawMsgType === 'tool_use' || role === 'tool_use') && toolName === 'TodoWrite' && toolInput?.todos && Array.isArray(toolInput.todos)) {
				latestTodos = (toolInput.todos as TodoItem[]).map(todo => ({
					content: todo.content || '',
					status: todo.status || 'pending',
					activeForm: todo.activeForm || todo.content || ''
				}));
			}

			if (rawMsgType === 'tool_result' || role === 'tool_result') {
				const toolUseId = m.toolId || m.tool_use_id;
				if (toolUseId) {
					toolResultMap.set(toolUseId as string, {
						content: m.content as string || '',
						toolStatus: 'complete'
					});
				}
			}
		}

		const isStreaming = data.isStreaming as boolean || false;
		const messages: ChatMessage[] = [];
		const seenToolResults = new Set<string>();

		for (let i = 0; i < rawMessages.length; i++) {
			const m = rawMessages[i];
			let rawMsgType: MessageType | undefined = m.type as MessageType | undefined;

			if (!rawMsgType && m.role === 'assistant') rawMsgType = 'text';
			else if (!rawMsgType && (m.role === 'tool_use' || m.role === 'tool_result')) rawMsgType = m.role as MessageType;

			if (rawMsgType === 'system' || m.role === 'system') {
				messages.push({
					id: String(m.id || `msg-${i}`),
					jsonlUuid: m.uuid as string | undefined,
					role: 'system' as const,
					content: m.content as string,
					type: 'system' as const,
					systemSubtype: (m.systemSubtype || m.subtype) as string | undefined,
					systemData: (m.systemData as Record<string, unknown>) || { content: m.content },
					metadata: m.metadata as Record<string, unknown>,
					streaming: false
				});
				continue;
			}

			if (rawMsgType === 'tool_result') {
				const toolUseId = m.toolId || m.tool_use_id;
				if (toolUseId) seenToolResults.add(toolUseId as string);
				continue;
			}

			const chatMessage: ChatMessage = {
				id: String(m.id || `msg-${i}`),
				jsonlUuid: m.uuid as string | undefined,
				role: (m.role === 'tool_use' || m.role === 'tool_result' ? 'assistant' : m.role) as 'user' | 'assistant',
				content: m.content as string,
				type: rawMsgType,
				toolName: (m.toolName || m.tool_name) as string | undefined,
				toolId: m.toolId as string | undefined,
				toolInput: (m.toolInput || m.tool_input) as Record<string, unknown> | undefined,
				toolResult: m.toolResult as string | undefined,
				toolStatus: m.toolStatus as 'running' | 'complete' | 'error' | undefined,
				metadata: m.metadata as Record<string, unknown>,
				streaming: isStreaming ? ((m.streaming as boolean) ?? false) : false
			};

			if (rawMsgType === 'tool_use' && chatMessage.toolId) {
				const result = toolResultMap.get(chatMessage.toolId);
				if (result && !chatMessage.toolResult) {
					chatMessage.toolResult = result.content;
					chatMessage.toolStatus = result.toolStatus;
					seenToolResults.add(chatMessage.toolId);
				}
				if (!chatMessage.toolStatus) chatMessage.toolStatus = 'complete';
			}

			if (rawMsgType === 'subagent') {
				chatMessage.agentId = m.agentId as string | undefined;
				chatMessage.agentType = m.agentType as string | undefined;
				chatMessage.agentDescription = m.agentDescription as string | undefined;
				chatMessage.agentStatus = m.agentStatus as 'pending' | 'running' | 'completed' | 'error' | undefined;
				chatMessage.agentChildren = m.agentChildren as SubagentChildMessage[] | undefined;
			}

			messages.push(chatMessage);
		}

		// Merge streaming buffer for late-joining devices
		let finalMessages = messages;
		const streamingBuffer = data.streamingBuffer as Array<Record<string, unknown>> | undefined;
		if (isStreaming && streamingBuffer && streamingBuffer.length > 0) {
			const existingContentHashes = new Set<string>();
			for (const msg of messages) {
				existingContentHashes.add(`${msg.type || 'text'}:${msg.toolId || ''}:${(msg.content || '').substring(0, 100)}`);
			}

			const bufferToolResults: Map<string, { content: string; status: 'complete' | 'error' }> = new Map();
			for (const m of streamingBuffer) {
				const bufferType = (m.type || m.chunk_type || 'text') as string;
				if (bufferType === 'tool_result') {
					const toolId = (m.tool_id || '') as string;
					if (toolId) bufferToolResults.set(toolId, { content: (m.content || '') as string, status: 'complete' });
				}
			}

			for (const m of streamingBuffer) {
				const bufferType = (m.type || m.chunk_type || 'text') as MessageType;
				const bufferToolId = (m.tool_id || '') as string;
				const bufferContent = (m.content || '') as string;
				const hash = `${bufferType}:${bufferToolId}:${bufferContent.substring(0, 100)}`;

				if (bufferType === 'tool_result') continue;
				if (existingContentHashes.has(hash)) continue;

				if (bufferType === 'system') {
					finalMessages.push({
						id: `buffer-system-${Date.now()}-${Math.random()}`,
						role: 'system' as const,
						content: bufferContent,
						type: 'system' as const,
						systemSubtype: m.subtype as string | undefined,
						systemData: m.data as Record<string, unknown> | undefined,
						streaming: false
					});
					existingContentHashes.add(hash);
					continue;
				}

				if (bufferType === 'subagent') {
					finalMessages.push({
						id: `buffer-subagent-${m.agent_id || Date.now()}-${Math.random()}`,
						role: 'assistant' as const,
						content: bufferContent,
						type: 'subagent' as const,
						toolId: m.tool_id as string | undefined,
						agentId: m.agent_id as string | undefined,
						agentType: m.agent_type as string | undefined,
						agentDescription: m.agent_description as string | undefined,
						agentPrompt: m.agent_prompt as string | undefined,
						agentStatus: (m.agent_status as 'pending' | 'running' | 'completed' | 'error') || 'running',
						agentChildren: (m.agent_children as SubagentChildMessage[]) || [],
						streaming: (m.streaming as boolean) ?? (m.agent_status === 'running')
					});
					existingContentHashes.add(hash);
					continue;
				}

				const bufferMsg: ChatMessage = {
					id: `buffer-${Date.now()}-${Math.random()}`,
					role: 'assistant' as const,
					content: bufferContent,
					type: bufferType,
					toolName: m.tool_name as string | undefined,
					toolId: bufferToolId || undefined,
					toolInput: m.tool_input as Record<string, unknown> | undefined,
					streaming: (m.streaming as boolean) ?? true
				};

				if (bufferType === 'tool_use' && bufferToolId) {
					if (m.tool_result !== undefined) {
						bufferMsg.toolResult = m.tool_result as string;
						bufferMsg.toolStatus = (m.tool_status as 'complete' | 'error') || 'complete';
						bufferMsg.streaming = false;
					} else if (bufferToolResults.has(bufferToolId)) {
						const result = bufferToolResults.get(bufferToolId)!;
						bufferMsg.toolResult = result.content;
						bufferMsg.toolStatus = result.status;
						bufferMsg.streaming = false;
					}
				}

				finalMessages.push(bufferMsg);
				existingContentHashes.add(hash);
			}
		}

		// Extract token counts and session context from session data
		const session = data.session as Record<string, unknown> | undefined;
		const tokenUpdates: Record<string, unknown> = {};
		if (session) {
			if (typeof session.total_tokens_in === 'number') tokenUpdates.totalTokensIn = session.total_tokens_in;
			if (typeof session.total_tokens_out === 'number') tokenUpdates.totalTokensOut = session.total_tokens_out;
			if (typeof session.cache_creation_tokens === 'number') tokenUpdates.totalCacheCreationTokens = session.cache_creation_tokens;
			if (typeof session.cache_read_tokens === 'number') tokenUpdates.totalCacheReadTokens = session.cache_read_tokens;
			if (typeof session.context_tokens === 'number') tokenUpdates.contextUsed = session.context_tokens;
			if (typeof session.profile_id === 'string') tokenUpdates.sessionProfileId = session.profile_id;
			if (typeof session.profile_id === 'string') tokenUpdates.profile = session.profile_id;
			if (typeof session.project_id === 'string') tokenUpdates.project = session.project_id;
		}

		// Late-joining context usage from streamingUsage
		const streamingUsage = data.streamingUsage as Record<string, number> | undefined;
		if (isStreaming && streamingUsage) {
			const inputTokens = streamingUsage.input_tokens || 0;
			const cacheCreationTokens = streamingUsage.cache_creation_input_tokens || 0;
			const cacheReadTokens = streamingUsage.cache_read_input_tokens || 0;
			const streamContextUsed = inputTokens + cacheCreationTokens + cacheReadTokens;
			if (streamContextUsed > 0) {
				tokenUpdates.contextUsed = streamContextUsed;
				tokenUpdates.totalCacheCreationTokens = cacheCreationTokens;
				tokenUpdates.totalCacheReadTokens = cacheReadTokens;
			}
		}

		// Defensive check: don't replace messages with fewer/empty data
		const currentState = getState();
		const currentAssistantMsgs = currentState.messages.filter(m => m.role === 'assistant' && m.type === 'text');
		const incomingAssistantMsgs = finalMessages.filter(m => m.role === 'assistant' && m.type === 'text');
		const shouldPreserveMessages = currentState.messages.length > 0 && !isStreaming && (
			finalMessages.length === 0 ||
			(currentAssistantMsgs.length > incomingAssistantMsgs.length) ||
			(finalMessages.length > 0 && currentState.messages.length > finalMessages.length * 1.5)
		);

		if (shouldPreserveMessages) {
			console.warn(`[ActiveSession] Received empty history but have ${currentState.messages.length} messages - preserving`);
			update(s => ({
				...s,
				sessionId: data.session_id as string,
				isStreaming,
				error: null,
				...tokenUpdates
			}));
		} else {
			// Preserve existing message IDs to prevent Svelte component recreation
			const existingMessages = currentState.messages;
			const messagesWithPreservedIds = finalMessages.map((msg, i) => {
				const existingMsg = existingMessages[i];
				const canPreserveId = existingMsg && existingMsg.type === msg.type && existingMsg.role === msg.role;
				return canPreserveId ? { ...msg, id: existingMsg.id } : msg;
			});

			const stateUpdate: Record<string, unknown> = {
				sessionId: data.session_id as string,
				messages: messagesWithPreservedIds,
				isStreaming,
				todos: latestTodos,
				error: null,
				...tokenUpdates
			};

			// Restore worktree state from session
			const worktreeId = session?.worktree_id as string | undefined;
			const worktreeBranch = session?.worktree_branch as string | undefined;
			const worktreeDeletedFlag = session?.worktree_deleted as boolean | undefined;
			if (worktreeId && worktreeBranch) {
				stateUpdate.executionMode = 'worktree';
				stateUpdate.worktreeId = worktreeId;
				stateUpdate.worktreeBranch = worktreeBranch;
				stateUpdate.selectedWorktreeBranch = worktreeBranch;
				stateUpdate.worktreeMode = 'existing';
				stateUpdate.worktreeDeleted = false;
			} else if (worktreeDeletedFlag && worktreeBranch) {
				stateUpdate.executionMode = 'worktree';
				stateUpdate.worktreeId = null;
				stateUpdate.worktreeBranch = worktreeBranch;
				stateUpdate.selectedWorktreeBranch = worktreeBranch;
				stateUpdate.worktreeMode = 'existing';
				stateUpdate.worktreeDeleted = true;
			} else {
				stateUpdate.executionMode = 'local';
				stateUpdate.worktreeId = null;
				stateUpdate.worktreeBranch = '';
				stateUpdate.selectedWorktreeBranch = null;
				stateUpdate.worktreeDeleted = false;
			}

			// Restore pending permissions/questions on reconnect
			const pendingPerms = data.pendingPermissions as PermissionRequestType[] | undefined;
			const pendingQs = data.pendingQuestions as UserQuestionRequest[] | undefined;
			if (pendingPerms && pendingPerms.length > 0) stateUpdate.pendingPermissions = pendingPerms;
			if (pendingQs && pendingQs.length > 0) stateUpdate.pendingQuestions = pendingQs;

			update(s => ({ ...s, ...stateUpdate }));

			// Update title from first message
			if (finalMessages.length > 0 && finalMessages[0].role === 'user') {
				const title = finalMessages[0].content.substring(0, 100) + (finalMessages[0].content.length > 100 ? '...' : '');
				update(s => ({ ...s, title }));
			}
		}

		// Cache for instant rendering on next load
		if (finalMessages.length > 0 && data.session_id) {
			messageCache.set(data.session_id as string, finalMessages);
		}

		// Start polling if streaming
		const receivedSession = data.session_id as string;
		if (isStreaming && receivedSession) {
			internal.streamingOnDisconnect = null;
			onStartPolling(receivedSession);
			wsSend({ type: 'get_pending_permissions' });
			wsSend({ type: 'get_pending_questions' });
		} else if (receivedSession) {
			// Session is NOT streaming -- ensure streamingSessions is clean.
			streamingSessions.update(map => {
				if (!map.has(receivedSession)) return map;
				const newMap = new Map(map);
				newMap.delete(receivedSession);
				return newMap;
			});
			const disconnectSessionId = internal.streamingOnDisconnect;
			if (disconnectSessionId && disconnectSessionId === receivedSession) {
				internal.streamingOnDisconnect = null;
				setTimeout(() => {
					const gracedSession = sessionStateDep.getSession(disconnectSessionId);
					if (gracedSession && !gracedSession.isStreaming && sessionPoller.isPolling(disconnectSessionId)) {
						sessionPoller.stop(disconnectSessionId);
						sessionStateDep.removeSession(disconnectSessionId);
					}
				}, 10000);
			} else {
				internal.streamingOnDisconnect = null;
				onStopPolling();
			}
		}

		// Clear background permissions for this session since we just loaded it
		backgroundPermissions.update(map => {
			if (!map.has(receivedSession)) return map;
			const newMap = new Map(map);
			newMap.delete(receivedSession);
			return newMap;
		});

		onProcessWaitingEvents();
	}

	// ── Handler: done ────────────────────────────────────────────────────

	function handleDone(data: Record<string, unknown>): void {
		const metadata = data.metadata as Record<string, unknown>;
		const sessionId = data.session_id as string;

		internal.interruptPending = false;

		const currentDoneState = getState();
		const isActiveSession = !sessionId || sessionId === currentDoneState.sessionId;
		const doneSessionId = sessionId || currentDoneState.sessionId;

		if (doneSessionId) sessionPoller.pollNow(doneSessionId);

		if (isActiveSession) {
			clearStopTimeout();
			internal.pendingTools.clear();
		}

		if (isActiveSession) {
			const durationSeconds = currentDoneState.streamStartTime
				? (Date.now() - currentDoneState.streamStartTime) / 1000
				: undefined;

			update(s => {
				let title = s.title;
				if (title === 'New Chat' && s.messages.length > 0) {
					const firstUserMsg = s.messages.find(m => m.role === 'user');
					if (firstUserMsg) {
						title = firstUserMsg.content.substring(0, 100) + (firstUserMsg.content.length > 100 ? '...' : '');
					}
				}
				return { ...s, isStreaming: false, streamStartTime: null, title };
			});

			const doneState = getState();
			onNotifySessionComplete({
				sessionId: sessionId || '',
				sessionTitle: doneState.title ?? null,
				durationSeconds,
				totalCostUsd: metadata?.total_cost_usd as number | undefined,
				tokensIn: metadata?.tokens_in as number | undefined,
				tokensOut: metadata?.tokens_out as number | undefined,
				model: metadata?.model as string | undefined
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

		// Safety: ensure cleanup if poller pipeline fails (only for active session)
		if (isActiveSession && doneSessionId) {
			const cleanupId = doneSessionId;
			setTimeout(() => {
				if (internal.streamEndCleanedUp === cleanupId) { internal.streamEndCleanedUp = null; return; }
				console.warn(`[ActiveSession] Safety: onStreamEnd didn't fire for ${cleanupId}, running fallback`);
				if (sessionPoller.isPolling(cleanupId)) sessionPoller.stop(cleanupId);
				const sd = sessionStateDep.getSession(cleanupId);
				if (sd) sessionStateDep.removeSession(cleanupId);
				const refreshState = getState();
				if (!refreshState.isStreaming && refreshState.sessionId === cleanupId && wsIsConnected()) {
					internal.expectedSessionId = cleanupId;
					internal.waitingForHistory = [];
					wsSend({ type: 'load_session', session_id: cleanupId });
				}
			}, 5000);
		}
	}

	// ── Handler: stopped / interrupted ───────────────────────────────────

	function handleStoppedOrInterrupted(_data: Record<string, unknown>): void {
		if (internal.interruptPending) { internal.pendingTools.clear(); return; }

		const stoppedState = getState();
		const stoppedSessionId = stoppedState.sessionId;
		if (stoppedSessionId) sessionPoller.pollNow(stoppedSessionId);

		clearStopTimeout();
		internal.pendingTools.clear();

		update(s => ({ ...s, isStreaming: false }));

		if (stoppedSessionId) {
			streamingSessions.update(map => {
				if (!map.has(stoppedSessionId)) return map;
				const newMap = new Map(map);
				newMap.delete(stoppedSessionId);
				return newMap;
			});
		}

		setTimeout(() => onProcessNextQueuedMessage(), 500);

		// Safety cleanup
		if (stoppedSessionId) {
			setTimeout(() => {
				if (sessionPoller.isPolling(stoppedSessionId)) sessionPoller.stop(stoppedSessionId);
				const sd = sessionStateDep.getSession(stoppedSessionId);
				if (sd) sessionStateDep.removeSession(stoppedSessionId);
			}, 5000);
		}
	}

	// ── Main dispatch ────────────────────────────────────────────────────

	function handleMessage(data: Record<string, unknown>): void {
		const msgType = data.type as string;
		const eventType = data.event_type as string;

		// Forward to notification system (triggers poller)
		const state = getState();
		if (state.sessionId) {
			const isInterruptTransition = internal.interruptPending &&
				(msgType === 'interrupted' || msgType === 'stopped');
			if (!isInterruptTransition) {
				sessionNotifier.handleMessage(state.sessionId, data);
			}
		}

		// Handle sync events from other devices -- queue if waiting for history
		if (eventType) {
			if (internal.waitingForHistory !== null) {
				internal.waitingForHistory.push(data);
			}
			// Notifier already triggered pollNow for update events
			return;
		}

		// ── User-level messages (not per-session) ─────────────────────────

		if (msgType === 'tabs_sync') return; // No-op in single-session model

		if (msgType === 'user_session_created') {
			appLoadSessions();
			appLoadApiUserSessions();
			return;
		}

		if (msgType === 'user_streaming_changed') {
			if (!data.session_id) return;
			const sessionId = data.session_id as string;
			const isStreamingFlag = data.is_streaming as boolean;
			const projectId = (data.project_id as string | null) ?? null;
			const worktreeId = (data.worktree_id as string | null) ?? null;
			streamingSessions.update(map => {
				const newMap = new Map(map);
				if (isStreamingFlag) {
					newMap.set(sessionId, { projectId, worktreeId });
				} else {
					newMap.delete(sessionId);
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

		// ── Per-session message switch ────────────────────────────────────

		switch (msgType) {
			case 'history':
				handleHistory(data);
				break;

			case 'session_created': {
				if (!data.session_id) break;
				const sessionId = data.session_id as string;
				const currentState = getState();
				if (!currentState.sessionId || currentState.sessionId === sessionId) {
					update(s => ({ ...s, sessionId, sessionProfileId: s.profile }));
					onPersistSession();
				}
				appLoadSessions();
				break;
			}

			case 'start': {
				if (!data.session_id) break;
				const sessionId = data.session_id as string;
				internal.interruptPending = false;
				const currentStartState = getState();
				if (sessionId === currentStartState.sessionId || !currentStartState.sessionId) {
					update(s => ({ ...s, sessionId, isStreaming: true, streamStartTime: Date.now() }));
					// Cancel any pending removeSession timer from a previous query's
					// onStreamEnd and start streaming polling. Without this, the 100ms
					// removeSession timer can delete session state mid-stream, and
					// stale lastUpdateId causes all polls to return 304.
					onStartPolling(sessionId);
					onPersistSession();
				}
				break;
			}

			// Notification-only events -- poller handles state via sessionNotifier
			case 'chunk':
			case 'stream_start':
			case 'stream_block_start':
			case 'stream_delta':
			case 'stream_block_stop':
			case 'system':
			case 'subagent_start':
			case 'subagent_chunk':
			case 'subagent_done':
			case 'plugin_message':
				break;

			case 'stream_message_delta':
				// Intentionally no queue processing here. stop_reason can be 'tool_use'
				// (agent still working) or 'end_turn'. Queued "Send Later" messages are
				// processed from the 'done' handler only, which fires after the full
				// agent run (all tool calls) completes.
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
				onStopPolling();
				update(s => ({ ...s, isStreaming: false, error: data.message as string }));
				const errorSessionId = getState().sessionId;
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
					sessionLockedBy: lockedBy
				}));
				break;
			}

			case 'session_takeover': {
				const newDevice = data.new_device as string;
				console.warn(`[ActiveSession] Session taken over by device ${newDevice}`);
				update(s => ({
					...s,
					error: 'This session was taken over by another device.',
					isStreaming: false,
					sessionLocked: false,
					sessionLockedBy: undefined
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
					queue_total: data.queue_total as number | undefined
				} as PermissionRequestType;

				onNotifyWaitingForResponse('permission', { toolName: request.tool_name });
				update(s => ({ ...s, pendingPermissions: [...s.pendingPermissions, request] }));
				break;
			}

			case 'permission_queue_update': {
				const resolvedIds = data.resolved_ids as string[];
				update(s => ({
					...s,
					pendingPermissions: s.pendingPermissions.filter(p => !resolvedIds.includes(p.request_id))
				}));
				break;
			}

			case 'permission_response_ack':
			case 'permission_timeout': {
				const requestId = data.request_id as string;
				update(s => ({
					...s,
					pendingPermissions: s.pendingPermissions.filter(p => p.request_id !== requestId)
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
						description: typeof opt === 'object' && opt !== null ? (opt as Record<string, unknown>).description as string || '' : ''
					})) : [],
					multiSelect: Boolean(q.multiSelect)
				}));

				const request = {
					request_id: data.request_id as string,
					tool_use_id: data.tool_use_id as string,
					questions: normalizedQuestions
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
					pendingQuestions: s.pendingQuestions.filter(q => q.request_id !== requestId)
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
