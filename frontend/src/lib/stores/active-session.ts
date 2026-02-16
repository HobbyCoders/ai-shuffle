/**
 * Active Session Store — Single-session model replacing the multi-tab architecture.
 *
 * One WebSocket connection, one active session at a time.
 * Session switching saves ephemeral state to sessionEphemeralCache and restores it.
 * Polling is delegated to sessionPoller; WebSocket is notification-only for streaming.
 */

import { writable, derived, get } from 'svelte/store';
import type { PermissionRequest as PermissionRequestType, UserQuestionRequest } from '$lib/api/client';
import { api } from '$lib/api/client';
import { notifySessionComplete, notifyWaitingForResponse } from '$lib/services/notifications';

// Session infrastructure
import { sessionPoller } from './session/session-poller';
import { sessionNotifier } from './session/session-notifier';
import { sessionState, type SessionUsage } from './session/session-state';

// Caches and helpers
import { messageCache } from './message-cache';
import { sessionEphemeralCache } from './session-cache';
import { appData } from './app-data';
import { git } from './git';
import { browserState } from './browser';

// Shared types
import type {
	ChatMessage, MessageType, TodoItem, SubagentChildMessage,
	QueuedMessage, ExecutionMode
} from './types';

// ─── Module-Level Exports ────────────────────────────────────────────────────

/** Cross-device streaming info for sidebar indicators */
export interface StreamingSessionInfo {
	projectId: string | null;
	worktreeId: string | null;
}

/** Track which sessions are streaming across all user devices */
export const streamingSessions = writable<Map<string, StreamingSessionInfo>>(new Map());

/** Track pending permissions for non-active sessions ("needs attention" indicator) */
export const backgroundPermissions = writable<Map<string, number>>(new Map());

// ─── Interfaces ──────────────────────────────────────────────────────────────

/** Full reactive state for the single active session. */
export interface ActiveSessionState {
	sessionId: string | null;
	title: string;
	messages: ChatMessage[];
	isStreaming: boolean;
	wsConnected: boolean;
	error: string | null;
	profile: string;
	sessionProfileId: string | null;
	currentQueryProfileId: string | null;
	project: string;
	totalTokensIn: number;
	totalTokensOut: number;
	totalCacheCreationTokens: number;
	totalCacheReadTokens: number;
	contextUsed: number | null;
	contextMax: number;
	modelOverride: string | null;
	permissionModeOverride: string | null;
	thinkingModeOverride: string | null;
	pendingPermissions: PermissionRequestType[];
	pendingQuestions: UserQuestionRequest[];
	todos: TodoItem[];
	queuedMessages: QueuedMessage[];
	draft: string;
	scrollTop: number;
	streamStartTime: number | null;
	executionMode: ExecutionMode;
	worktreeBranch: string;
	worktreeBaseBranch: string;
	worktreeCreateNewBranch: boolean;
	worktreeId: string | null;
	worktreeMode: 'new' | 'existing';
	selectedWorktreeBranch: string | null;
	isCreatingWorktree: boolean;
	worktreeError: string | null;
	worktreeDeleted: boolean;
	sessionLocked: boolean;
	sessionLockedBy: string | undefined;
}

/** Minimal shape persisted to the backend. */
export interface PersistedSession {
	sessionId: string | null;
	title: string;
	profile: string;
	project: string;
	modelOverride: string | null;
	permissionModeOverride: string | null;
	thinkingModeOverride: string | null;
	executionMode?: ExecutionMode;
	worktreeId?: string | null;
	worktreeBranch?: string;
	selectedWorktreeBranch?: string | null;
	worktreeMode?: 'new' | 'existing';
	worktreeDeleted?: boolean;
	queuedMessages?: QueuedMessage[];
}

// ─── Constants ───────────────────────────────────────────────────────────────

const CONNECTION_HEALTH_TIMEOUT_MS = 90_000;
const STREAMING_HEALTH_TIMEOUT_MS = 45_000;
const TOOL_EXECUTION_TIMEOUT_MS = 120_000;
const MAX_RECONNECT_ATTEMPTS = 50;
const MAX_RECONNECT_DELAY_MS = 30_000;
const PING_INTERVAL_MS = 30_000;
const SELECTION_SYNC_DEBOUNCE_MS = 1000;

// ─── Module-Level Helpers ────────────────────────────────────────────────────

function getPersistedProfile(): string {
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProfile') || '';
	}
	return '';
}

function getPersistedProject(): string {
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProject') || '';
	}
	return '';
}

async function loadSelectionFromBackend(): Promise<{ selectedProfile: string; selectedProject: string } | null> {
	if (typeof window === 'undefined') return null;
	try {
		const response = await fetch('/api/v1/preferences/selection', { credentials: 'include' });
		if (response.ok) {
			const data = await response.json();
			if (data?.value) {
				const sel = data.value as { selectedProfile?: string; selectedProject?: string };
				const selectedProfile = sel.selectedProfile || '';
				const selectedProject = sel.selectedProject || '';
				if (selectedProfile) localStorage.setItem('aihub_selectedProfile', selectedProfile);
				if (selectedProject) localStorage.setItem('aihub_selectedProject', selectedProject);
				return { selectedProfile, selectedProject };
			}
		}
	} catch (e) {
		console.warn('[ActiveSession] Failed to load selection from backend:', e);
	}
	return null;
}

let pendingSelection: { profile: string; project: string } | null = null;
let selectionSyncTimeout: ReturnType<typeof setTimeout> | null = null;

function saveSelectionToBackend(profile: string, project: string) {
	const effectiveProfile = profile || getPersistedProfile();
	const effectiveProject = project || getPersistedProject();

	if (typeof window !== 'undefined') {
		if (effectiveProfile) localStorage.setItem('aihub_selectedProfile', effectiveProfile);
		if (effectiveProject) localStorage.setItem('aihub_selectedProject', effectiveProject);
	}

	pendingSelection = { profile: effectiveProfile, project: effectiveProject };

	if (selectionSyncTimeout) clearTimeout(selectionSyncTimeout);
	selectionSyncTimeout = setTimeout(async () => {
		if (!pendingSelection) return;
		try {
			await fetch('/api/v1/preferences/selection', {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({
					key: 'selection',
					value: { selectedProfile: pendingSelection.profile, selectedProject: pendingSelection.project }
				})
			});
		} catch (e) {
			console.error('[ActiveSession] Failed to sync selection to backend:', e);
		}
		pendingSelection = null;
		selectionSyncTimeout = null;
	}, SELECTION_SYNC_DEBOUNCE_MS);
}

function getOrCreateDeviceId(): string {
	const CANONICAL_KEY = 'ai-shuffle-device-id';
	const LEGACY_KEY = 'aihub_device_id';

	if (typeof window === 'undefined') {
		return `device-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
	}

	let deviceId = localStorage.getItem(CANONICAL_KEY);
	if (deviceId) return deviceId;

	const legacyId = localStorage.getItem(LEGACY_KEY);
	if (legacyId) {
		localStorage.setItem(CANONICAL_KEY, legacyId);
		localStorage.removeItem(LEGACY_KEY);
		return legacyId;
	}

	deviceId = `device-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
	localStorage.setItem(CANONICAL_KEY, deviceId);
	return deviceId;
}

function getWsUrl(): string {
	const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
	const host = window.location.host;
	const deviceId = getOrCreateDeviceId();
	return `${protocol}//${host}/api/v1/ws/chat?device_id=${encodeURIComponent(deviceId)}`;
}

function getAuthToken(): string | null {
	const cookies = document.cookie.split(';');
	for (const cookie of cookies) {
		const [name, value] = cookie.trim().split('=');
		if (name === 'session') return value;
	}
	return null;
}

function formatWorktreeError(rawError: string, branchName: string): string {
	const lowerError = rawError.toLowerCase();
	if (lowerError.includes('already checked out') || lowerError.includes('is already used by worktree')) {
		return `A worktree already exists for branch '${branchName}'. Select it from Active Worktrees instead.`;
	}
	if (lowerError.includes('not a valid branch') || lowerError.includes('branch not found') ||
		lowerError.includes('unknown revision') || (lowerError.includes('pathspec') && lowerError.includes('did not match'))) {
		return `Branch '${branchName}' doesn't exist. Try fetching from remote first or create a new branch.`;
	}
	if (lowerError.includes('already exists') && (lowerError.includes('path') || lowerError.includes('folder') || lowerError.includes('directory'))) {
		return `Cannot create worktree: a folder already exists at the target path. Try a different branch name.`;
	}
	if (lowerError.includes('.lock') || lowerError.includes('unable to lock') || lowerError.includes('index.lock')) {
		return `Git operation in progress. Please wait a moment and try again.`;
	}
	if (lowerError.includes('permission denied') || lowerError.includes('access denied')) {
		return `Permission denied while creating worktree. Check folder permissions.`;
	}
	if (lowerError.includes('could not resolve') || lowerError.includes('connection refused') ||
		lowerError.includes('network') || lowerError.includes('timeout')) {
		return `Network error while creating worktree. Check your connection and try again.`;
	}
	return `Failed to create worktree: ${rawError}`;
}

/** Build a partial store update from SessionUsage, skipping if no context data. */
function usageStoreUpdate(usage: SessionUsage): Partial<ActiveSessionState> {
	if (usage.contextUsed <= 0) return {};
	return {
		totalTokensIn: usage.inputTokens,
		totalTokensOut: usage.outputTokens,
		totalCacheCreationTokens: usage.cacheCreationTokens,
		totalCacheReadTokens: usage.cacheReadTokens,
		contextUsed: usage.contextUsed,
	};
}

// ─── Default state ───────────────────────────────────────────────────────────

function defaultState(): ActiveSessionState {
	return {
		sessionId: null,
		title: 'New Chat',
		messages: [],
		isStreaming: false,
		wsConnected: false,
		error: null,
		profile: getPersistedProfile(),
		sessionProfileId: null,
		currentQueryProfileId: null,
		project: getPersistedProject(),
		totalTokensIn: 0,
		totalTokensOut: 0,
		totalCacheCreationTokens: 0,
		totalCacheReadTokens: 0,
		contextUsed: null,
		contextMax: 200_000,
		modelOverride: null,
		permissionModeOverride: null,
		thinkingModeOverride: null,
		pendingPermissions: [],
		pendingQuestions: [],
		todos: [],
		queuedMessages: [],
		draft: '',
		scrollTop: 0,
		streamStartTime: null,
		executionMode: 'local',
		worktreeBranch: '',
		worktreeBaseBranch: 'main',
		worktreeCreateNewBranch: false,
		worktreeId: null,
		worktreeMode: 'new',
		selectedWorktreeBranch: null,
		isCreatingWorktree: false,
		worktreeError: null,
		worktreeDeleted: false,
		sessionLocked: false,
		sessionLockedBy: undefined,
	};
}

// ─── Store Factory ───────────────────────────────────────────────────────────

function createActiveSessionStore() {
	const store = writable<ActiveSessionState>(defaultState());
	const { subscribe, update, set } = store;

	// ── Module-level scalars (replace per-tab Maps) ────────────────────────

	let ws: WebSocket | null = null;
	let pingTimer: ReturnType<typeof setInterval> | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let stopTimeout: ReturnType<typeof setTimeout> | null = null;
	let persistTimer: ReturnType<typeof setTimeout> | null = null;
	let waitingForHistory: Array<Record<string, unknown>> | null = null;
	let expectedSessionId: string | null = null;
	let pendingTools: Set<string> = new Set();
	let lastMessageTime = 0;
	let streamEndCleanedUp = false;
	let streamingOnDisconnect: string | null = null;
	let interruptPending = false;
	let reconnectAttempts = 0;
	let intentionalDisconnect = false;

	// ── Internal helpers ───────────────────────────────────────────────────

	function startPolling(sessionId: string) {
		sessionPoller.startStreaming(sessionId);
	}

	function stopPolling() {
		const state = get(store);
		if (state.sessionId) {
			sessionPoller.stop(state.sessionId);
		}
	}

	function processNextQueuedMessage() {
		const state = get(store);
		if (state.queuedMessages.length === 0) return;
		if (!ws || ws.readyState !== WebSocket.OPEN) return;
		if (!state.sessionId) return;

		const nextQueued = state.queuedMessages[0];

		update(s => ({
			...s,
			queuedMessages: s.queuedMessages.slice(1)
		}));

		const userMsgId = `user-${Date.now()}`;
		const queryProfileId = state.currentQueryProfileId || state.profile;

		update(s => ({
			...s,
			messages: [...s.messages, {
				id: userMsgId,
				role: 'user' as const,
				content: nextQueued.content,
				profileId: queryProfileId
			}],
			isStreaming: true,
			error: null,
			streamStartTime: Date.now()
		}));

		const overrides: Record<string, string> = {};
		const current = get(store);
		if (current.modelOverride) overrides.model = current.modelOverride;
		if (current.permissionModeOverride) overrides.permission_mode = current.permissionModeOverride;
		if (current.thinkingModeOverride) overrides.thinking_mode = current.thinkingModeOverride;

		lastMessageTime = Date.now();
		ws.send(JSON.stringify({
			type: 'query',
			prompt: nextQueued.content,
			session_id: state.sessionId,
			profile: state.profile,
			project: state.project || undefined,
			worktree_id: state.worktreeId || undefined,
			...(Object.keys(overrides).length > 0 ? { overrides } : {})
		}));

		persistSession();
	}

	function processWaitingEvents() {
		if (waitingForHistory && waitingForHistory.length > 0) {
			const queued = waitingForHistory;
			waitingForHistory = null;
			for (const queuedData of queued) {
				// Re-route through notifier for poller triggers
				const state = get(store);
				if (state.sessionId) {
					sessionNotifier.handleMessage(state.sessionId, queuedData);
				}
			}
		} else {
			waitingForHistory = null;
		}
	}

	// ── Streaming bridge ──────────────────────────────────────────────────
	// Subscribes to sessionState so that polling updates flow to
	// activeSession.messages in real-time during streaming — not just at
	// stream end.

	function setupStreamingBridge() {
		sessionState.subscribe(($sessionState) => {
			const currentState = get(store);
			if (!currentState.sessionId) return;

			const sessionData = $sessionState.sessions.get(currentState.sessionId);
			if (!sessionData || sessionData.messages.length === 0) return;

			// Forward if the store says we're streaming OR the poller still has
			// data (sessionState tracks server-side streaming independently).
			// This closes the gap where `done` sets store.isStreaming=false but
			// the poller's final poll hasn't been applied yet.
			if (!currentState.isStreaming && !sessionData.isStreaming && !sessionPoller.isPolling(currentState.sessionId)) return;

			// Skip if messages reference hasn't changed. During compaction's full
			// refresh, preserveMessageIds may return the same array when content
			// matches. Skipping here avoids a redundant reactive update that the
			// deferred onStreamEnd would also trigger, reducing DOM churn.
			if (currentState.messages === sessionData.messages) return;

			update(s => {
				if (s.sessionId !== currentState.sessionId) return s;
				return {
					...s,
					messages: sessionData.messages,
					todos: sessionData.todos.length > 0 ? sessionData.todos : s.todos,
					...usageStoreUpdate(sessionData.usage),
				};
			});
		});
	}

	// ── onStreamEnd callback ───────────────────────────────────────────────
	// Wired up after factory creation in the returned init().
	// Flushes final messages from sessionState to the store before cleanup.

	function setupOnStreamEnd() {
		sessionPoller.onStreamEnd((sessionId, usage) => {
			// If in interrupt_and_query transition, skip cleanup
			if (interruptPending) return;

			// Refresh sessions list
			appData.loadSessions();

			// Flush final messages AND usage from sessionState to the store.
			// NOTE: This callback is deferred via queueMicrotask() from the poller
			// to avoid a double-update with the streaming bridge in the same
			// synchronous call stack (which can corrupt Svelte's effect list
			// during compaction full refresh + stream end).
			const finalSession = sessionState.getSession(sessionId);
			if (finalSession && finalSession.messages.length > 0) {
				update(s => {
					if (s.sessionId !== sessionId) return s;
					// If the bridge already flushed these exact messages, only
					// update isStreaming and usage — skip the messages reference
					// change to minimize reactive DOM churn.
					const messagesChanged = s.messages !== finalSession.messages;
					return {
						...s,
						messages: messagesChanged ? finalSession.messages : s.messages,
						isStreaming: false,
						todos: finalSession.todos.length > 0 ? finalSession.todos : s.todos,
						...usageStoreUpdate(finalSession.usage),
					};
				});
			}

			// Update cache with final post-streaming messages
			if (finalSession && finalSession.messages.length > 0) {
				messageCache.set(sessionId, finalSession.messages);
			}

			// Clean up sessionState after reactive cycle completes
			setTimeout(() => sessionState.removeSession(sessionId), 100);

			// Mark as handled so the safety timeout doesn't send redundant load_session
			streamEndCleanedUp = true;
		});
	}

	// ── WebSocket Message Handler ──────────────────────────────────────────

	function handleMessage(data: Record<string, unknown>) {
		const msgType = data.type as string;
		const eventType = data.event_type as string;

		// Forward to notification system (triggers poller)
		const state = get(store);
		if (state.sessionId) {
			const isInterruptTransition = interruptPending &&
				(msgType === 'interrupted' || msgType === 'stopped');
			if (!isInterruptTransition) {
				sessionNotifier.handleMessage(state.sessionId, data);
			}
		}

		// Handle sync events from other devices — queue if waiting for history
		if (eventType) {
			if (waitingForHistory !== null) {
				waitingForHistory.push(data);
			}
			// Notifier already triggered pollNow for update events
			return;
		}

		// ── User-level messages (not per-session) ─────────────────────────

		if (msgType === 'tabs_sync') return; // No-op in single-session model

		if (msgType === 'user_session_created') {
			appData.loadSessions();
			appData.loadApiUserSessions();
			return;
		}

		if (msgType === 'user_streaming_changed') {
			const sessionId = data.session_id as string;
			const isStreaming = data.is_streaming as boolean;
			const projectId = (data.project_id as string | null) ?? null;
			const worktreeId = (data.worktree_id as string | null) ?? null;
			streamingSessions.update(map => {
				const newMap = new Map(map);
				if (isStreaming) {
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
			const gitState = get(git);
			if (gitState.projectId === projectId && !gitState.loading) {
				git.refreshStatus();
				git.refreshDiffStats();
			}
			return;
		}

		if (msgType === 'git_state_changed') {
			const projectId = data.project_id as string;
			const worktreeId = (data.worktree_id as string) || null;
			const changes = data.changes as Record<string, unknown>;
			if (projectId && changes) {
				git.applyServerUpdate(projectId, worktreeId, changes);
			}
			return;
		}

		// ── Per-session message switch ────────────────────────────────────

		switch (msgType) {
			case 'history': {
				const receivedSessionId = data.session_id as string;
				if (expectedSessionId && receivedSessionId !== expectedSessionId) {
					waitingForHistory = null;
					break;
				}
				expectedSessionId = null;

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
					// Restore profile and project from the loaded session so the header
					// and profile selector reflect the session's actual context.
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
				const currentState = get(store);
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
						// Non-worktree session: reset to local mode so stale worktree
						// state from a previously viewed tab doesn't leak through.
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
					streamingOnDisconnect = null;
					startPolling(receivedSession);
					if (ws?.readyState === WebSocket.OPEN) {
						ws.send(JSON.stringify({ type: 'get_pending_permissions' }));
						ws.send(JSON.stringify({ type: 'get_pending_questions' }));
					}
				} else if (receivedSession) {
					// Session is NOT streaming — ensure streamingSessions is clean.
					// This handles the case where we missed a done/user_streaming_changed
					// event during a WS disconnect, leaving stale entries that lock git.
					streamingSessions.update(map => {
						if (!map.has(receivedSession)) return map;
						const newMap = new Map(map);
						newMap.delete(receivedSession);
						return newMap;
					});
					const disconnectSessionId = streamingOnDisconnect;
					if (disconnectSessionId && disconnectSessionId === receivedSession) {
						streamingOnDisconnect = null;
						setTimeout(() => {
							const gracedSession = sessionState.getSession(disconnectSessionId);
							if (gracedSession && !gracedSession.isStreaming && sessionPoller.isPolling(disconnectSessionId)) {
								sessionPoller.stop(disconnectSessionId);
								sessionState.removeSession(disconnectSessionId);
							}
						}, 10000);
					} else {
						streamingOnDisconnect = null;
						stopPolling();
					}
				}

				// Clear background permissions for this session since we just loaded it
				backgroundPermissions.update(map => {
					if (!map.has(receivedSession)) return map;
					const newMap = new Map(map);
					newMap.delete(receivedSession);
					return newMap;
				});

				processWaitingEvents();
				break;
			}

			case 'session_created': {
				const sessionId = data.session_id as string;
				const currentState = get(store);
				// Only adopt session ID for the active session or new chat (no session yet).
				// Prevents stale session_created from overwriting a session switch.
				if (!currentState.sessionId || currentState.sessionId === sessionId) {
					update(s => ({ ...s, sessionId, sessionProfileId: s.profile }));
					persistSession();
				}
				appData.loadSessions();
				break;
			}

			case 'start': {
				const sessionId = data.session_id as string;
				interruptPending = false;
				const currentStartState = get(store);
				// Only update if this is for the active session or we just started a new chat (no session yet).
				// Without this guard, a stale start from a previous session could hijack the current tab.
				if (sessionId === currentStartState.sessionId || !currentStartState.sessionId) {
					update(s => ({ ...s, sessionId, isStreaming: true, streamStartTime: Date.now() }));
					persistSession();
				}
				break;
			}

			// Notification-only events — poller handles state via sessionNotifier
			case 'chunk':
			case 'stream_start':
			case 'stream_block_start':
			case 'stream_delta':
			case 'stream_block_stop':
			case 'stream_message_delta':
			case 'system':
			case 'subagent_start':
			case 'subagent_chunk':
			case 'subagent_done':
			case 'plugin_message':
				break;

			case 'tool_use': {
				const toolId = data.id as string;
				if (toolId) pendingTools.add(toolId);
				break;
			}

			case 'tool_result': {
				const toolUseId = data.tool_use_id as string;
				if (toolUseId) pendingTools.delete(toolUseId);
				break;
			}

			case 'subagent_tool_use': {
				const toolId = data.id as string;
				if (toolId) pendingTools.add(toolId);
				break;
			}

			case 'subagent_tool_result': {
				const toolUseId = data.tool_use_id as string;
				if (toolUseId) pendingTools.delete(toolUseId);
				break;
			}

			case 'done': {
				const metadata = data.metadata as Record<string, unknown>;
				const sessionId = data.session_id as string;

				interruptPending = false;

				const currentDoneState = get(store);
				// Determine if this done event is for the currently active session.
				// If sessionId is falsy, assume it's for the active session (legacy compat).
				const isActiveSession = !sessionId || sessionId === currentDoneState.sessionId;
				const doneSessionId = sessionId || currentDoneState.sessionId;

				if (doneSessionId) sessionPoller.pollNow(doneSessionId);

				if (isActiveSession) {
					if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }
					pendingTools.clear();
				}

				// Only update session-level store state for the active session.
				// Without this guard, a done event from a previous session would
				// overwrite sessionId and corrupt the current tab's state.
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

					const doneState = get(store);
					notifySessionComplete({
						sessionId: sessionId || '',
						sessionTitle: doneState.title ?? null,
						durationSeconds,
						totalCostUsd: metadata?.total_cost_usd as number | undefined,
						tokensIn: metadata?.tokens_in as number | undefined,
						tokensOut: metadata?.tokens_out as number | undefined,
						model: metadata?.model as string | undefined
					});

					persistSession();
					setTimeout(() => processNextQueuedMessage(), 500);
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

				appData.loadSessions();

				// Safety: ensure cleanup if poller pipeline fails (only for active session)
				if (isActiveSession && doneSessionId) {
					const cleanupId = doneSessionId;
					setTimeout(() => {
						if (streamEndCleanedUp) { streamEndCleanedUp = false; return; }
						console.warn(`[ActiveSession] Safety: onStreamEnd didn't fire for ${cleanupId}, running fallback`);
						if (sessionPoller.isPolling(cleanupId)) sessionPoller.stop(cleanupId);
						const sd = sessionState.getSession(cleanupId);
						if (sd) sessionState.removeSession(cleanupId);
						const refreshState = get(store);
						if (!refreshState.isStreaming && refreshState.sessionId === cleanupId && ws?.readyState === WebSocket.OPEN) {
							expectedSessionId = cleanupId;
							waitingForHistory = [];
							ws.send(JSON.stringify({ type: 'load_session', session_id: cleanupId }));
						}
					}, 5000);
				}
				break;
			}

			case 'stopped':
			case 'interrupted': {
				if (interruptPending) { pendingTools.clear(); break; }

				const stoppedState = get(store);
				const stoppedSessionId = stoppedState.sessionId;
				if (stoppedSessionId) sessionPoller.pollNow(stoppedSessionId);

				if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }
				pendingTools.clear();

				update(s => ({ ...s, isStreaming: false }));

				if (stoppedSessionId) {
					streamingSessions.update(map => {
						if (!map.has(stoppedSessionId)) return map;
						const newMap = new Map(map);
						newMap.delete(stoppedSessionId);
						return newMap;
					});
				}

				setTimeout(() => processNextQueuedMessage(), 500);

				// Safety cleanup
				if (stoppedSessionId) {
					setTimeout(() => {
						if (sessionPoller.isPolling(stoppedSessionId)) sessionPoller.stop(stoppedSessionId);
						const sd = sessionState.getSession(stoppedSessionId);
						if (sd) sessionState.removeSession(stoppedSessionId);
					}, 5000);
				}
				break;
			}

			case 'error': {
				interruptPending = false;
				stopPolling();
				update(s => ({ ...s, isStreaming: false, error: data.message as string }));
				const errorSessionId = get(store).sessionId;
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
				const missingId = data.session_id as string;
				console.warn(`[ActiveSession] Session not found: ${missingId}`);
				// Refresh sidebar to remove stale entry
				appData.loadSessions();
				// Navigate to new chat if this was the active session
				const currentId = get(store).sessionId;
				if (currentId === missingId) {
					update(s => ({ ...s, error: 'This session no longer exists.' }));
					storeApi.newChat();
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
				if (ws?.readyState === WebSocket.OPEN) {
					ws.send(JSON.stringify({ type: 'pong' }));
				}
				break;
			}

			case 'heartbeat': {
				const pendingPermCount = data.pending_permissions as number | undefined;
				const pendingQCount = data.pending_questions as number | undefined;
				if (pendingPermCount && pendingPermCount > 0) {
					const cs = get(store);
					if (!cs.pendingPermissions || cs.pendingPermissions.length === 0) {
						if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'get_pending_permissions' }));
					}
				}
				if (pendingQCount && pendingQCount > 0) {
					const cs = get(store);
					if (!cs.pendingQuestions || cs.pendingQuestions.length === 0) {
						if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'get_pending_questions' }));
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

				notifyWaitingForResponse('permission', { toolName: request.tool_name });
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

				notifyWaitingForResponse('question', { questionCount: request.questions.length });
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
				// Only replace if the set of request IDs actually changed.
				// Blindly replacing creates new object references which can
				// trigger reactive effects in the UserQuestion component,
				// wiping the user's in-progress answers and "Other" text.
				update(s => {
					const existingIds = new Set(s.pendingQuestions.map(q => q.request_id));
					const newIds = new Set(questions.map(q => q.request_id));
					const same = existingIds.size === newIds.size && [...existingIds].every(id => newIds.has(id));
					if (same) return s; // No change — keep existing references
					return { ...s, pendingQuestions: questions };
				});
				break;
			}

			default:
				if (msgType) console.debug(`[ActiveSession] Unhandled message type: ${msgType}`);
				break;
		}
	}

	// ── WebSocket Connection ───────────────────────────────────────────────

	function connect() {
		if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

		const token = getAuthToken();
		let url = getWsUrl();
		if (token) url = `${url}&token=${encodeURIComponent(token)}`;

		ws = new WebSocket(url);

		ws.onopen = () => {
			reconnectAttempts = 0;
			update(s => ({ ...s, wsConnected: true, error: null }));

			const state = get(store);
			if (state.sessionId) {
				expectedSessionId = state.sessionId;
				waitingForHistory = [];
				ws!.send(JSON.stringify({ type: 'load_session', session_id: state.sessionId }));
			}

			startPing();
		};

		ws.onclose = (event) => {
			// Guard: ignore stale close events from a previous WebSocket instance.
			// Without this, an old WS's onclose can call stopPing() and kill the
			// health-check timer that belongs to the new, active connection.
			if (event.target !== ws && ws !== null) {
				return;
			}

			const state = get(store);
			const wasStreaming = state.isStreaming;
			const sessionId = state.sessionId;

			update(s => ({ ...s, wsConnected: false }));

			if (wasStreaming && sessionId) {
				sessionPoller.startStreaming(sessionId);
				streamingOnDisconnect = sessionId;
			}

			if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }
			stopPing();

			if (event.code === 4001) {
				console.warn('[ActiveSession] WebSocket auth failed (4001), redirecting to login');
				import('$app/navigation').then(({ goto }) => goto('/login'));
			} else if (intentionalDisconnect) {
				// User-initiated disconnect (newChat, destroy, session switch, etc.)
				intentionalDisconnect = false;
			} else {
				// Unexpected close — always reconnect regardless of close code.
				// The server can close with code 1000 in many legitimate scenarios
				// (handler exit, starlette cleanup) where the frontend should reconnect.
				if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
					console.error('[ActiveSession] Max reconnect attempts reached');
					update(s => ({ ...s, error: 'Connection lost. Please refresh the page.' }));
					return;
				}

				if (reconnectTimer) clearTimeout(reconnectTimer);
				const baseDelay = Math.min(1000 * Math.pow(2, reconnectAttempts), MAX_RECONNECT_DELAY_MS);
				const jitter = Math.random() * 1000;
				reconnectAttempts++;
				reconnectTimer = setTimeout(() => connect(), baseDelay + jitter);
			}
		};

		ws.onerror = (error) => {
			console.error('[ActiveSession] WebSocket error:', error);
		};

		ws.onmessage = (event) => {
			lastMessageTime = Date.now();
			try {
				handleMessage(JSON.parse(event.data));
			} catch (e) {
				console.error('[ActiveSession] Failed to parse message:', e);
			}
		};
	}

	function disconnect() {
		if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
		stopPing();

		const state = get(store);
		if (state.sessionId) sessionPoller.stop(state.sessionId);

		waitingForHistory = null;
		expectedSessionId = null;
		lastMessageTime = 0;
		pendingTools = new Set();
		streamingOnDisconnect = null;
		interruptPending = false;

		if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }

		intentionalDisconnect = true;
		if (ws) { ws.close(1000); ws = null; }
		update(s => ({ ...s, wsConnected: false }));
	}

	function startPing() {
		stopPing();
		lastMessageTime = Date.now();

		pingTimer = setInterval(() => {
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'pong' }));

				const state = get(store);
				let healthTimeout: number;
				if (pendingTools.size > 0) healthTimeout = TOOL_EXECUTION_TIMEOUT_MS;
				else if (state.isStreaming) healthTimeout = STREAMING_HEALTH_TIMEOUT_MS;
				else healthTimeout = CONNECTION_HEALTH_TIMEOUT_MS;

				const timeSinceLastMessage = Date.now() - lastMessageTime;
				if (timeSinceLastMessage > healthTimeout) {
					if (state.isStreaming && state.sessionId && sessionPoller.isPolling(state.sessionId)) {
						// Poller active — WS is secondary, skip force-close
					} else {
						console.warn(`[ActiveSession] Connection stale (${Math.round(timeSinceLastMessage / 1000)}s), forcing reconnect`);
						ws.close(4000, 'Connection health timeout');
					}
				}
			}
		}, PING_INTERVAL_MS);
	}

	function stopPing() {
		if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
	}

	// ── Init / Destroy ─────────────────────────────────────────────────────

	async function init() {
		setupOnStreamEnd();
		setupStreamingBridge();

		// Reconnect WebSocket when the tab regains focus.
		// Browsers can silently drop WebSocket connections when tabs are backgrounded.
		// Also pause pings + polling when hidden to reduce idle CPU usage.
		if (typeof document !== 'undefined') {
			document.addEventListener('visibilitychange', () => {
				if (document.hidden) {
					// Tab is hidden — pause pings to reduce idle CPU.
					// The backend ping will keep the connection alive.
					stopPing();
					// Pause session polling — no one is looking at the UI
					const state = get(store);
					if (state.sessionId && sessionPoller.isPolling(state.sessionId)) {
						sessionPoller.stop(state.sessionId);
					}
				} else {
					// Tab is visible again — resume pings and check connection health
					if (ws?.readyState === WebSocket.OPEN) {
						startPing();
					}

					// Resume streaming poll if session was streaming
					const state = get(store);
					if (state.isStreaming && state.sessionId) {
						sessionPoller.startStreaming(state.sessionId);
					}

					if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
						reconnectAttempts = 0;
						connect();
					} else if (ws.readyState === WebSocket.OPEN) {
						// Don't blindly reset lastMessageTime — the connection may be
						// half-open (browser reports OPEN but TCP is dead). Instead,
						// send a pong and verify we get a server response within 5s.
						const preVisibilityTime = lastMessageTime;
						try { ws.send(JSON.stringify({ type: 'pong' })); } catch {
							reconnectAttempts = 0;
							connect();
							return;
						}
						// If no server message arrives within 5s, force reconnect
						setTimeout(() => {
							if (lastMessageTime === preVisibilityTime && ws?.readyState === WebSocket.OPEN) {
								console.warn('[ActiveSession] No server response after tab return, forcing reconnect');
								ws.close(4000, 'Half-open connection detected');
							}
						}, 5000);
					}
				}
			});
		}

		try {
			const backendSelection = await loadSelectionFromBackend();
			const defaultProfile = backendSelection?.selectedProfile || getPersistedProfile();
			const defaultProject = backendSelection?.selectedProject || getPersistedProject();

			const persisted = await loadPersistedSession();

			if (persisted) {
				update(s => ({
					...s,
					sessionId: persisted.sessionId,
					title: persisted.title || 'New Chat',
					profile: persisted.profile || defaultProfile,
					project: persisted.project || defaultProject,
					modelOverride: persisted.modelOverride ?? null,
					permissionModeOverride: persisted.permissionModeOverride ?? null,
					thinkingModeOverride: persisted.thinkingModeOverride ?? null,
					executionMode: persisted.executionMode || 'local',
					worktreeBranch: persisted.worktreeBranch || '',
					worktreeId: persisted.worktreeId ?? null,
					worktreeMode: persisted.worktreeMode || 'new',
					selectedWorktreeBranch: persisted.selectedWorktreeBranch ?? null,
					worktreeDeleted: persisted.worktreeDeleted ?? false,
					queuedMessages: persisted.queuedMessages || [],
				}));

				if (persisted.sessionId) {
					const cached = await messageCache.get(persisted.sessionId);
					if (cached && cached.length > 0) {
						update(s => ({ ...s, messages: cached }));
					}
				}
			} else {
				update(s => ({ ...s, profile: defaultProfile, project: defaultProject }));
			}

			connect();
		} catch (error) {
			console.error('[ActiveSession] Failed to initialize:', error);
			connect();
		}
	}

	async function loadPersistedSession(): Promise<PersistedSession | null> {
		try {
			const response = await api.get<{ key: string; value: PersistedSession } | null>('/preferences/active_session');
			if (response?.value) return response.value;
		} catch {
			// No persisted session — fresh start
		}
		return null;
	}

	function destroy() {
		disconnect();
		if (persistTimer) { clearTimeout(persistTimer); persistTimer = null; }
		set(defaultState());
	}

	// ── Persistence ────────────────────────────────────────────────────────

	function persistSession() {
		if (persistTimer) clearTimeout(persistTimer);
		persistTimer = setTimeout(async () => {
			const state = get(store);
			try {
				await fetch('/api/v1/preferences/active_session', {
					method: 'PUT',
					headers: { 'Content-Type': 'application/json' },
					credentials: 'include',
					body: JSON.stringify({
						key: 'active_session',
						value: {
							sessionId: state.sessionId,
							title: state.title,
							profile: state.profile,
							project: state.project,
							modelOverride: state.modelOverride,
							permissionModeOverride: state.permissionModeOverride,
							thinkingModeOverride: state.thinkingModeOverride,
							executionMode: state.executionMode,
							worktreeId: state.worktreeId,
							worktreeBranch: state.worktreeBranch,
							selectedWorktreeBranch: state.selectedWorktreeBranch,
							worktreeMode: state.worktreeMode,
							worktreeDeleted: state.worktreeDeleted,
							queuedMessages: state.queuedMessages,
						} satisfies PersistedSession,
					})
				});
			} catch (e) {
				console.error('[ActiveSession] Failed to persist session:', e);
			}
		}, 500);
	}

	// ── Public API ─────────────────────────────────────────────────────────

	return {
		subscribe,

		init,
		destroy,

		// ── Session Navigation ──────────────────────────────────────────

		async switchSession(sessionId: string) {
			const state = get(store);

			// Save ephemeral for session we're leaving (use sentinel for new chat)
			const leavingKey = state.sessionId || '__new_chat__';
			sessionEphemeralCache.save(leavingKey, {
				draft: state.draft,
				scrollTop: state.scrollTop,
				modelOverride: state.modelOverride,
				permissionModeOverride: state.permissionModeOverride,
				thinkingModeOverride: state.thinkingModeOverride,
				executionMode: state.executionMode,
				worktreeId: state.worktreeId,
				worktreeBranch: state.worktreeBranch,
				worktreeBaseBranch: state.worktreeBaseBranch,
				selectedWorktreeBranch: state.selectedWorktreeBranch,
				worktreeMode: state.worktreeMode,
				worktreeDeleted: state.worktreeDeleted,
				queuedMessages: state.queuedMessages,
			});

			// Hydrate from IndexedDB cache for instant render
			const cachedMessages = await messageCache.get(sessionId);
			update(s => ({
				...s,
				sessionId,
				messages: cachedMessages && cachedMessages.length > 0 ? cachedMessages : [],
				error: null,
				// Reset session-specific state
				draft: '',
				pendingPermissions: [],
				pendingQuestions: [],
				isStreaming: false,
				isCreatingWorktree: false,
				worktreeError: null,
				sessionLocked: false,
				sessionLockedBy: undefined,
			}));

			// Switch browser panel to new session's context (saves/restores per-session).
			// Panel stays open; only the content (URL, screenshot, etc.) swaps.
			browserState.switchSession(state.sessionId, sessionId);

			// Restore ephemeral from cache
			const ephemeral = sessionEphemeralCache.restore(sessionId);
			if (ephemeral) {
				update(s => ({
					...s,
					draft: ephemeral.draft,
					scrollTop: ephemeral.scrollTop,
					modelOverride: ephemeral.modelOverride,
					permissionModeOverride: ephemeral.permissionModeOverride,
					thinkingModeOverride: ephemeral.thinkingModeOverride,
					executionMode: ephemeral.executionMode,
					worktreeId: ephemeral.worktreeId,
					worktreeBranch: ephemeral.worktreeBranch,
					worktreeBaseBranch: ephemeral.worktreeBaseBranch,
					selectedWorktreeBranch: ephemeral.selectedWorktreeBranch,
					worktreeMode: ephemeral.worktreeMode,
					worktreeDeleted: ephemeral.worktreeDeleted,
					queuedMessages: ephemeral.queuedMessages,
				}));
			}

			// Load full session from backend via WS
			if (ws?.readyState === WebSocket.OPEN) {
				expectedSessionId = sessionId;
				waitingForHistory = [];
				ws.send(JSON.stringify({ type: 'load_session', session_id: sessionId }));
			}

			persistSession();
		},

		newChat() {
			const state = get(store);

			// Save ephemeral for the session we're leaving
			const leavingKey = state.sessionId || '__new_chat__';
			sessionEphemeralCache.save(leavingKey, {
				draft: state.draft,
				scrollTop: state.scrollTop,
				modelOverride: state.modelOverride,
				permissionModeOverride: state.permissionModeOverride,
				thinkingModeOverride: state.thinkingModeOverride,
				executionMode: state.executionMode,
				worktreeId: state.worktreeId,
				worktreeBranch: state.worktreeBranch,
				worktreeBaseBranch: state.worktreeBaseBranch,
				selectedWorktreeBranch: state.selectedWorktreeBranch,
				worktreeMode: state.worktreeMode,
				worktreeDeleted: state.worktreeDeleted,
				queuedMessages: state.queuedMessages,
			});

			// Tell backend to release session lock
			if (ws?.readyState === WebSocket.OPEN && state.sessionId) {
				ws.send(JSON.stringify({ type: 'close_session' }));
			}

			// Reset session-specific state, restore profile/project to user defaults
			update(s => ({
				...s,
				sessionId: null,
				sessionProfileId: null,
				currentQueryProfileId: null,
				profile: getPersistedProfile() || s.profile,
				project: getPersistedProject() || s.project,
				messages: [],
				isStreaming: false,
				error: null,
				title: 'New Chat',
				totalTokensIn: 0,
				totalTokensOut: 0,
				totalCacheCreationTokens: 0,
				totalCacheReadTokens: 0,
				contextUsed: null,
				contextMax: 200_000,
				modelOverride: null,
				permissionModeOverride: null,
				thinkingModeOverride: null,
				pendingPermissions: [],
				pendingQuestions: [],
				todos: [],
				queuedMessages: [],
				draft: '',
				scrollTop: 0,
				streamStartTime: null,
				executionMode: 'local' as const,
				worktreeBranch: '',
				worktreeBaseBranch: 'main',
				worktreeCreateNewBranch: false,
				worktreeId: null,
				worktreeMode: 'new' as const,
				selectedWorktreeBranch: null,
				isCreatingWorktree: false,
				worktreeError: null,
				worktreeDeleted: false,
				sessionLocked: false,
				sessionLockedBy: undefined,
			}));

			pendingTools.clear();
			expectedSessionId = null;
			waitingForHistory = null;

			// Save current browser state and show empty for the new chat
			browserState.switchSession(state.sessionId, null);

			// Restore new chat draft if one was saved earlier
			const newChatEphemeral = sessionEphemeralCache.restore('__new_chat__');
			if (newChatEphemeral?.draft) {
				update(s => ({ ...s, draft: newChatEphemeral.draft }));
			}

			persistSession();
		},

		// ── Messaging ───────────────────────────────────────────────────

		async sendMessage(prompt: string, options?: { skipWorktreeSetup?: boolean }) {
			const state = get(store);

			// Worktree setup on first message
			if (!options?.skipWorktreeSetup && state.executionMode === 'worktree' && !state.sessionId && state.messages.length === 0) {
				if (state.worktreeMode === 'new') {
					await storeApi.createWorktree(prompt);
					return;
				} else if (state.worktreeMode === 'existing' && state.worktreeId) {
					await startSessionOnExistingWorktree(prompt);
					return;
				} else {
					update(s => ({ ...s, error: 'Please select an existing worktree or create a new one' }));
					return;
				}
			}

			if (!ws || ws.readyState !== WebSocket.OPEN) {
				// Attempt reconnection instead of just showing an error
				reconnectAttempts = 0;
				connect();
				const connected = await new Promise<boolean>((resolve) => {
					if (ws?.readyState === WebSocket.OPEN) { resolve(true); return; }
					const timeout = setTimeout(() => { clearInterval(check); resolve(false); }, 3000);
					const check = setInterval(() => {
						if (ws?.readyState === WebSocket.OPEN) { clearTimeout(timeout); clearInterval(check); resolve(true); }
					}, 100);
				});
				if (!connected) {
					update(s => ({ ...s, error: 'Unable to connect. Please check your connection and try again.' }));
					return;
				}
			}

			const userMsgId = `user-${Date.now()}`;
			const isFirstMessage = state.title === 'New Chat';
			const newTitle = isFirstMessage
				? prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '')
				: state.title;
			const queryProfileId = state.profile;

			// Interrupt and re-query if currently streaming
			if (state.isStreaming && state.sessionId) {
				// Cleanup first: stop poller and remove session to prevent
				// the sessionState subscriber from overwriting our update
				sessionPoller.stop(state.sessionId);
				sessionState.removeSession(state.sessionId);
				interruptPending = true;

				// Single atomic update: mark running tools as errored + append user message.
				// Uses s.messages (current store state) instead of stale sessionState data.
				update(s => {
					const updatedMessages = s.messages.map(m => {
						if (m.type === 'tool_use' && m.toolStatus === 'running') {
							return { ...m, streaming: false, toolStatus: 'error' as const };
						}
						return m.streaming ? { ...m, streaming: false } : m;
					});
					return {
						...s,
						messages: [...updatedMessages, {
							id: userMsgId,
							role: 'user' as const,
							content: prompt,
							profileId: queryProfileId,
						}],
						isStreaming: true,
						error: null,
						title: newTitle,
						currentQueryProfileId: queryProfileId,
					};
				});

				pendingTools.clear();
				lastMessageTime = Date.now();
				ws!.send(JSON.stringify({
					type: 'interrupt_and_query',
					prompt,
					session_id: state.sessionId,
					profile: state.profile,
					project: state.project || undefined,
					worktree_id: state.worktreeId || undefined,
				}));
				return;
			}

			// Normal path (not currently streaming)
			update(s => ({
				...s,
				messages: [...s.messages, {
					id: userMsgId,
					role: 'user' as const,
					content: prompt,
					profileId: queryProfileId,
				}],
				isStreaming: true,
				error: null,
				title: newTitle,
				currentQueryProfileId: queryProfileId,
			}));

			const overrides: Record<string, string> = {};
			if (state.modelOverride) overrides.model = state.modelOverride;
			if (state.permissionModeOverride) overrides.permission_mode = state.permissionModeOverride;
			if (state.thinkingModeOverride) overrides.thinking_mode = state.thinkingModeOverride;

			lastMessageTime = Date.now();
			ws!.send(JSON.stringify({
				type: 'query',
				prompt,
				session_id: state.sessionId,
				profile: state.profile,
				project: state.project || undefined,
				worktree_id: state.worktreeId || undefined,
				overrides: Object.keys(overrides).length > 0 ? overrides : undefined,
			}));
		},

		stopGeneration() {
			const state = get(store);
			if (ws?.readyState === WebSocket.OPEN && state.sessionId) {
				ws.send(JSON.stringify({ type: 'stop', session_id: state.sessionId }));
			}

			if (stopTimeout) clearTimeout(stopTimeout);
			stopTimeout = setTimeout(() => {
				const current = get(store);
				if (current.isStreaming) {
					console.warn('[ActiveSession] Stop confirmation timeout - forcing UI recovery');
					update(s => {
						let messages = s.messages.map(m => {
							if (m.type === 'tool_use' && m.toolStatus === 'running') return { ...m, streaming: false, toolStatus: 'error' as const };
							return m.streaming ? { ...m, streaming: false } : m;
						});
						messages = messages.filter(m => !(m.type === 'text' && m.role === 'assistant' && !m.content));
						return { ...s, messages, isStreaming: false };
					});
				}
				stopTimeout = null;
			}, 10000);
		},

		// ── Permission & Question Responses ─────────────────────────────

		sendPermissionResponse(requestId: string, decision: 'allow' | 'deny', remember?: 'none' | 'session' | 'profile', pattern?: string) {
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'permission_response', request_id: requestId, decision, remember: remember || 'none', pattern }));
			}
		},

		sendUserQuestionResponse(requestId: string, toolUseId: string, answers: Record<string, string | string[]>) {
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'user_question_response', request_id: requestId, tool_use_id: toolUseId, answers }));
			}
		},

		syncPendingPermissions() {
			if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'get_pending_permissions' }));
		},

		syncPendingQuestions() {
			if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'get_pending_questions' }));
		},

		// ── Profile & Project ───────────────────────────────────────────

		setProfile(profileId: string) {
			update(s => ({ ...s, profile: profileId }));
			const state = get(store);
			saveSelectionToBackend(profileId, state.project);
			persistSession();
		},

		setProject(projectId: string) {
			const state = get(store);
			if (state.executionMode === 'local' && state.messages.length > 0) {
				console.warn('[ActiveSession] Cannot change project after session has started');
				return;
			}
			update(s => ({ ...s, project: projectId }));
			saveSelectionToBackend(state.profile, projectId);
			persistSession();
		},

		// ── Session Overrides ───────────────────────────────────────────

		setModelOverride(model: string | null) { update(s => ({ ...s, modelOverride: model })); persistSession(); },
		setPermissionModeOverride(mode: string | null) { update(s => ({ ...s, permissionModeOverride: mode })); persistSession(); },
		setThinkingModeOverride(mode: string | null) { update(s => ({ ...s, thinkingModeOverride: mode })); persistSession(); },

		// ── Ephemeral UI State ──────────────────────────────────────────

		setDraft(text: string) { update(s => ({ ...s, draft: text })); },
		setScrollTop(pos: number) { update(s => ({ ...s, scrollTop: pos })); },
		clearError() { update(s => ({ ...s, error: null })); },
		clearWorktreeError() { update(s => ({ ...s, worktreeError: null })); },
		/** Clear all messages (e.g. before reloading after rewind). */
		clearMessages() { update(s => ({ ...s, messages: [], todos: [] })); },

		/** Reload the current session from backend (e.g. after rewind).
		 *  @param forceJsonl - Skip live_session_store and read directly from JSONL.
		 *                      Use after rewind to avoid stale in-memory data.
		 */
		reloadSession(forceJsonl = false) {
			const state = get(store);
			if (state.sessionId && ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({
					type: 'load_session',
					session_id: state.sessionId,
					...(forceJsonl && { force_jsonl: true }),
				}));
			}
		},

		// ── Execution Mode & Worktree ───────────────────────────────────

		setExecutionMode(mode: ExecutionMode) { update(s => ({ ...s, executionMode: mode })); },
		setWorktreeBranch(branch: string) { update(s => ({ ...s, worktreeBranch: branch })); },
		setWorktreeBaseBranch(branch: string) { update(s => ({ ...s, worktreeBaseBranch: branch })); },
		setWorktreeMode(mode: 'new' | 'existing') { update(s => ({ ...s, worktreeMode: mode })); },
		setWorktreeCreateNewBranch(val: boolean) { update(s => ({ ...s, worktreeCreateNewBranch: val })); },

		selectWorktreeBranch(branch: string, worktreeId: string) {
			update(s => ({ ...s, worktreeId, selectedWorktreeBranch: branch, worktreeMode: 'existing' as const }));
		},

		markWorktreeDeleted() {
			update(s => ({ ...s, worktreeDeleted: true, worktreeId: null }));
		},

		async createWorktree(prompt?: string) {
			const state = get(store);
			if (!state.profile) { update(s => ({ ...s, worktreeError: 'Please select a profile' })); return; }
			if (!state.project) { update(s => ({ ...s, worktreeError: 'Please select a project' })); return; }
			if (!state.worktreeBranch) { update(s => ({ ...s, worktreeError: 'Please select a branch' })); return; }

			update(s => ({ ...s, isCreatingWorktree: true, worktreeError: null }));

			try {
				const response = await api.post<{
					session_id: string; worktree_id: string; branch_name: string;
					base_branch: string; worktree_path: string; status: string;
				}>('/sessions/with-worktree', {
					project_id: state.project,
					branch_name: state.worktreeBranch,
					base_branch: state.worktreeBaseBranch || 'main',
					profile_id: state.profile,
					create_new_branch: state.worktreeCreateNewBranch,
				});

				update(s => ({
					...s,
					sessionId: response.session_id,
					worktreeId: response.worktree_id,
					isCreatingWorktree: false,
					messages: [...s.messages, {
						id: `system-${Date.now()}`,
						role: 'system' as const,
						content: `Branch: \`${response.branch_name}\` (based on \`${response.base_branch}\`)\n\nPath: \`${response.worktree_path}\``,
						type: 'system' as MessageType,
						systemSubtype: 'worktree_created',
						systemData: { branch_name: response.branch_name, base_branch: response.base_branch, worktree_path: response.worktree_path, worktree_id: response.worktree_id },
					}],
				}));

				// Reconnect WS to register the new session
				disconnect();
				connect();

				if (prompt) {
					setTimeout(() => storeApi.sendMessage(prompt, { skipWorktreeSetup: true }), 500);
				}
			} catch (error) {
				const err = error as { detail?: string; message?: string };
				const rawError = err.detail || err.message || 'Unknown error';
				update(s => ({
					...s,
					isCreatingWorktree: false,
					worktreeError: formatWorktreeError(rawError, state.worktreeBranch),
				}));
			}
		},

		// ── Queued Messages ─────────────────────────────────────────────

		queueMessage(content: string) {
			const id = `queued-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
			update(s => ({ ...s, queuedMessages: [...s.queuedMessages, { id, content, queuedAt: Date.now() }] }));
			persistSession();
		},

		removeQueuedMessage(id: string) {
			update(s => ({ ...s, queuedMessages: s.queuedMessages.filter(m => m.id !== id) }));
			persistSession();
		},

		clearQueuedMessages() {
			update(s => ({ ...s, queuedMessages: [] }));
			persistSession();
		},

		// ── System Messages & Error Handling ────────────────────────────

		addSystemMessage(content: string, subtype?: string) {
			update(s => ({
				...s,
				messages: [...s.messages, {
					id: `system-${Date.now()}`,
					role: 'system' as const,
					content,
					type: 'system' as MessageType,
					systemSubtype: subtype,
				}]
			}));
		},

		acknowledgeError() { update(s => ({ ...s, error: null })); },

		// ── Rewind ──────────────────────────────────────────────────────

		async rewindToMessage(_messageId: string) {
			const state = get(store);
			if (!state.sessionId) return;
			// After rewind completes externally (via MessageActions component),
			// reload the session to pick up the truncated message list
			if (ws?.readyState === WebSocket.OPEN) {
				expectedSessionId = state.sessionId;
				waitingForHistory = [];
				ws.send(JSON.stringify({ type: 'load_session', session_id: state.sessionId }));
			}
		},

		// ── Session Takeover ────────────────────────────────────────────

		takeOverSession() {
			const state = get(store);
			if (!state.sessionId) return;
			if (ws?.readyState === WebSocket.OPEN) {
				update(s => ({ ...s, error: null, sessionLocked: false, sessionLockedBy: undefined }));
				expectedSessionId = state.sessionId;
				waitingForHistory = [];
				ws.send(JSON.stringify({ type: 'load_session', session_id: state.sessionId, force: true }));
			}
		},

		// ── Getters for derived stores ──────────────────────────────────

		getDeviceId: () => typeof window !== 'undefined' ? getOrCreateDeviceId() : '',
	};

	// Internal helper for worktree existing branch sessions
	async function startSessionOnExistingWorktree(prompt: string) {
		const state = get(store);
		if (!state.profile) { update(s => ({ ...s, error: 'Please select a profile' })); return; }
		if (!state.project) { update(s => ({ ...s, error: 'Please select a project' })); return; }
		if (!state.worktreeId) { update(s => ({ ...s, error: 'Please select an existing worktree' })); return; }
		if (!ws || ws.readyState !== WebSocket.OPEN) {
			reconnectAttempts = 0;
			connect();
			const connected = await new Promise<boolean>((resolve) => {
				if (ws?.readyState === WebSocket.OPEN) { resolve(true); return; }
				const timeout = setTimeout(() => { clearInterval(check); resolve(false); }, 3000);
				const check = setInterval(() => {
					if (ws?.readyState === WebSocket.OPEN) { clearTimeout(timeout); clearInterval(check); resolve(true); }
				}, 100);
			});
			if (!connected) { update(s => ({ ...s, error: 'Unable to connect. Please check your connection and try again.' })); return; }
		}

		const branchDisplay = state.selectedWorktreeBranch || 'existing branch';
		const userMsgId = `user-${Date.now()}`;
		const newTitle = prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '');

		update(s => ({
			...s,
			messages: [
				...s.messages,
				{
					id: `system-${Date.now()}`,
					role: 'system' as const,
					content: `Using existing worktree on branch \`${branchDisplay}\``,
					type: 'system' as MessageType,
					systemSubtype: 'worktree_selected',
					systemData: { branch_name: branchDisplay, worktree_id: state.worktreeId },
				},
				{
					id: userMsgId,
					role: 'user' as const,
					content: prompt,
				}
			],
			isStreaming: true,
			error: null,
			title: newTitle,
		}));

		const overrides: Record<string, string> = {};
		if (state.modelOverride) overrides.model = state.modelOverride;
		if (state.permissionModeOverride) overrides.permission_mode = state.permissionModeOverride;
		if (state.thinkingModeOverride) overrides.thinking_mode = state.thinkingModeOverride;

		lastMessageTime = Date.now();
		ws!.send(JSON.stringify({
			type: 'query',
			prompt,
			profile: state.profile,
			project: state.project,
			worktree_id: state.worktreeId,
			overrides: Object.keys(overrides).length > 0 ? overrides : undefined,
		}));
	}
}

// The store reference is needed before the factory returns.
// We hoist the variable so the factory's methods can reference it via the returned object.
const storeApi = createActiveSessionStore();
export const activeSession = storeApi;

// ─── Derived Convenience Stores ──────────────────────────────────────────────

export const activeSessionId = derived(activeSession, $s => $s.sessionId);
export const activeMessages = derived(activeSession, $s => $s.messages);
export const activeIsStreaming = derived(activeSession, $s => $s.isStreaming);
export const activeWsConnected = derived(activeSession, $s => $s.wsConnected);
export const activePendingPermissions = derived(activeSession, $s => $s.pendingPermissions);
export const activePendingQuestions = derived(activeSession, $s => $s.pendingQuestions);
export const activeTodos = derived(activeSession, $s => $s.todos);
export const activeError = derived(activeSession, $s => $s.error);
