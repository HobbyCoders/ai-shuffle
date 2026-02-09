/**
 * Tabbed Chat Store - Supports multiple concurrent chat sessions
 *
 * Each tab has its own WebSocket connection and independent state.
 * Tabs can run queries simultaneously without interfering with each other.
 *
 * Types, persistence, and utilities are in ./tabs/ sub-modules.
 */

import { writable, derived, get } from 'svelte/store';
import type { Session, Profile, PermissionRequest as PermissionRequestType, UserQuestionRequest, PaginatedSessions, SessionSearchResult, PaginatedSessionSearchResults } from '$lib/api/client';
import { api, searchSessionsPaginated } from '$lib/api/client';
import { isNotificationsEnabled, notifySessionComplete, notifyWaitingForResponse, getPermission } from '$lib/services/notifications';
import { deck } from './deck';
import { getDeviceIdSync } from './device';
import {
	setActiveBlockType
} from './streaming-buffer';

// New polling-primary architecture (polling delegated to sessionPoller)
import { sessionPoller } from './session/session-poller';
import { sessionNotifier } from './session/session-notifier';
import { sessionState } from './session/session-state';

// Extracted modules (Phase 6)
import type {
	ApiUser, MessageType, SubagentChildMessage, TodoItem, ChatMessage,
	Project, ExecutionMode, ChatTab, TabsState, PersistedTab, PersistedTabsState
} from './tabs/types';
import {
	formatWorktreeError, getOrCreateDeviceId, generateTabId
} from './tabs/utils';
import { git } from './git';

// Re-export types for backwards compatibility
export type {
	ApiUser, MessageType, SubagentChildMessage, TodoItem, ChatMessage,
	Project, ExecutionMode, ChatTab
};

// Types are defined in ./tabs/types.ts and re-exported above.

// Track which sessions are currently streaming across all user devices (user-level)
// Updated by user_streaming_changed and user_streaming_sessions WebSocket messages
// Maps sessionId → projectId for workspace-level streaming awareness
export const streamingSessions = writable<Map<string, string | null>>(new Map());

// WebSocket connections per tab
const tabConnections: Map<string, WebSocket> = new Map();
const tabPingTimers: Map<string, ReturnType<typeof setInterval>> = new Map();
const tabReconnectTimers: Map<string, ReturnType<typeof setTimeout>> = new Map();
// Track tabs waiting for history response - queue sync events until history arrives
const tabsWaitingForHistory: Map<string, Array<Record<string, unknown>>> = new Map();
// Track expected session ID for each tab - prevents race conditions when switching sessions
const tabExpectedSessionId: Map<string, string> = new Map();
// Track stop confirmation timeouts - prevents UI freeze if 'stopped' message never arrives
const stopTimeouts: Map<string, ReturnType<typeof setTimeout>> = new Map();
// Track last received message timestamp for connection health monitoring
const tabLastMessageTime: Map<string, number> = new Map();
// Connection health check timeouts:
// - Normal: if no message for 90s (3 missed pings), force reconnect
// - Streaming: if no message for 15s during active streaming, force reconnect
//   (messages should arrive frequently during streaming)
// - Tool execution: if tool is pending, use longer timeout (tools can take a while)
const CONNECTION_HEALTH_TIMEOUT_MS = 90000;
const STREAMING_HEALTH_TIMEOUT_MS = 45000; // 45s - generous to survive SDK compaction pauses (heartbeats may be delayed during auto-compact)
const TOOL_EXECUTION_TIMEOUT_MS = 120000; // 2 minutes for tool execution (Bash, etc. can take a while)

// Track pending tool executions per tab - when tools are running, we expect longer gaps between messages
// This prevents false disconnects when Claude SDK is waiting for tool results
const tabPendingTools: Map<string, Set<string>> = new Map();

// Track sessions where onStreamEnd already ran (prevents redundant safety timeout load_session)
const tabStreamEndCleanedUp: Map<string, boolean> = new Map();

// Track tabs that were streaming when WebSocket disconnected.
// Used to prevent premature polling stop on reconnect — if load_session returns isStreaming: false
// during a brief race (e.g., SDK compaction gap), we keep polling for a grace period instead of
// immediately killing the poller.
const tabStreamingOnDisconnect: Map<string, string> = new Map(); // tabId → sessionId

// Track tabs that sent interrupt_and_query and are waiting for the new stream to start.
// Prevents the 'interrupted'/'stopped' handler from resetting isStreaming and scheduling
// cleanup that would destroy the incoming new stream's polling and session state.
const tabInterruptPending: Map<string, boolean> = new Map();

// NOTE: Old polling maps (tabPollingTimers, tabLastUpdateId) removed in Phase 4.
// Polling is now handled by sessionPoller (session/session-poller.ts).
// Types (PersistedTab, PersistedTabsState, etc.) moved to ./tabs/types.ts
// Persistence (saveTabsToServer, loadTabsFromServer, etc.) moved to ./tabs/tabs-persistence.ts
// Utilities (formatWorktreeError, generateTabId, etc.) moved to ./tabs/utils.ts

// formatWorktreeError, getOrCreateDeviceId, generateTabId → imported from ./tabs/utils

// Debounce timer for saving tabs
let saveTabsTimer: ReturnType<typeof setTimeout> | null = null;
const SAVE_DEBOUNCE_MS = 1000;

// Flag to prevent saving during initial load
let isInitializing = false;

// Track pending tabs state for flush on unload
let pendingTabsState: TabsState | null = null;

/**
 * Immediately flush any pending tabs save to backend (sync, for beforeunload)
 */
function flushPendingTabsSave() {
	if (!pendingTabsState || isInitializing) return;

	// Cancel the debounce timer
	if (saveTabsTimer) {
		clearTimeout(saveTabsTimer);
		saveTabsTimer = null;
	}

	// Build the persisted state
	const persistedState: PersistedTabsState = {
		tabs: pendingTabsState.tabs.map(tab => ({
			id: tab.id,
			title: tab.title,
			sessionId: tab.sessionId,
			profile: tab.profile,
			project: tab.project,
			modelOverride: tab.modelOverride,
			permissionModeOverride: tab.permissionModeOverride,
			thinkingModeOverride: tab.thinkingModeOverride
		})),
		activeTabId: pendingTabsState.activeTabId
	};

	// Use sendBeacon for reliable delivery during page unload
	const data = JSON.stringify({ key: 'open_tabs', value: persistedState });
	navigator.sendBeacon('/api/v1/preferences/open_tabs', new Blob([data], { type: 'application/json' }));

	pendingTabsState = null;
}

// Register beforeunload handler to flush pending saves when browser closes
if (typeof window !== 'undefined') {
	window.addEventListener('beforeunload', flushPendingTabsSave);
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'hidden') {
			flushPendingTabsSave();
		}
	});
}

/**
 * Broadcast tabs update via WebSocket for real-time cross-device sync
 */
function broadcastTabsUpdate(persistedState: PersistedTabsState) {
	// Find any open WebSocket connection to send through
	for (const [, ws] of tabConnections) {
		if (ws.readyState === WebSocket.OPEN) {
			ws.send(JSON.stringify({
				type: 'tabs_update',
				tabs_data: persistedState
			}));
			return; // Only need to send once, server will broadcast to all devices
		}
	}
}

/**
 * Save tabs state to backend (debounced)
 */
async function saveTabsToServer(state: TabsState) {
	// Don't save during initialization
	if (isInitializing) return;

	// Store pending state for flush on unload
	pendingTabsState = state;

	// Clear existing timer
	if (saveTabsTimer) {
		clearTimeout(saveTabsTimer);
	}

	// Debounce the save
	saveTabsTimer = setTimeout(async () => {
		if (!pendingTabsState) return;

		try {
			const persistedState: PersistedTabsState = {
				tabs: pendingTabsState.tabs.map(tab => ({
					id: tab.id,
					title: tab.title,
					sessionId: tab.sessionId,
					profile: tab.profile,
					project: tab.project,
					modelOverride: tab.modelOverride,
					permissionModeOverride: tab.permissionModeOverride,
					thinkingModeOverride: tab.thinkingModeOverride,
					// Worktree fields for restoring worktree mode on app resume
					executionMode: tab.executionMode,
					worktreeId: tab.worktreeId,
					worktreeBranch: tab.worktreeBranch,
					selectedWorktreeBranch: tab.selectedWorktreeBranch,
					worktreeMode: tab.worktreeMode,
					worktreeDeleted: tab.worktreeDeleted
				})),
				activeTabId: pendingTabsState.activeTabId
			};

			// Save to backend (REST API for persistence)
			await api.put('/preferences/open_tabs', {
				key: 'open_tabs',
				value: persistedState
			});
			// Also broadcast via WebSocket for real-time sync to other devices
			broadcastTabsUpdate(persistedState);
		} catch (error) {
			console.error('[Tabs] Failed to save tabs state:', error);
		}

		pendingTabsState = null;
		saveTabsTimer = null;
	}, SAVE_DEBOUNCE_MS);
}

/**
 * Load tabs state from backend
 */
async function loadTabsFromServer(): Promise<PersistedTabsState | null> {
	try {
		const response = await api.get<{ key: string; value: PersistedTabsState } | null>('/preferences/open_tabs');
		if (response && response.value) {
			return response.value;
		}
	} catch (error) {
		console.error('[Tabs] Failed to load tabs state:', error);
	}
	return null;
}

// Load persisted values - empty string means no selection (user must choose)
// These also check backend for cross-device persistence
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

// Load selection from backend (used during init to sync with backend)
async function loadSelectionFromBackend(): Promise<{ selectedProfile: string; selectedProject: string } | null> {
	if (typeof window === 'undefined') return null;

	try {
		const response = await fetch('/api/v1/preferences/selection', {
			credentials: 'include'
		});

		if (response.ok) {
			const data = await response.json();
			if (data && data.value) {
				// Update localStorage with backend values
				if (data.value.selectedProfile) {
					localStorage.setItem('aihub_selectedProfile', data.value.selectedProfile);
				}
				if (data.value.selectedProject) {
					localStorage.setItem('aihub_selectedProject', data.value.selectedProject);
				}
				return {
					selectedProfile: data.value.selectedProfile || '',
					selectedProject: data.value.selectedProject || ''
				};
			}
		}
	} catch (e) {
		console.error('[Tabs] Failed to load selection from backend:', e);
	}

	return null;
}

// Debounce timer for selection sync
let selectionSyncTimeout: ReturnType<typeof setTimeout> | null = null;
let pendingSelection: { profile: string; project: string } | null = null;
const SELECTION_SYNC_DEBOUNCE_MS = 500;

/**
 * Immediately flush any pending selection save to backend (sync, for beforeunload)
 */
function flushPendingSelectionSave() {
	if (!pendingSelection) return;

	// Cancel the debounce timer
	if (selectionSyncTimeout) {
		clearTimeout(selectionSyncTimeout);
		selectionSyncTimeout = null;
	}

	// Use sendBeacon for reliable delivery during page unload
	const data = JSON.stringify({
		key: 'selection',
		value: { selectedProfile: pendingSelection.profile, selectedProject: pendingSelection.project }
	});
	navigator.sendBeacon('/api/v1/preferences/selection', new Blob([data], { type: 'application/json' }));

	pendingSelection = null;
}

// Register beforeunload handler to flush pending saves when browser closes
if (typeof window !== 'undefined') {
	window.addEventListener('beforeunload', flushPendingSelectionSave);
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'hidden') {
			flushPendingSelectionSave();
		}
	});
}

/**
 * Save selection to backend (debounced) for cross-device persistence
 *
 * IMPORTANT: If profile or project is empty, we preserve the existing localStorage value
 * to avoid accidentally clearing the user's selection when only one field changes.
 */
async function saveSelectionToBackend(profile: string, project: string) {
	// If either value is empty, preserve the existing localStorage value
	// This prevents clearing one selection when only the other is changed
	const effectiveProfile = profile || getPersistedProfile();
	const effectiveProject = project || getPersistedProject();

	// Update localStorage immediately (always update both to keep in sync)
	if (typeof window !== 'undefined') {
		if (effectiveProfile) {
			localStorage.setItem('aihub_selectedProfile', effectiveProfile);
		}
		if (effectiveProject) {
			localStorage.setItem('aihub_selectedProject', effectiveProject);
		}
	}

	// Store pending selection for flush on unload (use effective values)
	pendingSelection = { profile: effectiveProfile, project: effectiveProject };

	// Clear existing timer
	if (selectionSyncTimeout) {
		clearTimeout(selectionSyncTimeout);
	}

	// Debounce the sync to avoid excessive API calls
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
			console.error('[Tabs] Failed to sync selection to backend:', e);
		}

		pendingSelection = null;
		selectionSyncTimeout = null;
	}, SELECTION_SYNC_DEBOUNCE_MS);
}

// generateTabId → imported from ./tabs/utils

function createTabsStore() {
	// Start with no tabs - will be populated from server or user creates one
	const { subscribe: _rawSubscribe, set, update } = writable<TabsState>({
		tabs: [],
		activeTabId: null,
		profiles: [],
		projects: [],
		sessions: [],
		adminSessions: [],
		apiUsers: [],
		adminSessionsFilter: null,
		// Initialize selection state
		selectedSessionIds: new Set<string>(),
		selectedAdminSessionIds: new Set<string>(),
		selectionMode: false,
		adminSelectionMode: false,
		// Initialize loading state
		sessionsLoading: false,
		// Initialize tag filter state
		sessionsTagFilter: null,
		adminSessionsTagFilter: null,
		// Pagination state
		sessionsTotal: 0,
		sessionsOffset: 0,
		sessionsHasMore: false,
		sessionsLoadingMore: false,
		// Search state
		searchQuery: '',
		searchResults: [],
		searchTotal: 0,
		searchOffset: 0,
		searchHasMore: false,
		searchLoading: false,
		searchLoadingMore: false
	});

	// ── Derived merge: overlay session data onto tabs at read-time ────────
	// Replaces the old bridge subscription (sessionState.subscribe → tabs.update)
	// which caused synchronous cascading updates and RangeError during autocompact.
	const _merged = derived(
		[{ subscribe: _rawSubscribe }, sessionState],
		([$raw, $session]) => {
			if ($session.sessions.size === 0) return $raw;

			let changed = false;
			const mergedTabs = $raw.tabs.map(tab => {
				if (!tab.sessionId) return tab;
				const s = $session.sessions.get(tab.sessionId);
				if (!s || s.messages.length === 0) return tab;

				// Skip re-creation if this tab's session hasn't changed.
				// Returning the same object reference prevents downstream
				// $derived consumers (ChatView, MessageArea) from re-rendering.
				if (tab._lastMergedUpdateId === s.lastUpdateId) return tab;

				changed = true;
				return {
					...tab,
					messages: s.messages,
					isStreaming: s.isStreaming,
					todos: s.todos.length > 0 ? s.todos : tab.todos,
					_lastMergedUpdateId: s.lastUpdateId,
				};
			});

			return changed ? { ...$raw, tabs: mergedTabs } : $raw;
		}
	);

	const subscribe = _merged.subscribe;

	/**
	 * Get WebSocket URL with device_id for multi-device sync
	 */
	function getWsUrl(): string {
		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		const host = window.location.host;
		const deviceId = getOrCreateDeviceId();
		return `${protocol}//${host}/api/v1/ws/chat?device_id=${encodeURIComponent(deviceId)}`;
	}

	/**
	 * Get auth token from cookie (may return null for httpOnly cookies)
	 */
	function getAuthToken(): string | null {
		const cookies = document.cookie.split(';');
		for (const cookie of cookies) {
			const [name, value] = cookie.trim().split('=');
			if (name === 'session') {
				return value;
			}
		}
		// Cookie might be httpOnly, return null and let server check cookie directly
		return null;
	}

	/**
	 * Update a specific tab's state
	 */
	function updateTab(tabId: string, updates: Partial<ChatTab>) {
		update(s => ({
			...s,
			tabs: s.tabs.map(tab =>
				tab.id === tabId ? { ...tab, ...updates } : tab
			)
		}));
	}

	/**
	 * Get tab by ID
	 */
	function getTab(tabId: string): ChatTab | undefined {
		const state = get({ subscribe });
		return state.tabs.find(t => t.id === tabId);
	}

	/**
	 * Check if WebSocket is healthy (connected and receiving messages recently)
	 * NOTE: isWebSocketHealthy() removed in Phase 4.
	 * The new architecture uses polling-primary via sessionPoller.
	 * WebSocket is notification-only - no need to check its health for polling decisions.
	 */

	// NOTE: processDiscreteEvent, applyBatchedUpdate, and initStreamingBuffer removed in Phase 7.
	// Streaming buffer is no longer used — all state updates come from the poller.

	/**
	 * Connect WebSocket for a specific tab
	 */
	function connectTab(tabId: string, isReconnect = false) {
		const existingWs = tabConnections.get(tabId);
		if (existingWs && (existingWs.readyState === WebSocket.OPEN || existingWs.readyState === WebSocket.CONNECTING)) {
			return;
		}

		// Try to get token from cookie (may be null if httpOnly)
		// Server will also check cookie directly for httpOnly cookies
		const token = getAuthToken();
		let url = getWsUrl(); // Already includes device_id
		if (token) {
			url = `${url}&token=${encodeURIComponent(token)}`;
		}
		const ws = new WebSocket(url);
		tabConnections.set(tabId, ws);

		ws.onopen = () => {
			updateTab(tabId, { wsConnected: true, error: null });

			// Always register with SyncEngine by sending load_session if tab has a session
			// This is critical for multi-device sync - devices must be registered to receive events
			const tab = getTab(tabId);
			if (tab?.sessionId) {
				// Track expected session ID to prevent race conditions when switching sessions
				tabExpectedSessionId.set(tabId, tab.sessionId);

				// Mark tab as waiting for history - queue sync events until history arrives
				// This prevents race conditions where sync events arrive before history
				tabsWaitingForHistory.set(tabId, []);

				// Use the WebSocket to register with SyncEngine and get latest messages
				// The backend will return isStreaming=true with buffer if still active,
				// or isStreaming=false with complete history if finished
				ws.send(JSON.stringify({
					type: 'load_session',
					session_id: tab.sessionId
				}));
			}

			// Start ping timer with connection health monitoring
			const existingPing = tabPingTimers.get(tabId);
			if (existingPing) clearInterval(existingPing);

			// Initialize last message time
			tabLastMessageTime.set(tabId, Date.now());

			const pingTimer = setInterval(() => {
				const currentWs = tabConnections.get(tabId);
				if (currentWs?.readyState === WebSocket.OPEN) {
					// Send pong to keep connection alive
					currentWs.send(JSON.stringify({ type: 'pong' }));

					// Check connection health - if no message received for too long, connection may be dead
					// Use appropriate timeout based on state:
					// - Tool execution pending: use long timeout (tools like Bash can take 2+ minutes)
					// - Streaming: use short timeout (text should flow frequently)
					// - Idle: use normal timeout
					const currentTab = getTab(tabId);
					const pendingTools = tabPendingTools.get(tabId);
					const hasToolsPending = pendingTools && pendingTools.size > 0;

					let healthTimeout: number;
					if (hasToolsPending) {
						// Tool is running - use long timeout (tools can take a while)
						healthTimeout = TOOL_EXECUTION_TIMEOUT_MS;
					} else if (currentTab?.isStreaming) {
						// Streaming text - should get frequent updates
						healthTimeout = STREAMING_HEALTH_TIMEOUT_MS;
					} else {
						// Not streaming - use normal timeout
						healthTimeout = CONNECTION_HEALTH_TIMEOUT_MS;
					}

					const lastMessageTime = tabLastMessageTime.get(tabId) || 0;
					const timeSinceLastMessage = Date.now() - lastMessageTime;
					if (timeSinceLastMessage > healthTimeout) {
						// During streaming, if the poller is successfully fetching data,
						// the WebSocket is just a notification channel — don't force-close it.
						// This prevents false disconnects during SDK operations like auto-compaction
						// where heartbeats may be delayed but the poller still works fine.
						if (currentTab?.isStreaming && currentTab?.sessionId && sessionPoller.isPolling(currentTab.sessionId)) {
						} else {
							console.warn(`[Tab ${tabId}] Connection appears stale (${Math.round(timeSinceLastMessage / 1000)}s since last message, streaming=${currentTab?.isStreaming}, toolsPending=${hasToolsPending ? pendingTools?.size : 0}), forcing reconnect`);
							// Force close to trigger reconnect
							currentWs.close(4000, 'Connection health timeout');
						}
					}
				}
			}, 10000); // Check every 10s instead of 25s for faster detection
			tabPingTimers.set(tabId, pingTimer);
		};

		ws.onclose = (event) => {
			// Check if tab was streaming when connection dropped
			const tab = getTab(tabId);
			const wasStreaming = tab?.isStreaming;
			const sessionId = tab?.sessionId;

			// Mark as disconnected but DON'T reset streaming state yet
			// The server might still be streaming - we'll poll to find out
			updateTab(tabId, { wsConnected: false });

			// If we were streaming, start polling immediately to keep getting updates
			// This provides continuity even when WebSocket is disconnected
			if (wasStreaming && sessionId) {
				// sessionPoller.startStreaming() is idempotent — safe to call even if already polling
				startPolling(tabId, sessionId);
				// Track that this tab was streaming on disconnect so the reconnect handler
				// doesn't prematurely stop polling if load_session returns isStreaming: false
				// (brief race during SDK compaction)
				tabStreamingOnDisconnect.set(tabId, sessionId);
			}

			// Clear any pending stop timeout
			const stopTimeout = stopTimeouts.get(tabId);
			if (stopTimeout) {
				clearTimeout(stopTimeout);
				stopTimeouts.delete(tabId);
			}

			const pingTimer = tabPingTimers.get(tabId);
			if (pingTimer) {
				clearInterval(pingTimer);
				tabPingTimers.delete(tabId);
			}

			// Reconnect after delay (unless intentionally closed, auth failed, or tab removed)
			if (event.code === 4001) {
				// Auth failure - don't retry, redirect to login
				console.warn(`[Tab ${tabId}] WebSocket auth failed (4001), redirecting to login`);
				import('$app/navigation').then(({ goto }) => goto('/login'));
			} else if (event.code !== 1000) {
				const existingReconnect = tabReconnectTimers.get(tabId);
				if (existingReconnect) clearTimeout(existingReconnect);

				const reconnectTimer = setTimeout(() => {
					// Check if tab still exists
					const currentTab = getTab(tabId);
					if (currentTab) {
						connectTab(tabId, true); // Pass isReconnect flag
					}
				}, 3000);
				tabReconnectTimers.set(tabId, reconnectTimer);
			}
		};

		ws.onerror = (error) => {
			console.error(`[Tab ${tabId}] WebSocket error:`, error);
		};

		ws.onmessage = (event) => {
			// Update last message time for connection health monitoring
			tabLastMessageTime.set(tabId, Date.now());

			try {
				const data = JSON.parse(event.data);
				handleTabMessage(tabId, data);
			} catch (e) {
				console.error(`[Tab ${tabId}] Failed to parse message:`, e);
			}
		};
	}

	/**
	 * Disconnect WebSocket for a specific tab
	 */
	function disconnectTab(tabId: string) {
		const reconnectTimer = tabReconnectTimers.get(tabId);
		if (reconnectTimer) {
			clearTimeout(reconnectTimer);
			tabReconnectTimers.delete(tabId);
		}

		const pingTimer = tabPingTimers.get(tabId);
		if (pingTimer) {
			clearInterval(pingTimer);
			tabPingTimers.delete(tabId);
		}

		// Stop polling if active
		stopPolling(tabId);

		// Clear any waiting for history state, expected session tracking, health monitoring, and pending tools
		tabsWaitingForHistory.delete(tabId);
		tabExpectedSessionId.delete(tabId);
		tabLastMessageTime.delete(tabId);
		tabPendingTools.delete(tabId);
		tabStreamingOnDisconnect.delete(tabId);
		tabInterruptPending.delete(tabId);

		const ws = tabConnections.get(tabId);
		if (ws) {
			ws.close(1000);
			tabConnections.delete(tabId);
		}

		updateTab(tabId, { wsConnected: false });
	}

	/**
	 * Start polling for a tab's session via the new sessionPoller.
	 * Thin wrapper that delegates to session/session-poller.ts.
	 */
	function startPolling(tabId: string, sessionId: string) {
		sessionPoller.startStreaming(sessionId);
	}

	/**
	 * Stop polling for a tab's session via the new sessionPoller.
	 * Thin wrapper that delegates to session/session-poller.ts.
	 */
	function stopPolling(tabId: string) {
		const tab = getTab(tabId);
		if (tab?.sessionId) {
			sessionPoller.stop(tab.sessionId);
		}
	}

	// Wire up stream-end callback for cleanup and JSONL refresh.
	// Note: Notifications are handled by the 'done' handler (which fires before onStreamEnd).
	// This callback focuses on: sessionState cleanup + authoritative JSONL refresh.
	sessionPoller.onStreamEnd((sessionId, usage) => {
		// If a tab with this session is in interrupt_and_query transition, skip cleanup.
		// The new stream is about to start and we don't want to interfere.
		const interruptTab = get({ subscribe }).tabs.find(t => t.sessionId === sessionId);
		if (interruptTab && tabInterruptPending.get(interruptTab.id)) {
			return;
		}

		// Refresh sessions list
		loadSessionsInternal();

		// Flush final messages AND usage from sessionState to the raw store BEFORE
		// removing the session. The derived merge store overlays sessionState onto
		// tabs, but once the session is removed, it falls back to the raw store.
		// Without this flush, the raw store still has pre-streaming messages (just
		// the user message) and assistant messages would disappear. Similarly,
		// context usage would revert to stale pre-streaming values.
		const finalSession = sessionState.getSession(sessionId);
		if (finalSession && finalSession.messages.length > 0) {
			const finalUsage = finalSession.usage;
			update(s => ({
				...s,
				tabs: s.tabs.map(tab => {
					if (tab.sessionId !== sessionId) return tab;
					return {
						...tab,
						messages: finalSession.messages,
						isStreaming: false,
						todos: finalSession.todos.length > 0 ? finalSession.todos : tab.todos,
						// Flush usage data so context indicator doesn't revert to stale values
						// after sessionState cleanup removes the real-time usage source
						...(finalUsage.contextUsed > 0 ? {
							totalTokensIn: finalUsage.inputTokens,
							totalTokensOut: finalUsage.outputTokens,
							totalCacheCreationTokens: finalUsage.cacheCreationTokens,
							totalCacheReadTokens: finalUsage.cacheReadTokens,
							contextUsed: finalUsage.contextUsed,
						} : {}),
					};
				})
			}));
		}

		// Clean up sessionState now that final messages are persisted in the raw store.
		// Delay slightly to let the reactive cycle complete.
		setTimeout(() => sessionState.removeSession(sessionId), 100);

		// Mark this session's stream-end as handled so the safety timeout
		// doesn't send a redundant load_session (which can race with JSONL writes)
		tabStreamEndCleanedUp.set(sessionId, true);
	});

	/**
	 * Handle tabs_sync message from another device
	 * This updates the local tabs to match the state from another device
	 */
	function handleTabsSyncMessage(data: Record<string, unknown>) {
		const tabs = data.tabs as Array<{
			id: string;
			title: string;
			sessionId: string | null;
			profile: string;
			project: string;
			modelOverride?: string | null;
			permissionModeOverride?: string | null;
			thinkingModeOverride?: string | null;
		}> | undefined;
		const activeTabId = data.activeTabId as string | undefined;
		const sourceDeviceId = data.source_device_id as string | undefined;

		// Get our device ID to ignore messages from ourselves (shouldn't happen, but safety check)
		const ourDeviceId = getDeviceIdSync();
		if (sourceDeviceId === ourDeviceId) {
			return;
		}

		if (!tabs) {
			console.warn('[Tabs] Received tabs_sync with no tabs data');
			return;
		}

		// Get current state to preserve messages and streaming state
		const currentState = get({ subscribe });
		const currentTabsMap = new Map(currentState.tabs.map(t => [t.id, t]));

		// Merge synced tabs with existing state
		// Keep messages and streaming state for tabs we already have
		const mergedTabs: ChatTab[] = tabs.map(syncedTab => {
			const existing = currentTabsMap.get(syncedTab.id);
			if (existing) {
				// Tab exists - update metadata but preserve messages and streaming state
				return {
					...existing,
					title: syncedTab.title,
					sessionId: syncedTab.sessionId,
					profile: syncedTab.profile || existing.profile,
					project: syncedTab.project || existing.project,
					modelOverride: syncedTab.modelOverride ?? existing.modelOverride,
					permissionModeOverride: syncedTab.permissionModeOverride ?? existing.permissionModeOverride,
					thinkingModeOverride: syncedTab.thinkingModeOverride ?? existing.thinkingModeOverride
				};
			} else {
				// New tab from another device - create with defaults
				return {
					id: syncedTab.id,
					title: syncedTab.title,
					sessionId: syncedTab.sessionId,
					messages: [],
					isStreaming: false,
					wsConnected: false,
					error: null,
					profile: syncedTab.profile || currentState.tabs[0]?.profile || '',
					sessionProfileId: null,  // Will be set when session is loaded
					currentQueryProfileId: null,  // Will be set when query starts
					project: syncedTab.project || currentState.tabs[0]?.project || '',
					totalTokensIn: 0,
					totalTokensOut: 0,
					totalCacheCreationTokens: 0,
					totalCacheReadTokens: 0,
					contextUsed: null,
					contextMax: 200000,
					modelOverride: syncedTab.modelOverride ?? null,
					permissionModeOverride: syncedTab.permissionModeOverride ?? null,
					thinkingModeOverride: syncedTab.thinkingModeOverride ?? null,
					pendingPermissions: [],
					pendingQuestions: [],
					todos: [],
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
					sessionLockedBy: undefined
				};
			}
		});

		// Find tabs that were closed on other device (exist locally but not in sync)
		const syncedTabIds = new Set(tabs.map(t => t.id));
		const closedTabs = currentState.tabs.filter(t => !syncedTabIds.has(t.id));

		// Disconnect WebSocket for closed tabs
		for (const closedTab of closedTabs) {
			disconnectTab(closedTab.id);
		}

		// Connect WebSocket for new tabs
		const newTabs = mergedTabs.filter(t => !currentTabsMap.has(t.id));
		for (const newTab of newTabs) {
			connectTab(newTab.id);
		}

		// Send load_session for existing tabs whose sessionId was just set
		// This handles the case where Device A creates a tab (sessionId: null initially),
		// then gets sessionId from session_created response, and sends a second tabs_sync.
		// Device B already has the tab connected but never loaded the session.
		for (const mergedTab of mergedTabs) {
			const existing = currentTabsMap.get(mergedTab.id);
			if (existing && !existing.sessionId && mergedTab.sessionId) {
				const ws = tabConnections.get(mergedTab.id);
				if (ws?.readyState === WebSocket.OPEN) {
					tabExpectedSessionId.set(mergedTab.id, mergedTab.sessionId);
					tabsWaitingForHistory.set(mergedTab.id, []);
					ws.send(JSON.stringify({ type: 'load_session', session_id: mergedTab.sessionId }));
				}
			}
		}

		// Update state
		update(s => ({
			...s,
			tabs: mergedTabs,
			activeTabId: activeTabId || s.activeTabId
		}));
	}

	/**
	 * Handle sync events from other devices (via SyncEngine)
	 */
	function handleSyncEvent(tabId: string, data: Record<string, unknown>) {
		const eventType = data.event_type as string;
		const sessionId = data.session_id as string;
		const eventData = data.data as Record<string, unknown>;

		// Only process events for tabs that have this session loaded
		const tab = getTab(tabId);
		if (!tab || tab.sessionId !== sessionId) {
			return;
		}

		// If tab is waiting for history, queue the event to process after history loads
		// This prevents race conditions where sync events arrive before history
		const queuedEvents = tabsWaitingForHistory.get(tabId);
		if (queuedEvents !== undefined) {
			queuedEvents.push(data);
			return;
		}

		switch (eventType) {
			case 'message_added': {
				// A new message was added by another device (e.g., user message)
				const message = eventData.message as Record<string, unknown>;
				update(s => ({
					...s,
					tabs: s.tabs.map(t => {
						if (t.id !== tabId) return t;
						return {
							...t,
							messages: [...t.messages, {
								id: `msg-sync-${Date.now()}`,
								role: message.role as 'user' | 'assistant',
								content: message.content as string,
								type: message.role === 'user' ? undefined : 'text' as const,
								streaming: false
							}]
						};
					})
				}));
				break;
			}

			case 'stream_start': {
				// Another device started streaming
				// DON'T create empty message here - wait for actual content via stream_chunk events
				// This prevents empty "Claude [time] ●●●" headers appearing before content arrives
				const usage = eventData.usage as Record<string, number> | undefined;

				update(s => ({
					...s,
					tabs: s.tabs.map(t => {
						if (t.id !== tabId) return t;

						// Extract input token counts from usage data (synced from message_start)
						// These represent the FULL context sent to Claude for this turn
						// NOTE: REPLACE values, not add - SDK sends total context
						const inputTokens = usage?.input_tokens || 0;
						const cacheCreationTokens = usage?.cache_creation_input_tokens || 0;
						const cacheReadTokens = usage?.cache_read_input_tokens || 0;

						// Only update context if we have usage data
						const contextUpdate = usage ? {
							totalTokensIn: inputTokens,  // REPLACE, not add
							totalCacheCreationTokens: cacheCreationTokens,
							totalCacheReadTokens: cacheReadTokens,
							contextUsed: inputTokens + cacheCreationTokens + cacheReadTokens
						} : {};

						// Just mark as streaming - messages will be created when content arrives
						return {
							...t,
							isStreaming: true,
							...contextUpdate
						};
					})
				}));
				break;
			}

			case 'stream_chunk': {
				// If poller is active, let it handle message updates via server-authoritative state.
				// This prevents dual update paths from creating inconsistencies.
				if (sessionPoller.isPolling(sessionId)) {
					sessionPoller.pollNow(sessionId);
					break;
				}

				// Content chunk from another device (fallback when polling isn't active)
				const chunkType = eventData.chunk_type as string;
				const content = eventData.content as string;
				if (chunkType === 'thinking') {
					// Thinking chunk - append to existing thinking message or create new one
					// This keeps thinking separate from text messages
					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = [...t.messages];
							const thinkingIdx = messages.findLastIndex(
								m => m.type === 'thinking' && m.role === 'assistant' && m.streaming
							);

							if (thinkingIdx !== -1) {
								messages[thinkingIdx] = {
									...messages[thinkingIdx],
									content: messages[thinkingIdx].content + (content || '')
								};
							} else {
								messages.push({
									id: `thinking-sync-${Date.now()}`,
									role: 'assistant' as const,
									content: content || '',
									type: 'thinking' as const,
									streaming: true
								});
							}

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'text') {
					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = [...t.messages];
							const streamingIdx = messages.findLastIndex(
								m => m.type === 'text' && m.role === 'assistant' && m.streaming
							);

							if (streamingIdx !== -1) {
								messages[streamingIdx] = {
									...messages[streamingIdx],
									content: messages[streamingIdx].content + (content || '')
								};
							} else {
								messages.push({
									id: `text-sync-${Date.now()}`,
									role: 'assistant' as const,
									content: content || '',
									type: 'text' as const,
									streaming: true
								});
							}

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'tool_input') {
					// Tool input streaming - accumulate JSON in the current tool_use message
					const partialJson = content;
					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = [...t.messages];
							let todos = t.todos;
							const toolIdx = messages.findLastIndex(
								m => m.type === 'tool_use' && m.streaming
							);

							if (toolIdx !== -1) {
								const current = messages[toolIdx];
								const partialInput = (current.partialToolInput || '') + partialJson;

								// Try to parse accumulated JSON
								let parsedInput = current.toolInput || {};
								try {
									parsedInput = JSON.parse(partialInput);

									// Handle TodoWrite tool - update todos as they stream in
									if (current.toolName === 'TodoWrite' && parsedInput?.todos && Array.isArray(parsedInput.todos)) {
										todos = (parsedInput.todos as TodoItem[]).map(todo => ({
											content: todo.content || '',
											status: todo.status || 'pending',
											activeForm: todo.activeForm || todo.content || ''
										}));
									}
								} catch {
									// Not valid JSON yet, keep accumulating
								}

								messages[toolIdx] = {
									...current,
									partialToolInput: partialInput,
									toolInput: parsedInput
								};
							}

							return { ...t, messages, todos };
						})
					}));
				} else if (chunkType === 'tool_use') {
					// Tool use event - this is the ONLY place we create tool messages for sync
					// (stream_block_start is ignored for tool_use to avoid duplicates)
					const toolId = eventData.tool_id as string;
					const toolName = eventData.tool_name as string;
					const toolInput = eventData.tool_input as Record<string, unknown>;
					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							let messages = [...t.messages];
							let todos = t.todos;

							// Handle TodoWrite tool - extract todos from input
							if (toolName === 'TodoWrite' && toolInput?.todos && Array.isArray(toolInput.todos)) {
								todos = (toolInput.todos as TodoItem[]).map(todo => ({
									content: todo.content || '',
									status: todo.status || 'pending',
									activeForm: todo.activeForm || todo.content || ''
								}));
							}

							// Check if this tool_use already exists (from history or buffer)
							const existingToolIdx = messages.findIndex(
								m => m.type === 'tool_use' && m.toolId === toolId
							);
							if (existingToolIdx !== -1) {
								// Tool already exists - just update it if needed
								messages[existingToolIdx] = {
									...messages[existingToolIdx],
									toolName: toolName || messages[existingToolIdx].toolName,
									toolInput: toolInput || messages[existingToolIdx].toolInput,
									toolStatus: messages[existingToolIdx].toolStatus || 'running',
									streaming: messages[existingToolIdx].toolStatus !== 'complete'
								};
								return { ...t, messages, todos };
							}

							// Handle current streaming text message - finalize it
							const streamingIdx = messages.findLastIndex(
								m => m.type === 'text' && m.role === 'assistant' && m.streaming
							);
							if (streamingIdx !== -1) {
								if (messages[streamingIdx].content) {
									messages[streamingIdx] = { ...messages[streamingIdx], streaming: false };
								} else {
									messages = messages.filter((_, i) => i !== streamingIdx);
								}
							}

							// Create the tool use message with profileId for per-query tracking
							messages.push({
								id: `tool-sync-${Date.now()}-${toolId || ''}`,
								role: 'assistant' as const,
								content: '',
								type: 'tool_use' as const,
								toolName: toolName,
								toolId: toolId,
								toolInput: toolInput,
								toolStatus: 'running' as const,
								streaming: true,
								profileId: t.currentQueryProfileId || undefined
							});

							return { ...t, messages, todos };
						})
					}));
				} else if (chunkType === 'tool_result') {
					const toolId = eventData.tool_id as string;
					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = [...t.messages];

							// Find the matching tool_use - first try exact match by toolId
							let toolUseIdx = messages.findIndex(
								m => m.type === 'tool_use' && m.toolId === toolId
							);

							// Fallback: find last tool_use without a result
							if (toolUseIdx === -1) {
								toolUseIdx = messages.findLastIndex(
									m => m.type === 'tool_use' && !m.toolResult && (m.streaming || m.toolStatus === 'running')
								);
							}

							if (toolUseIdx !== -1) {
								// Only update if tool doesn't already have this result
								if (!messages[toolUseIdx].toolResult) {
									messages[toolUseIdx] = {
										...messages[toolUseIdx],
										toolResult: content,
										toolStatus: 'complete' as const,
										streaming: false
									};
								} else {
								}
							}

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'system') {
					// System message from another device
					const subtype = eventData.subtype as string;
					const systemData = eventData.data as Record<string, unknown>;
					// Extract content for display
					let displayContent = '';
					if (systemData?.content && typeof systemData.content === 'string') {
						displayContent = systemData.content;
					} else if (typeof systemData === 'object') {
						displayContent = JSON.stringify(systemData, null, 2);
					}

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							// Mark any streaming text message as complete
							let messages = t.messages.map(m =>
								m.streaming && m.type === 'text' ? { ...m, streaming: false } : m
							);
							// Remove empty streaming text messages
							messages = messages.filter(
								m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
							);

							// Add the system message
							messages.push({
								id: `system-sync-${Date.now()}`,
								role: 'system' as const,
								content: displayContent,
								type: 'system' as const,
								systemSubtype: subtype,
								systemData: systemData,
								streaming: false
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'subagent_start') {
					// Subagent task started on another device
					const agentId = eventData.agent_id as string;
					const agentType = eventData.agent_type as string;
					const description = eventData.description as string;
					const prompt = eventData.prompt as string;
					const toolId = eventData.tool_id as string;
					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							let messages = [...t.messages];

							// Mark any streaming text message as complete
							const streamingIdx = messages.findLastIndex(
								m => m.type === 'text' && m.role === 'assistant' && m.streaming
							);
							if (streamingIdx !== -1) {
								if (messages[streamingIdx].content) {
									messages[streamingIdx] = { ...messages[streamingIdx], streaming: false };
								} else {
									messages = messages.filter((_, i) => i !== streamingIdx);
								}
							}

							// Add subagent message group
							messages.push({
								id: `subagent-sync-${agentId}`,
								role: 'assistant' as const,
								content: '',
								type: 'subagent' as const,
								toolId: toolId,
								agentId: agentId,
								agentType: agentType,
								agentDescription: description,
								agentPrompt: prompt,
								agentStatus: 'running' as const,
								agentChildren: [],
								streaming: true
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'subagent_chunk') {
					// Text chunk from subagent on another device
					const agentId = eventData.agent_id as string;
					const content = eventData.content as string;

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = t.messages.map(m => {
								if (m.type === 'subagent' && m.agentId === agentId) {
									const children = [...(m.agentChildren || [])];
									const lastChild = children[children.length - 1];

									if (lastChild && lastChild.type === 'text') {
										children[children.length - 1] = {
											...lastChild,
											content: lastChild.content + content
										};
									} else {
										children.push({
											id: `${agentId}-text-sync-${Date.now()}`,
											type: 'text',
											content: content
										});
									}
									return { ...m, agentChildren: children };
								}
								return m;
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'subagent_tool_use') {
					// Tool use within subagent on another device
					const agentId = eventData.agent_id as string;
					const toolName = eventData.name as string;
					const toolId = eventData.id as string;
					const toolInput = eventData.input as Record<string, unknown>;

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = t.messages.map(m => {
								if (m.type === 'subagent' && m.agentId === agentId) {
									const children = [...(m.agentChildren || [])];
									children.push({
										id: `${agentId}-tool-sync-${toolId}`,
										type: 'tool_use',
										content: '',
										toolName: toolName,
										toolId: toolId,
										toolInput: toolInput,
										toolStatus: 'running'
									});
									return { ...m, agentChildren: children };
								}
								return m;
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'subagent_tool_result') {
					// Tool result within subagent on another device
					const agentId = eventData.agent_id as string;
					const toolUseId = eventData.tool_use_id as string;
					const output = eventData.output as string;

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = t.messages.map(m => {
								if (m.type === 'subagent' && m.agentId === agentId) {
									const children = [...(m.agentChildren || [])];
									// Find matching tool_use and embed result
									for (let i = children.length - 1; i >= 0; i--) {
										if (children[i].type === 'tool_use' && children[i].toolId === toolUseId) {
											children[i] = {
												...children[i],
												toolResult: output,
												toolStatus: 'complete'
											};
											break;
										}
									}
									return { ...m, agentChildren: children };
								}
								return m;
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'subagent_done') {
					// Subagent completed on another device
					const agentId = eventData.agent_id as string;
					const result = eventData.result as string | undefined;
					const isError = eventData.is_error as boolean | undefined;
					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							const messages = t.messages.map(m => {
								if (m.type === 'subagent' && m.agentId === agentId) {
									return {
										...m,
										content: result || '',
										agentStatus: isError ? 'error' as const : 'completed' as const,
										streaming: false
									};
								}
								return m;
							});

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'stream_block_start') {
					// Start of a content block from another device
					// For tool_use blocks, we DO NOT create messages here - we let the 'tool_use' event
					// create the message to avoid race conditions and duplicates.
					// The 'tool_use' event has the full data (name, id, input) anyway.
					const blockType = eventData.block_type as string;
					const contentBlock = eventData.content_block as { id?: string; name?: string };
					// No action needed - tool_use event will create the message
				} else if (chunkType === 'stream_block_stop') {
					// End of a content block - just log for now
				} else if (chunkType === 'stream_message_delta') {
					// Final message metadata with output token usage (synced from another device)
					// NOTE: These are TOTAL values for the current turn, not increments!
					const usage = eventData.usage as Record<string, number> | undefined;
					if (usage && usage.output_tokens) {
						update(s => ({
							...s,
							tabs: s.tabs.map(t => {
								if (t.id !== tabId) return t;

								// REPLACE output token count - SDK sends total, not delta
								const outputTokens = usage.output_tokens || 0;

								return {
									...t,
									totalTokensOut: outputTokens
								};
							})
						}));
					}
				}
				break;
			}

			case 'usage_update': {
				// Usage update during streaming (agentic loops with multiple turns)
				// Each new assistant turn has updated token usage
				// NOTE: These are TOTAL values for the current turn, not increments!
				const usage = eventData.usage as Record<string, number> | undefined;
				if (usage) {
					const inputTokens = usage.input_tokens || 0;
					const cacheCreationTokens = usage.cache_creation_input_tokens || 0;
					const cacheReadTokens = usage.cache_read_input_tokens || 0;
					const newContextUsed = inputTokens + cacheCreationTokens + cacheReadTokens;

					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							// REPLACE values, not add - SDK sends total context for current turn
							return {
								...t,
								totalTokensIn: inputTokens,
								totalCacheCreationTokens: cacheCreationTokens,
								totalCacheReadTokens: cacheReadTokens,
								contextUsed: newContextUsed
							};
						})
					}));
				}
				break;
			}

			case 'stream_end': {
				// Another device finished streaming
				const metadata = eventData.metadata as Record<string, unknown>;
				const interrupted = eventData.interrupted as boolean;

				// Extract token counts from final metadata for reconciliation
				// This ensures context is updated even if stream_start usage was missed
				const tokensIn = metadata?.tokens_in as number | undefined;
				const tokensOut = metadata?.tokens_out as number | undefined;
				const cacheCreation = metadata?.cache_creation_tokens as number | undefined;
				const cacheRead = metadata?.cache_read_tokens as number | undefined;

				update(s => ({
					...s,
					tabs: s.tabs.map(t => {
						if (t.id !== tabId) return t;

						// Mark all streaming messages as complete
						let messages = t.messages.map(m =>
							m.streaming ? { ...m, streaming: false } : m
						);

						// Remove empty text messages
						messages = messages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						// Add metadata to last assistant message
						if (messages.length > 0) {
							const lastIdx = messages.findLastIndex(m => m.role === 'assistant');
							if (lastIdx !== -1) {
								messages[lastIdx] = {
									...messages[lastIdx],
									metadata,
									content: interrupted
										? messages[lastIdx].content + '\n\n[Stopped]'
										: messages[lastIdx].content
								};
							}
						}

						// Reconcile token counts from final metadata
						// Only update if we have the data and contextUsed is not already set
						// This acts as a fallback if stream_start usage was missed
						let tokenUpdates: Partial<typeof t> = {};
						if (tokensIn !== undefined && cacheCreation !== undefined && cacheRead !== undefined) {
							const newContextUsed = tokensIn + cacheCreation + cacheRead;
							// Only update if contextUsed seems stale (more than 10% different)
							// This prevents overwriting values from stream_start with potentially stale final metadata
							const currentContext = t.contextUsed || 0;
							const percentDiff = currentContext > 0 ? Math.abs(newContextUsed - currentContext) / currentContext : 1;

							if (percentDiff > 0.1 || currentContext === 0) {
								tokenUpdates = {
									contextUsed: newContextUsed,
									totalCacheCreationTokens: cacheCreation,
									totalCacheReadTokens: cacheRead
								};
							}
						}

						return { ...t, messages, isStreaming: false, ...tokenUpdates };
					})
				}));

				// Refresh sessions list
				loadSessionsInternal();
				break;
			}

			case 'session_rewound': {
				// Another device rewound the session - reload to get updated messages
				const targetUuid = eventData.target_uuid as string;
				const messagesRemoved = eventData.messages_removed as number;
				// Reload the session to get the updated message list
				const tab = getTab(tabId);
				if (tab?.sessionId) {
					const ws = tabConnections.get(tabId);
					if (ws && ws.readyState === WebSocket.OPEN) {
						ws.send(JSON.stringify({
							type: 'load_session',
							session_id: tab.sessionId
						}));
					}
				}
				break;
			}

			case 'session_opened': {
				// Another device opened/resumed this session
				const deviceId = eventData.device_id as string;
				const isNew = eventData.is_new as boolean;
				// Could show a notification or indicator
				break;
			}

			case 'session_closed': {
				// Another device closed this session
				const deviceId = eventData.device_id as string;
				// Could show a notification or indicator
				break;
			}
		}
	}

	/**
	 * Handle incoming WebSocket message for a tab
	 */
	function handleTabMessage(tabId: string, data: Record<string, unknown>) {
		const msgType = data.type as string;
		const eventType = data.event_type as string;

		// ── Phase 2: Forward to new notification system (parallel) ────────
		// The notifier triggers polls in the new session-poller.
		// Both old and new systems run simultaneously during transition.
		const tab = getTab(tabId);
		if (tab?.sessionId) {
			// During interrupt_and_query transitions, skip notifier for interrupted/stopped events.
			// We already stopped the poller and removed session state in sendMessage() — a stale
			// pollNow() here would recreate session state with is_streaming: false, causing the
			// derived merge store to briefly show the tab as not-streaming (UI flicker).
			const isInterruptTransition = tabInterruptPending.get(tabId) &&
				(msgType === 'interrupted' || msgType === 'stopped');
			if (!isInterruptTransition) {
				sessionNotifier.handleMessage(tab.sessionId, data);
			}
		}

		// Handle sync events from other devices
		if (eventType) {
			handleSyncEvent(tabId, data);
			return;
		}

		// Handle tabs_sync message from another device
		// This is NOT tab-specific, it updates all tabs for the user
		if (msgType === 'tabs_sync') {
			handleTabsSyncMessage(data);
			return;
		}

		// Handle user-level session list update from another device
		if (msgType === 'user_session_created') {
			loadSessionsInternal();
			return;
		}

		// Handle user-level streaming status change from another device
		if (msgType === 'user_streaming_changed') {
			const sessionId = data.session_id as string;
			const isStreaming = data.is_streaming as boolean;
			const projectId = (data.project_id as string | null) ?? null;
			streamingSessions.update(map => {
				const newMap = new Map(map);
				if (isStreaming) {
					newMap.set(sessionId, projectId);
				} else {
					newMap.delete(sessionId);
				}
				return newMap;
			});
			return;
		}

		// Handle initial streaming sessions snapshot on connection
		if (msgType === 'user_streaming_sessions') {
			const sessions = data.sessions as Array<{ session_id: string; project_id: string | null }> | undefined;
			const sessionIds = data.session_ids as string[] | undefined;
			const newMap = new Map<string, string | null>();
			if (sessions) {
				// New format with project_id
				for (const s of sessions) {
					newMap.set(s.session_id, s.project_id ?? null);
				}
			} else if (sessionIds) {
				// Backward compat: old format without project_id
				for (const sid of sessionIds) {
					newMap.set(sid, null);
				}
			}
			streamingSessions.set(newMap);
			return;
		}

		// Handle branch change from another device — refresh git state
		if (msgType === 'user_branch_changed') {
			const projectId = data.project_id as string;
			// Refresh git state if we're currently viewing this project
			const gitState = get(git);
			if (gitState.projectId === projectId) {
				git.refreshStatus();
				git.refreshDiffStats();
			}
			return;
		}

		// Handle git state changes pushed by backend file watcher
		if (msgType === 'git_state_changed') {
			const projectId = data.project_id as string;
			const worktreeId = (data.worktree_id as string) || null;
			const changes = data.changes as Record<string, unknown>;
			if (projectId && changes) {
				git.applyServerUpdate(projectId, worktreeId, changes);
			}
			return;
		}

		switch (msgType) {
			case 'history': {
				// Validate session_id to prevent race conditions when switching sessions
				const receivedSessionId = data.session_id as string;
				const expectedSessionId = tabExpectedSessionId.get(tabId);
				if (expectedSessionId && receivedSessionId !== expectedSessionId) {
					// Clear the waiting state for this stale response
					tabsWaitingForHistory.delete(tabId);
					break;
				}
				// Clear expected session ID now that we received it
				tabExpectedSessionId.delete(tabId);

				// Handle both JSONL format (with explicit type) and legacy DB format
				const rawMessages = (data.messages as Array<Record<string, unknown>>) || [];

				// First pass: convert raw messages to ChatMessage format
				// Also build a map of tool_results by tool_use_id for grouping
				const toolResultMap: Map<string, { content: string; toolStatus: 'complete' | 'error' }> = new Map();

				// Track latest todos from TodoWrite tool calls in history
				let latestTodos: TodoItem[] = [];

				// Pre-scan for tool_results to group them with tool_use
				// Also extract todos from TodoWrite tool calls
				for (const m of rawMessages) {
					const msgType = m.type as MessageType | undefined;
					const role = m.role as string;
					const toolName = (m.toolName || m.tool_name) as string | undefined;
					const toolInput = (m.toolInput || m.tool_input) as Record<string, unknown> | undefined;

					// Extract todos from TodoWrite tool calls
					if ((msgType === 'tool_use' || role === 'tool_use') && toolName === 'TodoWrite' && toolInput?.todos && Array.isArray(toolInput.todos)) {
						latestTodos = (toolInput.todos as TodoItem[]).map(todo => ({
							content: todo.content || '',
							status: todo.status || 'pending',
							activeForm: todo.activeForm || todo.content || ''
						}));
					}

					if (msgType === 'tool_result' || role === 'tool_result') {
						const toolUseId = m.toolId || m.tool_use_id;
						if (toolUseId) {
							toolResultMap.set(toolUseId as string, {
								content: m.content as string || '',
								toolStatus: 'complete'
							});
						}
					}
				}

				// Extract streaming state early so message conversion can preserve streaming flags
				const isStreaming = data.isStreaming as boolean || false;

				const messages: ChatMessage[] = [];
				const seenToolResults = new Set<string>(); // Track which tool_results we've merged

				for (let i = 0; i < rawMessages.length; i++) {
					const m = rawMessages[i];
					// Determine message type - JSONL messages have explicit 'type' field
					let msgType: MessageType | undefined = m.type as MessageType | undefined;

					// Legacy DB format: infer type from role
					if (!msgType && m.role === 'assistant') {
						msgType = 'text';
					} else if (!msgType && (m.role === 'tool_use' || m.role === 'tool_result')) {
						msgType = m.role as MessageType;
					}

					// Handle system messages (e.g., /context, /compact output)
					if (msgType === 'system' || m.role === 'system') {
						messages.push({
							id: String(m.id || `msg-${i}`),
							jsonlUuid: m.uuid as string | undefined,  // Raw UUID from JSONL for rewind
							role: 'system' as const,
							content: m.content as string,
							type: 'system' as const,
							systemSubtype: m.subtype as string | undefined,
							systemData: (m.systemData as Record<string, unknown>) || { content: m.content },
							metadata: m.metadata as Record<string, unknown>,
							streaming: false
						});
						continue;
					}

					// Skip standalone tool_result messages - they should be grouped with tool_use
					if (msgType === 'tool_result') {
						const toolUseId = m.toolId || m.tool_use_id;
						if (toolUseId) {
							seenToolResults.add(toolUseId as string);
						}
						// Skip - will be merged into tool_use message
						continue;
					}

					// Build the chat message with all fields
					const chatMessage: ChatMessage = {
						id: String(m.id || `msg-${i}`),
						jsonlUuid: m.uuid as string | undefined,  // Raw UUID from JSONL for rewind
						role: (m.role === 'tool_use' || m.role === 'tool_result' ? 'assistant' : m.role) as 'user' | 'assistant',
						content: m.content as string,
						type: msgType,
						toolName: (m.toolName || m.tool_name) as string | undefined,
						toolId: m.toolId as string | undefined,
						toolInput: (m.toolInput || m.tool_input) as Record<string, unknown> | undefined,
						toolResult: m.toolResult as string | undefined,
						toolStatus: m.toolStatus as 'running' | 'complete' | 'error' | undefined,
						metadata: m.metadata as Record<string, unknown>,
						// Preserve streaming flag from live_session_store during active streaming
						streaming: isStreaming ? ((m.streaming as boolean) ?? false) : false
					};

					// For tool_use messages, try to merge the corresponding tool_result
					if (msgType === 'tool_use' && chatMessage.toolId) {
						const result = toolResultMap.get(chatMessage.toolId);
						if (result && !chatMessage.toolResult) {
							chatMessage.toolResult = result.content;
							chatMessage.toolStatus = result.toolStatus;
							seenToolResults.add(chatMessage.toolId);
						}
						// If no result yet, mark as complete anyway (history = finished operations)
						if (!chatMessage.toolStatus) {
							chatMessage.toolStatus = 'complete';
						}
					}

					// Add subagent-specific fields if present
					if (msgType === 'subagent') {
						chatMessage.agentId = m.agentId as string | undefined;
						chatMessage.agentType = m.agentType as string | undefined;
						chatMessage.agentDescription = m.agentDescription as string | undefined;
						chatMessage.agentStatus = m.agentStatus as 'pending' | 'running' | 'completed' | 'error' | undefined;
						chatMessage.agentChildren = m.agentChildren as SubagentChildMessage[] | undefined;
					}

					messages.push(chatMessage);
				}

				// Handle streaming state from backend (isStreaming already extracted above for message conversion)
				const streamingBuffer = data.streamingBuffer as Array<Record<string, unknown>> | undefined;
				// If session is actively streaming and has buffered content, merge it intelligently
				let finalMessages = messages;
				if (isStreaming && streamingBuffer && streamingBuffer.length > 0) {
					// Create a set of existing message content hashes for deduplication
					const existingContentHashes = new Set<string>();
					for (const msg of messages) {
						// Create a simple hash based on content and type
						const hash = `${msg.type || 'text'}:${msg.toolId || ''}:${(msg.content || '').substring(0, 100)}`;
						existingContentHashes.add(hash);
					}

					// First pass: build a map of tool_results in buffer for grouping (legacy support)
					const bufferToolResults: Map<string, { content: string; status: 'complete' | 'error' }> = new Map();
					for (const m of streamingBuffer) {
						const bufferType = (m.type || m.chunk_type || 'text') as string;
						if (bufferType === 'tool_result') {
							const toolId = (m.tool_id || '') as string;
							if (toolId) {
								bufferToolResults.set(toolId, {
									content: (m.content || '') as string,
									status: 'complete'
								});
							}
						}
					}

					// Process buffer messages and only add non-duplicates
					for (const m of streamingBuffer) {
						const bufferType = (m.type || m.chunk_type || 'text') as MessageType;
						const bufferToolId = (m.tool_id || '') as string;
						const bufferContent = (m.content || '') as string;
						const hash = `${bufferType}:${bufferToolId}:${bufferContent.substring(0, 100)}`;

						// Skip standalone tool_result messages - they should be grouped with tool_use
						if (bufferType === 'tool_result') {
							continue;
						}

						// Skip if we already have similar content in history
						if (existingContentHashes.has(hash)) {
							continue;
						}

						// Handle system messages from buffer (e.g., /context output)
						if (bufferType === 'system') {
							const systemMsg: ChatMessage = {
								id: `buffer-system-${Date.now()}-${Math.random()}`,
								role: 'system' as const,
								content: bufferContent,
								type: 'system' as const,
								systemSubtype: m.subtype as string | undefined,
								systemData: m.data as Record<string, unknown> | undefined,
								streaming: false
							};
							finalMessages.push(systemMsg);
							existingContentHashes.add(hash);
							continue;
						}

						// Handle subagent messages from streaming buffer
						if (bufferType === 'subagent') {
							const subagentMsg: ChatMessage = {
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
							};
							finalMessages.push(subagentMsg);
							existingContentHashes.add(hash);
							continue;
						}

						// Build the buffer message
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

						// For tool_use messages, merge result if present (from new backend format)
						// or from legacy buffer format (separate tool_result messages)
						if (bufferType === 'tool_use' && bufferToolId) {
							// Check for embedded result (new format from backend)
							if (m.tool_result !== undefined) {
								bufferMsg.toolResult = m.tool_result as string;
								bufferMsg.toolStatus = (m.tool_status as 'complete' | 'error') || 'complete';
								bufferMsg.streaming = false;
							}
							// Check for result in legacy format (separate tool_result in buffer)
							else if (bufferToolResults.has(bufferToolId)) {
								const result = bufferToolResults.get(bufferToolId)!;
								bufferMsg.toolResult = result.content;
								bufferMsg.toolStatus = result.status;
								bufferMsg.streaming = false;
							}
						}

						finalMessages.push(bufferMsg);
						existingContentHashes.add(hash);
					}

					// Initialize the streaming buffer's active block type for late-joining devices
					// This ensures incoming streaming deltas are routed to the correct content block
					const activeBlockType = data.activeBlockType as 'thinking' | 'text' | 'tool_use' | null | undefined;
					if (activeBlockType) {
						setActiveBlockType(tabId, activeBlockType);
					}
				}

				// Extract token counts and profile from session data if available
				const session = data.session as Record<string, unknown> | undefined;
				const tokenUpdates: Partial<ChatTab> = {};
				if (session) {
					if (typeof session.total_tokens_in === 'number') {
						tokenUpdates.totalTokensIn = session.total_tokens_in;
					}
					if (typeof session.total_tokens_out === 'number') {
						tokenUpdates.totalTokensOut = session.total_tokens_out;
					}
					if (typeof session.cache_creation_tokens === 'number') {
						tokenUpdates.totalCacheCreationTokens = session.cache_creation_tokens;
					}
					if (typeof session.cache_read_tokens === 'number') {
						tokenUpdates.totalCacheReadTokens = session.cache_read_tokens;
					}
					if (typeof session.context_tokens === 'number') {
						tokenUpdates.contextUsed = session.context_tokens;
					}
					// Set session profile for message display (locked to creating profile)
					if (typeof session.profile_id === 'string') {
						tokenUpdates.sessionProfileId = session.profile_id;
					}
				}

				// For late-joining devices during streaming, also update context from streamingUsage
				// This provides real-time context usage even when joining mid-stream
				const streamingUsage = data.streamingUsage as Record<string, number> | undefined;
				if (isStreaming && streamingUsage) {
					const inputTokens = streamingUsage.input_tokens || 0;
					const cacheCreationTokens = streamingUsage.cache_creation_input_tokens || 0;
					const cacheReadTokens = streamingUsage.cache_read_input_tokens || 0;
					const streamContextUsed = inputTokens + cacheCreationTokens + cacheReadTokens;

					// Only update if streamingUsage provides more recent data than session
					// (which it should during active streaming)
					if (streamContextUsed > 0) {
						tokenUpdates.contextUsed = streamContextUsed;
						tokenUpdates.totalCacheCreationTokens = cacheCreationTokens;
						tokenUpdates.totalCacheReadTokens = cacheReadTokens;
					}
				}

				// DEFENSIVE CHECK: Don't replace existing messages with fewer/empty data.
				// This can happen if JSONL has partial snapshots (stop_reason: null) or
				// if the client reconnects before JSONL is fully written by the SDK.
				// Preserve existing messages to prevent "chat history disappearing" bug.
				const currentTab = getTab(tabId);
				const currentAssistantMsgs = currentTab?.messages.filter(m => m.role === 'assistant' && m.type === 'text') || [];
				const incomingAssistantMsgs = finalMessages.filter(m => m.role === 'assistant' && m.type === 'text');
				const shouldPreserveMessages = currentTab && currentTab.messages.length > 0 && !isStreaming && (
					finalMessages.length === 0 ||
					(currentAssistantMsgs.length > incomingAssistantMsgs.length) ||
					// Guard against large message count drops (e.g., stale JSONL with partial data)
					(finalMessages.length > 0 && currentTab.messages.length > finalMessages.length * 1.5)
				);

				if (shouldPreserveMessages) {
					console.warn(`[Tab ${tabId}] Received empty history but tab has ${currentTab.messages.length} messages - preserving existing messages`);
					// Still update other fields, but keep existing messages
					updateTab(tabId, {
						sessionId: data.session_id as string,
						// messages: NOT updated - keep existing
						isStreaming: isStreaming,
						// todos: NOT updated if we have existing messages
						error: null,
						...tokenUpdates
					});
				} else {
					// Preserve existing message IDs where possible to prevent Svelte component recreation
					// This keeps UI state (like ThinkingBlock expanded/collapsed) stable
					const existingMessages = currentTab?.messages || [];
					const messagesWithPreservedIds = finalMessages.map((msg, i) => {
						const existingMsg = existingMessages[i];
						const canPreserveId = existingMsg &&
							existingMsg.type === msg.type &&
							existingMsg.role === msg.role;
						return canPreserveId ? { ...msg, id: existingMsg.id } : msg;
					});

					const tabUpdate: Partial<ChatTab> = {
						sessionId: data.session_id as string,
						messages: messagesWithPreservedIds,
						isStreaming: isStreaming,
						todos: latestTodos,
						error: null,  // Clear any previous error
						...tokenUpdates
					};

					// Restore worktree state if session was created in worktree mode
					const session = data.session as Record<string, unknown> | undefined;
					const worktreeId = session?.worktree_id as string | undefined;
					const worktreeBranch = session?.worktree_branch as string | undefined;
					const worktreeDeletedFlag = session?.worktree_deleted as boolean | undefined;
					if (worktreeId && worktreeBranch) {
						// Active worktree
						tabUpdate.executionMode = 'worktree';
						tabUpdate.worktreeId = worktreeId;
						tabUpdate.worktreeBranch = worktreeBranch;
						tabUpdate.selectedWorktreeBranch = worktreeBranch;
						tabUpdate.worktreeMode = 'existing';
						tabUpdate.worktreeDeleted = false;
					} else if (worktreeDeletedFlag && worktreeBranch) {
						// Worktree was cleaned up - restore display info but mark as read-only
						tabUpdate.executionMode = 'worktree';
						tabUpdate.worktreeId = null;
						tabUpdate.worktreeBranch = worktreeBranch;
						tabUpdate.selectedWorktreeBranch = worktreeBranch;
						tabUpdate.worktreeMode = 'existing';
						tabUpdate.worktreeDeleted = true;
					}

					// Restore pending permissions/questions on reconnect
					// Backend includes these in the history response when session has pending requests
					const pendingPerms = data.pendingPermissions as PermissionRequestType[] | undefined;
					const pendingQs = data.pendingQuestions as UserQuestionRequest[] | undefined;
					if (pendingPerms && pendingPerms.length > 0) {
						tabUpdate.pendingPermissions = pendingPerms;
					}
					if (pendingQs && pendingQs.length > 0) {
						tabUpdate.pendingQuestions = pendingQs;
					}

					updateTab(tabId, tabUpdate);

					// Update tab title based on first message
					if (finalMessages.length > 0 && finalMessages[0].role === 'user') {
						const title = finalMessages[0].content.substring(0, 100) + (finalMessages[0].content.length > 100 ? '...' : '');
						updateTab(tabId, { title });
					}
				}

				// If session is streaming, start server-authoritative polling
				// This ensures we get the correct state regardless of any buffer merging issues
				const receivedSession = data.session_id as string;
				if (isStreaming && receivedSession) {
					tabStreamingOnDisconnect.delete(tabId); // Clear — reconnect confirmed streaming
					startPolling(tabId, receivedSession);

					// Request pending permissions/questions as fallback
					// The history response may include them, but if it doesn't (e.g., request
					// arrived after load_session was processed), this ensures we pick them up
					const currentWs = tabConnections.get(tabId);
					if (currentWs?.readyState === WebSocket.OPEN) {
						currentWs.send(JSON.stringify({ type: 'get_pending_permissions' }));
						currentWs.send(JSON.stringify({ type: 'get_pending_questions' }));
					}
				} else {
					// Not streaming according to load_session response.
					// Check if this tab was streaming when it disconnected — if so, the
					// isStreaming: false may be a brief race during SDK compaction.
					// Keep polling for a grace period and let the poller verify the real state.
					const disconnectSessionId = tabStreamingOnDisconnect.get(tabId);
					if (disconnectSessionId && disconnectSessionId === receivedSession) {
						tabStreamingOnDisconnect.delete(tabId);
						// Let the poller run for a few more cycles. If the backend truly
						// stopped streaming, the poller's own streamingJustEnded logic will
						// stop it and fire onStreamEnd. If streaming resumes (post-compaction),
						// the poller will pick it up seamlessly.
						// After 10s grace period, if poller is still running but never got
						// isStreaming: true, force-stop it to avoid infinite idle polling.
						setTimeout(() => {
							const gracedSession = sessionState.getSession(disconnectSessionId);
							if (gracedSession && !gracedSession.isStreaming && sessionPoller.isPolling(disconnectSessionId)) {
								sessionPoller.stop(disconnectSessionId);
								sessionState.removeSession(disconnectSessionId);
							}
						}, 10000);
					} else {
						// Normal case: not streaming, stop any existing polling
						tabStreamingOnDisconnect.delete(tabId);
						stopPolling(tabId);
					}
				}

				// Process any queued sync events that arrived while waiting for history
				const queuedEvents = tabsWaitingForHistory.get(tabId);
				if (queuedEvents && queuedEvents.length > 0) {
					// Remove from waiting map first to prevent re-queueing
					tabsWaitingForHistory.delete(tabId);
					// Process each queued event
					for (const queuedData of queuedEvents) {
						handleSyncEvent(tabId, queuedData);
					}
				} else {
					// No queued events, just clear the waiting state
					tabsWaitingForHistory.delete(tabId);
				}
				break;
			}

			case 'session_created': {
				// New session was created - save sessionId immediately so it persists on refresh
				// This is sent BEFORE streaming starts, ensuring we can reconnect if page refreshes
				const sessionId = data.session_id as string;
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						// Lock sessionProfileId to current profile - this is the profile creating the session
						return { ...tab, sessionId, sessionProfileId: tab.profile };
					})
				}));

				// Save immediately - critical for reconnection on refresh
				saveTabsToServer(get({ subscribe }));

				// Refresh sessions list so new session appears in sidebar immediately
				loadSessionsInternal();
				break;
			}

			case 'start': {
				// Phase 5: Mark streaming started. Notifier handles sessionPoller.startStreaming().
				const sessionId = data.session_id as string;

				// Clear interrupt pending flag — new stream has started successfully
				tabInterruptPending.delete(tabId);

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return {
							...tab,
							sessionId,
							isStreaming: true,
							streamStartTime: Date.now()
						};
					})
				}));

				// Save in case session_created wasn't received (backwards compat)
				saveTabsToServer(get({ subscribe }));
				break;
			}

			case 'chunk': {
				// Phase 5: Notification-only — notifier triggers pollNow(), poller updates sessionState
				break;
			}

			// StreamEvent handlers — Phase 5: notification-only
			// The notifier at the top of this function already forwarded the event to
			// sessionPoller.pollNow(), which fetches authoritative state from the server.
			// No local state updates needed here.
			case 'stream_start': {
				break;
			}

			case 'stream_block_start':
			case 'stream_delta':
			case 'stream_block_stop':
			case 'stream_message_delta': {
				// Streaming data events — handled by poller via sessionState
				break;
			}

			case 'tool_use': {
				// Phase 5: Notification-only — state updated by poller.
				// Keep tabPendingTools tracking for WS health timeout management.
				const toolId = data.id as string;
				if (toolId) {
					let pending = tabPendingTools.get(tabId);
					if (!pending) {
						pending = new Set();
						tabPendingTools.set(tabId, pending);
					}
					pending.add(toolId);
				}
				break;
			}

			case 'tool_result': {
				// Phase 5: Notification-only — state updated by poller.
				// Keep tabPendingTools tracking for WS health timeout management.
				const toolUseId = data.tool_use_id as string;
				if (toolUseId) {
					const pending = tabPendingTools.get(tabId);
					if (pending) {
						pending.delete(toolUseId);
					}
				}
				break;
			}

			case 'done': {
				// Phase 5: Simplified — poller handles messages, we handle control flow & notifications.
				const metadata = data.metadata as Record<string, unknown>;
				const sessionId = data.session_id as string;
				// Clear interrupt pending flag (safety — shouldn't still be set at 'done')
				tabInterruptPending.delete(tabId);

				// CRITICAL: Do NOT stop polling immediately. Instead, trigger one final poll
				// to fetch the filtered messages from end_streaming(). The poller will detect
				// the streaming→not-streaming transition and auto-stop + fire onStreamEnd.
				const doneSessionId = sessionId || getTab(tabId)?.sessionId;
				if (doneSessionId) {
					sessionPoller.pollNow(doneSessionId);
				}

				// Clear any pending stop timeout since streaming is complete
				const doneStopTimeout = stopTimeouts.get(tabId);
				if (doneStopTimeout) {
					clearTimeout(doneStopTimeout);
					stopTimeouts.delete(tabId);
				}

				// Clear pending tools tracking since stream is complete
				tabPendingTools.delete(tabId);

				// Get current tab state before updating (for notification)
				const currentDoneTab = getTab(tabId);
				const durationSeconds = currentDoneTab?.streamStartTime
					? (Date.now() - currentDoneTab.streamStartTime) / 1000
					: undefined;

				// Update tab control state (isStreaming, sessionId) — NOT messages
				// Messages are managed by sessionState via the poller
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						// Update title if this is the first response
						let title = tab.title;
						if (title === 'New Chat' && tab.messages.length > 0) {
							const firstUserMsg = tab.messages.find(m => m.role === 'user');
							if (firstUserMsg) {
								title = firstUserMsg.content.substring(0, 100) + (firstUserMsg.content.length > 100 ? '...' : '');
							}
						}

						return {
							...tab,
							isStreaming: false,
							streamStartTime: null,
							sessionId: sessionId || tab.sessionId,
							title,
							// Note: messages are NOT updated here. During streaming, the derived
							// merge store overlays sessionState messages onto the tab. The final
							// messages are flushed to the raw store in onStreamEnd, ensuring they
							// persist after sessionState cleanup.
						};
					})
				}));

				// Trigger browser notification if enabled and permission granted
				const doneTab = getTab(tabId);
				if (isNotificationsEnabled() && getPermission() === 'granted') {
					notifySessionComplete({
						sessionId: sessionId || '',
						sessionTitle: doneTab?.title ?? null,
						durationSeconds,
						totalCostUsd: metadata?.total_cost_usd as number | undefined,
						tokensIn: metadata?.tokens_in as number | undefined,
						tokensOut: metadata?.tokens_out as number | undefined,
						model: metadata?.model as string | undefined
					});
				}

				// Refresh sessions list
				loadSessionsInternal();

				// Save tabs state since sessionId may have changed (debounced)
				saveTabsToServer(get({ subscribe }));

				// Safety: ensure cleanup if the poller pipeline fails (network error, /live down).
				// The primary cleanup happens in onStreamEnd (fired by poller on streaming→not-streaming),
				// but if the final poll never completes, stale data would persist.
				{
					const cleanupId = sessionId || currentDoneTab?.sessionId;
					if (cleanupId) {
						setTimeout(() => {
							// Check if onStreamEnd already handled cleanup successfully
							if (tabStreamEndCleanedUp.get(cleanupId)) {
								tabStreamEndCleanedUp.delete(cleanupId);
								return;
							}

							console.warn(`[Tab ${tabId}] Safety: onStreamEnd didn't fire for ${cleanupId}, running fallback cleanup`);

							// Force stop polling if still running
							if (sessionPoller.isPolling(cleanupId)) {
								console.warn(`[Tab ${tabId}] Safety: forcing poller stop for ${cleanupId}`);
								sessionPoller.stop(cleanupId);
							}
							// Remove stale sessionState
							const session = sessionState.getSession(cleanupId);
							if (session) {
								sessionState.removeSession(cleanupId);
							}
							// Trigger JSONL refresh if not already done
							const refreshTab = getTab(tabId);
							if (refreshTab && !refreshTab.isStreaming && refreshTab.sessionId === cleanupId) {
								const ws = tabConnections.get(tabId);
								if (ws?.readyState === WebSocket.OPEN) {
									tabExpectedSessionId.set(tabId, cleanupId);
									tabsWaitingForHistory.set(tabId, []);
									ws.send(JSON.stringify({ type: 'load_session', session_id: cleanupId }));
								}
							}
						}, 5000);
					}
				}
				break;
			}

			case 'stopped':
			case 'interrupted': {
				// If this is from an interrupt_and_query, skip all cleanup.
				// sendMessage() already stopped the poller, flushed messages to raw store,
				// and removed session state. A new stream is expected momentarily.
				if (tabInterruptPending.get(tabId)) {
					tabPendingTools.delete(tabId);
					break;
				}

				// Phase 5: Simplified — poller handles messages, we handle control flow.

				// Trigger final poll to fetch cleaned-up messages (same pattern as 'done')
				const stoppedTab = getTab(tabId);
				const stoppedSessionId = stoppedTab?.sessionId;
				if (stoppedSessionId) {
					sessionPoller.pollNow(stoppedSessionId);
				}

				// Clear the fallback timeout since we received confirmation
				const stopTimeout = stopTimeouts.get(tabId);
				if (stopTimeout) {
					clearTimeout(stopTimeout);
					stopTimeouts.delete(tabId);
				}

				// Clear pending tools tracking since stream is stopped
				tabPendingTools.delete(tabId);

				// Mark streaming as ended — message state is managed by poller/sessionState
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return { ...tab, isStreaming: false };
					})
				}));

				// Safety: ensure cleanup if the poller pipeline fails
				{
					const cleanupId = stoppedSessionId;
					if (cleanupId) {
						setTimeout(() => {
							if (sessionPoller.isPolling(cleanupId)) {
								console.warn(`[Tab ${tabId}] Safety: forcing poller stop for ${cleanupId}`);
								sessionPoller.stop(cleanupId);
							}
							const session = sessionState.getSession(cleanupId);
							if (session) {
								sessionState.removeSession(cleanupId);
							}
						}, 5000);
					}
				}
				break;
			}

			case 'error': {
				// Phase 5: Simplified — keep error state for UI, poller handles messages.
				tabInterruptPending.delete(tabId);
				stopPolling(tabId);

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return {
							...tab,
							isStreaming: false,
							error: data.message as string
						};
					})
				}));
				break;
			}

			case 'message_queued': {
				// Message was successfully queued for streaming input
				const queuePosition = data.queue_position as number;
				const prompt = data.prompt as string;
				// The message was already added to the UI by sendMessage
				break;
			}

			case 'queued_message_processing': {
				// A queued message is now being processed
				const prompt = data.prompt as string;
				// The message should already be in the UI, no action needed
				break;
			}

			case 'session_locked': {
				// Session is locked by another device
				const lockedSessionId = data.session_id as string;
				const lockedBy = data.locked_by as string;
				const lockMessage = data.message as string;
				console.warn(`[Tab ${tabId}] Session ${lockedSessionId} is locked by device ${lockedBy}: ${lockMessage}`);

				// Set error on the tab to inform user
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return {
							...tab,
							error: 'This session is already open on another device. Click "Take Over" to use it here.',
							sessionLocked: true,
							sessionLockedBy: lockedBy
						};
					})
				}));
				break;
			}

			case 'session_takeover': {
				// Another device has taken over this session
				const newDevice = data.new_device as string;
				const takeoverMessage = data.message as string;
				console.warn(`[Tab ${tabId}] Session taken over by device ${newDevice}: ${takeoverMessage}`);

				// Clear the session from this tab and show message
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return {
							...tab,
							error: 'This session was taken over by another device.',
							isStreaming: false,
							sessionLocked: false,
							sessionLockedBy: undefined
						};
					})
				}));
				break;
			}

			case 'ping': {
				const ws = tabConnections.get(tabId);
				if (ws?.readyState === WebSocket.OPEN) {
					ws.send(JSON.stringify({ type: 'pong' }));
				}
				break;
			}

			case 'heartbeat': {
				// Heartbeat from server during long operations (tool execution, subagents)
				// Just receiving this message updates tabLastMessageTime (done in onmessage)
				// which prevents false disconnect detection.
				//
				// Recovery: heartbeats include pending permission/question counts.
				// If the server reports pending items that we don't have locally,
				// the original WebSocket message was likely missed — request full state.
				const pendingPermCount = data.pending_permissions as number | undefined;
				const pendingQCount = data.pending_questions as number | undefined;
				if (pendingPermCount && pendingPermCount > 0) {
					const currentTab = getTab(tabId);
					if (currentTab && (!currentTab.pendingPermissions || currentTab.pendingPermissions.length === 0)) {
						const currentWs = tabConnections.get(tabId);
						if (currentWs?.readyState === WebSocket.OPEN) {
							currentWs.send(JSON.stringify({ type: 'get_pending_permissions' }));
						}
					}
				}
				if (pendingQCount && pendingQCount > 0) {
					const currentTab = getTab(tabId);
					if (currentTab && (!currentTab.pendingQuestions || currentTab.pendingQuestions.length === 0)) {
						const currentWs = tabConnections.get(tabId);
						if (currentWs?.readyState === WebSocket.OPEN) {
							currentWs.send(JSON.stringify({ type: 'get_pending_questions' }));
						}
					}
				}
				break;
			}

			case 'system': {
				// Phase 5: Notification-only — system messages handled by poller.
				// Log for debugging; state is fetched from /sessions/{id}/live.
				const subtype = data.subtype as string;
				break;
			}

			// ── Subagent events — Phase 5: notification-only ──────────────────
			// Subagent state is fetched by the poller from /sessions/{id}/live.
			// We only keep tabPendingTools tracking for WS health timeouts.
			case 'subagent_start': {
				break;
			}

			case 'subagent_tool_use': {
				// Keep tabPendingTools tracking for WS health timeout
				const toolId = data.id as string;
				if (toolId) {
					let pending = tabPendingTools.get(tabId);
					if (!pending) {
						pending = new Set();
						tabPendingTools.set(tabId, pending);
					}
					pending.add(toolId);
				}
				break;
			}

			case 'subagent_tool_result': {
				// Keep tabPendingTools tracking for WS health timeout
				const toolUseId = data.tool_use_id as string;
				if (toolUseId) {
					const pending = tabPendingTools.get(tabId);
					if (pending) {
						pending.delete(toolUseId);
					}
				}
				break;
			}

			case 'subagent_chunk': {
				// Notification-only — poller handles state
				break;
			}

			case 'subagent_done': {
				break;
			}

			case 'permission_request': {
				// Tool permission request from backend - add to queue
				const request: PermissionRequestType = {
					request_id: data.request_id as string,
					tool_name: data.tool_name as string,
					tool_input: data.tool_input as Record<string, unknown>,
					queue_position: data.queue_position as number | undefined,
					queue_total: data.queue_total as number | undefined
				};

				// Send desktop notification if user is not focused on the window
				if (isNotificationsEnabled()) {
					notifyWaitingForResponse('permission', {
						toolName: request.tool_name
					});
				}

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						// Add to pending permissions queue
						const pendingPermissions = [...tab.pendingPermissions, request];
						return { ...tab, pendingPermissions };
					})
				}));
				break;
			}

			case 'permission_queue_update': {
				// Some permissions were auto-resolved (e.g., by "Allow Always")
				const resolvedIds = data.resolved_ids as string[];
				const remainingCount = data.remaining_count as number;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						// Remove resolved permissions from queue
						const pendingPermissions = tab.pendingPermissions.filter(
							p => !resolvedIds.includes(p.request_id)
						);
						return { ...tab, pendingPermissions };
					})
				}));
				break;
			}

			case 'permission_response_ack': {
				// Acknowledgement that permission response was processed
				const requestId = data.request_id as string;
				const result = data.result as Record<string, unknown>;

				// Remove this permission from the queue
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						const pendingPermissions = tab.pendingPermissions.filter(
							p => p.request_id !== requestId
						);
						return { ...tab, pendingPermissions };
					})
				}));
				break;
			}

			case 'pending_permissions': {
				// Response to get_pending_permissions request - sync queue state
				const requests = data.requests as PermissionRequestType[];

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return { ...tab, pendingPermissions: requests };
					})
				}));
				break;
			}

			case 'user_question': {
				// User question request from backend (AskUserQuestion tool) - add to queue
				// Ensure questions have the expected structure with options array
				const rawQuestions = data.questions as Array<Record<string, unknown>>;
				const normalizedQuestions = rawQuestions.map((q, idx) => ({
					question: q.question as string || '',
					header: q.header as string || `Question ${idx + 1}`,
					options: Array.isArray(q.options) ? q.options.map(opt => ({
						label: typeof opt === 'object' && opt !== null ? (opt as Record<string, unknown>).label as string || '' : String(opt),
						description: typeof opt === 'object' && opt !== null ? (opt as Record<string, unknown>).description as string || '' : ''
					})) : [],
					multiSelect: Boolean(q.multiSelect)
				}));

				const request: UserQuestionRequest = {
					request_id: data.request_id as string,
					tool_use_id: data.tool_use_id as string,
					questions: normalizedQuestions
				};

				// Send desktop notification if user is not focused on the window
				if (isNotificationsEnabled()) {
					notifyWaitingForResponse('question', {
						questionCount: request.questions.length
					});
				}

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						// Add to pending questions queue
						const pendingQuestions = [...tab.pendingQuestions, request];
						return { ...tab, pendingQuestions };
					})
				}));
				break;
			}

			case 'user_question_response_ack': {
				// Acknowledgement that user question response was processed
				const requestId = data.request_id as string;
				const success = data.success as boolean;

				// Remove this question from the queue
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						const pendingQuestions = tab.pendingQuestions.filter(
							q => q.request_id !== requestId
						);
						return { ...tab, pendingQuestions };
					})
				}));
				break;
			}

			case 'pending_questions': {
				// Response to get_pending_questions request - sync queue state
				const questions = data.questions as UserQuestionRequest[];

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return { ...tab, pendingQuestions: questions };
					})
				}));
				break;
			}

			case 'plugin_message': {
				// Phase 5: Notification-only — plugin messages handled by poller.
				break;
			}
		}
	}

	// Page size for sessions pagination
	const SESSIONS_PAGE_SIZE = 50;
	const SEARCH_PAGE_SIZE = 20;

	/**
	 * Load sessions list (user's own sessions) with pagination
	 * For admins: loads sessions where api_user_id IS NULL (their own chats)
	 * For API users: the backend automatically filters to their sessions
	 */
	async function loadSessionsInternal(tagId?: string | null) {
		update(s => ({ ...s, sessionsLoading: true }));
		try {
			// Load user's own sessions (admin_only=true for admins gets sessions without api_user_id)
			let url = `/sessions/paginated?limit=${SESSIONS_PAGE_SIZE}&offset=0&admin_only=true`;
			if (tagId) {
				url += `&tag_id=${encodeURIComponent(tagId)}`;
			}
			const result = await api.get<PaginatedSessions>(url);
			update(s => ({
				...s,
				sessions: result.items,
				sessionsTotal: result.total,
				sessionsOffset: 0,
				sessionsHasMore: result.has_more,
				sessionsLoading: false,
				sessionsTagFilter: tagId ?? null
			}));
		} catch (e) {
			console.error('Failed to load sessions:', e);
			update(s => ({ ...s, sessionsLoading: false }));
		}
	}

	/**
	 * Load more sessions (infinite scroll)
	 */
	async function loadMoreSessionsInternal() {
		const state = get({ subscribe });
		if (state.sessionsLoadingMore || !state.sessionsHasMore) return;

		update(s => ({ ...s, sessionsLoadingMore: true }));
		try {
			const newOffset = state.sessionsOffset + SESSIONS_PAGE_SIZE;
			let url = `/sessions/paginated?limit=${SESSIONS_PAGE_SIZE}&offset=${newOffset}&admin_only=true`;
			if (state.sessionsTagFilter) {
				url += `&tag_id=${encodeURIComponent(state.sessionsTagFilter)}`;
			}
			const result = await api.get<PaginatedSessions>(url);
			update(s => ({
				...s,
				sessions: [...s.sessions, ...result.items],
				sessionsOffset: newOffset,
				sessionsHasMore: result.has_more,
				sessionsLoadingMore: false
			}));
		} catch (e) {
			console.error('Failed to load more sessions:', e);
			update(s => ({ ...s, sessionsLoadingMore: false }));
		}
	}

	/**
	 * Search sessions with pagination
	 */
	async function searchSessionsInternal(query: string) {
		if (!query.trim()) {
			// Clear search results when query is empty
			update(s => ({
				...s,
				searchQuery: '',
				searchResults: [],
				searchTotal: 0,
				searchOffset: 0,
				searchHasMore: false,
				searchLoading: false
			}));
			return;
		}

		update(s => ({ ...s, searchLoading: true, searchQuery: query }));
		try {
			const result = await searchSessionsPaginated(query, {
				adminOnly: true,
				limit: SEARCH_PAGE_SIZE,
				offset: 0
			});
			update(s => ({
				...s,
				searchResults: result.items,
				searchTotal: result.total,
				searchOffset: 0,
				searchHasMore: result.has_more,
				searchLoading: false
			}));
		} catch (e) {
			console.error('Failed to search sessions:', e);
			update(s => ({ ...s, searchLoading: false }));
		}
	}

	/**
	 * Load more search results (infinite scroll)
	 */
	async function loadMoreSearchResultsInternal() {
		const state = get({ subscribe });
		if (state.searchLoadingMore || !state.searchHasMore || !state.searchQuery) return;

		update(s => ({ ...s, searchLoadingMore: true }));
		try {
			const newOffset = state.searchOffset + SEARCH_PAGE_SIZE;
			const result = await searchSessionsPaginated(state.searchQuery, {
				adminOnly: true,
				limit: SEARCH_PAGE_SIZE,
				offset: newOffset
			});
			update(s => ({
				...s,
				searchResults: [...s.searchResults, ...result.items],
				searchOffset: newOffset,
				searchHasMore: result.has_more,
				searchLoadingMore: false
			}));
		} catch (e) {
			console.error('Failed to load more search results:', e);
			update(s => ({ ...s, searchLoadingMore: false }));
		}
	}

	/**
	 * Clear search state
	 */
	function clearSearchInternal() {
		update(s => ({
			...s,
			searchQuery: '',
			searchResults: [],
			searchTotal: 0,
			searchOffset: 0,
			searchHasMore: false,
			searchLoading: false,
			searchLoadingMore: false
		}));
	}

	/**
	 * Load admin sessions (all API user sessions for admin view)
	 */
	async function loadAdminSessionsInternal(apiUserId?: string | null, tagId?: string | null) {
		try {
			let url = `/sessions/paginated?limit=${SESSIONS_PAGE_SIZE}&offset=0`;
			if (apiUserId) {
				// Filter by specific API user
				url += `&api_user_id=${encodeURIComponent(apiUserId)}`;
			} else {
				// Show all API user sessions (exclude admin's own sessions)
				url += '&api_users_only=true';
			}
			if (tagId) {
				url += `&tag_id=${encodeURIComponent(tagId)}`;
			}
			const result = await api.get<PaginatedSessions>(url);
			update(s => ({
				...s,
				adminSessions: result.items,
				adminSessionsFilter: apiUserId ?? null,
				adminSessionsTagFilter: tagId ?? null
			}));
		} catch (e) {
			console.error('Failed to load admin sessions:', e);
		}
	}

	/**
	 * Load API users list (admin only)
	 */
	async function loadApiUsersInternal() {
		try {
			const apiUsers = await api.get<ApiUser[]>('/api-users');
			update(s => ({ ...s, apiUsers }));
		} catch (e) {
			// Non-admins will get 403, that's expected
			console.debug('Failed to load API users (may not be admin):', e);
		}
	}

	return {
		subscribe,

		/**
		 * Initialize - load tabs from server and connect WebSockets
		 */
		async init() {
			isInitializing = true;

			try {
				// First, load selection from backend to sync localStorage
				// This ensures we have the latest profile/project selection before restoring tabs
				const backendSelection = await loadSelectionFromBackend();

				// Try to load persisted tabs from server
				const persistedState = await loadTabsFromServer();

				if (persistedState && persistedState.tabs) {
					// Get defaults for profile/project - prefer backend, fallback to localStorage
					const defaultProfile = backendSelection?.selectedProfile || getPersistedProfile();
					const defaultProject = backendSelection?.selectedProject || getPersistedProject();

					// Restore tabs from server (could be empty array)
					const restoredTabs: ChatTab[] = persistedState.tabs.map(pt => ({
						id: pt.id,
						title: pt.title,
						sessionId: pt.sessionId,
						messages: [],
						isStreaming: false,
						wsConnected: false,
						error: null,
						// Fall back to localStorage if server-saved profile/project is empty
						profile: pt.profile || defaultProfile,
						sessionProfileId: null,  // Will be set when session is loaded via WebSocket
						currentQueryProfileId: null,  // Will be set when query starts
						project: pt.project || defaultProject,
						totalTokensIn: 0,
						totalTokensOut: 0,
						totalCacheCreationTokens: 0,
						totalCacheReadTokens: 0,
						contextUsed: null,
						contextMax: 200000,
						modelOverride: pt.modelOverride ?? null,
						permissionModeOverride: pt.permissionModeOverride ?? null,
						thinkingModeOverride: pt.thinkingModeOverride ?? null,
						pendingPermissions: [],
						pendingQuestions: [],
						todos: [],
						draft: '',
						scrollTop: 0,
						streamStartTime: null,
						// Restore worktree state from persisted tab, with defaults for backward compatibility
						executionMode: pt.executionMode || 'local',
						worktreeBranch: pt.worktreeBranch || '',
						worktreeBaseBranch: 'main',
						worktreeCreateNewBranch: false,
						worktreeId: pt.worktreeId ?? null,
						worktreeMode: pt.worktreeMode || 'new',
						selectedWorktreeBranch: pt.selectedWorktreeBranch ?? null,
						isCreatingWorktree: false,
						worktreeError: null,
						worktreeDeleted: pt.worktreeDeleted ?? false,
						sessionLocked: false,
						sessionLockedBy: undefined
					}));

					update(s => ({
						...s,
						tabs: restoredTabs,
						activeTabId: persistedState.activeTabId || restoredTabs[0]?.id || null
					}));

					// Connect WebSockets for each tab (if any)
					// Note: We rely on WebSocket load_session (sent in ws.onopen) to load messages
					// This ensures we get streaming state and buffer for active sessions
					// DO NOT call loadSessionInTab here - it uses REST which doesn't have streaming info
					for (const tab of restoredTabs) {
						connectTab(tab.id);
					}

				} else {
					// No persisted state at all - start with empty tabs (user can create one)
				}
			} catch (error) {
				console.error('[Tabs] Failed to initialize from server:', error);
				// Fall back to default behavior
				const state = get({ subscribe });
				for (const tab of state.tabs) {
					connectTab(tab.id);
				}
			} finally {
				isInitializing = false;
			}
		},

		/**
		 * Cleanup all connections
		 */
		destroy() {
			const state = get({ subscribe });
			for (const tab of state.tabs) {
				disconnectTab(tab.id);
			}
		},

		/**
		 * Create a new tab
		 */
		createTab(sessionId?: string) {
			const newTabId = generateTabId();

			// Always use localStorage for profile/project selection
			// This is the user's explicitly saved preference, more reliable than
			// trying to inherit from "active" tabs which may be orphaned/stale
			const profile = getPersistedProfile();
			const project = getPersistedProject();

			const newTab: ChatTab = {
				id: newTabId,
				title: 'New Chat',
				sessionId: sessionId || null,
				messages: [],
				isStreaming: false,
				wsConnected: false,
				error: null,
				profile,
				sessionProfileId: null,  // Will be set when session is created/loaded
				currentQueryProfileId: null,  // Will be set when query starts
				project,
				totalTokensIn: 0,
				totalTokensOut: 0,
				totalCacheCreationTokens: 0,
				totalCacheReadTokens: 0,
				contextUsed: null,
				contextMax: 200000,
				modelOverride: null,
				permissionModeOverride: null,
				thinkingModeOverride: null,
				pendingPermissions: [],
				pendingQuestions: [],
				todos: [],
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
				sessionLockedBy: undefined
			};

			update(s => ({
				...s,
				tabs: [...s.tabs, newTab],
				activeTabId: newTabId
			}));

			// Connect WebSocket for new tab
			// Note: We rely on WebSocket load_session (sent in ws.onopen) to load messages
			// when sessionId is set. This ensures we get streaming state and buffer for
			// active sessions. DO NOT call loadSessionInTab here - it uses REST which
			// doesn't have streaming info and can race/clobber WebSocket streaming state.
			connectTab(newTabId);

			// Save tabs state (debounced)
			saveTabsToServer(get({ subscribe }));

			return newTabId;
		},

		/**
		 * Close a tab
		 */
		closeTab(tabId: string) {
			const state = get({ subscribe });
			const tab = state.tabs.find(t => t.id === tabId);

			// Don't close if the tab is actively streaming
			if (tab?.isStreaming) {
				return;
			}

			// Before closing, save this tab's profile/project as the default selection
			// This ensures the selection persists even when all tabs are closed
			if (tab?.profile || tab?.project) {
				saveSelectionToBackend(tab.profile, tab.project);
			}

			// Send close_session message before disconnecting (so other devices know)
			if (tab?.sessionId) {
				const ws = tabConnections.get(tabId);
				if (ws && ws.readyState === WebSocket.OPEN) {
					ws.send(JSON.stringify({ type: 'close_session' }));
				}
			}

			// Disconnect WebSocket
			disconnectTab(tabId);

			// Remove tab and update active tab if needed
			update(s => {
				const tabIndex = s.tabs.findIndex(t => t.id === tabId);
				const newTabs = s.tabs.filter(t => t.id !== tabId);

				let newActiveId = s.activeTabId;
				if (s.activeTabId === tabId) {
					// Switch to adjacent tab
					newActiveId = newTabs[Math.min(tabIndex, newTabs.length - 1)]?.id || null;
				}

				return {
					...s,
					tabs: newTabs,
					activeTabId: newActiveId
				};
			});

			// Save tabs state (debounced)
			saveTabsToServer(get({ subscribe }));
		},

		/**
		 * Switch to a tab
		 */
		setActiveTab(tabId: string) {
			update(s => ({ ...s, activeTabId: tabId }));
			// Save tabs state (debounced)
			saveTabsToServer(get({ subscribe }));
		},

		/**
		 * Find a tab that has a specific session loaded
		 */
		findTabBySessionId(sessionId: string): string | null {
			const state = get({ subscribe });
			const tab = state.tabs.find(t => t.sessionId === sessionId);
			return tab?.id ?? null;
		},

		/**
		 * Find an existing empty tab (no session, no messages) — singleton pattern
		 */
		findEmptyTab(): string | null {
			const state = get({ subscribe });
			const tab = state.tabs.find(t => t.sessionId === null && t.messages.length === 0);
			return tab?.id ?? null;
		},

		/**
		 * Open a session - switches to existing tab if already open, otherwise creates new tab
		 */
		openSession(sessionId: string) {
			const existingTabId = this.findTabBySessionId(sessionId);
			if (existingTabId) {
				// Session already open - switch to that tab
				this.setActiveTab(existingTabId);

				// Refresh from JSONL if session is not actively streaming.
				// This ensures stale messages from a previous streaming session
				// are replaced with the correct historical view from JSONL.
				const tab = getTab(existingTabId);
				if (tab && !tab.isStreaming && tab.sessionId) {
					const ws = tabConnections.get(existingTabId);
					if (ws?.readyState === WebSocket.OPEN) {
						tabExpectedSessionId.set(existingTabId, tab.sessionId);
						tabsWaitingForHistory.set(existingTabId, []);
						ws.send(JSON.stringify({ type: 'load_session', session_id: tab.sessionId }));
					}
				}

				return existingTabId;
			} else {
				// Create new tab with this session
				return this.createTab(sessionId);
			}
		},

		/**
		 * Send message in a specific tab.
		 * If in worktree mode and first message, creates worktree session first.
		 * If the tab is currently streaming, this will interrupt the current response
		 * and then send the new message.
		 */
		async sendMessage(tabId: string, prompt: string) {
			const tab = getTab(tabId);
			if (!tab) return;

			// If worktree mode and no session yet, handle worktree setup
			if (tab.executionMode === 'worktree' && !tab.sessionId && tab.messages.length === 0) {
				if (tab.worktreeMode === 'new') {
					// Create new worktree + session (legacy flow)
					await this.createWorktreeSession(tabId, prompt);
					return;
				} else if (tab.worktreeMode === 'existing' && tab.worktreeId) {
					// Use existing worktree - session will be created via WebSocket
					// with worktree_id linked
					await this.startSessionOnExistingWorktree(tabId, prompt);
					return;
				} else {
					updateTab(tabId, { error: 'Please select an existing worktree or create a new one' });
					return;
				}
			}

			const ws = tabConnections.get(tabId);
			if (!ws || ws.readyState !== WebSocket.OPEN) {
				updateTab(tabId, { error: 'Not connected' });
				return;
			}

			// Add user message immediately with profileId for per-query tracking
			const userMsgId = `user-${Date.now()}`;
			const isFirstMessage = tab.title === 'New Chat';
			const newTitle = isFirstMessage
				? prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '')
				: tab.title;

			// Capture current profile for this query
			const queryProfileId = tab.profile;

			update(s => ({
				...s,
				tabs: s.tabs.map(t => {
					if (t.id !== tabId) return t;
					return {
						...t,
						messages: [...t.messages, {
							id: userMsgId,
							role: 'user' as const,
							content: prompt,
							profileId: queryProfileId  // Capture profile for this query
						}],
						isStreaming: true,
						error: null,
						title: newTitle,
						currentQueryProfileId: queryProfileId  // Track for streaming messages
					};
				})
			}));

			// Update the deck card title if this is the first message
			if (isFirstMessage) {
				const deckCard = deck.findCardByMeta('tabId', tabId);
				if (deckCard) {
					deck.setCardTitle(deckCard.id, newTitle);
				}
			}

			// If currently streaming, send interrupt_and_query to stop current response
			// and immediately process the new message
			if (tab.isStreaming && tab.sessionId) {
				// Get current session state messages (the complete message list during streaming)
				// before we clean up, so we can flush them to the raw store.
				const currentSession = sessionState.getSession(tab.sessionId);
				const streamingMessages = currentSession?.messages || [];

				// Stop poller and remove session state BEFORE the 'interrupted' event arrives.
				// This prevents the poller from detecting a streaming→not-streaming transition
				// and firing onStreamEnd (which would remove session state and kill the new stream).
				sessionPoller.stop(tab.sessionId);
				sessionState.removeSession(tab.sessionId);

				// Mark that we're expecting a new stream from interrupt_and_query.
				// The 'interrupted'/'stopped' handler checks this flag to skip cleanup.
				tabInterruptPending.set(tabId, true);

				// Flush streaming messages to raw store + mark tools as interrupted + add user message.
				// The user message was already added by the update above (to raw store), but session
				// state was overlaying it. Now that session state is removed, we need the raw store
				// to have the full message list (streaming messages + new user message).
				const updatedMessages = streamingMessages.map(m => {
					if (m.type === 'tool_use' && m.toolStatus === 'running') {
						return { ...m, streaming: false, toolStatus: 'error' as const };
					}
					return m.streaming ? { ...m, streaming: false } : m;
				});

				update(s => ({
					...s,
					tabs: s.tabs.map(t => {
						if (t.id !== tabId) return t;
						return {
							...t,
							messages: [...updatedMessages, {
								id: userMsgId,
								role: 'user' as const,
								content: prompt,
								profileId: queryProfileId
							}]
							// isStreaming already set to true by the update above
						};
					})
				}));

				// Clear pending tools tracking since we're interrupting
				tabPendingTools.delete(tabId);

				// Update lastMessageTime to reset health check timer
				tabLastMessageTime.set(tabId, Date.now());
				ws.send(JSON.stringify({
					type: 'interrupt_and_query',
					prompt,
					session_id: tab.sessionId,
					profile: tab.profile,
					project: tab.project || undefined
				}));
				return;
			}

			// Build overrides object only if there are actual overrides
			const overrides: Record<string, string> = {};
			if (tab.modelOverride) {
				overrides.model = tab.modelOverride;
			}
			if (tab.permissionModeOverride) {
				overrides.permission_mode = tab.permissionModeOverride;
			}
			if (tab.thinkingModeOverride) {
				overrides.thinking_mode = tab.thinkingModeOverride;
			}

			// Send query
			// Update lastMessageTime BEFORE sending to prevent health check from
			// closing the connection while waiting for server response.
			// The health check uses this timestamp to detect stale connections,
			// but without this update, sending a message + setting isStreaming=true
			// could trigger the 15s streaming timeout before the server responds.
			tabLastMessageTime.set(tabId, Date.now());

			ws.send(JSON.stringify({
				type: 'query',
				prompt,
				session_id: tab.sessionId,
				profile: tab.profile,
				project: tab.project || undefined,
				overrides: Object.keys(overrides).length > 0 ? overrides : undefined
			}));
		},

		/**
		 * Create a worktree session for worktree mode
		 * Called on first message when in worktree mode
		 */
		async createWorktreeSession(tabId: string, prompt: string) {
			const tab = getTab(tabId);
			if (!tab) return;

			// Validate required fields
			if (!tab.profile) {
				updateTab(tabId, { worktreeError: 'Please select a profile before starting a worktree session' });
				return;
			}
			if (!tab.project) {
				updateTab(tabId, { worktreeError: 'Please select a project before starting a worktree session' });
				return;
			}
			if (!tab.worktreeBranch) {
				updateTab(tabId, { worktreeError: 'Please select a branch for the worktree' });
				return;
			}

			// Set loading state - this shows spinner on send button
			updateTab(tabId, { isCreatingWorktree: true, worktreeError: null });

			try {
				// Create worktree session via API
				// Note: api.post already prepends /api/v1, so we use relative path
				const response = await api.post<{
					session_id: string;
					worktree_id: string;
					branch_name: string;
					base_branch: string;
					worktree_path: string;
					status: string;
				}>('/sessions/with-worktree', {
					project_id: tab.project,
					branch_name: tab.worktreeBranch,
					base_branch: tab.worktreeBaseBranch || 'main',
					profile_id: tab.profile,
					create_new_branch: tab.worktreeCreateNewBranch
				});

				// Update tab with new session ID and clear loading state
				updateTab(tabId, { sessionId: response.session_id, isCreatingWorktree: false });

				// Add system message about worktree creation
				const systemMsgId = `system-${Date.now()}`;
				update(s => ({
					...s,
					tabs: s.tabs.map(t => {
						if (t.id !== tabId) return t;
						return {
							...t,
							messages: [...t.messages, {
								id: systemMsgId,
								role: 'system' as const,
								content: `Branch: \`${response.branch_name}\` (based on \`${response.base_branch}\`)\n\nPath: \`${response.worktree_path}\``,
								type: 'system' as const,
								systemSubtype: 'worktree_created',
								systemData: {
									branch_name: response.branch_name,
									base_branch: response.base_branch,
									worktree_path: response.worktree_path,
									worktree_id: response.worktree_id
								}
							}]
						};
					})
				}));

				// Reconnect WebSocket with new session
				disconnectTab(tabId);
				connectTab(tabId);

				// Wait a moment for WebSocket to connect, then send the message
				setTimeout(() => {
					this.sendMessage(tabId, prompt);
				}, 500);

			} catch (error) {
				console.error('Failed to create worktree session:', error);
				// API client throws { detail: string, status: number } on error
				const err = error as { detail?: string; message?: string; status?: number };
				const rawError = err.detail || err.message || 'Unknown error';
				console.error('Worktree error detail:', rawError);

				// Parse and format user-friendly error message
				const friendlyError = formatWorktreeError(rawError, tab.worktreeBranch);

				// Clear loading state and set error
				updateTab(tabId, {
					isCreatingWorktree: false,
					worktreeError: friendlyError
				});
			}
		},

		/**
		 * Start a new session on an existing worktree
		 * Called on first message when worktreeMode is 'existing'
		 * The session will be created via WebSocket with worktree_id linked
		 */
		async startSessionOnExistingWorktree(tabId: string, prompt: string) {
			const tab = getTab(tabId);
			if (!tab) return;

			// Validate required fields
			if (!tab.profile) {
				updateTab(tabId, { error: 'Please select a profile before starting a session' });
				return;
			}
			if (!tab.project) {
				updateTab(tabId, { error: 'Please select a project before starting a session' });
				return;
			}
			if (!tab.worktreeId) {
				updateTab(tabId, { error: 'Please select an existing worktree' });
				return;
			}

			const ws = tabConnections.get(tabId);
			if (!ws || ws.readyState !== WebSocket.OPEN) {
				updateTab(tabId, { error: 'Not connected. Please try again.' });
				return;
			}

			// Add system message about using existing worktree
			const branchDisplay = tab.selectedWorktreeBranch || 'existing branch';
			const systemMsgId = `system-${Date.now()}`;
			update(s => ({
				...s,
				tabs: s.tabs.map(t => {
					if (t.id !== tabId) return t;
					return {
						...t,
						messages: [...t.messages, {
							id: systemMsgId,
							role: 'system' as const,
							content: `Using existing worktree on branch \`${branchDisplay}\``,
							type: 'system' as const,
							systemSubtype: 'worktree_selected',
							systemData: {
								branch_name: branchDisplay,
								worktree_id: tab.worktreeId
							}
						}]
					};
				})
			}));

			// Add user message
			const userMsgId = `user-${Date.now()}`;
			const isFirstMessage = tab.title === 'New Chat';
			const newTitle = isFirstMessage
				? prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '')
				: tab.title;

			update(s => ({
				...s,
				tabs: s.tabs.map(t => {
					if (t.id !== tabId) return t;
					return {
						...t,
						messages: [...t.messages, {
							id: userMsgId,
							role: 'user' as const,
							content: prompt
						}],
						isStreaming: true,
						error: null,
						title: newTitle
					};
				})
			}));

			// Update the deck card title if this is the first message
			if (isFirstMessage) {
				const deckCard = deck.findCardByMeta('tabId', tabId);
				if (deckCard) {
					deck.setCardTitle(deckCard.id, newTitle);
				}
			}

			// Build overrides object only if there are actual overrides
			const overrides: Record<string, string> = {};
			if (tab.modelOverride) {
				overrides.model = tab.modelOverride;
			}
			if (tab.permissionModeOverride) {
				overrides.permission_mode = tab.permissionModeOverride;
			}
			if (tab.thinkingModeOverride) {
				overrides.thinking_mode = tab.thinkingModeOverride;
			}

			// Send query with worktree_id - backend will create session linked to this worktree
			ws.send(JSON.stringify({
				type: 'query',
				prompt,
				profile: tab.profile,
				project: tab.project,
				worktree_id: tab.worktreeId,  // Link to existing worktree
				overrides: Object.keys(overrides).length > 0 ? overrides : undefined
			}));
		},

		/**
		 * Stop generation in a specific tab
		 *
		 * We send a stop message and set a fallback timeout. If the backend
		 * confirms with 'stopped', the timeout is cleared. If no confirmation
		 * arrives within the timeout, we force the UI to recover to prevent freezing.
		 */
		stopGeneration(tabId: string) {
			const tab = getTab(tabId);
			if (!tab) return;

			const ws = tabConnections.get(tabId);
			if (ws?.readyState === WebSocket.OPEN && tab.sessionId) {
				ws.send(JSON.stringify({
					type: 'stop',
					session_id: tab.sessionId
				}));
			}

			// Set a fallback timeout to force UI recovery if 'stopped' message never arrives
			// This prevents the UI from freezing indefinitely if the backend fails to respond
			const existingTimeout = stopTimeouts.get(tabId);
			if (existingTimeout) {
				clearTimeout(existingTimeout);
			}

			const timeoutId = setTimeout(() => {
				const currentTab = getTab(tabId);
				if (currentTab?.isStreaming) {
					console.warn(`[Tab ${tabId}] Stop confirmation timeout - forcing UI recovery`);
					update(s => ({
						...s,
						tabs: s.tabs.map(t => {
							if (t.id !== tabId) return t;

							let messages = t.messages.map(m => {
								// Mark running tools as error (interrupted)
								if (m.type === 'tool_use' && m.toolStatus === 'running') {
									return { ...m, streaming: false, toolStatus: 'error' as const };
								}
								return m.streaming ? { ...m, streaming: false } : m;
							});
							// Remove empty assistant messages
							messages = messages.filter(
								m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
							);

							return { ...t, messages, isStreaming: false };
						})
					}));
				}
				stopTimeouts.delete(tabId);
			}, 10000); // 10 second timeout (generous fallback in case backend confirmation is delayed)

			stopTimeouts.set(tabId, timeoutId);
		},

		/**
		 * Load a session into a specific tab
		 */
		async loadSessionInTab(tabId: string, sessionId: string) {
			try {
				const session = await api.get<Session & { messages: Array<Record<string, unknown>> }>(`/sessions/${sessionId}`);
				const messages: ChatMessage[] = session.messages.map((m, i) => {
					// Determine message type - JSONL messages have explicit 'type' field
					let msgType: MessageType | undefined = m.type as MessageType | undefined;

					// Legacy DB format: infer type from role
					if (!msgType && m.role === 'assistant') {
						msgType = 'text';
					} else if (!msgType && (m.role === 'tool_use' || m.role === 'tool_result')) {
						msgType = m.role as MessageType;
					}

					// Handle system messages (e.g., /context, /compact output)
					if (msgType === 'system' || m.role === 'system') {
						return {
							id: String(m.id || `msg-${i}`),
							jsonlUuid: m.uuid as string | undefined,  // Raw UUID from JSONL for rewind
							role: 'system' as const,
							content: m.content as string,
							type: 'system' as const,
							systemSubtype: m.subtype as string | undefined,
							systemData: (m.systemData as Record<string, unknown>) || { content: m.content },
							metadata: m.metadata as Record<string, unknown>,
							streaming: false
						};
					}

					// Build the chat message with all fields
					const chatMessage: ChatMessage = {
						id: String(m.id || `msg-${i}`),
						jsonlUuid: m.uuid as string | undefined,  // Raw UUID from JSONL for rewind
						role: (m.role === 'tool_use' || m.role === 'tool_result' ? 'assistant' : m.role) as 'user' | 'assistant',
						content: m.content as string,
						type: msgType,
						toolName: (m.toolName || m.tool_name) as string | undefined,
						toolId: m.toolId as string | undefined,
						toolInput: (m.toolInput || m.tool_input) as Record<string, unknown> | undefined,
						toolResult: m.toolResult as string | undefined,
						toolStatus: m.toolStatus as 'running' | 'complete' | 'error' | undefined,
						metadata: m.metadata as Record<string, unknown>,
						streaming: false
					};

					// Add subagent-specific fields if present
					if (msgType === 'subagent') {
						chatMessage.agentId = m.agentId as string | undefined;
						chatMessage.agentType = m.agentType as string | undefined;
						chatMessage.agentDescription = m.agentDescription as string | undefined;
						chatMessage.agentStatus = m.agentStatus as 'pending' | 'running' | 'completed' | 'error' | undefined;
						chatMessage.agentChildren = m.agentChildren as SubagentChildMessage[] | undefined;
					}

					return chatMessage;
				});

				// Generate title from first user message
				let title = 'Chat';
				const firstUserMsg = messages.find(m => m.role === 'user');
				if (firstUserMsg) {
					title = firstUserMsg.content.substring(0, 100) + (firstUserMsg.content.length > 100 ? '...' : '');
				}

				// Preserve existing message IDs where possible to prevent Svelte component recreation
				// This keeps UI state (like ThinkingBlock expanded/collapsed) stable
				const currentTab = getTab(tabId);
				const existingMessages = currentTab?.messages || [];
				const messagesWithPreservedIds = messages.map((msg, i) => {
					const existingMsg = existingMessages[i];
					const canPreserveId = existingMsg &&
						existingMsg.type === msg.type &&
						existingMsg.role === msg.role;
					// Map snake_case profile_id to camelCase profileId for frontend interface
					// API returns snake_case, but our ChatMessage interface uses camelCase
					// Also preserve profileId from existing message if API doesn't have it
					// (JSONL and DB have different message granularity, so index matching may fail)
					const apiMsg = msg as ChatMessage & { profile_id?: string };
					const profileId = apiMsg.profile_id || apiMsg.profileId || (canPreserveId ? existingMsg.profileId : undefined);
					return canPreserveId
						? { ...msg, id: existingMsg.id, profileId }
						: { ...msg, profileId };
				});

				// Load token totals from session history - single source of truth
				// Note: Don't reset profile to session.profile_id - preserve user's current selection
				// sessionProfileId is for fallback display only (legacy messages without per-message profileId)
				const tabUpdate: Partial<ChatTab> = {
					sessionId: session.id,
					messages: messagesWithPreservedIds,
					title,
					error: null,
					totalTokensIn: session.total_tokens_in || 0,
					totalTokensOut: session.total_tokens_out || 0,
					totalCacheCreationTokens: session.cache_creation_tokens || 0,
					totalCacheReadTokens: session.cache_read_tokens || 0,
					// profile: session.profile_id,  // REMOVED: Don't reset user's current selection
					sessionProfileId: session.profile_id,  // Fallback for legacy messages without profileId
					project: session.project_id || ''
				};

				// Restore worktree state if session was created in worktree mode
				const worktreeId = (session as unknown as Record<string, unknown>).worktree_id as string | undefined;
				const worktreeBranch = (session as unknown as Record<string, unknown>).worktree_branch as string | undefined;
				const worktreeDeletedFlag = (session as unknown as Record<string, unknown>).worktree_deleted as boolean | undefined;
				if (worktreeId && worktreeBranch) {
					// Active worktree
					tabUpdate.executionMode = 'worktree';
					tabUpdate.worktreeId = worktreeId;
					tabUpdate.worktreeBranch = worktreeBranch;
					tabUpdate.selectedWorktreeBranch = worktreeBranch;
					tabUpdate.worktreeMode = 'existing';
					tabUpdate.worktreeDeleted = false;
				} else if (worktreeDeletedFlag && worktreeBranch) {
					// Worktree was cleaned up - restore display info but mark as read-only
					tabUpdate.executionMode = 'worktree';
					tabUpdate.worktreeId = null;
					tabUpdate.worktreeBranch = worktreeBranch;
					tabUpdate.selectedWorktreeBranch = worktreeBranch;
					tabUpdate.worktreeMode = 'existing';
					tabUpdate.worktreeDeleted = true;
				}

				updateTab(tabId, tabUpdate);

				// Save tabs state (debounced)
				saveTabsToServer(get({ subscribe }));

				return true;
			} catch (e: unknown) {
				const error = e as { detail?: string };
				updateTab(tabId, { error: error.detail || 'Failed to load session' });
				return false;
			}
		},

		/**
		 * Force takeover a session that is locked by another device
		 */
		forceLoadSessionInTab(tabId: string, sessionId: string) {
			const ws = tabConnections.get(tabId);
			if (ws && ws.readyState === WebSocket.OPEN) {
				// Clear the error and lock state
				updateTab(tabId, {
					error: null,
					sessionLocked: false,
					sessionLockedBy: undefined
				});

				// Set expected session for race condition prevention
				tabExpectedSessionId.set(tabId, sessionId);
				tabsWaitingForHistory.set(tabId, []);

				// Send load_session with force=true to take over
				ws.send(JSON.stringify({
					type: 'load_session',
					session_id: sessionId,
					force: true
				}));
			}
		},

		/**
		 * Set profile for a tab
		 * @param tabId - The tab to update
		 * @param profileId - The profile ID to set
		 * @param persistAsDefault - If true, saves as default selection for new chat cards (default: true)
		 *                           Set to false when setting profile for non-chat-card purposes (e.g., ConversationCard)
		 */
		setTabProfile(tabId: string, profileId: string, persistAsDefault: boolean = true) {
			updateTab(tabId, { profile: profileId });
			// Save tabs state (debounced) to persist the profile selection
			saveTabsToServer(get({ subscribe }));
			// Also save as default selection for new tabs (cross-device persistence)
			if (persistAsDefault) {
				const tab = getTab(tabId);
				if (tab) {
					saveSelectionToBackend(profileId, tab.project);
				}
			}
		},

		/**
		 * Set project for a tab
		 * @param tabId - The tab to update
		 * @param projectId - The project ID to set
		 * @param persistAsDefault - If true, saves as default selection for new chat cards (default: true)
		 *                           Set to false when setting project for non-chat-card purposes (e.g., ConversationCard)
		 */
		setTabProject(tabId: string, projectId: string, persistAsDefault: boolean = true) {
			const tab = getTab(tabId);

			// Prevent changing project after first message in local sessions
			// Once a session starts, the project is locked for the duration
			if (tab && tab.executionMode === 'local' && tab.messages.length > 0) {
				console.warn(`Cannot change project after session has started (tab: ${tabId})`);
				return;
			}

			updateTab(tabId, { project: projectId });
			// Save tabs state (debounced) to persist the project selection
			saveTabsToServer(get({ subscribe }));
			// Also save as default selection for new tabs (cross-device persistence)
			if (persistAsDefault) {
				const tab = getTab(tabId);
				if (tab) {
					saveSelectionToBackend(tab.profile, projectId);
				}
			}
		},

		/**
		 * Set model override for a tab (null = use profile default)
		 */
		setTabModelOverride(tabId: string, model: string | null) {
			updateTab(tabId, { modelOverride: model });
			// Save tabs state (debounced) to persist the override
			saveTabsToServer(get({ subscribe }));
		},

		/**
		 * Set permission mode override for a tab (null = use profile default)
		 */
		setTabPermissionModeOverride(tabId: string, permissionMode: string | null) {
			updateTab(tabId, { permissionModeOverride: permissionMode });
			// Save tabs state (debounced) to persist the override
			saveTabsToServer(get({ subscribe }));
		},

		/**
		 * Set thinking mode override for a tab (null = use profile default)
		 */
		setTabThinkingModeOverride(tabId: string, thinkingMode: string | null) {
			updateTab(tabId, { thinkingModeOverride: thinkingMode });
			// Save tabs state (debounced) to persist the override
			saveTabsToServer(get({ subscribe }));
		},

		/**
		 * Set draft input text for a tab (preserved when switching cards)
		 */
		setTabDraft(tabId: string, draft: string) {
			updateTab(tabId, { draft });
			// Don't save to server - drafts are ephemeral
		},

		/**
		 * Set scroll position for a tab (preserved when switching cards)
		 */
		setTabScrollTop(tabId: string, scrollTop: number) {
			updateTab(tabId, { scrollTop });
			// Don't save to server - scroll position is ephemeral
		},

		/**
		 * Set execution mode for a tab
		 */
		setTabExecutionMode(tabId: string, mode: ExecutionMode) {
			updateTab(tabId, { executionMode: mode });
		},

		/**
		 * Set worktree branch name for a tab
		 */
		setTabWorktreeBranch(tabId: string, branch: string) {
			updateTab(tabId, { worktreeBranch: branch });
		},

		/**
		 * Set worktree base branch for a tab
		 */
		setTabWorktreeBaseBranch(tabId: string, baseBranch: string) {
			updateTab(tabId, { worktreeBaseBranch: baseBranch });
		},

		/**
		 * Set whether to create a new branch (vs using existing)
		 */
		setTabWorktreeCreateNewBranch(tabId: string, createNew: boolean) {
			updateTab(tabId, { worktreeCreateNewBranch: createNew });
		},

		/**
		 * Set worktree ID for a tab (for selecting existing worktrees)
		 */
		setTabWorktreeId(tabId: string, worktreeId: string | null) {
			updateTab(tabId, { worktreeId });
		},

		/**
		 * Set worktree mode for a tab (new or existing)
		 */
		setTabWorktreeMode(tabId: string, mode: 'new' | 'existing') {
			updateTab(tabId, { worktreeMode: mode });
		},

		/**
		 * Select an existing worktree for a tab
		 * Sets both the worktree ID, branch name, and mode
		 */
		selectExistingWorktree(tabId: string, worktreeId: string, branchName: string) {
			updateTab(tabId, {
				worktreeId,
				selectedWorktreeBranch: branchName,
				worktreeMode: 'existing'
			});
		},

		/**
		 * Clear existing worktree selection and switch to new worktree mode
		 */
		clearWorktreeSelection(tabId: string) {
			updateTab(tabId, {
				worktreeId: null,
				selectedWorktreeBranch: null,
				worktreeMode: 'new',
				worktreeError: null  // Clear any worktree error when changing selection
			});
		},

		/**
		 * Clear worktree-specific error
		 */
		clearWorktreeError(tabId: string) {
			updateTab(tabId, { worktreeError: null });
		},

		/**
		 * Set worktree-specific error
		 */
		setWorktreeError(tabId: string, error: string) {
			updateTab(tabId, { worktreeError: error });
		},

		/**
		 * Update a tab's state (exposed for direct updates like todos)
		 */
		updateTab(tabId: string, updates: Partial<ChatTab>) {
			updateTab(tabId, updates);
		},

		/**
		 * Set error for a tab
		 */
		setTabError(tabId: string, error: string) {
			updateTab(tabId, { error });
		},

		/**
		 * Clear error for a tab
		 */
		clearTabError(tabId: string) {
			updateTab(tabId, { error: null });
		},

		/**
		 * Start new chat in current tab
		 */
		startNewChatInTab(tabId: string) {
			// Disconnect and reconnect to reset session
			disconnectTab(tabId);
			updateTab(tabId, {
				sessionId: null,
				sessionProfileId: null,  // Reset profile lock for new session
				currentQueryProfileId: null,  // Reset query profile
				messages: [],
				isStreaming: false,
				error: null,
				title: 'New Chat',
				totalTokensIn: 0,
				totalTokensOut: 0,
				totalCacheCreationTokens: 0,
				totalCacheReadTokens: 0,
				contextUsed: null,
				contextMax: 200000,
				modelOverride: null,
				permissionModeOverride: null,
				thinkingModeOverride: null,
				pendingPermissions: [],
				pendingQuestions: [],
				todos: [],
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
				sessionLockedBy: undefined
			});
			connectTab(tabId);
			// Save tabs state (debounced)
			saveTabsToServer(get({ subscribe }));
		},

		// Data loading functions
		async loadProfiles() {
			try {
				const profiles = await api.get<Profile[]>('/profiles');
				update(s => ({ ...s, profiles }));
			} catch (e) {
				console.error('Failed to load profiles:', e);
			}
		},

		async loadProjects() {
			try {
				const projects = await api.get<Project[]>('/projects');
				update(s => ({ ...s, projects }));
			} catch (e) {
				console.error('Failed to load projects:', e);
			}
		},

		async loadSessions(tagId?: string | null) {
			await loadSessionsInternal(tagId);
		},

		async loadMoreSessions() {
			await loadMoreSessionsInternal();
		},

		async loadAdminSessions(apiUserId?: string | null, tagId?: string | null) {
			await loadAdminSessionsInternal(apiUserId, tagId);
		},

		async loadApiUsers() {
			await loadApiUsersInternal();
		},

		setAdminSessionsFilter(apiUserId: string | null) {
			update(s => ({ ...s, adminSessionsFilter: apiUserId }));
			loadAdminSessionsInternal(apiUserId);
		},

		setSessionsTagFilter(tagId: string | null) {
			update(s => ({ ...s, sessionsTagFilter: tagId }));
			loadSessionsInternal(tagId);
		},

		setAdminSessionsTagFilter(tagId: string | null, apiUserId?: string | null) {
			update(s => ({ ...s, adminSessionsTagFilter: tagId }));
			loadAdminSessionsInternal(apiUserId, tagId);
		},

		// Search functions
		async searchSessions(query: string) {
			await searchSessionsInternal(query);
		},

		async loadMoreSearchResults() {
			await loadMoreSearchResultsInternal();
		},

		clearSearch() {
			clearSearchInternal();
		},

		// Profile/Project CRUD operations
		async createProfile(data: { id: string; name: string; description?: string; config: Record<string, unknown> }) {
			await api.post('/profiles', data);
			await this.loadProfiles();
		},

		async updateProfile(profileId: string, data: { name: string; description?: string; config: Record<string, unknown> }) {
			await api.put(`/profiles/${profileId}`, data);
			await this.loadProfiles();
		},

		async deleteProfile(profileId: string) {
			await api.delete(`/profiles/${profileId}`);
			await this.loadProfiles();
		},

		async createProject(data: { id: string; name: string; description?: string; path?: string }) {
			await api.post('/projects', data);
			await this.loadProjects();
		},

		async deleteProject(projectId: string) {
			await api.delete(`/projects/${projectId}`);
			await this.loadProjects();
		},

		async deleteSession(sessionId: string) {
			await api.delete(`/sessions/${sessionId}`);
			await this.loadSessions();

			// Clear any tabs that had this session
			update(s => ({
				...s,
				tabs: s.tabs.map(tab =>
					tab.sessionId === sessionId
						? { ...tab, sessionId: null, sessionProfileId: null, currentQueryProfileId: null, messages: [], title: 'New Chat' }
						: tab
				)
			}));
		},

		// Selection mode methods
		toggleSelectionMode(isAdmin: boolean = false) {
			update(s => {
				if (isAdmin) {
					return {
						...s,
						adminSelectionMode: !s.adminSelectionMode,
						selectedAdminSessionIds: new Set<string>()
					};
				}
				return {
					...s,
					selectionMode: !s.selectionMode,
					selectedSessionIds: new Set<string>()
				};
			});
		},

		exitSelectionMode(isAdmin: boolean = false) {
			update(s => {
				if (isAdmin) {
					return {
						...s,
						adminSelectionMode: false,
						selectedAdminSessionIds: new Set<string>()
					};
				}
				return {
					...s,
					selectionMode: false,
					selectedSessionIds: new Set<string>()
				};
			});
		},

		toggleSessionSelection(sessionId: string, isAdmin: boolean = false) {
			update(s => {
				if (isAdmin) {
					const newSet = new Set(s.selectedAdminSessionIds);
					if (newSet.has(sessionId)) {
						newSet.delete(sessionId);
					} else {
						newSet.add(sessionId);
					}
					return { ...s, selectedAdminSessionIds: newSet };
				}
				const newSet = new Set(s.selectedSessionIds);
				if (newSet.has(sessionId)) {
					newSet.delete(sessionId);
				} else {
					newSet.add(sessionId);
				}
				return { ...s, selectedSessionIds: newSet };
			});
		},

		selectAllSessions(isAdmin: boolean = false) {
			update(s => {
				if (isAdmin) {
					const allIds = new Set(s.adminSessions.map(session => session.id));
					return { ...s, selectedAdminSessionIds: allIds };
				}
				const allIds = new Set(s.sessions.map(session => session.id));
				return { ...s, selectedSessionIds: allIds };
			});
		},

		deselectAllSessions(isAdmin: boolean = false) {
			update(s => {
				if (isAdmin) {
					return { ...s, selectedAdminSessionIds: new Set<string>() };
				}
				return { ...s, selectedSessionIds: new Set<string>() };
			});
		},

		async deleteSelectedSessions(isAdmin: boolean = false) {
			const state = get({ subscribe });
			const sessionIds = isAdmin
				? Array.from(state.selectedAdminSessionIds)
				: Array.from(state.selectedSessionIds);

			if (sessionIds.length === 0) return;

			// Use batch delete endpoint
			await api.post('/sessions/batch-delete', { session_ids: sessionIds });

			// Reload sessions
			await this.loadSessions();
			if (isAdmin) {
				await loadAdminSessionsInternal(state.adminSessionsFilter);
			}

			// Clear any tabs that had deleted sessions
			update(s => ({
				...s,
				tabs: s.tabs.map(tab =>
					sessionIds.includes(tab.sessionId || '')
						? { ...tab, sessionId: null, sessionProfileId: null, currentQueryProfileId: null, messages: [], title: 'New Chat' }
						: tab
				),
				// Exit selection mode and clear selections
				selectionMode: isAdmin ? s.selectionMode : false,
				adminSelectionMode: isAdmin ? false : s.adminSelectionMode,
				selectedSessionIds: isAdmin ? s.selectedSessionIds : new Set<string>(),
				selectedAdminSessionIds: isAdmin ? new Set<string>() : s.selectedAdminSessionIds
			}));
		},

		/**
		 * Send permission response for a tool request
		 */
		sendPermissionResponse(
			tabId: string,
			requestId: string,
			decision: 'allow' | 'deny',
			remember?: 'none' | 'session' | 'profile',
			pattern?: string
		) {
			const ws = tabConnections.get(tabId);
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({
					type: 'permission_response',
					request_id: requestId,
					decision,
					remember: remember || 'none',
					pattern
				}));
			} else {
				console.error(`[Tab ${tabId}] Cannot send permission response: WebSocket not connected`);
			}
		},

		/**
		 * Get pending permissions for a tab
		 */
		getPendingPermissions(tabId: string): PermissionRequestType[] {
			const state = get({ subscribe });
			const tab = state.tabs.find(t => t.id === tabId);
			return tab?.pendingPermissions || [];
		},

		/**
		 * Request sync of pending permissions from server
		 */
		syncPendingPermissions(tabId: string) {
			const ws = tabConnections.get(tabId);
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'get_pending_permissions' }));
			}
		},

		/**
		 * Send user question response for an AskUserQuestion tool call
		 */
		sendUserQuestionResponse(
			tabId: string,
			requestId: string,
			toolUseId: string,
			answers: Record<string, string | string[]>
		) {
			const ws = tabConnections.get(tabId);
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({
					type: 'user_question_response',
					request_id: requestId,
					tool_use_id: toolUseId,
					answers
				}));
			} else {
				console.error(`[Tab ${tabId}] Cannot send user question response: WebSocket not connected`);
			}
		},

		/**
		 * Get pending user questions for a tab
		 */
		getPendingQuestions(tabId: string): UserQuestionRequest[] {
			const state = get({ subscribe });
			const tab = state.tabs.find(t => t.id === tabId);
			return tab?.pendingQuestions || [];
		},

		/**
		 * Request sync of pending questions from server
		 */
		syncPendingQuestions(tabId: string) {
			const ws = tabConnections.get(tabId);
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'get_pending_questions' }));
			}
		},

		/**
		 * Add a system message to a tab's message list
		 * Used for displaying warnings, notifications, etc.
		 */
		addSystemMessage(tabId: string, content: string, subtype?: string) {
			update(s => ({
				...s,
				tabs: s.tabs.map(t => {
					if (t.id !== tabId) return t;
					return {
						...t,
						messages: [...t.messages, {
							id: `system-${Date.now()}`,
							role: 'system' as const,
							content,
							type: 'system' as MessageType,
							systemSubtype: subtype
						}]
					};
				})
			}));
		}
	};
}

export const tabs = createTabsStore();

// Derived stores for convenience
export const allTabs = derived(tabs, $tabs => $tabs.tabs);
export const activeTabId = derived(tabs, $tabs => $tabs.activeTabId);
export const activeTab = derived(tabs, $tabs => $tabs.tabs.find(t => t.id === $tabs.activeTabId));
export const profiles = derived(tabs, $tabs => $tabs.profiles);
export const projects = derived(tabs, $tabs => $tabs.projects);
export const sessions = derived(tabs, $tabs => $tabs.sessions);
export const adminSessions = derived(tabs, $tabs => $tabs.adminSessions);
export const apiUsers = derived(tabs, $tabs => $tabs.apiUsers);
export const adminSessionsFilter = derived(tabs, $tabs => $tabs.adminSessionsFilter);
// Selection state derived stores
export const selectedSessionIds = derived(tabs, $tabs => $tabs.selectedSessionIds);
export const selectedAdminSessionIds = derived(tabs, $tabs => $tabs.selectedAdminSessionIds);
export const selectionMode = derived(tabs, $tabs => $tabs.selectionMode);
export const adminSelectionMode = derived(tabs, $tabs => $tabs.adminSelectionMode);
// Loading state
export const sessionsLoading = derived(tabs, $tabs => $tabs.sessionsLoading);
// Tag filter state
export const sessionsTagFilter = derived(tabs, $tabs => $tabs.sessionsTagFilter);
export const adminSessionsTagFilter = derived(tabs, $tabs => $tabs.adminSessionsTagFilter);
