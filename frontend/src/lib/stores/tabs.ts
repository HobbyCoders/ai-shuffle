/**
 * Tabbed Chat Store - Supports multiple concurrent chat sessions
 *
 * Each tab has its own WebSocket connection and independent state.
 * Tabs can run queries simultaneously without interfering with each other.
 */

import { writable, derived, get } from 'svelte/store';
import type { Session, Profile, PermissionRequest as PermissionRequestType, UserQuestionRequest, PaginatedSessions, SessionSearchResult, PaginatedSessionSearchResults } from '$lib/api/client';
import { api, searchSessionsPaginated } from '$lib/api/client';
import { isNotificationsEnabled, notifySessionComplete, notifyWaitingForResponse, getPermission, type SessionCompleteInfo } from '$lib/services/notifications';
import { deck } from './deck';
import { getDeviceIdSync } from './device';
import {
	initStreamingBuffer,
	pushEvent,
	flushNow,
	clearBuffer,
	removeBuffer,
	findLastStreamingTextIndex,
	findLastStreamingThinkingIndex,
	invalidateStreamingIndexCache,
	appendToStreamingText,
	appendToStreamingThinking,
	appendToToolInput,
	hasPendingData,
	type BatchedUpdate,
	type BufferedEvent
} from './streaming-buffer';

export interface ApiUser {
	id: string;
	name: string;
	description?: string;
	project_id?: string;
	profile_ids?: string[];
	is_active: boolean;
	web_login_allowed?: boolean;
	username?: string;
	created_at: string;
	updated_at: string;
	last_used_at?: string;
}

export type MessageType = 'text' | 'thinking' | 'tool_use' | 'tool_result' | 'system' | 'subagent';

// Subagent child message - represents a single tool call or text from a subagent
export interface SubagentChildMessage {
	id: string;
	type: 'text' | 'tool_use' | 'tool_result';
	content: string;
	toolName?: string;
	toolId?: string;
	toolInput?: Record<string, unknown>;
	toolResult?: string; // Result content for tool_use (grouped with tool call)
	toolStatus?: 'running' | 'complete' | 'error'; // Status of tool execution
	timestamp?: string;
}

// Todo item for task tracking
export interface TodoItem {
	content: string;
	status: 'pending' | 'in_progress' | 'completed';
	activeForm: string; // Present continuous form shown when in_progress
}

export interface ChatMessage {
	id: string;
	jsonlUuid?: string; // Raw UUID from JSONL file - used for rewind (id may be preserved for Svelte keying)
	role: 'user' | 'assistant' | 'system';
	content: string;
	type?: MessageType;
	toolName?: string;
	toolId?: string;
	toolInput?: Record<string, unknown>;
	toolResult?: string; // Result content for tool_use messages (grouped with tool call)
	toolStatus?: 'running' | 'complete' | 'error'; // Status of tool execution
	metadata?: Record<string, unknown>;
	streaming?: boolean;
	partialToolInput?: string; // Accumulator for streaming tool input JSON
	systemSubtype?: string; // For system messages (e.g., 'context' for /context command)
	systemData?: Record<string, unknown>; // Raw data from system message
	profileId?: string; // Profile ID that was active when this message was created (for per-query tracking)
	// Subagent-specific fields
	agentId?: string; // The subagent's unique ID (e.g., '00ed8f4d')
	agentType?: string; // The subagent type (e.g., 'Explore', 'Plan')
	agentDescription?: string; // Task description from the Task tool
	agentPrompt?: string; // The initial prompt sent to the subagent
	agentStatus?: 'pending' | 'running' | 'completed' | 'error'; // Current status
	agentChildren?: SubagentChildMessage[]; // Nested messages from subagent execution
}

export interface Project {
	id: string;
	name: string;
	description?: string;
	path: string;
	settings: Record<string, unknown>;
	created_at: string;
	updated_at: string;
}

// Execution mode for chat sessions
export type ExecutionMode = 'local' | 'worktree';

export interface ChatTab {
	id: string;
	title: string;
	sessionId: string | null;
	messages: ChatMessage[];
	isStreaming: boolean;
	wsConnected: boolean;
	error: string | null;
	profile: string;  // Current UI selection for next query
	sessionProfileId: string | null;  // Profile that created the session (for fallback display)
	currentQueryProfileId: string | null;  // Profile for the current streaming query
	project: string;
	totalTokensIn: number;
	totalTokensOut: number;
	totalCacheCreationTokens: number;
	totalCacheReadTokens: number;
	// Actual context window usage from /context command (source of truth)
	contextUsed: number | null;
	contextMax: number;
	// Session overrides (override profile settings for this session)
	modelOverride: string | null;  // null = use profile default
	permissionModeOverride: string | null;  // null = use profile default
	thinkingModeOverride: string | null;  // null = use profile default
	// Permission requests queue
	pendingPermissions: PermissionRequestType[];
	// User questions queue (for AskUserQuestion tool)
	pendingQuestions: UserQuestionRequest[];
	// Todo list for task tracking
	todos: TodoItem[];
	// Draft input text (preserved when switching cards)
	draft: string;
	// Scroll position (preserved when switching cards)
	scrollTop: number;
	// Stream start time for duration tracking (notifications)
	streamStartTime: number | null;
	// Execution mode settings (per-tab)
	executionMode: ExecutionMode;
	worktreeBranch: string;      // The new branch name for worktree mode
	worktreeBaseBranch: string;  // Branch to base from (and PR target)
	// New worktree model: existing worktree selection
	worktreeId: string | null;   // Selected existing worktree ID (null = create new)
	worktreeMode: 'new' | 'existing';  // Whether creating new or using existing worktree
	selectedWorktreeBranch: string | null;  // Branch name of selected existing worktree (for display)
	// Worktree creation state (for deferred creation on message send)
	isCreatingWorktree: boolean;  // True while creating worktree before first message
	worktreeError: string | null; // Specific error for worktree creation
	// Session lock state (multi-device conflict)
	sessionLocked: boolean;      // True if session is locked by another device
	sessionLockedBy: string | undefined;  // Device ID that has the lock
}

interface TabsState {
	tabs: ChatTab[];
	activeTabId: string | null;
	profiles: Profile[];
	projects: Project[];
	sessions: Session[];
	adminSessions: Session[];
	apiUsers: ApiUser[];
	adminSessionsFilter: string | null; // null = all, '' = admin only, 'user_id' = specific user
	// Selection state for batch operations
	selectedSessionIds: Set<string>;
	selectedAdminSessionIds: Set<string>;
	selectionMode: boolean;
	adminSelectionMode: boolean;
	// Loading state to prevent race conditions
	sessionsLoading: boolean;
	// Tag filter state
	sessionsTagFilter: string | null; // null = all, 'tag_id' = filter by tag
	adminSessionsTagFilter: string | null;
	// Pagination state for sessions
	sessionsTotal: number;
	sessionsOffset: number;
	sessionsHasMore: boolean;
	sessionsLoadingMore: boolean;
	// Search state
	searchQuery: string;
	searchResults: SessionSearchResult[];
	searchTotal: number;
	searchOffset: number;
	searchHasMore: boolean;
	searchLoading: boolean;
	searchLoadingMore: boolean;
}

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
const STREAMING_HEALTH_TIMEOUT_MS = 15000;
const TOOL_EXECUTION_TIMEOUT_MS = 120000; // 2 minutes for tool execution (Bash, etc. can take a while)

// Track pending tool executions per tab - when tools are running, we expect longer gaps between messages
// This prevents false disconnects when Claude SDK is waiting for tool results
const tabPendingTools: Map<string, Set<string>> = new Map();

// Server-authoritative polling for streaming sessions
// When streaming, poll /sessions/{id}/live for ground truth state
const tabPollingTimers: Map<string, ReturnType<typeof setInterval>> = new Map();
const tabLastUpdateId: Map<string, number> = new Map();
const POLLING_INTERVAL_MS = 500; // Poll every 500ms during streaming

// Interface for persisted tab state (only what we need to restore tabs)
interface PersistedTab {
	id: string;
	title: string;
	sessionId: string | null;
	profile: string;
	project: string;
	modelOverride: string | null;
	permissionModeOverride: string | null;
	thinkingModeOverride: string | null;
}

interface PersistedTabsState {
	tabs: PersistedTab[];
	activeTabId: string | null;
}

// Debounce timer for saving tabs
let saveTabsTimer: ReturnType<typeof setTimeout> | null = null;
const SAVE_DEBOUNCE_MS = 1000;

// Flag to prevent saving during initial load
let isInitializing = false;

/**
 * Format worktree error messages into user-friendly text
 * Parses common git/worktree errors and provides helpful context
 */
function formatWorktreeError(rawError: string, branchName: string): string {
	const lowerError = rawError.toLowerCase();

	// Branch already has a worktree
	if (lowerError.includes('already checked out') || lowerError.includes('is already used by worktree')) {
		return `A worktree already exists for branch '${branchName}'. Select it from Active Worktrees instead.`;
	}

	// Branch doesn't exist
	if (lowerError.includes('not a valid branch') || lowerError.includes('branch not found') ||
	    lowerError.includes('unknown revision') || lowerError.includes('pathspec') && lowerError.includes('did not match')) {
		return `Branch '${branchName}' doesn't exist. Try fetching from remote first or create a new branch.`;
	}

	// Path conflict - folder already exists
	if (lowerError.includes('already exists') && (lowerError.includes('path') || lowerError.includes('folder') || lowerError.includes('directory'))) {
		return `Cannot create worktree: a folder already exists at the target path. Try a different branch name.`;
	}

	// Git lock error
	if (lowerError.includes('.lock') || lowerError.includes('unable to lock') || lowerError.includes('index.lock')) {
		return `Git operation in progress. Please wait a moment and try again.`;
	}

	// Permission errors
	if (lowerError.includes('permission denied') || lowerError.includes('access denied')) {
		return `Permission denied while creating worktree. Check folder permissions.`;
	}

	// Network/fetch errors
	if (lowerError.includes('could not resolve') || lowerError.includes('connection refused') ||
	    lowerError.includes('network') || lowerError.includes('timeout')) {
		return `Network error while creating worktree. Check your connection and try again.`;
	}

	// Generic fallback with original message
	return `Failed to create worktree: ${rawError}`;
}

// Persistent device ID for multi-device sync
// This ensures the same browser/device keeps the same ID across reconnections
function getOrCreateDeviceId(): string {
	if (typeof window === 'undefined') {
		return `device-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
	}

	let deviceId = localStorage.getItem('aihub_device_id');
	if (!deviceId) {
		deviceId = `device-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
		localStorage.setItem('aihub_device_id', deviceId);
		console.log('[Tabs] Generated new device ID:', deviceId);
	}
	return deviceId;
}

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
			console.log('[Tabs] Broadcast tabs_update via WebSocket');
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
					thinkingModeOverride: tab.thinkingModeOverride
				})),
				activeTabId: pendingTabsState.activeTabId
			};

			// Save to backend (REST API for persistence)
			await api.put('/preferences/open_tabs', {
				key: 'open_tabs',
				value: persistedState
			});
			console.log('[Tabs] Saved tabs state to server');

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
			console.log('[Tabs] Loaded tabs state from server:', response.value);
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

function generateTabId(): string {
	return `tab-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

function createTabsStore() {
	// Start with no tabs - will be populated from server or user creates one
	const { subscribe, set, update } = writable<TabsState>({
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
	 * Used to skip polling when WS is working well.
	 * Threshold increased to match STREAMING_HEALTH_TIMEOUT_MS to prevent race conditions
	 * where both polling and WebSocket update state simultaneously.
	 */
	const WEBSOCKET_HEALTHY_THRESHOLD_MS = 10000; // 10 seconds - generous to avoid race conditions

	function isWebSocketHealthy(tabId: string): boolean {
		// First check if WebSocket is actually connected
		const ws = tabConnections.get(tabId);
		if (!ws || ws.readyState !== WebSocket.OPEN) {
			return false;
		}
		// Then check if we've received messages recently
		const lastMessage = tabLastMessageTime.get(tabId);
		if (!lastMessage) return false;
		return Date.now() - lastMessage < WEBSOCKET_HEALTHY_THRESHOLD_MS;
	}

	/**
	 * Process a discrete event from the buffer and apply it to messages array.
	 * This handles non-delta events like stream_block_start, stream_block_stop, etc.
	 */
	function processDiscreteEvent(messages: ChatMessage[], event: BufferedEvent, tab: ChatTab): void {
		const { type, data } = event;

		switch (type) {
			case 'stream_block_start': {
				const blockType = data.block_type as string;
				if (blockType === 'thinking') {
					// Don't finalize existing thinking here - the thinking message will be
					// created/appended by appendToStreamingThinking when deltas arrive.
					// Finalizing here would cause fragmentation when batch contains both
					// thinking deltas and stream_block_start(thinking) in the same batch.
				} else if (blockType === 'text') {
					// When text block starts, finalize any existing streaming thinking message
					const thinkingIdx = findLastStreamingThinkingIndex(messages);
					if (thinkingIdx !== -1) {
						messages[thinkingIdx] = { ...messages[thinkingIdx], streaming: false };
					}
				} else if (blockType === 'tool_use') {
					const contentBlock = data.content_block as { id?: string; name?: string };
					// Handle any existing streaming text message
					const streamingIdx = messages.findLastIndex(
						m => m.type === 'text' && m.role === 'assistant' && m.streaming
					);
					if (streamingIdx !== -1) {
						if (messages[streamingIdx].content) {
							messages[streamingIdx] = { ...messages[streamingIdx], streaming: false };
						} else {
							messages.splice(streamingIdx, 1);
						}
					}
					// Also finalize any streaming thinking message
					const thinkingIdx = findLastStreamingThinkingIndex(messages);
					if (thinkingIdx !== -1) {
						messages[thinkingIdx] = { ...messages[thinkingIdx], streaming: false };
					}
					// Add tool use message with profileId for per-query tracking
					messages.push({
						id: `tool-${Date.now()}-${contentBlock.id || ''}`,
						role: 'assistant',
						content: '',
						type: 'tool_use',
						toolName: contentBlock.name || '',
						toolId: contentBlock.id || '',
						toolInput: {},
						toolStatus: 'running',
						streaming: true,
						profileId: tab.currentQueryProfileId || undefined
					});
				}
				break;
			}

			case 'stream_block_stop': {
				// Block complete - finalize streaming text and thinking messages immediately
				// so indicators don't linger while waiting for the next block to start.
				// Tool_use messages are NOT finalized here because they remain "streaming"
				// until the tool_result arrives (tool execution could take time).

				// Finalize streaming text message
				const streamingTextIdx = messages.findLastIndex(
					m => m.type === 'text' && m.role === 'assistant' && m.streaming
				);
				if (streamingTextIdx !== -1) {
					if (messages[streamingTextIdx].content) {
						messages[streamingTextIdx] = { ...messages[streamingTextIdx], streaming: false };
					} else {
						// Remove empty streaming text messages
						messages.splice(streamingTextIdx, 1);
					}
				}

				// Finalize streaming thinking message
				const streamingThinkingIdx = findLastStreamingThinkingIndex(messages);
				if (streamingThinkingIdx !== -1) {
					messages[streamingThinkingIdx] = { ...messages[streamingThinkingIdx], streaming: false };
				}
				break;
			}

			case 'stream_message_delta': {
				// Final message metadata with output token usage
				// This is handled separately via usageUpdate in batch
				break;
			}
		}
	}

	/**
	 * Apply a batched update from the streaming buffer to the store.
	 * This is called by the streaming-buffer module when it flushes.
	 *
	 * IMPORTANT: Discrete events (block_start, block_stop) are processed FIRST
	 * to ensure proper state transitions before deltas are applied. The streaming
	 * buffer ensures that blocks are flushed before transitions, so within a single
	 * batch, all deltas belong to the same block type.
	 */
	function applyBatchedUpdate(tabId: string, batch: BatchedUpdate): void {
		update(s => {
			const tabIndex = s.tabs.findIndex(t => t.id === tabId);
			if (tabIndex === -1) return s;

			const tab = s.tabs[tabIndex];
			const messages = [...tab.messages]; // Single copy per flush

			// CRITICAL: Process discrete events FIRST to handle block transitions
			// This ensures streaming state is properly set before deltas are applied
			for (const event of batch.discreteEvents) {
				processDiscreteEvent(messages, event, tab);
			}

			// Apply all thinking deltas at once (with profileId for per-query tracking)
			if (batch.thinkingDelta) {
				appendToStreamingThinking(messages, batch.thinkingDelta, tab.currentQueryProfileId || undefined);
			}

			// Apply all text deltas at once (with profileId for per-query tracking)
			if (batch.textDelta) {
				appendToStreamingText(messages, batch.textDelta, tab.currentQueryProfileId || undefined);
			}

			// Apply all tool input deltas
			for (const [toolId, delta] of batch.toolInputDeltas) {
				appendToToolInput(messages, toolId, delta);
			}

			// Apply usage updates
			let newTotalTokensIn = tab.totalTokensIn;
			let newTotalTokensOut = tab.totalTokensOut;
			let newCacheCreation = tab.totalCacheCreationTokens;
			let newCacheRead = tab.totalCacheReadTokens;

			if (batch.usageUpdate) {
				if (batch.usageUpdate.inputTokens !== undefined) {
					newTotalTokensIn = batch.usageUpdate.inputTokens;
				}
				if (batch.usageUpdate.outputTokens !== undefined) {
					newTotalTokensOut = batch.usageUpdate.outputTokens;
				}
				if (batch.usageUpdate.cacheCreationTokens !== undefined) {
					newCacheCreation = batch.usageUpdate.cacheCreationTokens;
				}
				if (batch.usageUpdate.cacheReadTokens !== undefined) {
					newCacheRead = batch.usageUpdate.cacheReadTokens;
				}
			}

			// Single new object creation
			const newTabs = [...s.tabs];
			newTabs[tabIndex] = {
				...tab,
				messages,
				totalTokensIn: newTotalTokensIn,
				totalTokensOut: newTotalTokensOut,
				totalCacheCreationTokens: newCacheCreation,
				totalCacheReadTokens: newCacheRead
			};
			return { ...s, tabs: newTabs };
		});
	}

	// Initialize the streaming buffer with our callback
	initStreamingBuffer(applyBatchedUpdate);

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
		console.log(`[Tab ${tabId}] ${isReconnect ? 'Reconnecting' : 'Connecting'} to WebSocket...`);

		const ws = new WebSocket(url);
		tabConnections.set(tabId, ws);

		ws.onopen = () => {
			console.log(`[Tab ${tabId}] WebSocket connected`);
			updateTab(tabId, { wsConnected: true, error: null });

			// Always register with SyncEngine by sending load_session if tab has a session
			// This is critical for multi-device sync - devices must be registered to receive events
			const tab = getTab(tabId);
			if (tab?.sessionId) {
				console.log(`[Tab ${tabId}] Registering with session via load_session:`, tab.sessionId);

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
						console.warn(`[Tab ${tabId}] Connection appears stale (${Math.round(timeSinceLastMessage / 1000)}s since last message, streaming=${currentTab?.isStreaming}, toolsPending=${hasToolsPending ? pendingTools?.size : 0}), forcing reconnect`);
						// Force close to trigger reconnect
						currentWs.close(4000, 'Connection health timeout');
					}
				}
			}, 10000); // Check every 10s instead of 25s for faster detection
			tabPingTimers.set(tabId, pingTimer);
		};

		ws.onclose = (event) => {
			console.log(`[Tab ${tabId}] WebSocket closed:`, event.code, event.reason);

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
				console.log(`[Tab ${tabId}] WebSocket closed during streaming - starting fallback polling`);
				// Start polling if not already polling
				if (!tabPollingTimers.has(tabId)) {
					startPolling(tabId, sessionId);
				}
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

			// Reconnect after delay (unless intentionally closed or tab removed)
			if (event.code !== 1000) {
				const existingReconnect = tabReconnectTimers.get(tabId);
				if (existingReconnect) clearTimeout(existingReconnect);

				const reconnectTimer = setTimeout(() => {
					// Check if tab still exists
					const currentTab = getTab(tabId);
					if (currentTab) {
						console.log(`[Tab ${tabId}] Attempting reconnect...`);
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

		const ws = tabConnections.get(tabId);
		if (ws) {
			ws.close(1000);
			tabConnections.delete(tabId);
		}

		updateTab(tabId, { wsConnected: false });
	}

	/**
	 * Start polling server for live session state.
	 * This is the server-authoritative approach - frontend state is replaced
	 * with server state, no merging required.
	 */
	function startPolling(tabId: string, sessionId: string) {
		// Don't start if already polling
		if (tabPollingTimers.has(tabId)) {
			console.log(`[Tab ${tabId}] Already polling, skipping start`);
			return;
		}

		console.log(`[Tab ${tabId}] Starting server-authoritative polling for session ${sessionId}`);
		tabLastUpdateId.set(tabId, 0);

		const pollLiveState = async () => {
			// Skip polling if WebSocket is actively receiving events
			// This prevents double updates and reduces memory churn
			if (isWebSocketHealthy(tabId)) {
				return;
			}

			const tab = getTab(tabId);
			if (!tab || tab.sessionId !== sessionId) {
				// Tab closed or switched sessions
				stopPolling(tabId);
				return;
			}

			try {
				const lastUpdateId = tabLastUpdateId.get(tabId) || 0;
				const response = await fetch(
					`/api/v1/sessions/${sessionId}/live?since_update_id=${lastUpdateId}`,
					{ credentials: 'include' }
				);

				if (response.status === 304) {
					// No updates since last check
					return;
				}

				if (!response.ok) {
					console.warn(`[Tab ${tabId}] Polling failed: ${response.status}`);
					return;
				}

				const data = await response.json();
				const newUpdateId = data.last_update_id as number;
				tabLastUpdateId.set(tabId, newUpdateId);

				// Convert server messages to ChatMessage format
				// IMPORTANT: Preserve existing message IDs to prevent Svelte component recreation
				// This keeps expanded/collapsed state stable for ThinkingBlocks, etc.
				const serverMessages = (data.messages as Array<Record<string, unknown>>) || [];
				const tab = getTab(tabId);
				const existingMessages = tab?.messages || [];

				const messages: ChatMessage[] = serverMessages.map((m, i) => {
					const serverType = (m.type as MessageType) || 'text';
					const serverRole = (m.role as 'user' | 'assistant' | 'system') || 'assistant';

					// Try to match with existing message by index and type
					// This preserves the frontend's ID so Svelte doesn't recreate the component
					const existingMsg = existingMessages[i];
					const canPreserveId = existingMsg &&
						existingMsg.type === serverType &&
						existingMsg.role === serverRole;

					const msg: ChatMessage = {
						// Preserve existing ID if message matches, otherwise use server ID
						id: canPreserveId ? existingMsg.id : ((m.id as string) || `live-${i}`),
						role: serverRole,
						content: (m.content as string) || '',
						type: serverType,
						streaming: (m.streaming as boolean) || false,
					};

					// Tool fields
					if (m.toolName) msg.toolName = m.toolName as string;
					if (m.toolId) msg.toolId = m.toolId as string;
					if (m.toolInput) msg.toolInput = m.toolInput as Record<string, unknown>;
					if (m.toolResult !== undefined) msg.toolResult = m.toolResult as string;
					if (m.toolStatus) msg.toolStatus = m.toolStatus as 'running' | 'complete' | 'error';

					// Subagent fields
					if (m.agentId) msg.agentId = m.agentId as string;
					if (m.agentType) msg.agentType = m.agentType as string;
					if (m.agentDescription) msg.agentDescription = m.agentDescription as string;
					if (m.agentPrompt) msg.agentPrompt = m.agentPrompt as string;
					if (m.agentStatus) msg.agentStatus = m.agentStatus as 'pending' | 'running' | 'completed' | 'error';
					if (m.agentChildren) msg.agentChildren = m.agentChildren as SubagentChildMessage[];

					// System message fields
					if (m.systemSubtype) msg.systemSubtype = m.systemSubtype as string;
					if (m.systemData) msg.systemData = m.systemData as Record<string, unknown>;

					// Metadata
					if (m.metadata) msg.metadata = m.metadata as Record<string, unknown>;

					return msg;
				});

				// Extract todos from messages (look for TodoWrite)
				let latestTodos: TodoItem[] = [];
				for (const msg of messages) {
					if (msg.type === 'tool_use' && msg.toolName === 'TodoWrite' && msg.toolInput?.todos) {
						const todos = msg.toolInput.todos as TodoItem[];
						if (Array.isArray(todos)) {
							latestTodos = todos.map(todo => ({
								content: todo.content || '',
								status: todo.status || 'pending',
								activeForm: todo.activeForm || todo.content || ''
							}));
						}
					}
				}

				// Extract token usage
				const usage = data.usage as Record<string, number> || {};
				const inputTokens = usage.input_tokens || 0;
				const outputTokens = usage.output_tokens || 0;
				const cacheCreationTokens = usage.cache_creation_tokens || 0;
				const cacheReadTokens = usage.cache_read_tokens || 0;

				// Replace tab state with server state - NO MERGING!
				update(s => ({
					...s,
					tabs: s.tabs.map(t => {
						if (t.id !== tabId) return t;

						// Calculate new context from server usage
						const serverContextUsed = inputTokens + cacheCreationTokens + cacheReadTokens;

						// Don't reset context to 0 if we already have a value and server returns 0
						// This happens when polling starts before stream_start arrives with usage
						const shouldUpdateContext = serverContextUsed > 0 || t.contextUsed === null || t.contextUsed === 0;

						return {
							...t,
							messages,
							isStreaming: data.is_streaming as boolean,
							todos: latestTodos.length > 0 ? latestTodos : t.todos,
							totalTokensIn: shouldUpdateContext ? inputTokens : t.totalTokensIn,
							totalTokensOut: shouldUpdateContext ? outputTokens : t.totalTokensOut,
							totalCacheCreationTokens: shouldUpdateContext ? cacheCreationTokens : t.totalCacheCreationTokens,
							totalCacheReadTokens: shouldUpdateContext ? cacheReadTokens : t.totalCacheReadTokens,
							contextUsed: shouldUpdateContext ? serverContextUsed : t.contextUsed
						};
					})
				}));

				// Flush any pending deltas before clearing buffer (prevents losing characters)
				if (hasPendingData(tabId)) {
					flushNow(tabId);
				}
				// Clear streaming buffer after poll replaces state (avoid stale data)
				clearBuffer(tabId);
				invalidateStreamingIndexCache();

				// Stop polling if streaming ended
				if (!data.is_streaming) {
					console.log(`[Tab ${tabId}] Streaming ended, stopping polling`);
					stopPolling(tabId);

					// Trigger notification if enabled
					const currentTab = getTab(tabId);
					if (currentTab && isNotificationsEnabled() && getPermission() === 'granted') {
						const durationSeconds = currentTab.streamStartTime
							? (Date.now() - currentTab.streamStartTime) / 1000
							: undefined;

						notifySessionComplete({
							sessionId: sessionId,
							sessionTitle: currentTab.title,
							durationSeconds,
							tokensIn: inputTokens,
							tokensOut: outputTokens
						});
					}

					// Refresh sessions list (use internal function since we're inside store creation)
					loadSessionsInternal();
				}

			} catch (e) {
				console.error(`[Tab ${tabId}] Polling error:`, e);
			}
		};

		// Start polling immediately, then on interval
		pollLiveState();
		const timer = setInterval(pollLiveState, POLLING_INTERVAL_MS);
		tabPollingTimers.set(tabId, timer);
	}

	/**
	 * Stop polling for a tab
	 */
	function stopPolling(tabId: string) {
		const timer = tabPollingTimers.get(tabId);
		if (timer) {
			clearInterval(timer);
			tabPollingTimers.delete(tabId);
			tabLastUpdateId.delete(tabId);
			console.log(`[Tab ${tabId}] Stopped polling`);
		}
	}

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
			console.log('[Tabs] Ignoring tabs_sync from our own device');
			return;
		}

		if (!tabs) {
			console.warn('[Tabs] Received tabs_sync with no tabs data');
			return;
		}

		console.log(`[Tabs] Received tabs_sync from device ${sourceDeviceId?.slice(0, 8)}..., ${tabs.length} tabs`);

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
					worktreeId: null,
					worktreeMode: 'new' as const,
					selectedWorktreeBranch: null,
					isCreatingWorktree: false,
					worktreeError: null,
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
			console.log(`[Tabs] Tab ${closedTab.id} was closed on another device, disconnecting`);
			disconnectTab(closedTab.id);
		}

		// Connect WebSocket for new tabs
		const newTabs = mergedTabs.filter(t => !currentTabsMap.has(t.id));
		for (const newTab of newTabs) {
			console.log(`[Tabs] Tab ${newTab.id} was created on another device, connecting`);
			connectTab(newTab.id);
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
			console.log(`[Tab ${tabId}] Queueing sync event (waiting for history):`, eventType);
			queuedEvents.push(data);
			return;
		}

		console.log(`[Tab ${tabId}] Sync event:`, eventType, eventData);

		switch (eventType) {
			case 'message_added': {
				// A new message was added by another device (e.g., user message)
				const message = eventData.message as Record<string, unknown>;
				console.log(`[Tab ${tabId}] Message added from another device:`, message);

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
				console.log(`[Tab ${tabId}] Another device started streaming`);
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

						if (usage) {
							console.log(`[Tab ${tabId}] Sync stream_start with usage (REPLACING):`, {
								inputTokens,
								cacheCreationTokens,
								cacheReadTokens,
								contextUsed: inputTokens + cacheCreationTokens + cacheReadTokens
							});
						}

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
				// Content chunk from another device
				const chunkType = eventData.chunk_type as string;
				const content = eventData.content as string;
				console.log(`[Tab ${tabId}] stream_chunk: type=${chunkType}, content="${content?.substring(0, 50)}..."`);

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
					console.log(`[Tab ${tabId}] tool_input sync: ${partialJson?.length} chars`);

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
					console.log(`[Tab ${tabId}] tool_use sync:`, { toolName, toolId, toolInput });

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
								console.log(`[Tab ${tabId}] TodoWrite: Updated todos`, todos);
							}

							// Check if this tool_use already exists (from history or buffer)
							const existingToolIdx = messages.findIndex(
								m => m.type === 'tool_use' && m.toolId === toolId
							);
							if (existingToolIdx !== -1) {
								// Tool already exists - just update it if needed
								console.log(`[Tab ${tabId}] tool_use already exists, updating:`, toolId);
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
					console.log(`[Tab ${tabId}] tool_result sync:`, {
						tool_id: toolId,
						content: content?.substring(0, 100)
					});
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
									console.log(`[Tab ${tabId}] Attaching result to tool at index ${toolUseIdx}`);
									messages[toolUseIdx] = {
										...messages[toolUseIdx],
										toolResult: content,
										toolStatus: 'complete' as const,
										streaming: false
									};
								} else {
									console.log(`[Tab ${tabId}] Tool already has result, skipping duplicate`);
								}
							} else {
								console.log(`[Tab ${tabId}] No matching tool_use found for result, toolId:`, toolId);
							}

							return { ...t, messages };
						})
					}));
				} else if (chunkType === 'system') {
					// System message from another device
					const subtype = eventData.subtype as string;
					const systemData = eventData.data as Record<string, unknown>;
					console.log(`[Tab ${tabId}] system sync:`, { subtype, systemData });

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
					console.log(`[Tab ${tabId}] subagent_start sync:`, { agentId, agentType, description });

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
					console.log(`[Tab ${tabId}] subagent_done sync:`, { agentId, isError });

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
					console.log(`[Tab ${tabId}] stream_block_start sync (ignored for tool_use):`, { blockType, contentBlock });
					// No action needed - tool_use event will create the message
				} else if (chunkType === 'stream_block_stop') {
					// End of a content block - just log for now
					console.log(`[Tab ${tabId}] stream_block_stop sync:`, { index: eventData.index });
				} else if (chunkType === 'stream_message_delta') {
					// Final message metadata with output token usage (synced from another device)
					// NOTE: These are TOTAL values for the current turn, not increments!
					const usage = eventData.usage as Record<string, number> | undefined;
					if (usage && usage.output_tokens) {
						console.log(`[Tab ${tabId}] Sync stream_message_delta with output usage (REPLACING):`, usage);
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

					console.log(`[Tab ${tabId}] Sync usage_update (REPLACING not adding):`, {
						inputTokens,
						cacheCreationTokens,
						cacheReadTokens,
						contextUsed: newContextUsed
					});

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
				console.log(`[Tab ${tabId}] Another device finished streaming`);
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
								console.log(`[Tab ${tabId}] Reconciling context from stream_end metadata:`, {
									newContextUsed,
									currentContext,
									percentDiff,
									tokensIn,
									cacheCreation,
									cacheRead
								});
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
				console.log(`[Tab ${tabId}] Session rewound by another device:`, { targetUuid, messagesRemoved });

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
				console.log(`[Tab ${tabId}] Session opened by another device:`, { deviceId, isNew });
				// Just log for now - could show a notification or indicator
				break;
			}

			case 'session_closed': {
				// Another device closed this session
				const deviceId = eventData.device_id as string;
				console.log(`[Tab ${tabId}] Session closed by another device:`, { deviceId });
				// Just log for now - could show a notification or indicator
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

		switch (msgType) {
			case 'history': {
				// Validate session_id to prevent race conditions when switching sessions
				const receivedSessionId = data.session_id as string;
				const expectedSessionId = tabExpectedSessionId.get(tabId);
				if (expectedSessionId && receivedSessionId !== expectedSessionId) {
					console.log(`[Tab ${tabId}] Ignoring stale history for session ${receivedSessionId}, expected ${expectedSessionId}`);
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
							systemData: { content: m.content } as Record<string, unknown>,
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
						streaming: false
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

				// Handle streaming state from backend
				const isStreaming = data.isStreaming as boolean || false;
				const streamingBuffer = data.streamingBuffer as Array<Record<string, unknown>> | undefined;
				console.log(`[Tab ${tabId}] History response - isStreaming:`, isStreaming, 'bufferLen:', streamingBuffer?.length || 0);

				// If session is actively streaming and has buffered content, merge it intelligently
				let finalMessages = messages;
				if (isStreaming && streamingBuffer && streamingBuffer.length > 0) {
					console.log(`[Tab ${tabId}] Late-joining streaming session, merging buffer:`, streamingBuffer.length, 'messages');

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
							console.log(`[Tab ${tabId}] Skipping standalone tool_result, will merge with tool_use:`, bufferToolId);
							continue;
						}

						// Skip if we already have similar content in history
						if (existingContentHashes.has(hash)) {
							console.log(`[Tab ${tabId}] Skipping duplicate buffer message:`, hash.substring(0, 50));
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
				}

				console.log(`[Tab ${tabId}] Updating tab state - isStreaming:`, isStreaming, 'messageCount:', finalMessages.length, 'todoCount:', latestTodos.length);

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
					console.log(`[Tab ${tabId}] Token counts from session:`, tokenUpdates);
				}

				// For late-joining devices during streaming, also update context from streamingUsage
				// This provides real-time context usage even when joining mid-stream
				const streamingUsage = data.streamingUsage as Record<string, number> | undefined;
				if (isStreaming && streamingUsage) {
					const inputTokens = streamingUsage.input_tokens || 0;
					const cacheCreationTokens = streamingUsage.cache_creation_input_tokens || 0;
					const cacheReadTokens = streamingUsage.cache_read_input_tokens || 0;
					const streamContextUsed = inputTokens + cacheCreationTokens + cacheReadTokens;

					console.log(`[Tab ${tabId}] Streaming usage for late-joiner:`, {
						inputTokens,
						cacheCreationTokens,
						cacheReadTokens,
						streamContextUsed
					});

					// Only update if streamingUsage provides more recent data than session
					// (which it should during active streaming)
					if (streamContextUsed > 0) {
						tokenUpdates.contextUsed = streamContextUsed;
						tokenUpdates.totalCacheCreationTokens = cacheCreationTokens;
						tokenUpdates.totalCacheReadTokens = cacheReadTokens;
					}
				}

				// DEFENSIVE CHECK: Don't replace existing messages with empty array
				// This can happen if the client reconnects right after stop, before JSONL is written.
				// In that case, keep the existing messages to prevent "chat history disappearing" bug.
				const currentTab = getTab(tabId);
				const shouldPreserveMessages = finalMessages.length === 0 && currentTab && currentTab.messages.length > 0;

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

					updateTab(tabId, {
						sessionId: data.session_id as string,
						messages: messagesWithPreservedIds,
						isStreaming: isStreaming,
						todos: latestTodos,
						error: null,  // Clear any previous error
						...tokenUpdates
					});

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
					console.log(`[Tab ${tabId}] Session is streaming, starting server-authoritative polling`);
					startPolling(tabId, receivedSession);
				} else {
					// Not streaming - stop any existing polling (e.g., from a previous disconnect)
					stopPolling(tabId);
				}

				// Process any queued sync events that arrived while waiting for history
				const queuedEvents = tabsWaitingForHistory.get(tabId);
				if (queuedEvents && queuedEvents.length > 0) {
					console.log(`[Tab ${tabId}] Processing ${queuedEvents.length} queued sync events`);
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
				console.log(`[Tab ${tabId}] New session created:`, sessionId);

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
				break;
			}

			case 'start': {
				// Mark streaming as started, but DON'T create empty message yet
				// Messages will be created when we get actual content (stream_delta for text, stream_block_start for tools)
				// This prevents empty message boxes from appearing when Claude starts with a tool call
				const sessionId = data.session_id as string;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return {
							...tab,
							sessionId,
							isStreaming: true,
							streamStartTime: Date.now()
							// NO empty message added here - wait for actual content
						};
					})
				}));

				// Start server-authoritative polling for this streaming session
				// Polling ensures we always have correct state even if WebSocket events are missed
				if (sessionId) {
					startPolling(tabId, sessionId);
				}

				// Save again in case session_created wasn't received (backwards compat)
				saveTabsToServer(get({ subscribe }));
				break;
			}

			case 'chunk': {
				// 'chunk' is the final complete text from AssistantMessage
				// When include_partial_messages=True, we already have this text from stream_delta events
				// So we REPLACE the streaming message content (don't append) to avoid duplication
				console.log(`[WS] Chunk received: len=${(data.content as string).length}`);
				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						const messages = [...tab.messages];
						const streamingIdx = messages.findLastIndex(
							m => m.type === 'text' && m.role === 'assistant' && m.streaming
						);

						if (streamingIdx !== -1) {
							// REPLACE content - this is the final version from AssistantMessage
							// Don't append, as stream_delta already accumulated the text
							messages[streamingIdx] = {
								...messages[streamingIdx],
								content: data.content as string,
								streaming: false // Mark as complete
							};
						} else {
							// No streaming message exists - this is the first/only chunk
							// (happens when include_partial_messages=False)
							messages.push({
								id: `text-${Date.now()}`,
								role: 'assistant',
								content: data.content as string,
								type: 'text',
								streaming: true
							});
						}

						return { ...tab, messages };
					})
				}));
				break;
			}

			// StreamEvent handlers for real-time character-by-character streaming
			case 'stream_start': {
				// Start of a new streaming response
				// DON'T create empty message here - wait for actual content via stream_delta
				// This prevents empty message boxes when Claude's response starts with a tool call
				// BUT we can extract input token usage from the message_start event
				// NOTE: REPLACE values, not add - SDK sends total context for current turn
				const message = data.message as { usage?: Record<string, number> } | undefined;
				const usage = message?.usage;
				if (usage) {
					console.log('[WS] Stream start with input usage (REPLACING):', usage);
					update(s => ({
						...s,
						tabs: s.tabs.map(tab => {
							if (tab.id !== tabId) return tab;

							// Extract input token counts from message_start
							// These represent the FULL context sent to Claude for this turn
							const inputTokens = usage.input_tokens || 0;
							const cacheCreationTokens = usage.cache_creation_input_tokens || 0;
							const cacheReadTokens = usage.cache_read_input_tokens || 0;

							// REPLACE values - SDK sends total context, not increments
							return {
								...tab,
								totalTokensIn: inputTokens,
								totalCacheCreationTokens: cacheCreationTokens,
								totalCacheReadTokens: cacheReadTokens,
								// Update context used based on current turn's input tokens
								contextUsed: inputTokens + cacheCreationTokens + cacheReadTokens
							};
						})
					}));
				} else {
					console.log('[WS] Stream start received (no usage data)');
				}
				break;
			}

			case 'stream_block_start': {
				// Batch non-critical block start events
				pushEvent(tabId, 'stream_block_start', data);
				break;
			}

			case 'stream_delta': {
				// Use buffer for batched updates - dramatically reduces store updates
				// from 50-100/sec to ~60/sec (RAF-based)
				pushEvent(tabId, 'stream_delta', data);
				break;
			}

			case 'stream_block_stop': {
				// Batch block stop events
				pushEvent(tabId, 'stream_block_stop', data);
				break;
			}

			case 'stream_message_delta': {
				// Batch message delta (usage updates) - buffer handles accumulation
				pushEvent(tabId, 'stream_message_delta', data);
				break;
			}

			case 'tool_use': {
				// tool_use is the final event from AssistantMessage
				// When include_partial_messages=True, stream_block_start may have already created this tool
				const toolId = data.id as string;

				// Track this tool as pending - extends connection timeout while tool executes
				if (toolId) {
					let pending = tabPendingTools.get(tabId);
					if (!pending) {
						pending = new Set();
						tabPendingTools.set(tabId, pending);
					}
					pending.add(toolId);
					console.log(`[Tab ${tabId}] Tool started: ${data.name} (${toolId}), pending tools: ${pending.size}`);
				}

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = [...tab.messages];

						// Check if this tool already exists (from stream_block_start)
						const existingToolIdx = messages.findIndex(
							m => m.type === 'tool_use' && m.toolId === toolId
						);

						if (existingToolIdx !== -1) {
							// Update existing tool message with final data
							messages[existingToolIdx] = {
								...messages[existingToolIdx],
								toolName: data.name as string,
								toolInput: data.input as Record<string, unknown>,
								toolStatus: 'running'
							};
							return { ...tab, messages };
						}

						// No existing tool - create new one
						const streamingIdx = messages.findLastIndex(
							m => m.type === 'text' && m.role === 'assistant' && m.streaming
						);
						if (streamingIdx !== -1) {
							if (messages[streamingIdx].content) {
								// Mark text message as complete if it has content
								messages[streamingIdx] = {
									...messages[streamingIdx],
									streaming: false
								};
							} else {
								// Remove empty streaming text messages to avoid leaving empty cards
								messages = messages.filter((_, i) => i !== streamingIdx);
							}
						}

						messages.push({
							id: `tool-${Date.now()}-${toolId || ''}`,
							role: 'assistant',
							content: '',
							type: 'tool_use',
							toolName: data.name as string,
							toolId: toolId,
							toolInput: data.input as Record<string, unknown>,
							toolStatus: 'running',
							streaming: true,
							profileId: tab.currentQueryProfileId || undefined
						});

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'tool_result': {
				// Group tool result with its corresponding tool_use message
				const toolUseId = data.tool_use_id as string;
				const output = data.output as string;
				const isError = data.is_error as boolean;

				// Remove this tool from pending - no longer waiting for it
				if (toolUseId) {
					const pending = tabPendingTools.get(tabId);
					if (pending) {
						pending.delete(toolUseId);
						console.log(`[Tab ${tabId}] Tool completed: ${toolUseId}, pending tools: ${pending.size}`);
					}
				}

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = [...tab.messages];

						// Find the matching tool_use message and embed the result
						let found = false;
						for (let i = messages.length - 1; i >= 0; i--) {
							const m = messages[i];
							if (m.type === 'tool_use' && m.toolId === toolUseId) {
								messages[i] = {
									...m,
									toolResult: output,
									toolStatus: isError ? 'error' : 'complete',
									streaming: false
								};
								found = true;
								break;
							}
						}

						// Fallback: if not found by toolId, try any running tool_use
						if (!found) {
							for (let i = messages.length - 1; i >= 0; i--) {
								const m = messages[i];
								if (m.type === 'tool_use' && m.toolStatus === 'running') {
									messages[i] = {
										...m,
										toolResult: output,
										toolStatus: isError ? 'error' : 'complete',
										streaming: false
									};
									break;
								}
							}
						}

						// DON'T add empty streaming placeholder here
						// The stream_delta handler will create a message when text actually arrives
						// Adding empty placeholders causes lingering empty message boxes

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'done': {
				const metadata = data.metadata as Record<string, unknown>;
				const sessionId = data.session_id as string;
				console.log('[WS] Done event received, cleaning up streaming messages');

				// Stop polling since streaming is complete
				stopPolling(tabId);

				// Clear any pending stop timeout since streaming is complete
				const doneStopTimeout = stopTimeouts.get(tabId);
				if (doneStopTimeout) {
					clearTimeout(doneStopTimeout);
					stopTimeouts.delete(tabId);
				}

				// Clear pending tools tracking since stream is complete
				tabPendingTools.delete(tabId);

				// Get current tab state before updating (for notification)
				const currentTab = getTab(tabId);
				const durationSeconds = currentTab?.streamStartTime
					? (Date.now() - currentTab.streamStartTime) / 1000
					: undefined;

				let finalTitle: string | null = null;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = tab.messages.map(m =>
							m.streaming ? { ...m, streaming: false } : m
						);
						messages = messages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						if (messages.length > 0) {
							const lastIdx = messages.findLastIndex(m => m.role === 'assistant');
							if (lastIdx !== -1) {
								messages[lastIdx] = {
									...messages[lastIdx],
									metadata
								};
							}
						}

						// Update title if this is the first response
						let title = tab.title;
						if (title === 'New Chat' && messages.length > 0) {
							const firstUserMsg = messages.find(m => m.role === 'user');
							if (firstUserMsg) {
								title = firstUserMsg.content.substring(0, 100) + (firstUserMsg.content.length > 100 ? '...' : '');
							}
						}
						finalTitle = title;

						return {
							...tab,
							messages,
							isStreaming: false,
							streamStartTime: null,
							sessionId: sessionId || tab.sessionId,
							title
						};
					})
				}));

				// Trigger browser notification if enabled and permission granted
				if (isNotificationsEnabled() && getPermission() === 'granted') {
					notifySessionComplete({
						sessionId: sessionId || '',
						sessionTitle: finalTitle,
						durationSeconds,
						totalCostUsd: metadata?.total_cost_usd as number | undefined,
						tokensIn: metadata?.tokens_in as number | undefined,
						tokensOut: metadata?.tokens_out as number | undefined,
						model: metadata?.model as string | undefined
					});
				}

				// Reload full session with messages from backend
				// This ensures we have complete JSONL-based message content and token counts.
				// The metadata from the done event is already applied above, so we need to
				// preserve it if the reload doesn't include metadata (race condition with JSONL write).
				// Use longer delay to give SDK time to flush JSONL writes.
				if (sessionId) {
					setTimeout(async () => {
						try {
							// Get current state to preserve metadata if reload doesn't include it
							const currentTab = getTab(tabId);
							const currentLastAssistantIdx = currentTab?.messages.findLastIndex(m => m.role === 'assistant');
							const currentMetadata = currentLastAssistantIdx !== undefined && currentLastAssistantIdx !== -1
								? currentTab?.messages[currentLastAssistantIdx]?.metadata
								: undefined;

							const success = await tabs.loadSessionInTab(tabId, sessionId);
							if (success) {
								console.log('[Tab] Reloaded session with messages after streaming:', sessionId);

								// If the reloaded data doesn't have metadata but we had it before,
								// restore the metadata from the done event
								const reloadedTab = getTab(tabId);
								if (reloadedTab && currentMetadata) {
									const lastAssistantIdx = reloadedTab.messages.findLastIndex(m => m.role === 'assistant');
									if (lastAssistantIdx !== -1) {
										const lastMsg = reloadedTab.messages[lastAssistantIdx];
										// Only restore if the reloaded message doesn't have cost/duration metadata
										if (!lastMsg.metadata?.cost_usd && !lastMsg.metadata?.total_cost_usd) {
											console.log('[Tab] Restoring metadata from done event that was lost in reload');
											update(s => ({
												...s,
												tabs: s.tabs.map(t => {
													if (t.id !== tabId) return t;
													const messages = [...t.messages];
													if (messages[lastAssistantIdx]) {
														messages[lastAssistantIdx] = {
															...messages[lastAssistantIdx],
															metadata: { ...messages[lastAssistantIdx].metadata, ...currentMetadata }
														};
													}
													return { ...t, messages };
												})
											}));
										}
									}
								}
							}
						} catch (err) {
							// Log but don't fail - we already have the messages from streaming
							// The metadata from done event is already applied
							console.warn('[Tab] Failed to reload session after streaming, keeping streamed data:', err);
						}
					}, 1000); // 1000ms delay to give SDK more time to flush JSONL
				}

				// Refresh sessions list
				loadSessionsInternal();

				// Save tabs state since sessionId may have changed (debounced)
				saveTabsToServer(get({ subscribe }));
				break;
			}

			case 'stopped':
			case 'interrupted': {
				// Both 'stopped' (from task cancellation) and 'interrupted' (from SDK interrupt)
				// should be handled the same way - stop streaming and mark as stopped
				console.log(`[Tab ${tabId}] Received ${msgType} message`);

				// Stop polling since streaming is stopped
				stopPolling(tabId);

				// Clear the fallback timeout since we received confirmation
				const stopTimeout = stopTimeouts.get(tabId);
				if (stopTimeout) {
					clearTimeout(stopTimeout);
					stopTimeouts.delete(tabId);
				}

				// Clear pending tools tracking since stream is stopped
				tabPendingTools.delete(tabId);

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = tab.messages.map(m => {
							// Mark running tools as error (interrupted)
							if (m.type === 'tool_use' && m.toolStatus === 'running') {
								return { ...m, streaming: false, toolStatus: 'error' as const };
							}
							// Mark streaming text as not streaming
							return m.streaming ? { ...m, streaming: false } : m;
						});
						messages = messages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						if (messages.length > 0) {
							const lastIdx = messages.findLastIndex(m => m.role === 'assistant');
							if (lastIdx !== -1) {
								messages[lastIdx] = {
									...messages[lastIdx],
									content: messages[lastIdx].content + '\n\n[Stopped]'
								};
							}
						}

						return { ...tab, messages, isStreaming: false };
					})
				}));
				break;
			}

			case 'error': {
				// Stop polling since streaming errored
				stopPolling(tabId);

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = tab.messages.map(m =>
							m.streaming ? { ...m, streaming: false } : m
						);
						messages = messages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						return {
							...tab,
							messages,
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
				console.log(`[Tab ${tabId}] Message queued at position ${queuePosition}: ${prompt.substring(0, 50)}...`);
				// The message was already added to the UI by sendMessage, just log
				break;
			}

			case 'queued_message_processing': {
				// A queued message is now being processed
				const prompt = data.prompt as string;
				console.log(`[Tab ${tabId}] Processing queued message: ${prompt.substring(0, 50)}...`);
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
				// which prevents false disconnect detection
				break;
			}

			case 'system': {
				// Handle system messages from SDK (e.g., /context command output, streaming events)
				const subtype = data.subtype as string;
				const systemData = data.data as Record<string, unknown>;

				console.log('[WS] System message received:', subtype, systemData);

				// Check if this is a streaming event (when include_partial_messages=true)
				const streamEvent = systemData?.event as { type?: string; delta?: { type?: string; text?: string }; index?: number } | undefined;
				if (streamEvent?.type) {
					const eventType = streamEvent.type;
					console.log(`[WS] Streaming event: ${eventType}`);

					// Handle streaming text deltas
					if (eventType === 'content_block_delta' && streamEvent.delta?.type === 'text_delta' && streamEvent.delta?.text) {
						const deltaText = streamEvent.delta.text;
						console.log(`[WS] Text delta received: len=${deltaText.length}`);

						// Append delta text to the current streaming message
						update(s => ({
							...s,
							tabs: s.tabs.map(tab => {
								if (tab.id !== tabId) return tab;

								const messages = [...tab.messages];
								const streamingIdx = messages.findLastIndex(
									m => m.type === 'text' && m.role === 'assistant' && m.streaming
								);

								if (streamingIdx !== -1) {
									messages[streamingIdx] = {
										...messages[streamingIdx],
										content: messages[streamingIdx].content + deltaText
									};
								} else {
									// Create new streaming message if none exists
									messages.push({
										id: `text-${Date.now()}`,
										role: 'assistant',
										content: deltaText,
										type: 'text',
										streaming: true
									});
								}

								return { ...tab, messages };
							})
						}));
						break;
					}

					// For other streaming events (message_start, content_block_start, etc.), just log
					// These are informational and the actual text comes via content_block_delta
					break;
				}

				// Filter out spurious status messages with no meaningful status
				// The SDK sometimes sends empty status messages during compaction
				if (subtype === 'status') {
					const status = systemData?.status as string | undefined;
					if (!status || status === 'unknown') {
						console.log('[WS] Ignoring empty/unknown status message:', systemData);
						break;
					}
				}

				// Extract content for display - for local_command, the content is in data.content
				let displayContent = '';
				if (systemData?.content && typeof systemData.content === 'string') {
					displayContent = systemData.content;
				} else if (typeof systemData === 'object') {
					displayContent = JSON.stringify(systemData, null, 2);
				}

				// Check if this is a compact_boundary - we need to reset context usage
				// since the SDK doesn't provide post_tokens, we reset to null so the
				// next stream_start will set the proper value
				const isCompactBoundary = subtype === 'compact_boundary';

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						// First, mark any streaming text message as complete
						let messages = tab.messages.map(m =>
							m.streaming ? { ...m, streaming: false } : m
						);
						// Remove empty streaming text messages
						messages = messages.filter(
							m => !(m.type === 'text' && m.role === 'assistant' && !m.content)
						);

						// Add the system message
						messages.push({
							id: `system-${Date.now()}`,
							role: 'system' as const,
							content: displayContent,
							type: 'system' as const,
							systemSubtype: subtype,
							systemData: systemData,
							streaming: false
						});

						// Reset context usage on compact_boundary - SDK doesn't provide post_tokens
						// The next stream_start will set the correct compacted value
						if (isCompactBoundary) {
							return {
								...tab,
								messages,
								contextUsed: null,
								totalTokensIn: 0,
								totalTokensOut: 0,
								totalCacheCreationTokens: 0,
								totalCacheReadTokens: 0
							};
						}

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'subagent_start': {
				// Subagent task started - create a new subagent message group
				const agentId = data.agent_id as string;
				const agentType = data.agent_type as string;
				const description = data.description as string;
				const prompt = data.prompt as string;
				const toolId = data.tool_id as string; // The tool_use ID that launched this subagent

				console.log('[WS] Subagent started:', agentId, agentType, description);

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						let messages = [...tab.messages];

						// Find and mark the Task tool_use as not streaming
						const toolUseIdx = messages.findIndex(m => m.type === 'tool_use' && m.toolId === toolId);
						if (toolUseIdx !== -1) {
							messages[toolUseIdx] = { ...messages[toolUseIdx], streaming: false };
						}

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
							id: `subagent-${agentId}`,
							role: 'assistant',
							content: '',
							type: 'subagent',
							toolId: toolId,
							agentId: agentId,
							agentType: agentType,
							agentDescription: description,
							agentPrompt: prompt,
							agentStatus: 'running',
							agentChildren: [],
							streaming: true
						});

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'subagent_tool_use': {
				// Tool use within a subagent - add to the subagent's children
				const agentId = data.agent_id as string;
				const toolName = data.name as string;
				const toolId = data.id as string;
				const toolInput = data.input as Record<string, unknown>;

				// Track this subagent tool as pending - extends connection timeout while tool executes
				if (toolId) {
					let pending = tabPendingTools.get(tabId);
					if (!pending) {
						pending = new Set();
						tabPendingTools.set(tabId, pending);
					}
					pending.add(toolId);
					console.log(`[Tab ${tabId}] Subagent tool started: ${toolName} (${toolId}), pending tools: ${pending.size}`);
				}

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						const messages = tab.messages.map(m => {
							if (m.type === 'subagent' && m.agentId === agentId) {
								const children = [...(m.agentChildren || [])];
								children.push({
									id: `${agentId}-tool-${toolId}`,
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

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'subagent_tool_result': {
				// Tool result within a subagent - embed in matching tool_use child
				const agentId = data.agent_id as string;
				const toolUseId = data.tool_use_id as string;
				const output = data.output as string;
				const isError = data.is_error as boolean;

				// Remove this subagent tool from pending - no longer waiting for it
				if (toolUseId) {
					const pending = tabPendingTools.get(tabId);
					if (pending) {
						pending.delete(toolUseId);
						console.log(`[Tab ${tabId}] Subagent tool completed: ${toolUseId}, pending tools: ${pending.size}`);
					}
				}

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						const messages = tab.messages.map(m => {
							if (m.type === 'subagent' && m.agentId === agentId) {
								const children = [...(m.agentChildren || [])];
								// Find matching tool_use and embed result
								let found = false;
								for (let i = children.length - 1; i >= 0; i--) {
									if (children[i].type === 'tool_use' && children[i].toolId === toolUseId) {
										children[i] = {
											...children[i],
											toolResult: output,
											toolStatus: isError ? 'error' : 'complete'
										};
										found = true;
										break;
									}
								}
								// Fallback: try any running tool_use
								if (!found) {
									for (let i = children.length - 1; i >= 0; i--) {
										if (children[i].type === 'tool_use' && children[i].toolStatus === 'running') {
											children[i] = {
												...children[i],
												toolResult: output,
												toolStatus: isError ? 'error' : 'complete'
											};
											break;
										}
									}
								}
								return { ...m, agentChildren: children };
							}
							return m;
						});

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'subagent_chunk': {
				// Text chunk from subagent - add to children or update last text child
				const agentId = data.agent_id as string;
				const content = data.content as string;

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						const messages = tab.messages.map(m => {
							if (m.type === 'subagent' && m.agentId === agentId) {
								const children = [...(m.agentChildren || [])];
								const lastChild = children[children.length - 1];

								if (lastChild && lastChild.type === 'text') {
									// Append to existing text child
									children[children.length - 1] = {
										...lastChild,
										content: lastChild.content + content
									};
								} else {
									// Create new text child
									children.push({
										id: `${agentId}-text-${Date.now()}`,
										type: 'text',
										content: content
									});
								}
								return { ...m, agentChildren: children };
							}
							return m;
						});

						return { ...tab, messages };
					})
				}));
				break;
			}

			case 'subagent_done': {
				// Subagent completed - update status and store final result
				const agentId = data.agent_id as string;
				const result = data.result as string | undefined;
				const isError = data.is_error as boolean | undefined;

				console.log('[WS] Subagent done:', agentId, isError ? 'error' : 'success');

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;

						// Find the subagent to get its linked toolId
						const subagent = tab.messages.find(m => m.type === 'subagent' && m.agentId === agentId);
						const linkedToolId = subagent?.toolId;

						const messages = tab.messages.map(m => {
							// Update the subagent message
							if (m.type === 'subagent' && m.agentId === agentId) {
								return {
									...m,
									content: result || '',
									agentStatus: isError ? 'error' as const : 'completed' as const,
									streaming: false
								};
							}
							// Also mark the linked Task tool_use as complete
							if (linkedToolId && m.type === 'tool_use' && m.toolId === linkedToolId) {
								return {
									...m,
									toolStatus: 'complete' as const,
									streaming: false
								};
							}
							return m;
						});

						// DON'T add empty streaming placeholder here
						// The stream_delta handler will create a message when text actually arrives
						// Adding empty placeholders causes lingering empty message boxes

						return { ...tab, messages };
					})
				}));
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

				console.log('[WS] Permission request:', request.tool_name, request.request_id);

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

				console.log('[WS] Permission queue update:', resolvedIds.length, 'resolved,', remainingCount, 'remaining');

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

				console.log('[WS] Permission response acknowledged:', requestId, result);

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

				console.log('[WS] Pending permissions sync:', requests.length, 'requests');

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

				console.log('[WS] User question request:', request.request_id, request.questions.length, 'questions');
				console.log('[WS] User question data (full):', JSON.stringify(request.questions, null, 2));

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

				console.log('[WS] User question response acknowledged:', requestId, success);

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

				console.log('[WS] Pending questions sync:', questions.length, 'questions');

				update(s => ({
					...s,
					tabs: s.tabs.map(tab => {
						if (tab.id !== tabId) return tab;
						return { ...tab, pendingQuestions: questions };
					})
				}));
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
						executionMode: 'local',
						worktreeBranch: '',
						worktreeBaseBranch: 'main',
						worktreeId: null,
						worktreeMode: 'new',
						selectedWorktreeBranch: null,
						isCreatingWorktree: false,
						worktreeError: null,
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

					console.log('[Tabs] Restored', restoredTabs.length, 'tabs from server');
				} else {
					// No persisted state at all - start with empty tabs (user can create one)
					console.log('[Tabs] No persisted state, starting with empty tabs');
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
				worktreeId: null,
				worktreeMode: 'new',
				selectedWorktreeBranch: null,
				isCreatingWorktree: false,
				worktreeError: null,
				sessionLocked: false,
				sessionLockedBy: undefined
			};

			update(s => ({
				...s,
				tabs: [...s.tabs, newTab],
				activeTabId: newTabId
			}));

			// Connect WebSocket for new tab
			connectTab(newTabId);

			// If sessionId provided, load that session
			if (sessionId) {
				this.loadSessionInTab(newTabId, sessionId);
			} else {
				// Save tabs state (debounced)
				saveTabsToServer(get({ subscribe }));
			}

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
				console.log('[Tabs] closeTab - saving selection:', tab.profile, tab.project);
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
		 * Open a session - switches to existing tab if already open, otherwise creates new tab
		 */
		openSession(sessionId: string) {
			const existingTabId = this.findTabBySessionId(sessionId);
			if (existingTabId) {
				// Session already open - switch to that tab
				this.setActiveTab(existingTabId);
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
				console.log(`[Tab ${tabId}] Interrupting and sending new message: ${prompt.substring(0, 50)}...`);

				// Immediately mark running tools as interrupted in the UI
				// This prevents the spinner from showing on old tools while new streaming starts
				update(s => ({
					...s,
					tabs: s.tabs.map(t => {
						if (t.id !== tabId) return t;
						const messages = t.messages.map(m => {
							// Mark running tools as error (interrupted)
							if (m.type === 'tool_use' && m.toolStatus === 'running') {
								return { ...m, streaming: false, toolStatus: 'error' as const };
							}
							// Mark streaming text as not streaming
							return m.streaming ? { ...m, streaming: false } : m;
						});
						return { ...t, messages };
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
					profile_id: tab.profile
				});

				console.log(`[Tab ${tabId}] Created worktree session: ${response.session_id}`);

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
								content: `🌿 Work tree created on branch \`${response.branch_name}\` (based on \`${response.base_branch}\`)`,
								type: 'system' as const,
								systemSubtype: 'worktree_created'
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

			console.log(`[Tab ${tabId}] Starting session on existing worktree: ${tab.worktreeId}`);

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
							content: `🌿 Using existing worktree on branch \`${branchDisplay}\``,
							type: 'system' as const,
							systemSubtype: 'worktree_selected'
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
				console.log(`[Tab ${tabId}] Sending stop request for session ${tab.sessionId}`);
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
			}, 5000); // 5 second timeout

			stopTimeouts.set(tabId, timeoutId);
		},

		/**
		 * Load a session into a specific tab
		 */
		async loadSessionInTab(tabId: string, sessionId: string) {
			try {
				const session = await api.get<Session & { messages: Array<Record<string, unknown>> }>(`/sessions/${sessionId}`);
				// Debug: log subagent messages from API response
				const subagentMsgs = session.messages.filter(m => m.type === 'subagent');
				if (subagentMsgs.length > 0) {
					console.log('[tabs] Subagent messages from API:', subagentMsgs.map(m => ({
						type: m.type,
						agentId: m.agentId,
						agentStatus: m.agentStatus,
						agentChildrenCount: (m.agentChildren as unknown[])?.length || 0
					})));
				}
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
							systemData: { content: m.content } as Record<string, unknown>,
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
				updateTab(tabId, {
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
				});

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
				worktreeId: null,
				worktreeMode: 'new',
				selectedWorktreeBranch: null,
				isCreatingWorktree: false,
				worktreeError: null,
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

		async createProject(data: { id: string; name: string; description?: string }) {
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
				console.log(`[Tab ${tabId}] Sent permission response: ${decision} for ${requestId}`);
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
				console.log(`[Tab ${tabId}] Sent user question response for ${requestId}`);
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
